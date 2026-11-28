"""ECHO bronze loader: saves one engine result into PostgreSQL (schema: bronze).

Mapping
-------
    engine result["query"], ["verdict"], whole dict -> bronze.runs   (one row)
    engine result["step_trace"]  (list)             -> bronze.steps  (one row per step)
    engine result["detected_bugs"] (list)           -> bronze.cases  (one row per bug)

Usage
-----
    python loader.py check              # test the database connection
    python loader.py                    # load fake_run.json
    python loader.py my_result.json     # load any saved engine result

From other code (for example M5's API):
    from loader import load_result
    run_id = load_result(result_dict)
"""

import json
import math
import os
import re
import sys
from decimal import Decimal

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import Json

load_dotenv()

VALID_SEVERITIES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}

# The 5 official bug types (bug_taxonomy.md). Detectors, the benchmark and the docs
# spell them differently, so bug_type keeps the raw label and bug_category gets the
# standard name. Labels are compared ignoring case, spaces, hyphens and underscores.
# A label that is not listed (performance notes, syntax errors ...) gets NULL.
BUG_CATEGORIES = {
    "possiblejoinfanout": "JOIN Fan-out",
    "joinfanout": "JOIN Fan-out",
    "inflatedaggregate": "Inflated Totals",
    "inflatedtotals": "Inflated Totals",
    "lostrowsinaleftjoin": "Lost Rows in a LEFT JOIN",
    "leftjoinrowloss": "Lost Rows in a LEFT JOIN",
    "nulljoinkeys": "NULL Join Keys",
    "joinkeytypemismatch": "Key Type Mismatch",
    "keytypemismatch": "Key Type Mismatch",
}


def bug_category(label):
    """Standard bug name for a detector / benchmark label, or None if it is not one of the 5."""
    return BUG_CATEGORIES.get(re.sub(r"[^a-z0-9]", "", str(label or "").lower()))


# --------------------------------------------------------------------------- #
# Connection
# --------------------------------------------------------------------------- #
def get_connection():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def check_connection():
    """Check we can reach the database and that the bronze tables exist."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version();")
            print("Connected!", cur.fetchone()[0])
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'bronze' ORDER BY table_name;"
            )
            print("Bronze tables:", [row[0] for row in cur.fetchall()])
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'bronze' AND table_name = 'runs' "
                "AND column_name = 'raw_result';"
            )
            if not cur.fetchone():
                print("WARNING: bronze.runs has no raw_result column - apply 002_bronze_engine_output.sql.")
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Small helpers: engine output can be incomplete, so never trust a key exists
# --------------------------------------------------------------------------- #
def _clean(value):
    """NaN / Infinity are not valid JSON (PostgreSQL rejects them): turn them into None."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _json(value):
    """Wrap a dict/list for a JSONB column (unknown types become text, never crash)."""
    return Json(_clean(value), dumps=lambda obj: json.dumps(obj, default=str))


def _row_count(value):
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return int(value) if value >= 0 else None


def _number(value):
    if isinstance(value, Decimal):  # DuckDB SUMs often come back as Decimal
        value = float(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _text(value, limit=None):
    if value is None:
        return None
    text = str(value)
    return text[:limit] if limit else text


# --------------------------------------------------------------------------- #
# The three inserts
# --------------------------------------------------------------------------- #
def _insert_run(cur, result):
    trace = result.get("step_trace") or []
    crashed = any(
        isinstance(s, dict) and s.get("step_type") == "ENGINE_ERROR" for s in trace
    )
    cur.execute(
        "INSERT INTO bronze.runs (query_text, status, verdict, raw_result) "
        "VALUES (%s, %s, %s, %s) RETURNING run_id",
        (
            result.get("query") or "",
            "FAILED" if crashed else "DONE",
            _text(result.get("verdict"), 30),
            _json(result),
        ),
    )
    return cur.fetchone()[0]


def _insert_steps(cur, run_id, trace):
    """Insert every step. Returns {engine step id ('step_2'): database step_id}."""
    step_ids = {}
    for number, step in enumerate(trace, start=1):
        if not isinstance(step, dict):
            continue
        cur.execute(
            "INSERT INTO bronze.steps "
            "(run_id, step_number, step_type, step_name, sql_snippet, row_count, "
            " sum_value, step_key, scope, status, error, execution_time_ms, columns_metrics) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING step_id",
            (
                run_id,
                number,
                _text(step.get("step_type") or "UNKNOWN", 50),
                _text(step.get("name"), 100),
                step.get("query"),
                _row_count(step.get("row_count")),
                _number(step.get("sum_value")),
                _text(step.get("step_id"), 100),
                _text(step.get("scope")),
                _text(step.get("status"), 20),
                _text(step.get("error")),
                _number(step.get("execution_time_ms")),
                _json(step.get("columns_metrics") or {}),
            ),
        )
        db_id = cur.fetchone()[0]
        if step.get("step_id") is not None:  # a step without an id can never be linked to a bug
            step_ids[step["step_id"]] = db_id
    return step_ids


def _insert_cases(cur, run_id, bugs, step_ids):
    for bug in bugs:
        if not isinstance(bug, dict):
            continue
        severity = str(bug.get("severity") or "").upper()
        if severity not in VALID_SEVERITIES:
            severity = "MEDIUM"
        step_key = bug.get("suspicious_step_id")
        cur.execute(
            "INSERT INTO bronze.cases "
            "(run_id, step_id, detector_name, bug_type, bug_category, severity, description, "
            " confidence, scope, suspicious_step_key, suggested_fix, evidence) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                run_id,
                step_ids.get(step_key) if step_key is not None else None,
                _text(bug.get("detector") or "UNKNOWN", 100),
                _text(bug.get("bug_type") or "Unknown", 100),
                bug_category(bug.get("bug_type")),
                severity,
                _text(bug.get("description")),
                _text(bug.get("confidence"), 20),
                _text(bug.get("scope")),
                _text(step_key, 100),
                _text(bug.get("suggested_fix")),
                _json(bug.get("evidence") or {}),
            ),
        )


# --------------------------------------------------------------------------- #
# Public function
# --------------------------------------------------------------------------- #
def load_result(result):
    """Save one engine result (the dict from EchoEngineRunner.analyze_query). Returns run_id.

    All-or-nothing: if anything fails, nothing is saved.
    """
    if not isinstance(result, dict):
        raise TypeError("result must be a dict (the output of analyze_query)")
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            run_id = _insert_run(cur, result)
            step_ids = _insert_steps(cur, run_id, result.get("step_trace") or [])
            _insert_cases(cur, run_id, result.get("detected_bugs") or [], step_ids)
        conn.commit()
        return run_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "fake_run.json"
    if arg == "check":
        check_connection()
    else:
        with open(arg, encoding="utf-8") as f:
            print("Loaded run", load_result(json.load(f)))
