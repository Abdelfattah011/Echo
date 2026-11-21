import re
import pytest

from Engine.explainer.explainer import LLMExplainer, explain_finding, explain_report


ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def test_explain_finding_uses_llm_callable_and_keeps_code_decision():
    import json

    seen_prompts = []

    def fake_llm(prompt: str) -> str:
        seen_prompts.append(prompt)
        return json.dumps({
            "summary": "Joining on customer_id multiplied rows from 18 to 6 and raised the total from 63 to 21.",
            "why_it_happened": "Multiple matching rows on customer_id caused fan-out during the join.",
            "fix_and_proof": "Deduplicating the right side reduced rows from 18 to 6 and total from 63 to 21; verification passed.",
        })

    bug = {
        "bug_type": "JOIN Fan-out",
        "severity": "High",
        "suspicious_step_id": "step_2",
        "evidence": {
            "left_rows": 6,
            "right_rows": 9,
            "joined_rows": 18,
            "join_key": "customer_id",
        },
    }
    fix_result = {
        "applied": True,
        "fixed_sql": "SELECT 1",
        "strategy": "dedupe_right_side_row_number",
    }
    proof = {
        "verified": True,
        "passed": True,
        "rows_before": 18,
        "rows_after": 6,
        "total_before": 63.0,
        "total_after": 21.0,
        "failed_steps_after": [],
    }

    result = explain_finding(
        bug,
        fix_result=fix_result,
        verification_result=proof,
        llm_callable=fake_llm,
    )

    assert len(seen_prompts) == 1
    assert "18" in seen_prompts[0]
    assert "6" in seen_prompts[0]
    assert result["source"] == "llm"
    assert isinstance(result["plain_text"], str) and result["plain_text"].strip()
    assert not ANSI_ESCAPE_RE.search(result["plain_text"])
    assert proof["passed"] is True
    assert proof["verified"] is True


def test_explain_finding_falls_back_when_llm_raises():
    def broken_llm(_prompt: str) -> str:
        raise RuntimeError("LLM provider timeout")

    bug = {
        "bug_type": "JOIN Fan-out",
        "evidence": {"joined_rows": 18, "join_key": "customer_id"},
    }
    fix_result = {"applied": True, "fixed_sql": "SELECT 1"}
    proof = {
        "verified": True,
        "passed": True,
        "rows_before": 18,
        "rows_after": 6,
        "total_before": 63.0,
        "total_after": 21.0,
    }

    result = explain_finding(
        bug,
        fix_result=fix_result,
        verification_result=proof,
        llm_callable=broken_llm,
    )

    assert result["source"] == "fallback"
    assert "18" in result["plain_text"]
    assert "6" in result["plain_text"]
    assert not ANSI_ESCAPE_RE.search(result["plain_text"])


def test_explain_finding_handles_none_totals_and_failed_verification():
    explainer = LLMExplainer(llm_callable=lambda _p: "")
    bug = {
        "bug_type": "Silent Data Drop / Empty Result Set",
        "evidence": {"rows_before": 2, "rows_after": 0},
    }
    fix_result = {"applied": False, "fixed_sql": None}
    proof = {
        "verified": False,
        "passed": False,
        "rows_before": 2,
        "rows_after": 0,
        "total_before": None,
        "total_after": None,
        "failed_steps_after": ["step_3"],
    }

    result = explainer.explain_finding(bug, fix_result=fix_result, verification_result=proof)

    assert result["source"] == "fallback"
    assert isinstance(result["plain_text"], str) and result["plain_text"].strip()
    assert not ANSI_ESCAPE_RE.search(result["plain_text"])
    assert proof["passed"] is False


def test_explain_report_summarizes_multiple_findings_safely():
    analysis = {
        "detected_bugs": [
            {
                "bug_type": "JOIN Fan-out",
                "suspicious_step_id": "step_2",
                "evidence": {"joined_rows": 18, "join_key": "customer_id"},
            }
        ]
    }
    fix_report = {
        "fixes": [{"applied": True, "fixed_sql": "SELECT 1"}],
    }
    proof = {
        "verified": True,
        "passed": True,
        "rows_before": 18,
        "rows_after": 6,
        "total_before": 63.0,
        "total_after": 21.0,
    }

    report_out = explain_report(
        analysis,
        fix_report=fix_report,
        verification_result=proof,
        llm_callable=lambda _p: (_ for _ in ()).throw(ValueError("offline")),
    )

    assert isinstance(report_out, dict)
    assert isinstance(report_out.get("summary"), str) and report_out["summary"].strip()
    assert not ANSI_ESCAPE_RE.search(report_out["summary"])