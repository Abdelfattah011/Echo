"""ECHO dashboard: ONE file.   Run:  streamlit run echo_dashboard.py

Flow: editor -> /investigate + /fix (verify) -> explainer -> panels.
Slots (search for "SLOT n"): 1 Existing dashboard, 2 Parser, 3 Runner, 4 Base detector,
5 Fan-out detector, 6 General detector, 7 Fixer & proof, 8 Explainer, 9 FastAPI client.

session_state contract:
    analysis      /investigate payload     fix_result   normalised /fix payload
    verification  verify() metrics         explanation  explainer output
    api_error     last error message
"""
import difflib
import html
import importlib
import math
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

import altair as alt
import pandas as pd
import requests
import sqlglot
import streamlit as st
from sqlglot import expressions as exp

st.set_page_config(
    page_title="ECHO | SQL Debugging & Proof",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="expanded",
)

DEFAULT_SQL = """SELECT
    o.status,
    SUM(o.amount) AS total
FROM orders o
JOIN customers c ON o.customer_id = c.customer_id
JOIN customer_addresses a ON c.customer_id = a.customer_id
GROUP BY o.status"""
NO_TOTAL = "N/A (no numeric columns)"
STEPS_PER_ROW = 4

# =====================================================================
# SHARED HELPERS (ANSI stripping, safe casts, formatting)
# =====================================================================
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")


def strip_ansi(value: Any) -> str:
    return _ANSI.sub("", "" if value is None else str(value))


def as_dict(v: Any) -> Dict[str, Any]:
    return v if isinstance(v, dict) else {}


def as_list(v: Any) -> List[Any]:
    return v if isinstance(v, list) else []


def to_plain(obj: Any) -> Any:
    """Turn pydantic models / dataclass-like results into plain dicts (in-process engine)."""
    if obj is None or isinstance(obj, (dict, list, str, int, float, bool)):
        return obj
    for attr in ("model_dump", "dict", "to_dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                return fn()
            except Exception:  # noqa: BLE001
                continue
    return obj


def esc(v: Any, fallback: str = "") -> str:
    text = strip_ansi(v).strip()
    return html.escape(text) if text else fallback


def num(v: Any) -> Optional[float]:
    """Finite number or None (bool, NaN, inf, strings are rejected)."""
    if isinstance(v, bool) or not isinstance(v, (int, float, Decimal)):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def fmt(v: Any, empty: str = "N/A") -> str:
    n = num(v)
    if n is None:
        return empty
    return f"{int(n):,}" if n == int(n) else f"{n:,.2f}"


def delta_badge(before: Any, after: Any, unit: str = "") -> str:
    b, a = num(before), num(after)
    if b is None or a is None:
        return ""
    d = a - b
    sign = "+" if d > 0 else ("−" if d < 0 else "±")
    return f"<span class='delta'>{sign}{fmt(abs(d))}{unit}</span>"


def suspicious_step_id(analysis: Dict[str, Any]) -> Optional[str]:
    """Id of the first suspicious step, whatever shape the engine used (str or dict)."""
    value = analysis.get("first_suspicious_step")
    if isinstance(value, dict):
        value = value.get("step_id")
    if not value:
        for bug in as_list(analysis.get("detected_bugs")):
            if isinstance(bug, dict) and bug.get("suspicious_step_id"):
                value = bug.get("suspicious_step_id")
                break
    text = strip_ansi(value).strip() if value else ""
    return text or None


# =====================================================================
# SLOT 1: EXISTING DASHBOARD
# =====================================================================
# In: analysis (the /investigate payload). Paste/replace your step-trace grid, chart and
# any extra views here. It is rendered once, inside the "Step trace" section.
# --- START EXISTING DASHBOARD CODE ---
def render_existing_dashboard(analysis: Dict[str, Any]) -> None:
    traces = [s for s in as_list(analysis.get("step_trace")) if isinstance(s, dict)]
    if not traces:
        st.info("No execution step traces returned from the engine runner.")
        return
    suspect = suspicious_step_id(analysis)
    for start in range(0, len(traces), STEPS_PER_ROW):
        cols = st.columns(STEPS_PER_ROW, gap="medium")
        for i, step in enumerate(traces[start : start + STEPS_PER_ROW]):
            failed = step.get("status") == "failed"
            flagged = bool(suspect) and strip_ansi(step.get("step_id")) == suspect
            ms = step.get("execution_time_ms")
            err = f"<div class='tc-err'><b>Error:</b> {esc(step.get('error'))}</div>" if failed else ""
            css = "failed" if (failed or flagged) else "ok"
            label = "FAILED" if failed else ("SUSPICIOUS" if flagged else esc(step.get("step_type"), "STEP"))
            with cols[i]:
                st.markdown(
                    f"<div class='trace-card {css}'>"
                    f"<div class='tc-top'><span>{label}</span>"
                    f"<span class='cy'>⚡ {f'{esc(ms)}ms' if ms is not None else '—'}</span></div>"
                    f"<div class='tc-title'>{esc(step.get('step_id'))}</div>"
                    f"<div class='tc-rows'>{fmt(step.get('row_count'), '—')}</div>"
                    f"<div class='mono dim'>ROW COUNT</div>{err}</div>",
                    unsafe_allow_html=True,
                )
    valid = [
        (i, s) for i, s in enumerate(traces)
        if num(s.get("row_count")) is not None and s.get("status") == "success"
    ]
    if not valid:
        return
    try:
        # Only plain scalar columns go to Altair: raw step dicts hold nested payloads it cannot serialise.
        df = pd.DataFrame(
            [
                {
                    "step_id": strip_ansi(s.get("step_id")) or f"step_{i + 1}",
                    "step_type": strip_ansi(s.get("step_type")),
                    "row_count": num(s.get("row_count")),
                    "execution_time_ms": num(s.get("execution_time_ms")),
                    "flag": "suspicious"
                    if suspect and strip_ansi(s.get("step_id")) == suspect
                    else "normal",
                }
                for i, s in valid
            ]
        )
        chart = (
            alt.Chart(df)
            .mark_bar(cornerRadiusTopLeft=8, cornerRadiusTopRight=8, size=44)
            .encode(
                x=alt.X("step_id:N", sort=None, title=None, axis=alt.Axis(labelAngle=-15, labelColor="#a0abc2")),
                y=alt.Y("row_count:Q", title="Row count", axis=alt.Axis(labelColor="#a0abc2", titleColor="#8b95ad")),
                color=alt.Color(
                    "flag:N",
                    scale=alt.Scale(domain=["normal", "suspicious"], range=["#58e6d9", "#ff5c75"]),
                    legend=None,
                ),
                tooltip=[
                    alt.Tooltip("step_id:N", title="Step"),
                    alt.Tooltip("step_type:N", title="Type"),
                    alt.Tooltip("row_count:Q", title="Rows", format=","),
                    alt.Tooltip("execution_time_ms:Q", title="Duration (ms)"),
                ],
            )
            .properties(height=320)
            .configure(background="transparent", view=alt.ViewConfig(stroke=None))
            .configure_axis(gridColor="#1b2438", domainColor="#2b3856", tickColor="#2b3856")
        )
        st.altair_chart(chart, use_container_width=True)
    except Exception as exc:  # noqa: BLE001
        st.caption(f"Row-count chart unavailable: {strip_ansi(exc)}")
# --- END EXISTING DASHBOARD CODE ---


# =====================================================================
# SLOT 2: PARSER
# =====================================================================
# In: sql. Out: list of step dicts or None. Sidebar preview only.
# --- START PARSER CODE / ADAPTER ---
def parser_adapter(sql: str) -> Optional[List[Dict[str, Any]]]:
    try:
        from Engine.parser.parser import parse_sql_to_steps

        return parse_sql_to_steps(sql)
    except Exception:  # noqa: BLE001
        return None
# --- END PARSER CODE / ADAPTER ---


# =====================================================================
# SLOT 3: RUNNER
# =====================================================================
# In: sql. Out: analysis dict (EchoEngineRunner.analyze_query) or None.
# Used only when "In-process engine" is on. Needs api.main importable from the project root.
# --- START RUNNER CODE / ADAPTER ---
def runner_adapter(sql: str) -> Optional[Dict[str, Any]]:
    try:
        from api.main import InvestigateRequest, investigate

        return to_plain(investigate(InvestigateRequest(sql=sql)))
    except Exception:  # noqa: BLE001
        return None
# --- END RUNNER CODE / ADAPTER ---


# =====================================================================
# SLOT 4: BASE DETECTOR
# =====================================================================
# In: one bug dict. Out: True when it belongs in the "Base" tab (runner-level findings).
# --- START BASE DETECTOR CODE / ADAPTER ---
def base_detector_adapter(bug: Dict[str, Any]) -> bool:
    name = strip_ansi(bug.get("detector")).lower()
    return "runner" in name or "base" in name or not name
# --- END BASE DETECTOR CODE / ADAPTER ---


# =====================================================================
# SLOT 5: FAN-OUT DETECTOR
# =====================================================================
# --- START FAN-OUT DETECTOR CODE / ADAPTER ---
def fanout_detector_adapter(bug: Dict[str, Any]) -> bool:
    return "fanout" in strip_ansi(bug.get("detector")).lower() or "fan-out" in strip_ansi(bug.get("bug_type")).lower()
# --- END FAN-OUT DETECTOR CODE / ADAPTER ---


# =====================================================================
# SLOT 6: GENERAL DETECTOR
# =====================================================================
# --- START GENERAL DETECTOR CODE / ADAPTER ---
_GENERAL_BUG_MARKERS = (
    "performance bottleneck",
    "silent data drop",
    "severe row loss",
    "empty source",
    "complex aggregation",
)


def general_detector_adapter(bug: Dict[str, Any]) -> bool:
    # Findings do not always carry a "detector" label, so also recognise them by bug type.
    if "general" in strip_ansi(bug.get("detector")).lower():
        return True
    bug_type = strip_ansi(bug.get("bug_type")).lower()
    return any(marker in bug_type for marker in _GENERAL_BUG_MARKERS)
# --- END GENERAL DETECTOR CODE / ADAPTER ---


def group_bugs(bugs: List[Any]) -> Dict[str, List[Dict[str, Any]]]:
    groups: Dict[str, List[Dict[str, Any]]] = {"Fan-Out": [], "General": [], "Base": []}
    for bug in bugs:
        if not isinstance(bug, dict):
            continue
        try:
            key = "Fan-Out" if fanout_detector_adapter(bug) else "General" if general_detector_adapter(bug) else "Base"
        except Exception:  # noqa: BLE001
            key = "Base"
        groups[key].append(bug)
    return groups


# =====================================================================
# SLOT 7: FIXER & verify() PROOF
# =====================================================================
# In: raw /fix payload. Out: normalised dict (never missing keys). total_* may be None.
# verification keys: passed|verified, rows_before/after, total_before/after, failed_steps_after,
# fan_out_findings_before/after, join_rows_before/after.
# --- START FIXER & PROOF CODE / ADAPTER ---
def normalize_fix(payload: Any, sql: str) -> Dict[str, Any]:
    data = as_dict(to_plain(payload))
    ver = as_dict(data.get("verification"))
    passed = ver.get("passed", ver.get("verified"))
    return {
        "applied": bool(data.get("applied")),
        "fixed_sql": strip_ansi(data.get("fixed_sql") or sql),
        "original_sql": strip_ansi(data.get("original_sql") or sql),
        "fixes": [f for f in as_list(data.get("fixes")) if isinstance(f, dict)],
        "warnings": [strip_ansi(w) for w in as_list(data.get("warnings"))],
        "analysis": as_dict(data.get("analysis")),
        "verification": {
            "provided": bool(ver),
            "passed": passed if isinstance(passed, bool) else None,
            "rows_before": ver.get("rows_before"),
            "rows_after": ver.get("rows_after"),
            "total_before": ver.get("total_before"),
            "total_after": ver.get("total_after"),
            "failed_steps_after": as_list(ver.get("failed_steps_after")),
            "fan_out_findings_before": ver.get("fan_out_findings_before"),
            "fan_out_findings_after": ver.get("fan_out_findings_after"),
            "error": strip_ansi(ver.get("error")) if ver.get("error") else None,
            "reasons": [strip_ansi(r) for r in as_list(ver.get("reasons"))],
            "checks": as_dict(ver.get("checks")),
        },
        "input_notes": [strip_ansi(n) for n in as_list(data.get("input_notes"))],
        # --- optional proposal fields (absent in older API versions -> safe defaults) ---
        "status": str(data.get("status") or ("applied" if data.get("applied") else "none")),
        "proposed_sql": strip_ansi(data.get("proposed_sql")) if data.get("proposed_sql") else "",
        "proposals": [p for p in as_list(data.get("proposals")) if isinstance(p, dict)],
        "proposal_verification": as_dict(data.get("proposal_verification")),
    }


FIX_STATUS_LABELS = {
    "verified": ("VERIFIED FIX", "t-ok"),
    "applied_unverified": ("APPLIED • NOT VERIFIED", "t-warn"),
    "applied": ("APPLIED FIX", "t-ok"),
    "suggested": ("SUGGESTED FIX • REVIEW GRAIN / LOGIC", "t-warn"),
    "manual": ("MANUAL ACTION REQUIRED", "t-bad"),
}


def build_proposal_sql(proposed_sql: str, status: str, notes: List[str]) -> str:
    """The engine's proposed script under ECHO comment lines (stripped again on the next run)."""
    label = FIX_STATUS_LABELS.get(status, ("PROPOSAL", ""))[0]
    header = f"-- ECHO {label}: not applied automatically, review it before using it.\n"
    header += "".join(f"-- ECHO note: {_one_line(n)}\n" for n in notes[:4])
    return header + strip_echo_headers(proposed_sql)
# --- END FIXER & PROOF CODE / ADAPTER ---


# ---------------------------------------------------------------------
# Advisory SQL (shown when the engine could not apply a verified rewrite)
#
# Convergence rules: every line this builder adds starts with "-- ECHO ", leading ECHO header
# lines are stripped before anything else, trailing semicolons are normalised, and a query that
# already carries a deduplicated JOIN is returned unchanged. Pasting the advisory back into the
# editor therefore never stacks headers, never adds ";;" and never rewrites twice.
# ---------------------------------------------------------------------
_ECHO_LINE = re.compile(r"^[ \t]*--[ \t]*ECHO\b.*$")
_ECHO_HEADER = re.compile(  # legacy advisory headers, removed only at the very top of the text
    r"^[ \t]*--[ \t]*((?-i:ECHO)\b|Suggested grain-safe rewrite|Advisory SQL for|Recommendation:|"
    r"Syntax error detected|Fix the SQL syntax first)",
    re.IGNORECASE,
)
# A finding is only turned into a JOIN rewrite when it is about duplicated join keys.
_REWRITABLE_LABELS = ("fan-out", "fanout", "inflated", "duplicat")


def _one_line(value: Any) -> str:
    return " ".join(strip_ansi(value).split())


def strip_echo_headers(sql: Any) -> str:
    """Remove the comment lines ECHO itself adds (same rules as Engine.parser.strip_echo_headers).

    Whole-line ``-- ECHO ...`` comments are removed wherever they are (never inside a string or a
    block comment); legacy header lines only at the top. Everything else is left untouched.
    """
    text = strip_ansi(sql).replace("\r\n", "\n").replace("\r", "\n")
    kept: List[str] = []
    in_block, in_quote = False, None
    for line in text.split("\n"):
        if not (in_block or in_quote is not None) and _ECHO_LINE.match(line):
            continue
        kept.append(line)
        i, n = 0, len(line)
        while i < n:
            ch = line[i]
            if in_block:
                end = line.find("*/", i)
                if end == -1:
                    i = n
                else:
                    in_block, i = False, end + 2
            elif in_quote:
                if ch == in_quote:
                    if i + 1 < n and line[i + 1] == in_quote:
                        i += 2
                        continue
                    in_quote = None
                i += 1
            elif line[i:i + 2] == "--":
                i = n
            elif line[i:i + 2] == "/*":
                in_block, i = True, i + 2
            elif ch in ("'", '"', "`"):
                in_quote, i = ch, i + 1
            else:
                i += 1
    while kept and (not kept[0].strip() or _ECHO_HEADER.match(kept[0])):
        kept.pop(0)
    return "\n".join(kept).strip()


def _strip_terminators(sql: str) -> str:
    return re.sub(r"[;\s]+$", "", sql)


def _terminate(sql: str, terminator: str) -> str:
    """Append ``;`` without letting a trailing ``-- comment`` swallow it."""
    if not terminator:
        return sql
    last_line = sql.rsplit("\n", 1)[-1]
    return f"{sql}\n{terminator}" if "--" in last_line else f"{sql}{terminator}"


def _flatten_and(node: Any) -> List[Any]:
    node = node.unnest()
    if isinstance(node, exp.And):
        return _flatten_and(node.this) + _flatten_and(node.expression)
    return [node]


def _has_dedupe_join(select: Any) -> bool:
    """True when a JOIN target is already a one-row-per-key sub-query (QUALIFY or DISTINCT)."""
    for join in select.args.get("joins") or []:
        target = join.this
        if isinstance(target, exp.Subquery):
            inner = target.this
            if isinstance(inner, exp.Select) and (
                inner.args.get("qualify") is not None or inner.args.get("distinct") is not None
            ):
                return True
    return False


def inflating_join_indexes(traces: List[Any]) -> Optional[set]:
    """0-based positions (in JOIN order of the main query) of JOIN steps whose row count GREW.

    Returns None when the trace carries no usable JOIN information.
    """
    previous: Optional[float] = None
    join_index = -1
    grown: set = set()
    saw_join = False
    for step in traces:
        if not isinstance(step, dict) or step.get("status") == "failed":
            continue
        scope = str(step.get("scope") or "main")
        if "/" in scope or scope.startswith("cte:"):
            continue  # only the main query pipeline
        rows = num(step.get("row_count"))
        kind = str(step.get("step_type") or "").upper()
        if kind == "JOIN":
            saw_join = True
            join_index += 1
            if rows is not None and previous is not None and rows > previous:
                grown.add(join_index)
        if kind in ("BASE_TABLE", "JOIN", "WHERE") and rows is not None:
            previous = rows
    return grown if saw_join else None


def _select_uses_star(select: Any) -> bool:
    for item in select.expressions:
        if isinstance(item, exp.Star) or (isinstance(item, exp.Column) and item.is_star):
            return True
    return False


def _dedupe_joins(select: Any, only: Optional[set] = None) -> List[Dict[str, Any]]:
    """Reduce the chosen JOIN targets to one row per join key (AST based, columns preserved).

    * If the query never reads a column of the joined table other than its join key, the target
      becomes ``SELECT DISTINCT <keys>`` (deterministic, nothing arbitrary).
    * Otherwise it becomes ``SELECT * ... QUALIFY ROW_NUMBER() ... = 1`` so every column stays
      available (which duplicate is kept is then arbitrary).
    Returns one dict per rewritten join: {"alias", "keys", "arbitrary"}.
    """
    changes: List[Dict[str, Any]] = []
    star = _select_uses_star(select)
    for position, join in enumerate(list(select.args.get("joins") or [])):
        if only is not None and position not in only:
            continue
        target = join.this
        if not isinstance(target, exp.Table) or not target.name:
            continue
        side = str(join.side or "").upper()
        kind = str(join.kind or "").upper()
        if side in ("RIGHT", "FULL") or kind in ("CROSS", "SEMI", "ANTI", "POSITIONAL", "ASOF"):
            continue
        if join.method or join.args.get("using") or join.args.get("on") is None:
            continue
        alias = target.alias or target.name
        keys: List[str] = []
        pure_keys = True
        for cond in _flatten_and(join.args["on"]):
            if (isinstance(cond, exp.EQ) and isinstance(cond.this, exp.Column)
                    and isinstance(cond.expression, exp.Column)):
                a, b = cond.this, cond.expression
                a_right, b_right = a.table == alias, b.table == alias
                if a_right != b_right:
                    name = (a if a_right else b).name
                    if name and name not in keys:
                        keys.append(name)
                    continue
            pure_keys = False
            break
        if not pure_keys or not keys:
            continue

        key_set = set(keys)
        # Columns of the joined table other than its keys. An UNQUALIFIED column may belong to any
        # table, so it counts too: SELECT DISTINCT <keys> would otherwise drop a column the query uses.
        other_columns = [
            c for c in select.find_all(exp.Column)
            if (c.table == alias or not c.table) and c.name not in key_set
        ]
        needs_all_columns = bool(other_columns) or star
        bare = target.copy()
        bare.set("alias", None)
        relation = bare.sql(dialect="duckdb")
        alias_sql = exp.to_identifier(alias).sql(dialect="duckdb")
        key_sql = ", ".join(exp.to_identifier(k).sql(dialect="duckdb") for k in keys)
        if needs_all_columns:
            # No ORDER BY: ordering by the partition key itself would only pretend to be deterministic.
            text = (
                f"SELECT * FROM {relation} AS {alias_sql} "
                f"QUALIFY ROW_NUMBER() OVER (PARTITION BY {key_sql}) = 1"
            )
        else:
            text = f"SELECT DISTINCT {key_sql} FROM {relation} AS {alias_sql}"
        inner = sqlglot.parse_one(text, read="duckdb")
        join.set("this", inner.subquery(alias))
        changes.append({"alias": alias, "keys": keys, "arbitrary": needs_all_columns})
    return changes


def build_advisory_sql(
    raw_sql: Any,
    bug_name: str,
    suggested_fix: str,
    traces: Optional[List[Any]] = None,
    allow_rewrite: bool = True,
    reason: str = "",
) -> str:
    """Advisory SQL for a finding the engine could not fix automatically. Never raises.

    The SQL itself is only rewritten when ALL of these hold: ``allow_rewrite`` (the engine did not
    already judge the finding and refuse), the finding is about duplicated join keys, the script is
    exactly ONE statement (so no other statement can be lost) and a JOIN really increased the rows.
    In every other case the user's SQL comes back unchanged, under ``-- ECHO`` comment lines that
    are stripped again on the next run.
    """
    clean = strip_echo_headers(raw_sql)
    body = _strip_terminators(clean)
    if not body:
        return ""
    label = _one_line(bug_name) or "Detected Issue"
    recommendation = _one_line(suggested_fix) or "Verify join uniqueness per grain before aggregating."
    note = _one_line(reason)
    note_line = f"-- ECHO engine note: {note}\n" if note else ""
    terminator = ";" if clean.rstrip().endswith(";") else ""
    lowered = label.lower()

    if "syntax" in lowered or "parse" in lowered:
        return (
            f"-- ECHO advisory: syntax error detected ({label}); automated rewrite skipped.\n"
            f"-- ECHO advisory: fix the SQL syntax first, then re-run the analysis.\n"
            f"{note_line}{_terminate(body, terminator)}"
        )

    plain_header = (
        f"-- ECHO advisory for: {label}\n"
        f"-- ECHO recommendation: {recommendation}\n"
        f"{note_line}"
    )
    unchanged = f"{plain_header}{_terminate(body, terminator)}"
    if not allow_rewrite or not any(word in lowered for word in _REWRITABLE_LABELS):
        return unchanged
    try:
        statements = sqlglot.parse(body, read="duckdb")
    except Exception:  # noqa: BLE001
        return unchanged
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        return unchanged  # several statements (or not a SELECT): never rewrite a part of a script
    tree = statements[0]

    if _has_dedupe_join(tree):  # already rewritten: return unchanged (convergence)
        return f"-- ECHO suggested grain-safe rewrite (already applied)\n{_terminate(body, terminator)}"

    grown = inflating_join_indexes(as_list(traces)) if traces else None
    if grown is not None and not grown:
        return (
            f"-- ECHO advisory for: {label}\n"
            f"-- ECHO note: no JOIN increased the row count in the step trace, so no rewrite is suggested.\n"
            f"-- ECHO recommendation: {recommendation}\n"
            f"{note_line}{_terminate(body, terminator)}"
        )

    if tree.args.get("joins"):
        try:
            rewritten = tree.copy()
            changes = _dedupe_joins(rewritten, grown)
            if changes:
                notes = "".join(
                    f"-- ECHO note: '{c['alias']}' reduced to one row per ({', '.join(c['keys'])})"
                    + ("; which duplicate is kept is arbitrary, add an ORDER BY to choose it.\n"
                       if c["arbitrary"] else " using SELECT DISTINCT.\n")
                    for c in changes
                )
                pretty = rewritten.sql(dialect="duckdb", pretty=True).rstrip(";")
                return f"-- ECHO suggested grain-safe rewrite (JOIN fan-out)\n{notes}{pretty};"
        except Exception:  # noqa: BLE001
            pass
    return unchanged


# =====================================================================
# SLOT 8: LLM EXPLAINER
# =====================================================================
# In: analysis, fix report, verification. Out: {plain_text, summary, why_it_happened,
# fix_and_proof, source: "llm"|"fallback"}. Set EXPLAINER_MODULES to your explainer module path.
# --- START EXPLAINER CODE / ADAPTER ---
EXPLAINER_MODULES = (
    "Engine.explainer.llm_explainer",
    "Engine.explainer.explainer",
    "Engine.explainer",
    "Engine.llm_explainer",
)


def explainer_adapter(analysis: Dict[str, Any], fix_report: Dict[str, Any], ver: Dict[str, Any]) -> Dict[str, Any]:
    analysis, fix_report, ver = as_dict(analysis), as_dict(fix_report), as_dict(ver)
    for path in EXPLAINER_MODULES:
        try:
            explain_report = importlib.import_module(path).explain_report
            raw = explain_report(analysis, fix_report, ver if ver.get("provided") else None)
            if isinstance(raw, dict) and raw.get("plain_text"):
                raw["source"] = "llm" if raw.get("source") == "llm" else "fallback"
                return raw
        except Exception:  # noqa: BLE001
            continue
    bugs = [b for b in as_list(analysis.get("detected_bugs")) if isinstance(b, dict)]
    if not bugs:
        t = "ECHO found no issues in this query."
        return {
            "plain_text": t,
            "summary": t,
            "why_it_happened": "Nothing to explain.",
            "fix_and_proof": "No fix was needed.",
            "source": "fallback",
            "fallback_reason": "explainer module not found",
        }
    top = bugs[0]
    summary = (
        f"ECHO found {len(bugs)} issue(s). Most important: {strip_ansi(top.get('bug_type'))} "
        f"(severity {strip_ansi(top.get('severity'))})."
    )
    why = strip_ansi(top.get("description")) or "See the detector evidence."
    if not fix_report.get("applied"):
        proof = "No automatic fix was applied."
    elif ver.get("passed") is True:
        proof = "A fix was applied and verification passed."
    elif ver.get("passed") is False:
        proof = "A fix was applied but verification did NOT pass."
    else:
        proof = "A fix was applied but has not been verified yet."
    return {
        "plain_text": f"{summary}\n\nWhy it happened: {why}\n\nFix and proof: {proof}",
        "summary": summary,
        "why_it_happened": why,
        "fix_and_proof": proof,
        "source": "fallback",
        "fallback_reason": "explainer module not found",
    }
# --- END EXPLAINER CODE / ADAPTER ---


# =====================================================================
# SLOT 9: FASTAPI (api/main.py) CLIENT
# =====================================================================
# POST /investigate {sql}, POST /fix {sql, verify}, GET /health. Each returns (data, error).
# --- START API INTEGRATION CODE / ADAPTER ---
def _http_detail(resp: Optional[requests.Response]) -> str:
    """Best human-readable message from an error response (FastAPI puts it in 'detail')."""
    if resp is None:
        return ""
    try:
        body = resp.json()
        detail = body.get("detail") if isinstance(body, dict) else None
        if detail:
            return strip_ansi(detail if isinstance(detail, str) else str(detail))
    except ValueError:
        pass
    return strip_ansi(resp.text[:300]) if getattr(resp, "text", "") else ""


def _post(api_url: str, path: str, body: Dict[str, Any]) -> Tuple[Optional[Dict], Optional[str]]:
    try:
        resp = requests.post(f"{api_url}{path}", json=body, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        return (data, None) if isinstance(data, dict) else (None, "API response must be a JSON object.")
    except requests.HTTPError as exc:
        detail = _http_detail(exc.response)
        return None, f"{api_url}{path} returned an error: {detail or strip_ansi(exc)}"
    except requests.RequestException as exc:
        return None, f"Could not reach {api_url}{path}: {strip_ansi(exc)}"
    except ValueError as exc:
        return None, f"Invalid JSON from {path}: {strip_ansi(exc)}"


def api_investigate(api_url: str, sql: str):
    return _post(api_url, "/investigate", {"sql": sql})


def api_fix(api_url: str, sql: str, verify: bool = True):
    return _post(api_url, "/fix", {"sql": sql, "verify": verify})


@st.cache_data(ttl=5, show_spinner=False)
def _cached_health(api_url: str) -> bool:
    try:
        return requests.get(f"{api_url}/health", timeout=3).ok
    except requests.RequestException:
        return False


def api_health(api_url: str) -> bool:
    # Cached for a few seconds: an offline API must not add a 3 s timeout to every rerun.
    return _cached_health(api_url)
# --- END API INTEGRATION CODE / ADAPTER ---


CSS = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600&display=swap');
:root{--bg:#070a12;--bg2:#0c111c;--panel:rgba(18,24,38,.72);--line:#222c44;--line2:#2f3c5c;
--text:#eef2ff;--dim:#8b95ad;--brand:#9b8cff;--cy:#58e6d9;--ok:#34e0a1;--warn:#ffb547;--bad:#ff5c75;--info:#58e6d9;
--r:18px;--rs:11px;--shadow:0 14px 34px -16px rgba(0,0,0,.85)}
html,body,.stApp{font-family:Inter,system-ui,sans-serif;color:var(--text);scroll-behavior:smooth}
.stApp{background:radial-gradient(circle at 8% 0%,rgba(155,140,255,.13),transparent 34%),
radial-gradient(circle at 96% 8%,rgba(88,230,217,.09),transparent 30%),
radial-gradient(circle at 50% 100%,rgba(52,224,161,.05),transparent 40%),var(--bg)}
header[data-testid=stHeader]{background:transparent}footer{visibility:hidden}
.block-container{max-width:1420px;padding:2.6rem 3.4rem 4rem}
@media(max-width:900px){.block-container{padding:1.2rem 1rem 3rem}}
.mono{font-family:'JetBrains Mono',ui-monospace,monospace}.dim{color:var(--dim);font-size:11.5px}.cy{color:var(--cy)}
hr{border-color:var(--line);margin:1.6rem 0}
::-webkit-scrollbar{width:8px;height:8px}::-webkit-scrollbar-thumb{background:#25314d;border-radius:6px}

/* sidebar */
[data-testid=stSidebar]{background:linear-gradient(180deg,#0b0f19,#06080e);border-right:1px solid var(--line)}
[data-testid=stSidebarContent]{padding:1.6rem 1.2rem}
.brand{display:flex;align-items:center;gap:10px;font-size:28px;font-weight:900;letter-spacing:-1px}
.brand span{background:linear-gradient(135deg,#a99aff,#58e6d9);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.caption{font:500 10.5px 'JetBrains Mono',monospace;color:var(--dim);letter-spacing:1.3px;text-transform:uppercase}
.nav{display:flex;align-items:center;gap:11px;padding:11px 13px;margin-bottom:7px;border:1px solid rgba(255,255,255,.05);
border-radius:var(--rs);background:rgba(255,255,255,.02);font:500 11.5px 'JetBrains Mono',monospace;color:#cfd6e6;
transition:all .25s cubic-bezier(.16,1,.3,1)}
.nav:hover{border-color:var(--brand);transform:translateX(4px);background:rgba(155,140,255,.07)}
.nav i{width:7px;height:7px;border-radius:50%;background:var(--cy);box-shadow:0 0 10px var(--cy);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.7)}}

/* hero */
.eyebrow{display:inline-block;padding:4px 11px;border:1px solid rgba(88,230,217,.25);border-radius:99px;
background:rgba(88,230,217,.07);color:var(--cy);font:600 11px 'JetBrains Mono',monospace;letter-spacing:2.4px}
.hero{font-size:clamp(38px,5.2vw,62px);font-weight:900;line-height:1.04;letter-spacing:-2.4px;margin:16px 0 14px}
.hero b{background:linear-gradient(120deg,#b4a7ff,#58e6d9);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.lead{color:var(--dim);font-size:15.5px;line-height:1.8;max-width:68ch}
.sec{font-size:20px;font-weight:800;letter-spacing:-.4px;margin:40px 0 4px}.sub{color:var(--dim);font-size:13px;margin-bottom:16px}

/* badges + cards */
.t-ok{--c:var(--ok)}.t-info{--c:var(--cy)}.t-warn{--c:var(--warn)}.t-bad{--c:var(--bad)}.t-idle{--c:#5c6786}
.badge{display:inline-flex;align-items:center;gap:8px;padding:6px 13px;border-radius:99px;border:1px solid var(--c,var(--cy));
color:var(--c,var(--cy));background:color-mix(in srgb,var(--c,var(--cy)) 11%,transparent);font:600 11.5px 'JetBrains Mono',monospace;letter-spacing:.8px}
.badge.live::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--c,var(--cy));box-shadow:0 0 9px var(--c,var(--cy));animation:pulse 1.5s ease-in-out infinite}
.panel{position:relative;overflow:hidden;background:var(--panel);backdrop-filter:blur(14px);border:1px solid var(--line);
border-radius:var(--r);padding:22px 24px;margin:8px 0 16px;box-shadow:var(--shadow);transition:border-color .3s,box-shadow .3s}
.panel::after{content:"";position:absolute;inset:0 0 auto 0;height:2px;background:linear-gradient(90deg,transparent,var(--c,var(--brand)),transparent);opacity:.55}
.panel.glow{border-color:color-mix(in srgb,var(--c) 55%,transparent);box-shadow:0 0 38px -16px var(--c)}
.kpi{background:var(--panel);backdrop-filter:blur(14px);border:1px solid var(--line);border-left:3px solid var(--c);border-radius:var(--r);
padding:18px 20px;box-shadow:var(--shadow);transition:transform .25s,box-shadow .25s}
.kpi:hover{transform:translateY(-3px);box-shadow:0 16px 32px -16px var(--c)}
.kpi .l{font-size:12px;color:var(--dim);font-weight:600}.kpi .v{font-size:30px;font-weight:800;letter-spacing:-.6px;margin:4px 0}
.kpi .s{font:500 11.5px 'JetBrains Mono',monospace;color:var(--c)}

/* pipeline stepper */
.stages{display:grid;grid-template-columns:repeat(5,1fr);gap:0;margin:14px 0 6px}
.stage{position:relative;padding:12px 10px 0 22px;font-size:13px}
.stage::before{content:"";position:absolute;left:0;top:16px;width:11px;height:11px;border-radius:50%;background:var(--c);box-shadow:0 0 12px var(--c)}
.stage::after{content:"";position:absolute;left:14px;right:6px;top:21px;height:1px;background:var(--line2)}
.stage:last-child::after{display:none}
.stage b{display:block}.stage i{font:500 11px 'JetBrains Mono',monospace;color:var(--c);font-style:normal}
@media(max-width:800px){.stages{grid-template-columns:1fr 1fr}}

/* proof */
.cmp{display:grid;grid-template-columns:1fr auto 1fr;gap:12px;align-items:center;margin-top:10px}
.cmp .n{font-size:28px;font-weight:800;letter-spacing:-.5px}.cmp .arrow{color:var(--dim);font-size:20px}
.delta{display:inline-block;margin-top:12px;padding:3px 10px;border-radius:8px;background:rgba(88,230,217,.12);color:var(--cy);
font:600 12px 'JetBrains Mono',monospace;animation:pop .6s cubic-bezier(.16,1,.3,1) both}
@keyframes pop{from{opacity:0;transform:translateY(6px) scale(.9)}to{opacity:1;transform:none}}
.diff{background:#080b13;border:1px solid var(--line);border-radius:12px;padding:12px 0;overflow-x:auto;font:12.5px/1.65 'JetBrains Mono',monospace;white-space:pre}
.diff div{padding:0 16px}.diff .a{background:rgba(52,224,161,.12);color:#9bf3cf}.diff .d{background:rgba(255,92,117,.12);color:#ffb0bd}.diff .h{color:var(--dim)}

/* findings + explainer */
.bug{border:1px solid color-mix(in srgb,var(--c) 45%,transparent);border-left:3px solid var(--c);border-radius:13px;padding:15px 18px;margin:9px 0;
background:color-mix(in srgb,var(--c) 6%,transparent);transition:transform .2s}
.bug:hover{transform:translateX(3px)}.bug b{font-size:15px}.bug p{color:#cdd3e4;font-size:13px;line-height:1.7;margin:7px 0 0}
.explain h4{margin:16px 0 4px;font-size:12px;color:var(--dim);font-weight:600;letter-spacing:.3px}
.explain p{font-size:15px;line-height:1.8;margin:0;max-width:78ch}

/* step cards */
.trace-card{background:linear-gradient(145deg,#141b2b,#0f1420);border:1px solid #28334d;border-radius:14px;padding:18px;min-height:170px;margin-bottom:14px;
transition:transform .25s,border-color .25s,box-shadow .25s}
.trace-card:hover{transform:translateY(-4px);border-color:var(--brand);box-shadow:0 16px 30px -14px rgba(155,140,255,.4)}
.trace-card.ok{border-color:#215240}.trace-card.failed{border-color:var(--bad);background:linear-gradient(145deg,#361520,#161017)}
.tc-top{display:flex;justify-content:space-between;font:600 11px 'JetBrains Mono',monospace;color:var(--dim);margin-bottom:8px}
.tc-title{font-size:15px;font-weight:700;margin-bottom:12px;word-break:break-all}.tc-rows{font-size:26px;font-weight:800}
.tc-err{color:var(--bad);font-size:11.5px;margin-top:9px}

/* inputs, buttons, metrics */
div[data-testid=stTextArea] textarea{background:#080b13!important;color:#d4e0ff!important;border:1px solid #28334d!important;border-radius:14px!important;
padding:16px!important;font-family:'JetBrains Mono',monospace!important;font-size:13.5px!important;line-height:1.75!important;transition:all .3s!important}
div[data-testid=stTextArea] textarea:focus{border-color:var(--brand)!important;box-shadow:0 0 0 4px rgba(155,140,255,.18)!important}
div[data-testid=stTextInput] input{background:#080b13!important;color:#d4e0ff!important;border:1px solid #28334d!important;border-radius:var(--rs)!important;font-family:'JetBrains Mono',monospace!important;font-size:13px!important}
.stButton>button{border-radius:var(--rs)!important;min-height:50px!important;font-weight:700!important;transition:all .3s cubic-bezier(.16,1,.3,1)!important}
.stButton>button[kind=primary]{background:linear-gradient(135deg,#8c7ae6,#3fb8c4)!important;border:1px solid #a29bfe!important;color:#fff!important;box-shadow:0 10px 25px -8px rgba(140,122,230,.8)!important}
.stButton>button[kind=primary]:hover{transform:translateY(-2px)!important;filter:brightness(1.12)!important}
.stButton>button[kind=secondary]{background:rgba(255,255,255,.03)!important;border:1px solid #28334d!important;color:#d0d7de!important}
.stTabs [data-baseweb=tab]{font-weight:600}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)


def section(title: str, sub: str = "") -> None:
    st.markdown(f"<div class='sec'>{esc(title)}</div><div class='sub'>{esc(sub)}</div>", unsafe_allow_html=True)


def kpi(label: str, value: str, sub: str, tone: str) -> str:
    return (
        f"<div class='kpi t-{tone}'><div class='l'>{esc(label)}</div>"
        f"<div class='v'>{esc(value)}</div><div class='s'>{esc(sub)}</div></div>"
    )


def stage(name: str, sub: str, tone: str) -> str:
    return f"<div class='stage t-{tone}'><b>{esc(name)}</b><i>{esc(sub)}</i></div>"


def sql_diff_html(before: str, after: str) -> str:
    lines = list(
        difflib.unified_diff(before.splitlines(), after.splitlines(), "original", "fixed", lineterm="", n=2)
    )
    if not lines:
        return "<div class='diff'><div class='h'>No differences: the query was left untouched.</div></div>"
    out = []
    for ln in lines:
        cls = (
            "a"
            if ln.startswith("+") and not ln.startswith("+++")
            else "d"
            if ln.startswith("-") and not ln.startswith("---")
            else "h"
        )
        out.append(f"<div class='{cls}'>{html.escape(ln) or ' '}</div>")
    return "<div class='diff'>" + "".join(out) + "</div>"


# =====================================================================
# SIDEBAR
# =====================================================================
with st.sidebar:
    st.markdown(
        "<div class='brand'><span>◈</span> ECHO</div>"
        "<div class='caption'>Enterprise SQL Intelligence Engine</div>",
        unsafe_allow_html=True,
    )
    st.divider()
    st.markdown("#### 🔗 API connection")
    api_url = st.text_input("API URL endpoint", "http://127.0.0.1:8000").strip().rstrip("/")
    in_process = st.toggle(
        "In-process engine (no HTTP)",
        value=False,
        help="Imports api.main directly instead of calling the API.",
    )
    online = in_process or api_health(api_url)
    st.markdown(
        f"<span class='badge live t-{'ok' if online else 'bad'}'>{'ENGINE ONLINE' if online else 'ENGINE OFFLINE'}</span>",
        unsafe_allow_html=True,
    )
    st.markdown("#### ⚡ System modules")
    st.markdown(
        "".join(
            f"<div class='nav'><i></i>{m}</div>"
            for m in (
                "AST PARSER & SECURITY GUARD",
                "DUCKDB STEP TRACE RUNNER",
                "FAN-OUT & ANOMALY DETECTORS",
                "FIXGENERATOR + VERIFY()",
                "PLAIN-WORDS EXPLAINER",
            )
        ),
        unsafe_allow_html=True,
    )
    with st.expander("Parsed statements"):
        steps = parser_adapter(st.session_state.get("sql_input", DEFAULT_SQL)) or []
        st.write(f"{len(steps)} step(s)" if steps else "Parser slot empty or unavailable.")
    st.divider()
    st.caption("Advanced SQL diagnostics & observability for enterprise data pipelines.")
    st.caption("DEPI Capstone Project • 2026 Edition")

# =====================================================================
# HERO + WORKSPACE
# =====================================================================
st.markdown(
    "<span class='eyebrow'>ENTERPRISE OBSERVABILITY & DIAGNOSTICS</span>"
    "<div class='hero'>Trace the bug.<br><b>Prove the fix.</b></div>"
    "<div class='lead'>Code finds the anomaly, rewrites the query and proves the result on fresh sandboxes. "
    "The explanation only puts those facts into plain words.</div>",
    unsafe_allow_html=True,
)
st.divider()

section(
    "SQL workspace",
    "Paste a query. ECHO traces every step, detects anomalies, fixes what is safe and verifies the fix.",
)
ed, side = st.columns([3, 1.1], gap="large")
with ed:
    sql = st.text_area("SQL", DEFAULT_SQL, height=270, key="sql_input", label_visibility="collapsed")
with side:
    st.markdown(
        """<div class='panel t-info'><div class='caption'>Security & compliance</div>
    <div style='font-weight:700;margin:8px 0'>Read-only guard</div>
    <div class='dim' style='line-height:1.9;font-size:12.5px'>• Strict AST protection<br>• Blocks DROP / DELETE / UPDATE<br>
• Isolated in-memory DuckDB<br>• Fresh sandbox per request</div></div>""",
        unsafe_allow_html=True,
    )
b1, b2, _ = st.columns([1.7, 1, 4], gap="medium")
run = b1.button("🚀 Investigate, fix and prove", type="primary", use_container_width=True)
if b2.button("Clear results", use_container_width=True):
    for k in ("analysis", "fix_result", "verification", "explanation", "api_error"):
        st.session_state.pop(k, None)
    st.rerun()

# =====================================================================
# PIPELINE (code decides, explainer explains)
# =====================================================================
if run:
    if not sql.strip():
        st.warning("Enter a SQL query before running.")
    else:
        with st.spinner("Tracing steps, rewriting and verifying…"):
            err: Optional[str] = None
            fix_err: Optional[str] = None
            analysis_raw: Any = None
            fix_raw: Any = None
            if in_process:
                analysis_raw = runner_adapter(sql)
                if analysis_raw is None:
                    err = "In-process engine failed. Run Streamlit from the project root so api.main is importable."
                else:
                    try:
                        from api.main import FixRequest, fix as fix_endpoint

                        fix_raw = to_plain(fix_endpoint(FixRequest(sql=sql, verify=True)))
                    except Exception as exc:  # noqa: BLE001
                        fix_err = f"In-process fix failed: {strip_ansi(exc)}"
            else:
                analysis_raw, err = api_investigate(api_url, sql)
                if not err:
                    fix_raw, fix_err = api_fix(api_url, sql, True)
        if err:
            st.session_state["api_error"] = err
            for k in ("analysis", "fix_result", "verification", "explanation"):
                st.session_state.pop(k, None)
        else:
            analysis_plain = as_dict(to_plain(analysis_raw))
            fix_norm = normalize_fix(fix_raw, sql)
            try:
                explanation = explainer_adapter(
                    fix_norm["analysis"] or analysis_plain, fix_norm, fix_norm["verification"]
                )
            except Exception:  # noqa: BLE001
                explanation = {}
            # A failed /fix call must not throw away a successful /investigate result.
            st.session_state.update(
                analysis=analysis_plain,
                fix_result=fix_norm,
                verification=fix_norm["verification"],
                api_error=fix_err,
                explanation=explanation,
            )

analysis = as_dict(st.session_state.get("analysis"))
fix = as_dict(st.session_state.get("fix_result"))
ver = as_dict(st.session_state.get("verification"))
expl = as_dict(st.session_state.get("explanation"))

if st.session_state.get("api_error"):
    (st.warning if analysis else st.error)(st.session_state["api_error"])
for _note in as_list(fix.get("input_notes")):
    st.caption(strip_ansi(_note))

if not analysis:
    st.markdown(
        """<div class='panel' style='text-align:center;padding:60px 24px;margin-top:28px'>
    <div style='font-size:46px;color:var(--brand)'>◈</div>
    <div style='font-size:23px;font-weight:800;margin:10px 0'>Ready for investigation</div>
    <div class='dim' style='font-size:14px;line-height:1.8;max-width:560px;margin:0 auto'>
    Run a query to see the pipeline status, the fixed SQL with its proof and a plain-words explanation.</div></div>""",
        unsafe_allow_html=True,
    )
else:
    bugs = [b for b in as_list(analysis.get("detected_bugs")) if isinstance(b, dict)]
    traces = [t for t in as_list(analysis.get("step_trace")) if isinstance(t, dict)]
    failed = sum(1 for t in traces if t.get("status") == "failed")
    syntax = any(t.get("step_type") == "SYNTAX_ERROR" for t in traces)
    passed = ver.get("passed")

    # ---------------- command bar -----------------------------------------
    section("Pipeline overview", "Every value here is produced by the engine, never by the language model.")
    if failed:
        health = ("Failed", f"{failed} failed step(s)", "bad")
    elif any(not (str(b.get("severity")).lower() == "low" and str(b.get("confidence")).lower() == "low")
             for b in bugs):
        health = ("Warning", f"{len(bugs)} anomaly(ies)", "warn")
    elif bugs:
        health = ("Optimal", f"{len(bugs)} informational note(s)", "ok")
    else:
        health = ("Optimal", "no anomalies", "ok")
    verdict = (
        ("PASSED", "verify() passed", "ok")
        if passed is True
        else ("FAILED", "verify() failed", "bad")
        if passed is False
        else ("PENDING", "no fix to verify" if not fix.get("applied") else "not verified yet", "info")
    )
    k1, k2, k3, k4 = st.columns(4, gap="medium")
    k1.markdown(kpi("Total steps", str(len(traces)), f"{len(traces) - failed} succeeded", "info"), unsafe_allow_html=True)
    k2.markdown(
        kpi(
            "Detected anomalies",
            str(len(bugs)),
            "clean query" if not bugs else "action required",
            "ok" if not bugs else "warn",
        ),
        unsafe_allow_html=True,
    )
    k3.markdown(kpi("Pipeline health", health[0], health[1], health[2]), unsafe_allow_html=True)
    k4.markdown(kpi("Verification", verdict[0], verdict[1], verdict[2]), unsafe_allow_html=True)

    fixed_ok = bool(fix.get("applied"))
    st.markdown(
        "<div class='panel'><div class='stages'>"
        + "".join(
            (
                stage("Parse", "syntax error" if syntax else "ok", "bad" if syntax else "ok"),
                stage("Run", "failed" if failed else "ok", "bad" if failed else "ok"),
                stage("Detect", f"{len(bugs)} finding(s)" if bugs else "clean", "warn" if bugs else "ok"),
                stage("Fix", "applied" if fixed_ok else (fix.get("status") if fix.get("status") in ("suggested", "manual") else "none"),
                      "ok" if fixed_ok else ("warn" if fix.get("status") in ("suggested", "manual") else "idle")),
                stage("Verify", verdict[0].lower(), verdict[2] if passed is not None else "idle"),
            )
        )
        + "</div></div>",
        unsafe_allow_html=True,
    )

    # ---------------- fix and proof ---------------------------------------
    section("Fix and proof", "Rewrites SQL when safe and verified, or provides a structured fix and telemetry proof.")
    original_sql = fix.get("original_sql") or sql
    fixed_sql = fix.get("fixed_sql") or sql

    # First / last step that actually produced numbers (a failed step has no row count).
    measured = [t for t in traces if t.get("status") != "failed" and num(t.get("row_count")) is not None]
    first_step = as_dict(measured[0]) if measured else {}
    last_step = as_dict(measured[-1]) if measured else {}
    r_start, r_end = num(first_step.get("row_count")), num(last_step.get("row_count"))
    s_start, s_end = num(first_step.get("sum_value")), num(last_step.get("sum_value"))

    top_bug = as_dict(bugs[0]) if bugs else {}
    bug_name = str(top_bug.get("bug_type") or "")
    suggested_fix = str(top_bug.get("suggested_fix") or "").strip()

    # Why the engine did not apply a fix (its own refusal reason, e.g. "key already unique").
    engine_reason = next(iter(as_list(fix.get("warnings"))), None) or next(
        (w for f in as_list(fix.get("fixes")) for w in as_list(as_dict(f).get("warnings"))),
        None,
    )
    # The engine is the single authority on rewrites. When it evaluated the findings and declined,
    # the dashboard must not override it with a rewrite of its own: it shows the SQL unchanged.
    engine_judged = bool(as_list(fix.get("fixes")))
    # "No automatic rewrite exists ..." says nothing useful: show the real cause (the database's
    # own message, e.g. "Table ... does not exist! Did you mean ...") instead.
    bug_detail = strip_ansi(top_bug.get("description"))
    if bug_detail and (not engine_reason or str(engine_reason).startswith("No automatic rewrite exists")):
        engine_reason = bug_detail
    advisory_sql = ""
    proposals = [p for p in as_list(fix.get("proposals")) if isinstance(p, dict)]
    fix_status = str(fix.get("status") or "none")
    proposal_notes = [n for p in proposals for n in as_list(p.get("notes"))]
    if not fixed_ok and fix.get("proposed_sql"):
        advisory_sql = build_proposal_sql(fix["proposed_sql"], fix_status, [strip_ansi(n) for n in proposal_notes])
    elif not fixed_ok and bugs:
        try:
            advisory_sql = build_advisory_sql(
                original_sql, bug_name, suggested_fix, traces,
                allow_rewrite=not engine_judged, reason=strip_ansi(engine_reason or ""),
            )
        except Exception:  # noqa: BLE001 - advisory is best-effort, never crash the page
            advisory_sql = strip_echo_headers(original_sql)

    if not fixed_ok:
        if not bugs:
            st.markdown(
                "<div class='panel t-ok'><span class='badge'>CLEAN • NO FIX NEEDED</span>"
                "<p class='dim' style='margin:12px 0 0;font-size:13px'>"
                "No anomalies were detected in this query. All steps executed cleanly.</p></div>",
                unsafe_allow_html=True,
            )
        else:
            reason = engine_reason
            rec_text = (
                suggested_fix
                or "Ensure each joined table is unique at the join/aggregation grain before summing measures."
            )
            badge_text, badge_class = FIX_STATUS_LABELS.get(fix_status, ("FIX &amp; TELEMETRY PROOF", "t-warn"))
            st.markdown(
                f"<div class='panel {badge_class}'><span class='badge'>{badge_text}</span>"
                f"<div style='font-weight:700;margin:12px 0 6px'>Issue: {esc(bug_name, 'Anomaly')}</div>"
                f"<p class='dim' style='margin:0;font-size:13px;line-height:1.7'>{esc(rec_text)}</p>"
                f"<div class='dim' style='margin-top:10px;font-size:12px'>Engine note: {esc(reason, 'Advisory fix provided.')}</div></div>",
                unsafe_allow_html=True,
            )

            tab_names = ["Recommended SQL Fix", "Telemetry Proof"] + (["Review notes & diagnostics"] if proposals else [])
            tabs = st.tabs(tab_names)
            t_adv, t_cmp = tabs[0], tabs[1]
            if proposals:
                with tabs[2]:
                    for proposal in proposals:
                        label = FIX_STATUS_LABELS.get(str(proposal.get("status")), ("PROPOSAL", ""))[0]
                        st.markdown(f"**{esc(proposal.get('bug_type'), 'Finding')}** — {label}")
                        for note in as_list(proposal.get("notes")):
                            st.markdown("- " + esc(strip_ansi(note)))
                        if proposal.get("diagnostic_sql"):
                            st.caption("Diagnostic query (read-only; run it to see the problem rows):")
                            st.code(strip_ansi(proposal["diagnostic_sql"]), language="sql")
                        for alt in as_list(proposal.get("alternatives")):
                            with st.expander("Alternative statement"):
                                st.code(strip_ansi(alt), language="sql")
                    preview = as_dict(fix.get("proposal_verification"))
                    if preview:
                        st.caption(
                            "Preview check on a sandbox copy — this is NOT a verified fix: "
                            f"rows {fmt(num(preview.get('rows_before')))} → {fmt(num(preview.get('rows_after')))}, "
                            f"checks {'passed' if preview.get('passed') else 'did not all pass'}."
                        )
            with t_adv:
                st.code(advisory_sql or "-- No advisory SQL available for this query.", language="sql")
            with t_cmp:
                p1, p2, p3 = st.columns([1.2, 1.2, 1], gap="medium")
                p1.markdown(
                    f"<div class='panel t-info'><div class='caption'>Trace Rows</div><div class='cmp'>"
                    f"<div><div class='dim'>{esc(first_step.get('step_id'), 'start')}</div><div class='n'>{fmt(r_start)}</div></div>"
                    f"<div class='arrow'>→</div>"
                    f"<div><div class='dim'>{esc(last_step.get('step_id'), 'end')}</div><div class='n'>{fmt(r_end)}</div></div></div>"
                    f"{delta_badge(r_start, r_end, ' rows')}</div>",
                    unsafe_allow_html=True,
                )
                p2.markdown(
                    f"<div class='panel t-info'><div class='caption'>Trace Sum</div><div class='cmp'>"
                    f"<div><div class='dim'>{esc(first_step.get('step_id'), 'start')}</div><div class='n'>{fmt(s_start, NO_TOTAL)}</div></div>"
                    f"<div class='arrow'>→</div>"
                    f"<div><div class='dim'>{esc(last_step.get('step_id'), 'end')}</div><div class='n'>{fmt(s_end, NO_TOTAL)}</div></div></div>"
                    f"{delta_badge(s_start, s_end)}</div>",
                    unsafe_allow_html=True,
                )
                trace_tone, trace_label = ("bad", "TRACE HAS FAILURES") if failed else ("ok", "TRACE COMPLETE")
                p3.markdown(
                    f"<div class='panel t-{trace_tone}'><div class='caption'>Proof Status</div>"
                    f"<div style='margin:12px 0'><span class='badge live' style='font-size:14px'>{trace_label}</span></div>"
                    f"<div class='dim'>Steps executed: {len(traces)}</div>"
                    f"<div class='dim'>First suspicious: {esc(suspicious_step_id(analysis), 'none')}</div></div>",
                    unsafe_allow_html=True,
                )
    else:
        t_fixed, t_diff = st.tabs(["Fixed SQL", "Original vs fixed"])
        with t_fixed:
            st.code(fixed_sql, language="sql")
        with t_diff:
            st.markdown(sql_diff_html(original_sql, fixed_sql), unsafe_allow_html=True)
        for f in as_list(fix.get("fixes")):
            f = as_dict(f)
            if f.get("applied") and f.get("explanation"):
                st.caption(f"Engine note ({strip_ansi(f.get('strategy_used'))}): {strip_ansi(f.get('explanation'))}")
            validation = as_dict(f.get("validation"))
            if f.get("applied") and validation.get("needs_review"):
                st.warning(
                    "Best-guess repair: it parses and plans on the database, but only you know what the query "
                    "was meant to do. Review it before using it."
                )
                for alt in as_list(validation.get("alternatives")):
                    with st.expander("Other possible repair"):
                        st.code(strip_ansi(alt), language="sql")

        if not ver.get("provided"):
            st.info("verify() did not return metrics for this fix.")
        else:
            tone = "ok" if passed is True else "bad" if passed is False else "warn"
            glow = "glow" if passed is not None else ""
            c1, c2, c3 = st.columns([1.2, 1.2, 1], gap="medium")
            c1.markdown(
                f"<div class='panel t-{tone} {glow}'><div class='caption'>Rows</div><div class='cmp'>"
                f"<div><div class='dim'>before</div><div class='n'>{fmt(ver.get('rows_before'))}</div></div><div class='arrow'>→</div>"
                f"<div><div class='dim'>after</div><div class='n'>{fmt(ver.get('rows_after'))}</div></div></div>"
                f"{delta_badge(ver.get('rows_before'), ver.get('rows_after'), ' rows')}</div>",
                unsafe_allow_html=True,
            )
            no_total = num(ver.get("total_before")) is None and num(ver.get("total_after")) is None
            size = 17 if no_total else 28
            c2.markdown(
                f"<div class='panel t-{tone} {glow}'><div class='caption'>Total</div><div class='cmp'>"
                f"<div><div class='dim'>before</div><div class='n' style='font-size:{size}px'>{fmt(ver.get('total_before'), NO_TOTAL)}</div></div>"
                f"<div class='arrow'>→</div>"
                f"<div><div class='dim'>after</div><div class='n' style='font-size:{size}px'>{fmt(ver.get('total_after'), NO_TOTAL)}</div></div></div>"
                f"{delta_badge(ver.get('total_before'), ver.get('total_after'))}</div>",
                unsafe_allow_html=True,
            )
            badge = "PASS" if passed is True else "FAIL" if passed is False else "UNVERIFIED"
            still = [esc(s) for s in as_list(ver.get("failed_steps_after"))]
            err_html = f"<div class='tc-err'>{esc(ver.get('error'))}</div>" if ver.get("error") else ""
            if passed is False and as_list(ver.get("reasons")):
                err_html += "".join(
                    f"<div class='tc-err'>Not verified: {esc(r)}</div>" for r in as_list(ver.get("reasons"))
                )
            c3.markdown(
                f"<div class='panel t-{tone} {glow}'><div class='caption'>Verification</div>"
                f"<div style='margin:12px 0'><span class='badge live' style='font-size:15px'>{badge}</span></div>"
                f"<div class='dim'>Fan-out findings: {fmt(ver.get('fan_out_findings_before'))} → {fmt(ver.get('fan_out_findings_after'))}</div>"
                f"<div class='dim'>Failed steps after fix: {', '.join(still) if still else 'none'}</div>{err_html}</div>",
                unsafe_allow_html=True,
            )

    # ---------------- explainer -------------------------------------------
    section("Plain-words explanation", "Written from the engine's facts. It cannot change a finding, a number or the verdict.")
    display_expl = dict(expl)
    if bugs and not fixed_ok:
        rec_summary = suggested_fix or "Deduplicate join keys on the right/many side before aggregating."
        enriched_fix_proof = (
            f"Recommended fix: {rec_summary} "
            f"Telemetry proof: rows {fmt(r_start)} → {fmt(r_end)}, sum {fmt(s_start, NO_TOTAL)} → {fmt(s_end, NO_TOTAL)}."
        )
        display_expl["fix_and_proof"] = enriched_fix_proof
        if display_expl.get("plain_text"):
            display_expl["plain_text"] = str(display_expl["plain_text"]).replace(
                "No automatic fix was applied.",
                enriched_fix_proof,
            )

    if display_expl:
        is_llm = display_expl.get("source") == "llm"
        tone, label = ("info", "LLM") if is_llm else ("warn", "Deterministic fallback")
        parts = "".join(
            f"<h4>{title}</h4><p>{esc(display_expl.get(key))}</p>"
            for title, key in (
                ("What happened", "summary"),
                ("Why it happened", "why_it_happened"),
                ("Fix and proof", "fix_and_proof"),
            )
            if display_expl.get(key)
        ) or f"<p>{esc(display_expl.get('plain_text'), 'No explanation available.')}</p>"
        st.markdown(
            f"<div class='panel explain t-{tone}'><span class='badge'>Source: {label}</span>{parts}"
            f"<div class='dim' style='margin-top:18px'>Code made the decision. The explanation only restates it.</div></div>",
            unsafe_allow_html=True,
        )
        if display_expl.get("fallback_reason") and not is_llm:
            st.caption(f"Fallback reason: {strip_ansi(display_expl.get('fallback_reason'))}")
        with st.expander("Full explanation text"):
            st.text(strip_ansi(display_expl.get("plain_text")))
    else:
        st.info("No explanation available yet.")

    # ---------------- detectors -------------------------------------------
    section("Detector breakdown", "Findings from the fan-out detector, the general detector and the base runner.")
    groups = group_bugs(bugs)
    for tab, (name, items) in zip(st.tabs([f"{n} ({len(i)})" for n, i in groups.items()]), groups.items()):
        with tab:
            if not items:
                st.markdown(f"<div class='dim'>No {name.lower()} findings.</div>", unsafe_allow_html=True)
            for b in items:
                sev = esc(b.get("severity"), "Medium")
                tone = "bad" if sev in ("Critical", "High") else "warn"
                hint = f"<p><b>Suggested fix:</b> {esc(b.get('suggested_fix'))}</p>" if b.get("suggested_fix") else ""
                st.markdown(
                    f"<div class='bug t-{tone}'><b>{esc(b.get('bug_type'), 'Finding')}</b> "
                    f"<span class='badge' style='margin-left:8px'>{sev}</span><p>{esc(b.get('description'))}</p>{hint}"
                    f"<div class='dim mono' style='margin-top:8px'>step: {esc(b.get('suspicious_step_id'), 'n/a')}</div></div>",
                    unsafe_allow_html=True,
                )
                if b.get("evidence"):
                    with st.expander("Evidence"):
                        st.json(b["evidence"])

    # ---------------- step trace (slot 1) ---------------------------------
    section("Step trace", "Row volume and duration across each phase of the query.")
    try:
        render_existing_dashboard(analysis)
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Existing dashboard slot failed: {strip_ansi(exc)}")

    with st.expander("Inspect raw engine payloads"):
        st.json({"analysis": analysis, "fix_result": fix, "explanation": expl}, expanded=False)

st.markdown(
    "<div style='border-top:1px solid var(--line);padding-top:22px;margin-top:56px;color:#5a6582;font:11.5px "
    '"JetBrains Mono",monospace;letter-spacing:1.2px\'>ECHO · ENTERPRISE SQL INTELLIGENCE & OBSERVABILITY PLATFORM'
    "<span style='float:right'>TRACE THE BUG. PROVE THE FIX.</span></div>",
    unsafe_allow_html=True,
)