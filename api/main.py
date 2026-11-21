import datetime
import logging
import math
import os
import threading
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Dict

import duckdb
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from Engine.fixer.fixer import FixGenerator
from Engine.parser.parser import normalize_sql_input, parse_sql_to_steps, split_statements
from Engine.runner.runner import EchoEngineRunner

log = logging.getLogger("echo.api")

app = FastAPI(title="ECHO Enterprise SQL Intelligence API", version="4.0")

# Browser dashboards need CORS. Restrict this in production, e.g. ECHO_CORS_ORIGINS="https://dash.example.com"
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("ECHO_CORS_ORIGINS", "*").split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)

QUERY_TIMEOUT_SECONDS = float(os.getenv("ECHO_TIMEOUT_SECONDS", "15"))
SANDBOX = os.getenv("ECHO_SANDBOX", "1") != "0"
MAX_SQL_CHARS = 100_000

# Demo data. It is loaded into a FRESH in-memory database for every request, so a user's
# DROP / DELETE / UPDATE can never damage anything or leak into another request.
SEED_SQL = """
CREATE TABLE users (id INTEGER, name VARCHAR, status VARCHAR);
INSERT INTO users VALUES (1, 'Ahmed', 'active'), (2, 'Mohamed', 'inactive'), (3, 'Ali', 'active');

CREATE TABLE orders (order_id INTEGER, customer_id INTEGER, status VARCHAR, amount DECIMAL);
INSERT INTO orders VALUES (101, 1, 'completed', 250.0), (102, 2, 'pending', 150.0);

CREATE TABLE customers (customer_id INTEGER, name VARCHAR);
INSERT INTO customers VALUES (1, 'Ahmed'), (2, 'Mohamed');

CREATE TABLE customer_addresses (customer_id INTEGER, city VARCHAR);
INSERT INTO customer_addresses VALUES (1, 'Alexandria'), (2, 'Cairo');
"""


def new_sandbox():
    """Create an isolated in-memory DuckDB pre-loaded with the demo tables."""
    conn = duckdb.connect(database=":memory:")
    for statement in split_statements(SEED_SQL):
        conn.execute(statement)
    if SANDBOX:
        try:
            conn.execute("SET memory_limit = '512MB'")
            conn.execute("SET threads = 2")
            conn.execute("SET enable_external_access = false")  # no read_csv('/etc/...'), no file ATTACH
            conn.execute("SET lock_configuration = true")       # user SQL cannot undo the settings above
        except Exception as exc:  # noqa: BLE001 - older DuckDB versions lack some settings
            log.warning("Could not fully sandbox the connection: %s", exc)
    return conn


@contextmanager
def deadline(*targets):
    """Interrupt every target (connection / cursor) if the block runs longer than the timeout.

    ``targets`` may be objects or zero-argument callables returning a list of objects (so that
    connections created inside the block are covered too). Yields an Event that is set on timeout.
    """
    timed_out = threading.Event()

    def interrupt():
        timed_out.set()
        for target in targets:
            try:
                items = target() if callable(target) and not hasattr(target, "interrupt") else [target]
            except Exception:  # noqa: BLE001
                continue
            for item in items or []:
                try:
                    item.interrupt()
                except Exception:  # noqa: BLE001
                    pass

    timer = threading.Timer(QUERY_TIMEOUT_SECONDS, interrupt)
    timer.daemon = True
    timer.start()
    try:
        yield timed_out
    finally:
        timer.cancel()


def clean(value):
    """Make any engine result JSON-safe (NaN/Infinity, Decimal, dates and unknown types)."""
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [clean(v) for v in value]
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    return str(value)


class InvestigateRequest(BaseModel):
    sql: str = Field(..., max_length=MAX_SQL_CHARS)


def engine_error(sql, message):
    """Structured response for unexpected engine failures (never a bare 500, never mislabelled as SQL syntax)."""
    return {
        "query": sql,
        "verdict": "error",
        "first_suspicious_step": None,
        "step_trace": [{
            "step_id": "step_1",
            "step_type": "ENGINE_ERROR",
            "status": "failed",
            "error": message,
        }],
        "detected_bugs": [{
            "bug_type": "Engine Error",
            "severity": "High",
            "description": message,
        }],
        "warnings": [],
        "detector_errors": [],
    }


@app.get("/health")
def health():
    return {"status": "ok", "engine": "Enterprise Echo Runner Active", "sandbox": SANDBOX}


# response_model is a plain dict on purpose: a strict model silently DROPS every field it
# does not list (verdict, warnings, profiling, scope, rows_affected, fallback ...).
@app.post("/investigate", response_model=Dict[str, Any])
def investigate(request: InvestigateRequest):
    # ECHO's own advisory comments ("-- ECHO ...") and CRLF line endings are removed once, here,
    # so pasting a suggested fix back into the editor can never feed ECHO its own output.
    sql, notes = normalize_sql_input(request.sql)
    if not sql.strip():
        raise HTTPException(status_code=400, detail="SQL query cannot be empty.")

    conn = None
    try:
        conn = new_sandbox()
        runner = EchoEngineRunner(conn)
        # The runner executes on a session cursor; interrupt it as well as the connection.
        with deadline(lambda: [getattr(runner, "_session", None), conn]) as timed_out:
            # Syntax / unsupported statements become step_type SYNTAX_ERROR inside the steps,
            # and the runner reports them per statement while still analysing the rest of the script.
            parsed_steps = parse_sql_to_steps(sql)
            if not parsed_steps:
                return engine_error(sql, "Failed to extract execution steps.")

            result = runner.analyze_query(sql, parsed_steps)
            result["timed_out"] = timed_out.is_set()
        if notes:
            result["input_notes"] = notes
        return clean(result)

    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected engine failure")
        return engine_error(sql, f"Unexpected engine failure: {type(exc).__name__}: {exc}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


class FixRequest(BaseModel):
    sql: str = Field(..., max_length=MAX_SQL_CHARS)
    verify: bool = True


@app.post("/fix", response_model=Dict[str, Any])
def fix(request: FixRequest):
    """Analyse the SQL, rewrite what can be fixed safely, and (optionally) prove the fix on fresh sandboxes."""
    sql, notes = normalize_sql_input(request.sql)
    if not sql.strip():
        raise HTTPException(status_code=400, detail="SQL query cannot be empty.")
    conn = None
    sandboxes = []

    def tracked_sandbox():
        sandbox = new_sandbox()
        sandboxes.append(sandbox)  # so the deadline can interrupt the verification runs as well
        return sandbox

    try:
        conn = new_sandbox()
        runner = EchoEngineRunner(conn)
        with deadline(lambda: [getattr(runner, "_session", None), conn, *sandboxes]) as timed_out:
            steps = parse_sql_to_steps(sql)
            analysis = runner.analyze_query(sql, steps)
            generator = FixGenerator()
            report = generator.fix_all(sql, analysis, conn, parsed_steps=steps)
            payload = report.to_dict()
            payload["analysis"] = {
                "verdict": analysis.get("verdict"),
                "first_suspicious_step": analysis.get("first_suspicious_step"),
                "detected_bugs": analysis.get("detected_bugs", []),
            }
            if report.applied and request.verify:
                payload["verification"] = generator.verify(
                    report.original_sql, report.fixed_sql, tracked_sandbox, fixes=report.fixes
                )
            # A proposal is NEVER presented as a verified fix. When the proposal is concrete SQL, run the
            # same checks on a sandbox copy and attach them as a preview (clearly labelled as such).
            if (not report.applied and request.verify and report.proposed_sql
                    and any(p.get("status") == "suggested" for p in report.proposals)):
                try:
                    dedupe = any(str(p.get("kind") or "").endswith(("dedupe_right_side", "pre_dedupe_for_aggregation"))
                                 for p in report.proposals)
                    preview = generator.verify(
                        report.original_sql, report.proposed_sql, tracked_sandbox,
                        fixes=[{"applied": True, "strategy_used": "dedupe_right_side_row_number"}] if dedupe else None,
                    )
                    preview["preview_only"] = True
                    payload["proposal_verification"] = preview
                except Exception as exc:  # noqa: BLE001 - the preview is a bonus
                    log.warning("Proposal preview failed: %s", exc)
        # One explicit status for the dashboard: verified | applied_unverified | applied | suggested | manual | none
        status = report.status
        if report.applied:
            verification = payload.get("verification")
            if isinstance(verification, dict):
                status = "verified" if verification.get("passed") else "applied_unverified"
        payload["status"] = status
        payload["timed_out"] = timed_out.is_set()
        if notes:
            payload["input_notes"] = notes
        return clean(payload)
    except Exception as exc:  # noqa: BLE001
        log.exception("Fix endpoint failure")
        return {"applied": False, "fixed_sql": sql, "original_sql": sql, "fixes": [],
                "warnings": [f"Unexpected engine failure: {type(exc).__name__}: {exc}"]}
    finally:
        for handle in [conn, *sandboxes]:
            if handle is not None:
                try:
                    handle.close()
                except Exception:  # noqa: BLE001
                    pass


@app.get("/cases")
def list_cases():
    return [{"case_id": 1, "description": "Enterprise SQL Analysis Ready"}]