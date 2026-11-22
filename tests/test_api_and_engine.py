"""Automatic checks that run on every pull request (see .github/workflows/ci.yml).

They test ECHO end to end through the real FastAPI app, using the same small
deterministic dataset as the benchmark. No network, no API key, no database server.
"""

import duckdb
import pytest
from fastapi.testclient import TestClient

from api import main as api_main
from benchmark.cases import ACCEPTED_LABELS, CASES, SETUP_SQL


def _case_sql(case_id):
    return next(c["sql"] for c in CASES if c["id"] == case_id)


def _bug_types(response_json):
    return [b["bug_type"] for b in response_json.get("detected_bugs", [])]


@pytest.fixture()
def client(monkeypatch):
    """A test client whose sandbox database already holds the benchmark tables."""

    def sandbox_with_data():
        conn = duckdb.connect(":memory:")
        conn.execute(SETUP_SQL)
        return conn

    monkeypatch.setattr(api_main, "new_sandbox", sandbox_with_data)
    return TestClient(api_main.app)


# --------------------------------------------------------------------------- #
# API basics
# --------------------------------------------------------------------------- #
def test_health_endpoint_is_ok(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_empty_sql_is_rejected(client):
    response = client.post("/investigate", json={"sql": "   "})
    assert response.status_code == 400


def test_investigate_returns_the_report_shape(client):
    response = client.post("/investigate", json={"sql": _case_sql("clean_06")})
    assert response.status_code == 200
    body = response.json()
    for key in ("verdict", "detected_bugs", "step_trace"):
        assert key in body


# --------------------------------------------------------------------------- #
# ECHO finds real bugs and stays quiet on clean SQL
# --------------------------------------------------------------------------- #
def test_fan_out_is_detected(client):
    body = client.post("/investigate", json={"sql": _case_sql("fanout_01")}).json()
    assert body["verdict"] == "issues_found"
    assert any(t in ACCEPTED_LABELS["join_fanout"] for t in _bug_types(body))


def test_key_type_mismatch_is_detected(client):
    body = client.post("/investigate", json={"sql": _case_sql("typemis_01")}).json()
    assert any(t in ACCEPTED_LABELS["key_type_mismatch"] for t in _bug_types(body))


def test_clean_query_raises_no_issue(client):
    body = client.post("/investigate", json={"sql": _case_sql("clean_01")}).json()
    assert body["verdict"] == "no_issues_found"
    assert _bug_types(body) == []


def test_fix_endpoint_answers_for_a_fan_out(client):
    response = client.post("/fix", json={"sql": _case_sql("fanout_01"), "verify": False})
    assert response.status_code == 200
    body = response.json()
    assert "fixed_sql" in body and "original_sql" in body


# --------------------------------------------------------------------------- #
# The benchmark file itself must stay well formed
# --------------------------------------------------------------------------- #
def test_benchmark_cases_are_well_formed():
    ids = [c["id"] for c in CASES]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    for case in CASES:
        assert case["bug"] == "clean" or case["bug"] in ACCEPTED_LABELS, case["id"]
        assert case["sql"].strip().endswith(";"), case["id"]
