import datetime
import logging
import math
import os
import threading
from decimal import Decimal
from typing import Any, Dict

import duckdb
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from Engine.parser.parser import parse_sql_to_steps, split_statements
from Engine.runner.runner import EchoEngineRunner

log = logging.getLogger("echo.api")

app = FastAPI(title="ECHO Enterprise SQL Intelligence API", version="3.0")

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
    if not request.sql.strip():
        raise HTTPException(status_code=400, detail="SQL query cannot be empty.")

    conn = None
    timer = None
    try:
        conn = new_sandbox()
        runner = EchoEngineRunner(conn)
        timed_out = threading.Event()

        def interrupt():
            timed_out.set()
            # The runner executes on a session cursor; interrupt it as well as the connection.
            for target in (getattr(runner, "_session", None), conn):
                try:
                    target.interrupt()
                except Exception:  # noqa: BLE001
                    pass

        timer = threading.Timer(QUERY_TIMEOUT_SECONDS, interrupt)
        timer.start()

        # Syntax / unsupported statements become step_type SYNTAX_ERROR inside the steps,
        # and the runner reports them per statement while still analysing the rest of the script.
        parsed_steps = parse_sql_to_steps(request.sql)
        if not parsed_steps:
            return engine_error(request.sql, "Failed to extract execution steps.")

        result = runner.analyze_query(request.sql, parsed_steps)
        result["timed_out"] = timed_out.is_set()
        return clean(result)

    except Exception as exc:  # noqa: BLE001
        log.exception("Unexpected engine failure")
        return engine_error(request.sql, f"Unexpected engine failure: {type(exc).__name__}: {exc}")
    finally:
        if timer is not None:
            timer.cancel()
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


@app.get("/cases")
def list_cases():
    return [{"case_id": 1, "description": "Enterprise SQL Analysis Ready"}]