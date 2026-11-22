"""Ollama integration tests: success, server down / timeout, and guardrail rejection.

No real server is needed: ``urllib.request.urlopen`` (or ``HttpLLMClient.__call__``) is mocked.
Run:  pytest tests/test_explainer_ollama.py
"""
import io
import json
import socket
import urllib.error

import pytest

from Engine.explainer import explainer as ex

BUG = {
    "bug_type": "Possible JOIN Fan-out", "severity": "High", "suspicious_step_id": "step_3",
    "description": "Row count surged from 2 to 4.",
    "evidence": {"rows_before": 2, "rows_after": 4, "multiplication_factor": 2.0, "confirmed": True},
}
FIX = {"applied": True, "strategy_used": "dedupe_right_side_row_number", "target_table": "customer_addresses",
       "explanation": "Kept one row per customer_id before the JOIN."}
VER = {"passed": True, "rows_before": 4, "rows_after": 2, "total_before": None, "total_after": None}

GOOD = {
    "summary": "ECHO found a high-severity issue: Possible JOIN Fan-out at step 'step_3'. A fix was applied and verification passed.",
    "why_it_happened": "The join at step 'step_3' made the row count grow from 2 to 4.",
    "fix_and_proof": "The fix kept one row per key. Rows went from 4 to 2. Verification passed.",
}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def chat_payload(answer):
    text = answer if isinstance(answer, str) else json.dumps(answer)
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode()


def ollama_explainer():
    return ex.LLMExplainer(env={"ECHO_LLM_PROVIDER": "ollama", "ECHO_LLM_MODEL": "qwen2.5-coder:7b"})


# ------------------------------------------------------------------ config
def test_ollama_config_defaults():
    cfg = ex.LLMConfig.from_env({"ECHO_LLM_PROVIDER": "ollama"})
    assert cfg.base_url == "http://localhost:11434/v1"
    assert cfg.api_key == "ollama"          # no real key needed
    assert cfg.timeout == 40.0
    assert cfg.model == "llama3.2"


def test_ollama_ignores_cloud_key_and_honours_overrides():
    cfg = ex.LLMConfig.from_env({"ECHO_LLM_PROVIDER": "ollama", "OPENAI_API_KEY": "sk-real",
                                 "ECHO_LLM_BASE_URL": "http://10.0.0.5:11434/v1", "ECHO_LLM_TIMEOUT": "45"})
    assert cfg.api_key == "ollama" and cfg.base_url == "http://10.0.0.5:11434/v1" and cfg.timeout == 45.0


def test_no_provider_configured_still_means_fallback():
    assert ex.LLMConfig.from_env({}) is None


# ------------------------------------------------------------------ 1) success
def test_success_via_mocked_client(monkeypatch):
    monkeypatch.setattr(ex.HttpLLMClient, "__call__", lambda self, system, user: json.dumps(GOOD))
    result = ollama_explainer().explain_finding(BUG, FIX, VER)
    assert result["source"] == "llm" and result["model"] == "qwen2.5-coder:7b" and result["provider"] == "ollama"
    assert result["fallback_reason"] is None


def test_success_over_http_uses_ollama_openai_endpoint(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen.update(url=request.full_url, auth=request.get_header("Authorization"),
                    body=json.loads(request.data), timeout=timeout)
        return FakeResponse(chat_payload("```json\n" + json.dumps(GOOD) + "\n```"))  # fenced JSON is tolerated

    monkeypatch.setattr(ex.urllib.request, "urlopen", fake_urlopen)
    result = ollama_explainer().explain_finding(BUG, FIX, VER)
    assert result["source"] == "llm"
    assert seen["url"] == "http://localhost:11434/v1/chat/completions"
    assert seen["auth"] == "Bearer ollama" and seen["timeout"] == 40.0
    assert seen["body"]["response_format"] == {"type": "json_object"} and seen["body"]["stream"] is False


# ------------------------------------------------------------------ 2) server down / timeout
@pytest.mark.parametrize("error", [
    urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")),
    socket.timeout("timed out"),
    ConnectionResetError("reset"),
])
def test_server_down_falls_back_and_fails_fast(monkeypatch, error):
    calls = []

    def fake_urlopen(request, timeout=None):
        calls.append(1)
        raise error

    monkeypatch.setattr(ex.urllib.request, "urlopen", fake_urlopen)
    explainer = ollama_explainer()
    first = explainer.explain_finding(BUG, FIX, VER)
    second = explainer.explain_finding(BUG, FIX, VER)
    assert first["source"] == second["source"] == "fallback"
    assert "unreachable" in first["fallback_reason"] and first["plain_text"]
    assert len(calls) == 1                      # the second call is skipped (cool-down), not retried


def test_missing_model_gives_pull_hint(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 404, "not found", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(ex.urllib.request, "urlopen", fake_urlopen)
    result = ollama_explainer().explain_finding(BUG, FIX, VER)
    assert result["source"] == "fallback" and "ollama pull qwen2.5-coder:7b" in result["fallback_reason"]


# ------------------------------------------------------------------ 3) guardrails
@pytest.mark.parametrize("answer", [
    "",                                                                         # empty
    "I think the join is wrong.",                                               # not JSON
    {"summary": "ok", "why_it_happened": "x"},                                  # missing key
    {**GOOD, "why_it_happened": "The join made rows grow from 2 to 999."},      # invented number
    {**GOOD, "why_it_happened": "The table 'ghost_table' has duplicate keys."}, # invented table
    {**GOOD, "fix_and_proof": "Run SELECT * FROM orders to check."},            # SQL
    {**GOOD, "summary": "Verification failed for this high-severity issue."},   # contradicts passed=True
])
def test_guardrail_rejection_falls_back(monkeypatch, answer):
    text = answer if isinstance(answer, str) else json.dumps(answer)
    monkeypatch.setattr(ex.HttpLLMClient, "__call__", lambda self, system, user: text)
    result = ollama_explainer().explain_finding(BUG, FIX, VER)
    assert result["source"] == "fallback" and result["fallback_reason"] and result["plain_text"]
    assert "999" not in result["plain_text"] and "ghost_table" not in result["plain_text"]


def test_llm_cannot_claim_a_fix_that_was_not_applied(monkeypatch):
    lie = {**GOOD, "summary": "ECHO found an issue and the fix was applied.",
           "fix_and_proof": "Rows went from 4 to 2."}
    monkeypatch.setattr(ex.HttpLLMClient, "__call__", lambda self, system, user: json.dumps(lie))
    result = ollama_explainer().explain_finding(BUG, None, None)
    assert result["source"] == "fallback" and "No automatic fix was applied" in result["plain_text"]


def test_honest_no_fix_answer_is_accepted(monkeypatch):
    honest = {"summary": "ECHO found a high-severity issue: Possible JOIN Fan-out at step 'step_3'. No fix was applied.",
              "why_it_happened": "The join at step 'step_3' made the row count grow from 2 to 4.",
              "fix_and_proof": "No fix was applied, so nothing was verified."}
    monkeypatch.setattr(ex.HttpLLMClient, "__call__", lambda self, system, user: json.dumps(honest))
    assert ollama_explainer().explain_finding(BUG, None, None)["source"] == "llm"


def test_not_configured_is_deterministic():
    result = ex.LLMExplainer(env={}).explain_finding(BUG, FIX, VER)
    assert result["source"] == "fallback" and result["model"] is None


# ------------------------------------------------------------------ health check
def test_health_check(monkeypatch):
    def fake_urlopen(request, timeout=None):
        assert request.full_url == "http://localhost:11434/api/tags"
        return FakeResponse(json.dumps({"models": [{"name": "llama3.2:latest"}]}).encode())

    monkeypatch.setattr(ex.urllib.request, "urlopen", fake_urlopen)
    health = ex.check_llm_health("http://localhost:11434/v1", "llama3.2")
    assert health["ok"] and health["model_available"] is True
    assert ex.check_llm_health("http://localhost:11434/v1", "other")["model_available"] is False


def test_health_check_server_down(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))

    monkeypatch.setattr(ex.urllib.request, "urlopen", fake_urlopen)
    health = ex.check_llm_health()
    assert health["ok"] is False and health["error"]
