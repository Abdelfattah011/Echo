"""ECHO LLM Explainer (Milestone 3).

Architecture rule: THE CODE DECIDES, THE LLM ONLY EXPLAINS.

* Detectors / the fixer / ``verify()`` produce the findings, severities, fixes and the
  pass/fail verdict. This module never changes any of them.
* ``build_facts`` extracts the only information an explanation may use (the finding,
  its evidence, the fix result and the proof metrics).
* The LLM (optional) receives ONLY those facts and must answer with three short plain-words
  sections. Its answer is then GUARDED: unseen numbers, unknown table/column names,
  SQL, changed severity or a verdict that contradicts ``passed`` are rejected.
* Whenever the LLM is missing, slow, failing, empty, invalid or rejected by the guards,
  a deterministic plain-words explanation built from the same facts is returned
  (``source == "fallback"``). Nothing here raises into the caller.

Dependencies: standard library only. ``python-dotenv`` is used if it is installed.
"""

import copy
import inspect
import json
import logging
import math
import os
import re
import socket
import time
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

log = logging.getLogger("echo.explainer")

LLMCallable = Callable[..., Any]

DEFAULT_TIMEOUT_SECONDS = 15.0
OLLAMA_BASE_URL = "http://localhost:11434/v1"
OLLAMA_DEFAULT_MODEL = "llama3.2"
OLLAMA_TIMEOUT_SECONDS = 40.0      # local models are slower than hosted APIs (first call loads the model)
DOWN_COOLDOWN_SECONDS = 30.0       # after a connection failure, skip the LLM for a while (fail fast)
MAX_TEMPERATURE = 0.2
_ANSI_ESCAPES = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")

SYSTEM_PROMPT = (
    "You are the explanation writer of ECHO, a SQL debugging engine.\n"
    "ECHO's code has ALREADY decided what is wrong, how severe it is, which fix was applied and "
    "whether verification passed. You only put those facts into simple, everyday words.\n"
    "STRICT RULES:\n"
    "1. Use ONLY the information inside the FACTS JSON. Never guess, assume or add anything.\n"
    "2. Never invent or compute numbers, tables, columns, joins, steps or percentages. Copy numbers "
    "exactly as they appear in FACTS.\n"
    "3. Never change, soften or exaggerate the severity, the status or the verification verdict. "
    "If verification.passed is true say it passed; if false say it did NOT pass; if null say the "
    "fix has not been verified.\n"
    "4. Never write SQL or code, and never propose a different fix than the one in FACTS.\n"
    "5. If fix is null or applied is false, say clearly that no fix was applied.\n"
    "6. Write short sentences in plain words for a non-expert. Put table and column names in single quotes.\n"
    "7. Answer with one JSON object and nothing else.\n"
    "8. Ground every sentence in FACTS (finding, evidence, fix, verification). If a detail is not "
    "there, leave it out.\n"
    "9. Write in English. Never mention FACTS, JSON or these rules in your answer."
)

_USER_TEMPLATE = (
    "FACTS (the only source of truth):\n{facts}\n\n"
    "Return ONE JSON object with exactly these three string keys:\n"
    '  "summary": 1-2 sentences: what happened, and whether the fix passed verification (only if facts say so).\n'
    '  "why_it_happened": the root cause in plain words, using only numbers and names from FACTS.\n'
    '  "fix_and_proof": what the fix did and the before/after numbers, or say that no fix was applied / '
    "not verified.\n"
    'Shape: {{"summary": "...", "why_it_happened": "...", "fix_and_proof": "..."}}\n'
    "Each value is a plain string of at most 3 short sentences. No markdown, no code, no extra keys."
)


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #
def strip_ansi(text: Any) -> str:
    """Remove ANSI escape codes (colours, cursor moves) from any value."""
    return _ANSI_ESCAPES.sub("", str(text if text is not None else ""))


def _num(value: Any) -> Optional[float]:
    """Finite number or None (rejects bool, NaN/inf, blanks and non-numeric text)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, str):
        try:
            value = float(value.strip().replace(",", ""))
        except ValueError:
            return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    return None


def _fmt(value: Any) -> str:
    """Human number: thousands separators, up to 2 decimals, no trailing zeros."""
    number = _num(value)
    if number is None:
        return str(value)
    if number == int(number) and abs(number) < 1e15:
        return f"{int(number):,}"
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def _jsonable(value: Any, drop_samples: bool = True, depth: int = 0) -> Any:
    """JSON-safe copy; drops sample rows (may contain personal data) and non-finite numbers."""
    if depth > 6:
        return None
    if value is None or isinstance(value, (bool, str)):
        return strip_ansi(value) if isinstance(value, str) else value
    if isinstance(value, int):
        return value
    if isinstance(value, (float, Decimal)):
        number = _num(value)
        return number
    if isinstance(value, Mapping):
        out = {}
        for key, item in value.items():
            if drop_samples and str(key).lower().startswith("sample"):
                continue
            out[str(key)] = _jsonable(item, drop_samples, depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v, drop_samples, depth + 1) for v in list(value)[:50]]
    return strip_ansi(str(value))


def _as_dict(obj: Any) -> Dict[str, Any]:
    """Accept dicts, dataclass-like objects with to_dict(), or None."""
    if obj is None:
        return {}
    if isinstance(obj, Mapping):
        return dict(obj)
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        try:
            data = to_dict()
            return dict(data) if isinstance(data, Mapping) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _first_sentence_cap(text: str, limit: int) -> str:
    text = strip_ansi(text).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Facts: the only input an explanation may use
# --------------------------------------------------------------------------- #
def _pick_join_rows(before: Any, after: Any) -> Tuple[Optional[float], Optional[float]]:
    """From per-JOIN row counts pick the join the fix helped most (equal-length lists), else the last pair."""
    b = [_num(x) for x in before] if isinstance(before, (list, tuple)) else []
    a = [_num(x) for x in after] if isinstance(after, (list, tuple)) else []
    if not b or not a:
        return None, None
    if len(b) == len(a):
        pairs = [(x, y) for x, y in zip(b, a) if x is not None and y is not None]
        if not pairs:
            return None, None
        return max(pairs, key=lambda p: p[0] - p[1])
    return b[-1], a[-1]


def normalize_verification(verification: Any) -> Dict[str, Any]:
    """Unify the proof metrics. Accepts both ``passed`` and ``verified`` and per-JOIN row lists."""
    v = _as_dict(verification)
    if not v:
        return {"provided": False, "passed": None}
    passed = v.get("passed", v.get("verified"))
    passed = passed if isinstance(passed, bool) else None
    rows_before, rows_after = _num(v.get("rows_before")), _num(v.get("rows_after"))
    if rows_before is None and rows_after is None:
        rows_before, rows_after = _pick_join_rows(v.get("join_rows_before"), v.get("join_rows_after"))
    failed = v.get("failed_steps_after")
    return {
        "provided": True,
        "passed": passed,
        "rows_before": rows_before,
        "rows_after": rows_after,
        "total_before": _num(v.get("total_before")),
        "total_after": _num(v.get("total_after")),
        "failed_steps_after": [strip_ansi(x) for x in failed] if isinstance(failed, (list, tuple)) else [],
        "fan_out_findings_before": _num(v.get("fan_out_findings_before")),
        "fan_out_findings_after": _num(v.get("fan_out_findings_after")),
        "error": strip_ansi(v["error"]) if v.get("error") else None,
    }


def _table_from(bug: Dict[str, Any], fix: Dict[str, Any], evidence: Dict[str, Any]) -> Optional[str]:
    for candidate in (fix.get("target_table"), evidence.get("table"), evidence.get("right_table")):
        if candidate:
            return str(candidate)
    match = re.search(r"Confirmed:\s*([A-Za-z_][\w$]*)\.", str(bug.get("description") or ""))
    return match.group(1) if match else None


def build_facts(
    bug: Optional[Dict[str, Any]],
    fix_result: Any = None,
    verification_result: Any = None,
    include_samples: bool = False,
) -> Dict[str, Any]:
    """Extract the facts (and nothing else) that an explanation is allowed to rely on."""
    bug = _as_dict(bug)
    fix = _as_dict(fix_result)
    evidence = bug.get("evidence") if isinstance(bug.get("evidence"), Mapping) else {}
    validation = fix.get("validation") if isinstance(fix.get("validation"), Mapping) else {}
    drop = not include_samples

    facts: Dict[str, Any] = {
        "finding": {
            "bug_type": strip_ansi(bug.get("bug_type") or "Unknown issue"),
            "severity": strip_ansi(bug.get("severity") or "Unknown"),
            "step": bug.get("suspicious_step_id"),
            "scope": bug.get("scope"),
            "detector": bug.get("detector"),
            "table": _table_from(bug, fix, dict(evidence)),
            "code_description": strip_ansi(bug.get("description") or ""),
            "suggested_fix_hint": strip_ansi(bug.get("suggested_fix") or "") or None,
        },
        "evidence": _jsonable(dict(evidence), drop),
        "fix": None,
        "verification": normalize_verification(verification_result),
    }
    if fix:
        facts["fix"] = {
            "applied": bool(fix.get("applied")),
            "strategy": fix.get("strategy_used"),
            "target_table": fix.get("target_table"),
            "what_it_did": strip_ansi(fix.get("explanation") or "") or None,
            "warnings": [strip_ansi(w) for w in (fix.get("warnings") or [])][:5],
            "deterministic_choice": fix.get("deterministic"),
            "syntax_validated": validation.get("ast_sane") if validation else None,
            "database_planned": validation.get("db_planned") if validation else None,
        }
    return _jsonable(facts, drop)  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Deterministic plain-words explanation (the fallback, also the reference for the guards)
# --------------------------------------------------------------------------- #
def _why_text(facts: Dict[str, Any]) -> str:
    finding, ev = facts["finding"], facts.get("evidence") or {}
    kind = finding["bug_type"].lower()
    step = finding.get("step")
    where = f" at step '{step}'" if step else ""
    table = finding.get("table")

    if "fan-out" in kind:
        before, after = _num(ev.get("rows_before")), _num(ev.get("rows_after"))
        text = ""
        if before is not None and after is not None:
            factor = _num(ev.get("multiplication_factor"))
            times = f" ({_fmt(factor)} times as many)" if factor is not None else ""
            text = f"The join{where} made the row count grow from {_fmt(before)} to {_fmt(after)}{times}."
        stats = ev.get("right_key_stats") if isinstance(ev.get("right_key_stats"), dict) else None
        if stats and (_num(stats.get("duplicate_rows")) or 0) > 0:
            side = f" in '{table}'" if table else " of the joined table"
            text += (
                f" The join key{side} is not unique: {_fmt(stats.get('distinct_keys'))} different key value(s) "
                f"are spread over {_fmt(stats.get('key_rows'))} row(s), so each row on the left side is "
                "copied once for every matching row on the right side."
            )
        else:
            text += " ECHO could not confirm that the join key repeats, so this may be caused by a loose or missing join condition."
        if ev.get("likely_intentional"):
            text += " All totals in this query use columns from the 'many' side, so this growth is probably intentional."
        if ev.get("cartesian_suspected"):
            text += " The join condition does not tie both tables together, which can pair every row with every row."
        return text.strip() or finding["code_description"] or "Rows were multiplied by a join."

    if "inflated totals" in kind:
        measure = ev.get("measure")
        src, obs, fac = _num(ev.get("source_total")), _num(ev.get("observed_total")), _num(ev.get("inflation_factor"))
        text = f"The total{f' {measure}' if measure else ''} became too large{where}."
        if src is not None and obs is not None:
            text += f" Its own source only adds up to {_fmt(src)}, but the query reached {_fmt(obs)}"
            text += f" ({_fmt(fac)} times as much)." if fac is not None else "."
        if ev.get("last_correct_step"):
            text += f" The total was still correct at step '{ev['last_correct_step']}', so rows were duplicated after it."
        return text

    if "left join converted" in kind:
        before, after, lost = _num(ev.get("rows_before_filter")), _num(ev.get("rows_after_filter")), _num(ev.get("rows_lost"))
        text = "A condition in WHERE on the LEFT-joined table removes the rows that had no match, so the LEFT JOIN behaves like an INNER JOIN."
        if ev.get("filter"):
            text += f" The condition is: {ev['filter']}."
        if before is not None and after is not None:
            text += f" Rows went from {_fmt(before)} to {_fmt(after)}" + (f" ({_fmt(lost)} lost)." if lost is not None else ".")
        return text

    if "silent data drop" in kind or "empty result" in kind:
        before = _num(ev.get("rows_before"))
        text = f"Every row disappeared{where}"
        text += f": it had {_fmt(before)} row(s) coming in and 0 going out." if before is not None else "."
        return text + " This usually means a filter is too strict or the join keys never match."

    if "severe row loss" in kind:
        before, after, pct = _num(ev.get("rows_before")), _num(ev.get("rows_after")), _num(ev.get("rows_lost_pct"))
        text = f"Almost no rows found a partner in the join{where}."
        if before is not None and after is not None:
            text += f" Only {_fmt(after)} of {_fmt(before)} rows were kept" + (f" ({_fmt(pct)}% lost)." if pct is not None else ".")
        return text

    if "empty source" in kind:
        return f"The source table returned 0 rows{where}, so every later step is empty too."

    if "performance" in kind:
        took, limit = _num(ev.get("execution_time_ms")), _num(ev.get("threshold_ms"))
        text = f"One step{where} was slow."
        if took is not None:
            text += f" It took {_fmt(took)} ms" + (f", above the {_fmt(limit)} ms warning limit." if limit is not None else ".")
        return text

    if "null join key" in kind:
        nulls, total = _num(ev.get("null_rows")), _num(ev.get("total_rows"))
        name = ev.get("table") or table
        key = ", ".join(ev["key"]) if isinstance(ev.get("key"), list) else ev.get("key")
        text = "Some join keys are empty (NULL), and an empty key never matches anything in SQL."
        if nulls is not None and total is not None:
            text += f" {_fmt(nulls)} of {_fmt(total)} row(s)" + (f" in '{name}'" if name else "") + (f" have NULL in '{key}'." if key else " have a NULL key.")
        return text

    if "key type mismatch" in kind:
        text = "The two join columns have different data types, so the database has to convert one of them."
        if ev.get("left") and ev.get("right"):
            text += f" '{ev['left']}' is {ev.get('left_type')} but '{ev['right']}' is {ev.get('right_type')}."
        if ev.get("step_error"):
            text += f" The database reported: {ev['step_error']}"
        return text

    if "orphan" in kind or "lost rows" in kind:
        unmatched = _num(ev.get("unmatched_left_rows"))
        text = "Some rows on the left side have no partner on the right side of the join."
        if unmatched is not None:
            text += f" {_fmt(unmatched)} row(s) are affected."
        return text

    if "cartesian" in kind:
        before, after, right = _num(ev.get("rows_before")), _num(ev.get("rows_after")), _num(ev.get("right_table_rows"))
        text = "The join pairs every left row with every right row."
        if before is not None and after is not None:
            text += f" Rows went from {_fmt(before)} to {_fmt(after)}"
            text += f" ({_fmt(before)} x {_fmt(right)})." if right is not None else "."
        return text

    return finding["code_description"] or f"ECHO flagged this step as {finding['bug_type']}."


def _fix_text(facts: Dict[str, Any]) -> str:
    finding, fix, ver = facts["finding"], facts.get("fix"), facts.get("verification") or {}
    parts: List[str] = []
    if not fix:
        parts.append("No automatic fix was applied.")
        if finding.get("suggested_fix_hint"):
            parts.append(f"Suggested manual fix: {finding['suggested_fix_hint']}")
    elif not fix.get("applied"):
        parts.append("ECHO did not rewrite the query.")
        if fix.get("warnings"):
            parts.append(str(fix["warnings"][0]))
        elif finding.get("suggested_fix_hint"):
            parts.append(f"Suggested manual fix: {finding['suggested_fix_hint']}")
    else:
        target = fix.get("target_table") or finding.get("table")
        parts.append(f"ECHO rewrote the query{f' (table {chr(39)}{target}{chr(39)})' if target else ''}.")
        if fix.get("what_it_did"):
            parts.append(str(fix["what_it_did"]))
        checks = []
        if fix.get("syntax_validated"):
            checks.append("the new SQL was checked for valid syntax")
        if fix.get("database_planned"):
            checks.append("the database accepted it")
        if checks:
            parts.append("Checks: " + " and ".join(checks) + ".")
        if fix.get("deterministic_choice") is False:
            parts.append("Note: which duplicate row is kept is not fully defined, so set an explicit ordering.")

    if not ver.get("provided"):
        if fix and fix.get("applied"):
            parts.append("The fix has not been verified yet.")
    else:
        rb, ra = ver.get("rows_before"), ver.get("rows_after")
        tb, ta = ver.get("total_before"), ver.get("total_after")
        if rb is not None and ra is not None:
            parts.append(f"Rows at the affected step went from {_fmt(rb)} to {_fmt(ra)}.")
        if tb is not None and ta is not None:
            parts.append(f"The total went from {_fmt(tb)} to {_fmt(ta)}.")
        elif ver.get("total_before") is None and ver.get("total_after") is None and (rb is not None):
            parts.append("There was no numeric total to compare.")
        if ver.get("passed") is True:
            parts.append("Verification passed.")
        elif ver.get("passed") is False:
            text = "Verification did NOT pass."
            if ver.get("failed_steps_after"):
                text += " Steps that still fail: " + ", ".join(str(s) for s in ver["failed_steps_after"]) + "."
            parts.append(text)
        else:
            parts.append("The fix has not been verified yet.")
        if ver.get("error"):
            parts.append(f"Verification could not finish: {ver['error']}")
    return " ".join(parts)


def _summary_text(facts: Dict[str, Any]) -> str:
    finding, fix, ver = facts["finding"], facts.get("fix"), facts.get("verification") or {}
    where = f" at step '{finding['step']}'" if finding.get("step") else ""
    text = f"ECHO found a {finding['severity'].lower()}-severity issue: {finding['bug_type']}{where}."
    if fix and fix.get("applied"):
        if ver.get("passed") is True:
            text += " A fix was applied and verification passed."
        elif ver.get("passed") is False:
            text += " A fix was applied but verification did NOT pass."
        else:
            text += " A fix was applied but has not been verified yet."
    elif fix is not None:
        text += " No fix was applied."
    return text


def _assemble(summary: str, why: str, fix: str) -> str:
    return strip_ansi(f"{summary}\n\nWhy it happened: {why}\n\nFix and proof: {fix}").strip()


def fallback_explanation(facts: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic plain-words explanation built only from the facts."""
    summary, why, fix = _summary_text(facts), _why_text(facts), _fix_text(facts)
    return {
        "summary": strip_ansi(summary),
        "why_it_happened": strip_ansi(why),
        "fix_and_proof": strip_ansi(fix),
        "plain_text": _assemble(summary, why, fix),
        "source": "fallback",
        "fallback_reason": None,
    }


# --------------------------------------------------------------------------- #
# Prompt + guards for the LLM path
# --------------------------------------------------------------------------- #
def build_prompts(facts: Dict[str, Any]) -> Tuple[str, str]:
    """(system_prompt, user_prompt). The user prompt embeds the facts as JSON."""
    return SYSTEM_PROMPT, _USER_TEMPLATE.format(facts=json.dumps(facts, indent=2, sort_keys=True, default=str))


_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
_QUOTED = re.compile(r"`([^`]{1,60})`|\"([^\"]{1,60})\"|(?<![\w])'([A-Za-z_][\w.]{1,59})'(?![\w])")
_SQL_WORDS = {
    "select", "from", "join", "left", "right", "inner", "outer", "full", "cross", "where", "group", "by",
    "on", "null", "not", "and", "or", "row_number", "partition", "order", "qualify", "sum", "count", "avg",
    "union", "having", "distinct", "using", "true", "false", "key", "keys", "step",
}


def _collect_numbers(obj: Any, out: set) -> None:
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, (int, float)):
        if math.isfinite(obj):
            out.add(float(obj))
    elif isinstance(obj, str):
        for token in _NUMBER.findall(obj):
            try:
                out.add(float(token.replace(",", "")))
            except ValueError:
                pass
    elif isinstance(obj, Mapping):
        for value in obj.values():
            _collect_numbers(value, out)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _collect_numbers(value, out)


def _number_allowed(value: float, allowed: set) -> bool:
    if value in (0.0, 1.0):
        return True
    return any(
        abs(value - a) <= 1e-9 or abs(value - round(a)) <= 1e-9
        or abs(value - round(a, 1)) <= 1e-9 or abs(value - round(a, 2)) <= 1e-9
        for a in allowed
    )


_POSITIVE_CLAIM = re.compile(
    r"(?<!not )(?<!un)\bverified\b|(?<!not )(?<!n't )\bpassed\b|\bfix (?:worked|succeeded|is correct)\b"
    r"|\b(?:successfully|fully) (?:fixed|resolved|corrected)\b",
    re.IGNORECASE,
)
_NEGATIVE_CLAIM = re.compile(
    r"\b(?:did not|didn't|does not|not) (?:pass|work|succeed|verify)\b|\bverification failed\b|\bfix failed\b|\bnot verified\b",
    re.IGNORECASE,
)


_FIX_APPLIED_CLAIM = re.compile(
    r"\b(?:fix|rewrite|rewrote|rewritten)\b.{0,40}\b(?:applied|was made|was done)\b|\bwe (?:applied|fixed)\b", re.I)
_NEGATION = re.compile(r"\b(?:no|not|never|nothing|without|didn't|wasn't|hasn't|isn't)\b", re.I)


def _guard(sections: Dict[str, str], facts: Dict[str, Any]) -> None:
    """Raise ValueError when an LLM answer is not grounded in the facts."""
    text = " ".join(sections.values())
    if "```" in text or re.search(r"\bselect\b.{0,200}\bfrom\b", text, re.IGNORECASE | re.DOTALL):
        raise ValueError("the answer contains SQL/code")

    allowed: set = set()
    _collect_numbers(facts, allowed)
    for token in _NUMBER.findall(text):
        value = float(token.replace(",", ""))
        if not _number_allowed(value, allowed):
            raise ValueError(f"the answer contains a number that is not in the facts: {token}")

    facts_blob = json.dumps(facts, default=str).lower()
    for match in _QUOTED.finditer(text):
        name = next(g for g in match.groups() if g).strip()
        if name.lower() in _SQL_WORDS or name.lower() in facts_blob:
            continue
        raise ValueError(f"the answer names something that is not in the facts: {name}")

    severity = str(facts["finding"].get("severity") or "").lower()
    for match in re.finditer(r"\bseverity\W+(?:is |of |level )?(critical|high|medium|low)\b", text, re.IGNORECASE):
        if severity and match.group(1).lower() != severity:
            raise ValueError("the answer changes the severity")

    if not (facts.get("fix") or {}).get("applied"):
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if _FIX_APPLIED_CLAIM.search(sentence) and not _NEGATION.search(sentence):
                raise ValueError("the answer claims a fix was applied but none was")

    passed = (facts.get("verification") or {}).get("passed")
    if passed is not True:
        for sentence in re.split(r"(?<=[.!?])\s+", text):  # "has not been verified" is honest, not a success claim
            if _POSITIVE_CLAIM.search(sentence) and not _NEGATION.search(sentence):
                raise ValueError("the answer claims success but verification did not pass / was not run")
    if passed is True and _NEGATIVE_CLAIM.search(text):
        raise ValueError("the answer claims failure but verification passed")


def _extract_json(text: str) -> Dict[str, Any]:
    cleaned = strip_ansi(text).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        data = json.loads(cleaned)
    except ValueError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("the answer is not JSON")
        data = json.loads(cleaned[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("the answer is not a JSON object")
    return data


def _response_text(response: Any) -> str:
    """Pull the text out of a string, an OpenAI/Anthropic-style dict, or an object with .content."""
    if isinstance(response, str):
        return response
    if isinstance(response, Mapping):
        choices = response.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
            message = choices[0].get("message") or {}
            if isinstance(message, Mapping) and message.get("content"):
                return str(message["content"])
            if choices[0].get("text"):
                return str(choices[0]["text"])
        content = response.get("content")
        if isinstance(content, list):
            return "".join(str(b.get("text", "")) for b in content if isinstance(b, Mapping))
        for key in ("content", "text", "output_text", "response"):
            if isinstance(response.get(key), str):
                return response[key]
        return ""
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(getattr(b, "text", "")) for b in content)
    return ""


# --------------------------------------------------------------------------- #
# Default LLM client (urllib only)
# --------------------------------------------------------------------------- #
class LLMConfig:
    """Connection settings read from the environment (ECHO_LLM_*)."""

    def __init__(self, provider: str, api_key: Optional[str], base_url: str, model: str,
                 timeout: float, temperature: float, max_tokens: int):
        self.provider = provider
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Optional["LLMConfig"]:
        """Return a config, or None when no LLM is configured (-> deterministic fallback)."""
        provider = (env.get("ECHO_LLM_PROVIDER") or "openai").strip().lower()
        provider = "anthropic" if provider.startswith("anthropic") else "ollama" if provider.startswith("ollama") else "openai"
        key = (env.get("ECHO_LLM_API_KEY")
               or env.get("ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY") or "").strip()
        if provider == "ollama":  # local server: never send a real cloud key, any non-empty token works
            key = (env.get("ECHO_LLM_API_KEY") or "").strip() or "ollama"
        explicit_url = (env.get("ECHO_LLM_BASE_URL") or "").strip()
        if provider == "anthropic":
            if not key:
                return None
            base = explicit_url or "https://api.anthropic.com"
            model = (env.get("ECHO_LLM_MODEL") or "claude-haiku-5-5").strip()
        elif provider == "ollama":
            base = explicit_url or OLLAMA_BASE_URL
            model = (env.get("ECHO_LLM_MODEL") or OLLAMA_DEFAULT_MODEL).strip()
        else:
            if not key and not explicit_url:  # a local OpenAI-compatible server needs no key
                return None
            base = explicit_url or "https://api.openai.com/v1"
            model = (env.get("ECHO_LLM_MODEL") or "gpt-4o-mini").strip()
        default_timeout = OLLAMA_TIMEOUT_SECONDS if provider == "ollama" else DEFAULT_TIMEOUT_SECONDS
        try:
            timeout = float(env.get("ECHO_LLM_TIMEOUT") or default_timeout)
        except ValueError:
            timeout = default_timeout
        try:
            temperature = float(env.get("ECHO_LLM_TEMPERATURE") or 0.1)
        except ValueError:
            temperature = 0.1
        try:
            max_tokens = int(env.get("ECHO_LLM_MAX_TOKENS") or 700)
        except ValueError:
            max_tokens = 700
        return cls(provider, key or None, base, model, max(1.0, timeout),
                   min(max(temperature, 0.0), MAX_TEMPERATURE), max(64, max_tokens))


class HttpLLMClient:
    """OpenAI-compatible (``/chat/completions``) or Anthropic (``/v1/messages``) client on urllib."""

    def __init__(self, config: LLMConfig):
        self.config = config
        self._down_until = 0.0

    def __call__(self, system_prompt: str, user_prompt: str) -> str:
        cfg = self.config
        if time.monotonic() < self._down_until:  # server was unreachable a moment ago: fail fast
            raise RuntimeError("LLM unreachable (skipped: server was down moments ago)")
        if cfg.provider == "anthropic":
            url = cfg.base_url.rstrip("/") + "/v1/messages"
            headers = {"content-type": "application/json", "x-api-key": cfg.api_key or "",
                       "anthropic-version": "2023-06-01"}
            body = {"model": cfg.model, "max_tokens": cfg.max_tokens, "temperature": cfg.temperature,
                    "system": system_prompt, "messages": [{"role": "user", "content": user_prompt}]}
        else:
            url = cfg.base_url.rstrip("/") + "/chat/completions"
            headers = {"content-type": "application/json"}
            if cfg.api_key:
                headers["authorization"] = f"Bearer {cfg.api_key}"
            body = {"model": cfg.model, "temperature": cfg.temperature, "max_tokens": cfg.max_tokens,
                    "messages": [{"role": "system", "content": system_prompt},
                                 {"role": "user", "content": user_prompt}]}
            if cfg.provider == "ollama":
                body["stream"] = False
                body["response_format"] = {"type": "json_object"}
        request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=cfg.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            hint = f" (model not found: run 'ollama pull {cfg.model}')" if cfg.provider == "ollama" and exc.code == 404 else ""
            raise RuntimeError(f"LLM HTTP error {exc.code}{hint}") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            self._down_until = time.monotonic() + DOWN_COOLDOWN_SECONDS
            raise RuntimeError(f"LLM unreachable: {type(exc).__name__}") from None
        except ValueError:
            raise RuntimeError("LLM returned a non-JSON response") from None
        return _response_text(payload)


def check_llm_health(base_url: Optional[str] = None, model: Optional[str] = None,
                     timeout: float = 3.0) -> Dict[str, Any]:
    """Quick reachability check for a local Ollama server. Never raises.

    Tries Ollama's ``/api/tags`` first, then the OpenAI-style ``/v1/models``.
    Returns {ok, endpoint, models, model_available, error}.
    """
    base = (base_url or OLLAMA_BASE_URL).strip().rstrip("/")
    root = base[:-3] if base.endswith("/v1") else base
    result: Dict[str, Any] = {"ok": False, "endpoint": None, "models": [], "model_available": None, "error": None}
    for url, key in ((root + "/api/tags", "models"), (base + "/models", "data")):
        try:
            request = urllib.request.Request(url, headers={"accept": "application/json"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            result["error"] = strip_ansi(f"{type(exc).__name__}: {exc}")[:120]
            continue
        items = payload.get(key) if isinstance(payload, dict) else None
        names = [str(m.get("name") or m.get("id")) for m in (items or []) if isinstance(m, dict)]
        result.update(ok=True, endpoint=url, models=names, error=None)
        if model:
            wanted, lowered = model.strip().lower(), [n.lower() for n in names]
            result["model_available"] = wanted in lowered or f"{wanted}:latest" in lowered
        return result
    return result


_DOTENV_LOADED = False


def _load_dotenv_once() -> None:
    """Load a .env file if python-dotenv is installed (never overrides real environment variables)."""
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    _DOTENV_LOADED = True
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(override=False)
    except Exception:  # noqa: BLE001 - optional dependency
        pass


# --------------------------------------------------------------------------- #
# The explainer
# --------------------------------------------------------------------------- #
class LLMExplainer:
    """Explains findings in plain words. Uses an LLM when available, otherwise deterministic text.

    ``llm_callable(system_prompt, user_prompt) -> str`` (or a single-argument callable receiving
    both prompts joined) may be injected; otherwise the ECHO_LLM_* environment is used; otherwise
    no LLM is called.
    """

    def __init__(
        self,
        llm_callable: Optional[LLMCallable] = None,
        *,
        env: Optional[Mapping[str, str]] = None,
        config: Optional[LLMConfig] = None,
        include_samples: bool = False,
        max_findings: int = 3,
    ):
        if env is None:
            _load_dotenv_once()
            env = os.environ
        self.include_samples = include_samples
        self.max_findings = max(1, int(max_findings))
        self._llm: Optional[LLMCallable] = llm_callable
        self.config: Optional[LLMConfig] = None
        if self._llm is None:
            self.config = config or LLMConfig.from_env(env)
            if self.config is not None:
                self._llm = HttpLLMClient(self.config)

    @property
    def llm_enabled(self) -> bool:
        return self._llm is not None

    # ---- one finding ------------------------------------------------------ #
    def explain_finding(self, bug: Optional[Dict[str, Any]], fix_result: Any = None,
                        verification_result: Any = None) -> Dict[str, Any]:
        """Return {summary, why_it_happened, fix_and_proof, plain_text, source, fallback_reason, model, provider}.

        ``model`` / ``provider`` name the configured LLM (None when no LLM is configured); ``source``
        says whether that LLM actually produced the text.
        """
        result = self._explain_one(bug, fix_result, verification_result)
        result["model"] = self.config.model if self.config else None
        result["provider"] = self.config.provider if self.config else None
        return result

    def _explain_one(
        self,
        bug: Optional[Dict[str, Any]],
        fix_result: Any = None,
        verification_result: Any = None,
    ) -> Dict[str, Any]:
        """Return {summary, why_it_happened, fix_and_proof, plain_text, source, fallback_reason}."""
        try:
            facts = build_facts(bug, fix_result, verification_result, self.include_samples)
            reference = fallback_explanation(facts)
        except Exception as exc:  # noqa: BLE001 - last line of defence
            log.exception("Could not build facts")
            return self._emergency(f"could not read the findings ({type(exc).__name__})")

        if self._llm is None:
            reference["fallback_reason"] = "LLM not configured"
            return reference
        try:
            sections = self._ask_llm(facts)
        except Exception as exc:  # noqa: BLE001 - timeouts, HTTP errors, bad output, guard rejections
            log.warning("LLM explanation rejected, using fallback: %s", exc)
            reference["fallback_reason"] = strip_ansi(str(exc))[:200] or type(exc).__name__
            return reference
        return {
            "summary": sections["summary"],
            "why_it_happened": sections["why_it_happened"],
            "fix_and_proof": sections["fix_and_proof"],
            "plain_text": _assemble(sections["summary"], sections["why_it_happened"], sections["fix_and_proof"]),
            "source": "llm",
            "fallback_reason": None,
        }

    def _invoke(self, system_prompt: str, user_prompt: str) -> Any:
        fn = self._llm
        assert fn is not None
        try:
            params = [p for p in inspect.signature(fn).parameters.values()
                      if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
            variadic = any(p.kind == p.VAR_POSITIONAL for p in inspect.signature(fn).parameters.values())
            two_args = len(params) >= 2 or variadic
        except (TypeError, ValueError):
            two_args = True
        if two_args:
            return fn(system_prompt, user_prompt)
        return fn(f"{system_prompt}\n\n{user_prompt}")

    def _ask_llm(self, facts: Dict[str, Any]) -> Dict[str, str]:
        system_prompt, user_prompt = build_prompts(facts)
        raw = _response_text(self._invoke(system_prompt, user_prompt))
        if not raw or not raw.strip():
            raise ValueError("the LLM returned an empty answer")
        data = _extract_json(raw)
        sections: Dict[str, str] = {}
        for key in ("summary", "why_it_happened", "fix_and_proof"):
            value = data.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"the answer has no '{key}' text")
            sections[key] = _first_sentence_cap(value, 1200)
        _guard(sections, facts)
        return sections

    @staticmethod
    def _emergency(reason: str) -> Dict[str, Any]:
        text = "ECHO found an issue but could not build a detailed explanation. See the detected issue and evidence."
        return {"summary": text, "why_it_happened": "Unavailable.", "fix_and_proof": "Unavailable.",
                "plain_text": text, "source": "fallback", "fallback_reason": reason}

    # ---- whole analysis --------------------------------------------------- #
    def explain_report(
        self,
        analysis: Optional[Dict[str, Any]] = None,
        fix_report: Any = None,
        verification_result: Any = None,
    ) -> Dict[str, Any]:
        """Explain an analysis (``detected_bugs``) together with its fix report and proof."""
        try:
            result = self._explain_report(analysis, fix_report, verification_result)
        except Exception as exc:  # noqa: BLE001
            log.exception("explain_report failed")
            result = self._emergency(f"{type(exc).__name__}")
            result["findings"] = []
        result.setdefault("model", self.config.model if self.config else None)
        result.setdefault("provider", self.config.provider if self.config else None)
        return result

    def _explain_report(self, analysis, fix_report, verification_result) -> Dict[str, Any]:
        analysis = _as_dict(analysis)
        report = _as_dict(fix_report)
        bugs = [b for b in (analysis.get("detected_bugs") or []) if isinstance(b, Mapping)]
        fixes = [f for f in (report.get("fixes") or []) if isinstance(f, (Mapping, object))]
        fixes = [_as_dict(f) for f in fixes]

        if not bugs:
            text = "ECHO found no issues in this query."
            if analysis.get("verdict") == "error":
                text = "ECHO could not analyse this query because of an error."
            return {
                "summary": text,
                "why_it_happened": "There is nothing to explain: no anomaly was detected.",
                "fix_and_proof": "No fix was needed.",
                "plain_text": f"{text}\n\nNo fix was needed.",
                "source": "fallback",
                "fallback_reason": "no findings",
                "findings": [],
            }

        explained: List[Dict[str, Any]] = []
        for index, bug in enumerate(bugs[: self.max_findings]):
            fix = self._match_fix(bug, index, fixes, len(bugs))
            explanation = self.explain_finding(bug, fix, verification_result if (fix or {}).get("applied") else None)
            explanation["bug_type"] = bug.get("bug_type")
            explanation["suspicious_step_id"] = bug.get("suspicious_step_id")
            explained.append(explanation)

        top = explained[0]
        more = [str(b.get("bug_type")) for b in bugs[self.max_findings:]]
        n = len(bugs)
        summary = top["summary"] if n == 1 else f"ECHO found {n} issues. Most important: {top['summary']}"
        why = top["why_it_happened"]
        if n > 1:
            others = [e["bug_type"] for e in explained[1:]] + more
            why += " Other findings: " + ", ".join(str(o) for o in others) + "."
        blocks = []
        for e in explained:
            blocks.append(f"[{e['bug_type']}]\n{e['plain_text']}")
        if more:
            blocks.append("Other findings (not expanded): " + ", ".join(more))
        return {
            "summary": summary,
            "why_it_happened": why,
            "fix_and_proof": top["fix_and_proof"],
            "plain_text": strip_ansi("\n\n".join(blocks)),
            "source": "llm" if all(e["source"] == "llm" for e in explained) else "fallback",
            "fallback_reason": next((e["fallback_reason"] for e in explained if e["source"] != "llm"), None),
            "findings": explained,
        }

    @staticmethod
    def _match_fix(bug: Mapping, index: int, fixes: List[Dict[str, Any]], bug_count: int) -> Optional[Dict[str, Any]]:
        evidence = bug.get("evidence") if isinstance(bug.get("evidence"), Mapping) else {}
        steps = {bug.get("suspicious_step_id"), evidence.get("join_step")} - {None}
        for fix in fixes:
            if fix.get("bug_type") == bug.get("bug_type") and fix.get("target_step") in steps:
                return fix
        if len(fixes) == bug_count:  # FixGenerator.fix_all returns one result per finding, in order
            return fixes[index]
        return None


# --------------------------------------------------------------------------- #
# Module-level entry points
# --------------------------------------------------------------------------- #
_DEFAULT: Optional[LLMExplainer] = None


def get_default_explainer() -> LLMExplainer:
    """Process-wide explainer configured from the environment / .env (created lazily)."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = LLMExplainer()
    return _DEFAULT


def explain_finding(
    bug: Optional[Dict[str, Any]],
    fix_result: Any = None,
    verification_result: Any = None,
    *,
    llm_callable: Optional[LLMCallable] = None,
    explainer: Optional[LLMExplainer] = None,
) -> Dict[str, Any]:
    """Explain one finding. Never raises. See ``LLMExplainer.explain_finding``."""
    engine = explainer or (LLMExplainer(llm_callable) if llm_callable else get_default_explainer())
    return engine.explain_finding(bug, fix_result, verification_result)


def explain_report(
    analysis: Optional[Dict[str, Any]] = None,
    fix_report: Any = None,
    verification_result: Any = None,
    *,
    llm_callable: Optional[LLMCallable] = None,
    explainer: Optional[LLMExplainer] = None,
) -> Dict[str, Any]:
    """Explain a whole analysis (+ fix report + proof). Never raises. See ``LLMExplainer.explain_report``."""
    engine = explainer or (LLMExplainer(llm_callable) if llm_callable else get_default_explainer())
    return engine.explain_report(analysis, fix_report, verification_result)


def copy_for_prompt(obj: Any) -> Any:  # pragma: no cover - convenience for custom callers
    return copy.deepcopy(obj)