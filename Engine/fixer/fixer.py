"""ECHO SQL Fix Generator.

Turns a detector finding into a rewritten, validated SQL script - or explains why it will not.

Integration (see ``tests/test_fixer.py``)::

    runner   = EchoEngineRunner(conn)
    steps    = parse_sql_to_steps(sql)
    analysis = runner.analyze_query(sql, steps)            # detected_bugs carry the evidence
    report   = FixGenerator().fix_all(sql, analysis, conn, parsed_steps=steps)
    if report.applied:
        print(report.fixed_sql)                             # pretty, validated SQL
    proof = FixGenerator().verify(sql, report.fixed_sql, connection_factory)   # optional

Supported strategies
--------------------
* ``dedupe_right_side_row_number``  (bug types: "... JOIN Fan-out", "Inflated Totals")
      ``JOIN t a ON ...``  ->  ``JOIN (SELECT * FROM t AS a [WHERE <a-only predicates>]
                                       QUALIFY ROW_NUMBER() OVER (PARTITION BY <keys> ORDER BY ...) = 1) AS a ON ...``
  The alias is preserved, no column is added or dropped (so ``SELECT *`` and ``a.col``
  references keep working) and only the one JOIN is touched.
* ``left_join_filter_to_on``        (bug type: "LEFT JOIN Converted To INNER JOIN")
      ``LEFT JOIN o ON k WHERE o.status = 'x'``  ->  ``LEFT JOIN o ON k AND o.status = 'x'``

Output rules (why a fix can no longer corrupt your script): ONLY the statement that holds the
rewritten JOIN is regenerated; every other byte of the script (other statements, separators,
comments between statements, a statement's leading comments) is copied verbatim; the fixed SQL
never contains ECHO comment lines; a rewrite that would drop any identifier / literal of the
statement is refused.

Safety rules: a clean query is never altered; every rewrite is re-parsed (and, with a DB
connection, planned with EXPLAIN) before it is returned; any doubt means ``applied=False``
with the original SQL and a human-readable reason. Re-running the fixer on its own output
converges: an already-deduplicated JOIN, or a condition that already lives in ON, is skipped.
"""

import logging
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import sqlglot
from sqlglot import expressions as exp

from Engine.fixer import repair
from Engine.parser import parser as _parser
from Engine.parser.parser import (
    FALLBACK_DIALECTS,
    normalize_sql_input,
    parse_sql_to_steps,
    split_statement_spans,
    split_statements,
)

log = logging.getLogger("echo.fixer")

DEFAULT_DIALECT = "duckdb"
# SQL that is built as text and executed on the (DuckDB) sandbox is always DuckDB SQL.
_INTERNAL_DIALECT = "duckdb"
_ANSI_ESCAPES = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_SET_OPERATIONS = tuple(getattr(exp, n) for n in ("Union", "Intersect", "Except") if hasattr(exp, n))
_NULL_REJECTING = tuple(
    getattr(exp, n) for n in ("EQ", "NEQ", "GT", "GTE", "LT", "LTE", "Like", "ILike", "In", "Between")
    if hasattr(exp, n)
)

# Columns that say "this is the row to keep" (descending) or "the newest row" (descending).
_PREFERENCE_COLUMNS = ("is_primary", "primary", "is_default", "is_current", "is_latest", "is_active", "active")
_RECENCY_COLUMNS = (
    "updated_at", "modified_at", "last_modified", "updated", "created_at", "created", "inserted_at",
    "loaded_at", "load_ts", "effective_date", "valid_from", "event_time", "timestamp", "ts",
)

_NUMERIC_TYPES = {
    "TINYINT", "SMALLINT", "INTEGER", "INT", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT",
    "UINTEGER", "UBIGINT", "UHUGEINT", "INT1", "INT2", "INT4", "INT8", "SHORT", "LONG",
    "FLOAT", "FLOAT4", "FLOAT8", "DOUBLE", "REAL", "DECIMAL", "NUMERIC",
}


def _quote_ident(name: Any) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _leading_trivia_len(text: str) -> int:
    """Length of the whitespace / comments a statement starts with (kept verbatim by every rewrite)."""
    i, n = 0, len(text)
    while i < n:
        if text[i].isspace():
            i += 1
        elif text.startswith("--", i):
            end = text.find("\n", i)
            i = n if end == -1 else end + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                break  # unterminated comment: let the parser report it
            i = end + 2
        else:
            break
    return i


def _as_mapping(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        try:
            data = to_dict()
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


# --------------------------------------------------------------------------- #
# Public data structures
# --------------------------------------------------------------------------- #
@dataclass
class FixOptions:
    """Knobs for the generator. Defaults are the safe ones."""

    dialect: str = DEFAULT_DIALECT
    pretty: bool = True
    validate_on_db: bool = True          # EXPLAIN the rewritten statement when a connection is given
    require_deterministic: bool = False  # refuse dedupe fixes whose ORDER BY cannot be made deterministic
    # Optional explicit ordering per joined table / alias (lower-case key), e.g.
    # {"customer_addresses": ["is_primary DESC", "updated_at DESC"]}
    order_by: Dict[str, Sequence[str]] = field(default_factory=dict)


@dataclass
class FixResult:
    """Outcome for ONE finding."""

    applied: bool
    fixed_sql: str
    original_sql: str
    strategy_used: Optional[str] = None
    bug_type: Optional[str] = None
    target_step: Optional[str] = None
    target_table: Optional[str] = None
    explanation: str = ""
    warnings: List[str] = field(default_factory=list)
    deterministic: Optional[bool] = None
    validation: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class FixReport:
    """Outcome for a whole script (all findings)."""

    applied: bool
    fixed_sql: str
    original_sql: str
    fixes: List[FixResult] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "applied": self.applied,
            "fixed_sql": self.fixed_sql,
            "original_sql": self.original_sql,
            "fixes": [f.to_dict() for f in self.fixes],
            "warnings": list(self.warnings),
        }


class FixSkipped(Exception):
    """Raised inside a strategy when a safe rewrite is not possible (message = reason)."""


@dataclass
class _Context:
    node: Any                      # parsed statement (mutated in place by the strategy)
    select: Any                    # the SELECT that owns the target JOIN
    join: Any                      # the JOIN node
    step: Dict[str, Any]           # parser step (join_details, scope, with_prefix ...)
    bug: Dict[str, Any]
    conn: Any
    chunk_index: int


# --------------------------------------------------------------------------- #
# Generator
# --------------------------------------------------------------------------- #
class FixGenerator:
    """Rewrites SQL for the findings ECHO's detectors raise. Never raises on bad input."""

    def __init__(self, options: Optional[FixOptions] = None):
        self.options = options or FixOptions()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def fix_all(
        self,
        sql: str,
        analysis: Optional[Dict[str, Any]] = None,
        db_connection: Any = None,
        parsed_steps: Optional[List[Dict[str, Any]]] = None,
        bugs: Optional[List[Dict[str, Any]]] = None,
    ) -> FixReport:
        """Apply every actionable finding of ``analysis["detected_bugs"]`` (or ``bugs``), in order.

        Findings a detector attached as ``evidence["related_findings"]`` (further inflating JOINs
        of the same query) are processed too, so one run converges instead of fixing one JOIN per run.
        """
        raw_sql = sql if isinstance(sql, str) else ""
        original, notes = normalize_sql_input(raw_sql)  # LF endings, no ECHO advisory comments
        report = FixReport(applied=False, fixed_sql=original, original_sql=original)
        report.warnings.extend(notes)
        try:
            analysis_dict = analysis if isinstance(analysis, dict) else {}
            raw_findings = bugs if bugs is not None else analysis_dict.get("detected_bugs")
            findings = [b for b in (raw_findings if isinstance(raw_findings, list) else [])
                        if isinstance(b, dict)]
            if not original.strip():
                report.warnings.append("Empty SQL: nothing to fix.")
                return self._clean_report(report)
            if not findings:
                report.warnings.append("No findings: the query is left untouched.")
                return self._clean_report(report)

            spans = split_statement_spans(original)
            if not spans:
                report.warnings.append("No SQL statement found: nothing to fix.")
                return self._clean_report(report)
            texts = [original[start:end] for start, end in spans]
            steps = parsed_steps if isinstance(parsed_steps, list) else parse_sql_to_steps(original)
            steps = [s for s in steps if isinstance(s, dict)]
            step_map = {s.get("step_id"): s for s in steps}
            trace = [t for t in (analysis_dict.get("step_trace") or []) if isinstance(t, dict)]
            seen: set = set()

            for bug in self._expand_findings(findings):
                result = self._fix_one(original, texts, bug, step_map, db_connection, seen, steps, trace)
                report.fixes.append(result)
                if result.applied:
                    report.applied = True

            if report.applied:
                report.fixed_sql = self._splice(original, spans, texts)
        except Exception as exc:  # noqa: BLE001 - the fixer must never crash its caller
            log.exception("Fix generation failed")
            report.applied = False
            report.fixed_sql = original
            report.warnings.append(f"Fix generation aborted safely: {type(exc).__name__}: {exc}")
        return self._clean_report(report)

    @staticmethod
    def _expand_findings(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Findings + the extra JOINs a fan-out finding lists in ``evidence["related_findings"]``.

        FanOutDetector returns ONE report (the worst JOIN) and attaches the others as evidence.
        A related entry with severity "Low" is one the detector proved intentional (aggregates on
        the "many" side), so it is never rewritten. The fixer re-checks key duplication on the
        database for every JOIN it touches.
        """
        out: List[Dict[str, Any]] = []
        known = set()
        for bug in findings:
            out.append(bug)
            known.add(str((bug.get("evidence") or {}).get("join_step") or bug.get("suspicious_step_id")))
        for bug in findings:
            evidence = bug.get("evidence") if isinstance(bug.get("evidence"), dict) else {}
            related = evidence.get("related_findings")
            kind = str(bug.get("bug_type") or "").lower()
            if "key type mismatch" in kind and isinstance(related, list):
                for item in related:
                    if not isinstance(item, dict) or not item.get("suspicious_step_id"):
                        continue
                    step_id = str(item["suspicious_step_id"])
                    if step_id in known:
                        continue
                    known.add(step_id)
                    derived = {k: v for k, v in item.items() if k != "severity"}
                    derived.update(join_step=step_id, confirmed=True)
                    out.append({"bug_type": bug.get("bug_type"), "severity": item.get("severity"),
                                "suspicious_step_id": step_id, "scope": bug.get("scope"),
                                "detector": bug.get("detector"), "evidence": derived})
                continue
            if "fan-out" not in kind or not isinstance(related, list):
                continue
            for item in related:
                if not isinstance(item, dict) or not item.get("suspicious_step_id"):
                    continue
                step_id = str(item["suspicious_step_id"])
                if step_id in known or str(item.get("severity") or "").lower() == "low":
                    continue
                known.add(step_id)
                out.append({
                    "bug_type": bug.get("bug_type"),
                    "severity": item.get("severity"),
                    "suspicious_step_id": step_id,
                    "scope": bug.get("scope"),
                    "detector": bug.get("detector"),
                    "suggested_fix": bug.get("suggested_fix"),
                    "evidence": {
                        "join_step": step_id,
                        "rows_before": item.get("rows_before"),
                        "rows_after": item.get("rows_after"),
                        "multiplication_factor": item.get("multiplication_factor"),
                        "confirmed": False,  # the fixer proves duplication itself on the database
                        "derived_from_related_findings": True,
                    },
                })
        return out

    @staticmethod
    def _splice(original: str, spans: List[Tuple[int, int]], texts: List[str]) -> str:
        """Rebuild the script: rewritten statements in place, every other character untouched."""
        out: List[str] = []
        cursor = 0
        for (start, end), text in zip(spans, texts):
            out.append(original[cursor:start])
            out.append(text)
            cursor = end
        out.append(original[cursor:])
        return "".join(out)

    def fix_bug(
        self,
        sql: str,
        bug: Dict[str, Any],
        db_connection: Any = None,
        parsed_steps: Optional[List[Dict[str, Any]]] = None,
    ) -> FixResult:
        """Convenience: fix a single finding."""
        report = self.fix_all(sql, bugs=[bug], db_connection=db_connection, parsed_steps=parsed_steps)
        if report.fixes:
            result = report.fixes[0]
            result.fixed_sql = report.fixed_sql if result.applied else result.original_sql
            return result
        return FixResult(False, report.fixed_sql, report.original_sql, warnings=report.warnings)

    # ------------------------------------------------------------------ #
    # verify(): metrics + proof
    # ------------------------------------------------------------------ #
    def _numeric_total(self, conn: Any, wrapped: str) -> Optional[float]:
        """Sum of every numeric output column of ``wrapped`` (a parenthesised query), or None."""
        numeric: List[str] = []
        try:
            # DuckDB's cursor.description only reports coarse type codes ("NUMBER"), so ask DESCRIBE.
            for row in conn.execute(f"DESCRIBE SELECT * FROM {wrapped} AS _q").fetchall():
                base = str(row[1]).upper().split("(")[0].strip()
                if base in _NUMERIC_TYPES:
                    numeric.append(row[0])
        except Exception:  # noqa: BLE001
            try:
                cursor = conn.execute(f"SELECT * FROM {wrapped} AS _q LIMIT 0")
                for col in cursor.description or []:
                    code = str(col[1]).upper()
                    if code == "NUMBER" or code in _NUMERIC_TYPES:
                        numeric.append(col[0])
            except Exception:  # noqa: BLE001
                return None
        if not numeric:
            return None
        expressions = ", ".join(f"COALESCE(SUM({_quote_ident(name)}), 0)" for name in numeric)
        row = conn.execute(f"SELECT {expressions} FROM {wrapped} AS _q").fetchone()
        return sum(float(value or 0) for value in (row or ()))

    def _measure_query_metrics(self, sql: str, connection_factory: Callable[..., Any]) -> Dict[str, Any]:
        """Row count + numeric total of the LAST statement of a script, on a fresh connection.

        Earlier statements (CREATE TABLE ... etc.) are executed first so the final query can run.
        """
        empty: Dict[str, Any] = {"rows": None, "total": None}
        if not isinstance(sql, str):
            return empty
        statements = split_statements(sql)
        if not statements:
            return empty
        setup, last = statements[:-1], statements[-1].strip().rstrip(";").strip()
        if not last:
            return empty

        conn = None
        try:
            conn = connection_factory()
            for statement in setup:
                conn.execute(statement)
            wrapped = f"(\n{last}\n)"  # newline protects against a trailing -- comment
            row_count = conn.execute(f"SELECT COUNT(*) FROM {wrapped} AS _q").fetchone()[0]
            return {"rows": row_count, "total": self._numeric_total(conn, wrapped)}
        except Exception:  # noqa: BLE001
            return empty
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:  # noqa: BLE001
                pass

    def verify(
        self,
        original_sql: str,
        fixed_sql: str,
        connection_factory: Callable[..., Any],
        fixes: Optional[Sequence[Any]] = None,
    ) -> Dict[str, Any]:
        """Prove the fix: run original and fixed scripts on FRESH connections and compare findings.

        ``connection_factory`` must return a new, isolated connection each call (scripts with
        CREATE TABLE cannot run twice on one database). ``fixes`` (optional, ``FixReport.fixes``)
        tells which strategies were applied so strategy-specific checks can run.

        The fix passes only if ALL of these hold (``checks`` lists each one, ``None`` = not applicable):
        findings went down, no step fails that did not already fail before, row metrics could be
        measured, a non-empty result did not become empty, and a de-duplication never ADDS rows.
        """
        from Engine.runner.runner import EchoEngineRunner  # local import: avoids import cycles

        original_sql, _ = normalize_sql_input(original_sql)
        fixed_sql, _ = normalize_sql_input(fixed_sql)

        def run(script: str):
            conn = connection_factory()
            try:
                return EchoEngineRunner(conn).analyze_sql(script)
            finally:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

        try:
            before, after = run(original_sql), run(fixed_sql)
        except Exception as exc:  # noqa: BLE001
            return {"verified": False, "passed": False, "error": f"{type(exc).__name__}: {exc}"}
        before = before if isinstance(before, dict) else {}
        after = after if isinstance(after, dict) else {}

        def bugs_of(result):
            return [b for b in (result.get("detected_bugs") or []) if isinstance(b, dict)]

        def fan_outs(result):
            return [b for b in bugs_of(result)
                    if "fan-out" in str(b.get("bug_type", "")).lower()
                    and not (b.get("evidence") or {}).get("likely_intentional")]

        def steps_of(result):
            return [t for t in (result.get("step_trace") or []) if isinstance(t, dict)]

        def last_join_rows(result):
            return [t.get("row_count") for t in steps_of(result)
                    if t.get("step_type") == "JOIN" and t.get("status") == "success"]

        def failure_key(step):
            return (step.get("statement_index"), step.get("scope"), step.get("step_type"))

        failed_before_keys = {failure_key(t) for t in steps_of(before) if t.get("status") == "failed"}
        failed_after = [t for t in steps_of(after) if t.get("status") == "failed"]
        # A script that already contained a broken statement keeps failing there: only NEW failures count.
        new_failed = [t for t in failed_after if failure_key(t) not in failed_before_keys]
        before_metrics = self._measure_query_metrics(original_sql, connection_factory)
        after_metrics = self._measure_query_metrics(fixed_sql, connection_factory)

        fan_before, fan_after = len(fan_outs(before)), len(fan_outs(after))
        findings_before, findings_after = len(bugs_of(before)), len(bugs_of(after))
        # Fan-out fixes must remove fan-out findings; other fixes (e.g. LEFT JOIN filter moved to
        # ON) are proven by the total number of findings going down.
        def engine_errors(result):  # syntax / reference / runtime failures reported by the runner itself
            return [b for b in bugs_of(result) if b.get("detector") == "EchoEngineRunner"]

        errors_fixed = len(engine_errors(after)) < len(engine_errors(before))
        improved = (fan_after < fan_before or (fan_before == 0 and findings_after < findings_before)
                    or errors_fixed)
        # Row metrics are mandatory only when the original script could be measured at all.
        metrics_ok = after_metrics["rows"] is not None or before_metrics["rows"] is None

        rows_b, rows_a = before_metrics["rows"], after_metrics["rows"]
        strategies = {
            _as_mapping(f).get("strategy_used") for f in (fixes or []) if _as_mapping(f).get("applied")
        }
        dedupe_only = bool(strategies) and strategies <= {"dedupe_right_side_row_number"}
        measurable = rows_b is not None and rows_a is not None
        checks: Dict[str, Optional[bool]] = {
            "findings_improved": improved,
            "no_new_failed_steps": not new_failed,
            "metrics_measurable": metrics_ok,
            "result_not_emptied": (not (rows_b > 0 and rows_a == 0)) if measurable else None,
            "rows_not_increased": (rows_a <= rows_b) if (measurable and dedupe_only) else None,
        }
        passed = all(value is not False for value in checks.values())
        reasons = {
            "findings_improved": "the number of findings did not go down",
            "no_new_failed_steps": "the fixed script has a step that fails",
            "metrics_measurable": "the fixed query could not be measured",
            "result_not_emptied": "the fixed query returns no rows although the original returned rows",
            "rows_not_increased": "the de-duplication increased the number of rows",
        }

        return {
            "verified": passed,
            "passed": passed,
            "fan_out_findings_before": fan_before,
            "fan_out_findings_after": fan_after,
            "findings_before": findings_before,
            "findings_after": findings_after,
            "rows_before": rows_b,
            "rows_after": rows_a,
            "total_before": before_metrics["total"],
            "total_after": after_metrics["total"],
            "join_rows_before": last_join_rows(before),
            "join_rows_after": last_join_rows(after),
            "failed_steps_after": [t.get("step_id") for t in failed_after],
            "new_failed_steps": [t.get("step_id") for t in new_failed],
            "checks": checks,
            "reasons": [reasons[name] for name, value in checks.items() if value is False],
        }

    # ------------------------------------------------------------------ #
    # One finding
    # ------------------------------------------------------------------ #
    _STRATEGIES: Tuple[Tuple[str, str], ...] = (
        ("fan-out", "dedupe_right_side_row_number"),
        ("inflated totals", "dedupe_right_side_row_number"),
        ("left join converted", "left_join_filter_to_on"),
        ("key type mismatch", "cast_join_key"),
        ("syntax error", "repair_syntax"),
        ("reference error", "repair_reference"),
    )

    def _fix_one(self, original, texts, bug, step_map, conn, seen, steps=None, trace=None) -> FixResult:
        bug_type = str(bug.get("bug_type") or "")
        result = FixResult(False, original, original, bug_type=bug_type,
                           target_step=bug.get("suspicious_step_id"))
        evidence = bug.get("evidence") if isinstance(bug.get("evidence"), dict) else {}
        strategy = next((name for needle, name in self._STRATEGIES if needle in bug_type.lower()), None)
        if strategy is None:
            result.warnings.append(f"No automatic rewrite exists for '{bug_type}'.")
            if bug.get("suggested_fix"):
                result.explanation = f"Suggested manual fix: {bug['suggested_fix']}"
            return result
        if evidence.get("likely_intentional"):
            result.warnings.append("The row expansion was proven intentional (aggregates on the many side); not rewritten.")
            return result
        if strategy in ("repair_syntax", "repair_reference"):
            return self._fix_text_level(result, strategy, original, texts, bug, step_map, conn, seen)

        try:
            step_id = evidence.get("join_step") or bug.get("suspicious_step_id")
            step = step_map.get(step_id)
            if step is not None and not isinstance((step.get("join_details")), dict):
                # e.g. "Inflated Totals" points at the aggregate: find the JOIN that multiplied the rows.
                resolved = self._resolve_join_step(bug, step, steps or [], trace or [])
                if resolved:
                    step_id, step = resolved, step_map.get(resolved)
            details = (step or {}).get("join_details")
            if not step or not isinstance(details, dict) or not details.get("target_alias"):
                raise FixSkipped("The finding does not point at a JOIN step that can be rewritten.")
            result.target_step = step_id
            result.target_table = details.get("target_table") or details.get("target_alias")

            index = (step.get("statement_index") or 1) - 1
            if not 0 <= index < len(texts):
                raise FixSkipped("Could not map the finding to a statement of the script.")
            key = (index, step.get("scope"), details.get("target_alias"), strategy)
            if key in seen:
                raise FixSkipped("This JOIN was already rewritten by an earlier finding.")

            # Leading comments / blank lines are not part of the SQL: keep them exactly as written.
            lead = _leading_trivia_len(texts[index])
            prefix_text, body = texts[index][:lead], texts[index][lead:]
            if not body.strip():
                raise FixSkipped("The statement is empty.")

            node = self._parse_chunk(body)
            baseline = self._word_tokens(node.sql(dialect=self.options.dialect))
            select = self._locate_select(node, step.get("scope"))
            join = self._find_join(select, details["target_alias"])
            ctx = _Context(node=node, select=select, join=join, step=step, bug=bug, conn=conn, chunk_index=index)

            handler = {
                "dedupe_right_side_row_number": self._dedupe_right_side,
                "left_join_filter_to_on": self._left_join_filter_to_on,
                "cast_join_key": self._cast_join_key,
            }[strategy]
            explanation, extras = handler(ctx)

            new_sql = node.sql(dialect=self.options.dialect, pretty=self.options.pretty)
            self._guard_no_token_loss(baseline, new_sql)
            checks = self._validate(new_sql, strategy, conn)
            texts[index] = (prefix_text + self._sanitize(new_sql)).rstrip().rstrip(";")
            seen.add(key)

            result.applied = True
            result.strategy_used = strategy
            result.explanation = self._sanitize(explanation)
            result.validation = checks
            result.warnings.extend(extras.get("warnings", []))
            result.deterministic = extras.get("deterministic")
            result.fixed_sql = texts[index]
        except FixSkipped as skip:
            result.warnings.append(str(skip))
        except Exception as exc:  # noqa: BLE001
            log.exception("Strategy %s failed", strategy)
            result.warnings.append(f"Rewrite abandoned safely ({type(exc).__name__}: {exc}).")
        return result

    @staticmethod
    def _resolve_join_step(bug, step, steps, trace) -> Optional[str]:
        """The JOIN whose row count grew, in the scope of ``step`` (after the last correct step, if known)."""
        scope = step.get("scope")
        evidence = bug.get("evidence") if isinstance(bug.get("evidence"), dict) else {}
        last_correct = evidence.get("last_correct_step")
        rows_by_id = {
            t.get("step_id"): t.get("row_count")
            for t in trace if t.get("status") == "success" and isinstance(t.get("row_count"), (int, float))
        }
        started = last_correct is None
        previous = None
        for candidate in steps:
            if candidate.get("scope") != scope:
                continue
            step_id = candidate.get("step_id")
            rows = rows_by_id.get(step_id)
            kind = candidate.get("step_type")
            if (started and kind == "JOIN" and isinstance(candidate.get("join_details"), dict)
                    and rows is not None and previous is not None and rows > previous):
                return step_id
            if kind in ("BASE_TABLE", "JOIN", "WHERE") and rows is not None:
                previous = rows
            if last_correct is not None and step_id == last_correct:
                started = True
        return None

    # ------------------------------------------------------------------ #
    # Edit guard: a rewrite may add tokens, never lose them
    # ------------------------------------------------------------------ #
    def _word_tokens(self, sql: str) -> Optional[Counter]:
        """Multiset of identifier / literal / number texts of ``sql`` (None when tokenising is unavailable)."""
        try:
            from sqlglot.tokens import TokenType

            kinds = {TokenType.VAR, TokenType.IDENTIFIER, TokenType.STRING, TokenType.NUMBER}
            tokens = sqlglot.tokenize(sql, read=self.options.dialect)
        except Exception:  # noqa: BLE001 - the guard is an extra safety net, never a hard dependency
            return None
        counts: Counter = Counter()
        for token in tokens:
            if token.token_type in kinds:
                counts[str(token.text).lower()] += 1
        return counts

    def _guard_no_token_loss(self, baseline: Optional[Counter], new_sql: str) -> None:
        """Refuse a rewrite that drops any identifier or literal the statement had before the edit."""
        if baseline is None:
            return
        after = self._word_tokens(new_sql)
        if after is None:
            return
        missing = sorted(token for token, count in baseline.items() if after.get(token, 0) < count)
        if missing:
            raise FixSkipped(
                "The rewrite would drop part of the original statement "
                f"({', '.join(missing[:5])}); refusing to apply it."
            )

    # ------------------------------------------------------------------ #
    # Strategy 1: deduplicate the "many" side of a fan-out JOIN
    # ------------------------------------------------------------------ #
    def _dedupe_right_side(self, ctx: _Context) -> Tuple[str, Dict[str, Any]]:
        d = _INTERNAL_DIALECT
        join, step = ctx.join, ctx.step
        side = str(join.side or "").upper()
        kind = str(join.kind or "").upper()
        if side in ("RIGHT", "FULL") or kind in ("CROSS", "SEMI", "ANTI", "POSITIONAL", "ASOF") or join.method:
            raise FixSkipped(f"Deduplicating the right side is not safe for a {side or kind or 'NATURAL'} join.")
        target = join.this
        if not isinstance(target, (exp.Table, exp.Subquery)):
            raise FixSkipped("The joined relation is not a table, CTE or sub-query.")
        if isinstance(target, exp.Subquery) and self._is_dedupe_subquery(target):
            raise FixSkipped("The joined relation is already deduplicated.")

        alias = target.alias or (target.name if isinstance(target, exp.Table) else "")
        if not alias:
            raise FixSkipped("The joined sub-query has no alias.")
        keys, right_only, remaining, blocking = self._split_conditions(join, alias)
        keys = list(dict.fromkeys(keys))  # a key repeated in ON must not repeat in PARTITION BY
        if blocking:
            raise FixSkipped(
                "The ON clause has conditions that mix both tables beyond simple key equality "
                f"({blocking[0].sql(dialect=d)}); a key-only deduplication could change the result."
            )
        if not keys:
            raise FixSkipped("No equality join key was found to deduplicate on.")

        if isinstance(target, exp.Table):
            bare = target.copy()
            bare.set("alias", None)
            relation_sql = bare.sql(dialect=d)
        else:
            relation_sql = "(" + target.this.sql(dialect=d) + ")"
        ident = lambda name: exp.to_identifier(name).sql(dialect=d)  # noqa: E731
        alias_sql = ident(alias)
        where_sql = None
        if right_only:
            where_sql = exp.and_(*[p.copy() for p in right_only]).sql(dialect=d)
        prefix = step.get("with_prefix") or ""

        confirmed = self._confirm_duplicates(ctx.conn, prefix, relation_sql, alias_sql, keys, where_sql)
        if confirmed is False:
            raise FixSkipped("The join key is already unique on the joined side; deduplication would not help.")
        if confirmed is None and not (ctx.bug.get("evidence") or {}).get("confirmed"):
            raise FixSkipped("Key duplication could not be confirmed (no DB connection / evidence); not rewriting.")

        order_sql, deterministic, notes = self._order_clause(ctx, prefix, relation_sql, alias, alias_sql, keys)
        warnings = list(notes)
        if not deterministic and self.options.require_deterministic:
            raise FixSkipped("No deterministic ORDER BY could be derived and require_deterministic=True.")
        if self.options.dialect != "duckdb":
            warnings.append("QUALIFY is DuckDB/Snowflake/BigQuery syntax: verify the output on your dialect.")

        inner = self._build_dedupe_select(relation_sql, alias_sql, keys, order_sql, where_sql, ident)
        join.set("this", inner.subquery(alias))
        on = join.args.get("on")
        if on is not None:
            if not remaining:
                raise FixSkipped("Internal check failed: the ON clause would become empty.")
            join.set("on", exp.and_(*[c.copy() for c in remaining]))

        keys_text = ", ".join(keys)
        explanation = (
            f"Deduplicated '{ctx.step['join_details'].get('target_table') or alias}' (alias {alias}) to one row per "
            f"({keys_text}) before the JOIN using ROW_NUMBER() OVER (PARTITION BY {keys_text}"
            + (f" ORDER BY {order_sql}" if order_sql else "")
            + ") = 1, so every row of the left side matches at most one row and is no longer multiplied."
        )
        if right_only:
            explanation += " Conditions that only involve the joined table were applied before the deduplication."
        if not deterministic:
            warnings.append(
                "No ORDER BY could be derived, so WHICH duplicate is kept is arbitrary. "
                "Set FixOptions.order_by (e.g. 'updated_at DESC') for a deterministic choice."
            )
        return explanation, {"warnings": warnings, "deterministic": deterministic}

    def _build_dedupe_select(self, relation_sql, alias_sql, keys, order_sql, where_sql, ident):
        partition = ", ".join(ident(k) for k in keys)
        window = f"ROW_NUMBER() OVER (PARTITION BY {partition}" + (f" ORDER BY {order_sql}" if order_sql else "") + ")"
        text = f"SELECT * FROM {relation_sql} AS {alias_sql}"
        if where_sql:
            text += f" WHERE {where_sql}"
        text += f" QUALIFY {window} = 1"
        tree = sqlglot.parse_one(text, read=_INTERNAL_DIALECT)
        if tree is None or not isinstance(tree, exp.Select):
            raise FixSkipped("Internal error: could not build the deduplication sub-query.")
        return tree

    def _split_conditions(self, join, alias):
        """Split an ON clause into (key columns, right-only predicates, kept conditions, blocking conditions)."""
        using = join.args.get("using")
        if using:
            return [u.name for u in using], [], [], []
        on = join.args.get("on")
        if on is None:
            return [], [], [], []
        keys, right_only, remaining, blocking = [], [], [], []
        for cond in self._conjuncts(on):
            columns = list(cond.find_all(exp.Column))
            if (isinstance(cond, exp.EQ) and isinstance(cond.this, exp.Column)
                    and isinstance(cond.expression, exp.Column)):
                a, b = cond.this, cond.expression
                a_right, b_right = a.table == alias, b.table == alias
                if a_right != b_right:
                    keys.append((a if a_right else b).name)
                    remaining.append(cond)
                    continue
            if columns and all(c.table == alias for c in columns):
                right_only.append(cond)
            elif not columns:
                remaining.append(cond)
            else:
                blocking.append(cond)
        return keys, right_only, remaining, blocking

    def _order_clause(self, ctx, prefix, relation_sql, alias, alias_sql, keys):
        """(order_sql, deterministic, notes)."""
        d = _INTERNAL_DIALECT
        configured = None
        lowered_options = {str(k).lower(): v for k, v in (self.options.order_by or {}).items()}
        for name in (alias, (ctx.step.get("join_details") or {}).get("target_table")):
            if name and str(name).lower() in lowered_options:
                configured = lowered_options[str(name).lower()]
                break
        if configured:
            return ", ".join(configured), True, []

        columns = self._relation_columns(ctx.conn, prefix, relation_sql, alias_sql)
        if columns is None:
            return None, False, []
        key_set = {k.lower() for k in keys}
        ident = lambda name: exp.to_identifier(name).sql(dialect=d)  # noqa: E731
        lowered = {c.lower(): c for c in columns}
        parts, used = [], set(key_set)
        for name in _PREFERENCE_COLUMNS + _RECENCY_COLUMNS:
            if name in lowered and name not in used:
                parts.append(f"{ident(lowered[name])} DESC NULLS LAST")
                used.add(name)
        for column in columns:
            if column.lower() not in used:
                parts.append(f"{ident(column)} ASC NULLS LAST")
                used.add(column.lower())
        notes = []
        if any("DESC" in p for p in parts):
            notes.append("Kept the row ranked first by preference/recency columns, then by all other columns.")
        return (", ".join(parts) or None), True, notes

    def _relation_columns(self, conn, prefix, relation_sql, alias_sql) -> Optional[List[str]]:
        if conn is None:
            return None
        try:
            cursor = conn.execute(f"{prefix}SELECT * FROM {relation_sql} AS {alias_sql} LIMIT 0")
            return [c[0] for c in cursor.description]
        except Exception:  # noqa: BLE001
            return None

    def _confirm_duplicates(self, conn, prefix, relation_sql, alias_sql, keys, where_sql) -> Optional[bool]:
        """True = duplicates exist, False = keys unique, None = could not check."""
        if conn is None:
            return None
        d = _INTERNAL_DIALECT
        cols = ", ".join(exp.to_identifier(k).sql(dialect=d) for k in keys)
        not_null = " AND ".join(f"{exp.to_identifier(k).sql(dialect=d)} IS NOT NULL" for k in keys)
        cond = not_null + (f" AND ({where_sql})" if where_sql else "")
        src = f"{relation_sql} AS {alias_sql}"
        try:
            rows = conn.execute(f"{prefix}SELECT COUNT(*) FROM {src} WHERE {cond}").fetchone()[0]
            distinct = conn.execute(
                f"{prefix}SELECT COUNT(*) FROM (SELECT DISTINCT {cols} FROM {src} WHERE {cond}) AS _echo_d"
            ).fetchone()[0]
        except Exception:  # noqa: BLE001
            return None
        return int(rows) > int(distinct)

    @staticmethod
    def _is_dedupe_subquery(subquery) -> bool:
        inner = subquery.this
        return isinstance(inner, exp.Select) and inner.args.get("qualify") is not None

    # ------------------------------------------------------------------ #
    # Strategy 2: LEFT JOIN + WHERE on the right table  ->  condition in ON
    # ------------------------------------------------------------------ #
    def _left_join_filter_to_on(self, ctx: _Context) -> Tuple[str, Dict[str, Any]]:
        d = self.options.dialect
        join, select = ctx.join, ctx.select
        if str(join.side or "").upper() != "LEFT":
            raise FixSkipped("The target JOIN is not a LEFT JOIN.")
        if join.args.get("using") or join.args.get("on") is None:
            raise FixSkipped("The LEFT JOIN has no ON clause to move the condition into.")
        target = join.this
        alias = target.alias or (target.name if isinstance(target, exp.Table) else "")
        where = select.args.get("where")
        if not alias or where is None:
            raise FixSkipped("No WHERE clause on the joined table was found.")

        movable, kept = [], []
        for cond in self._conjuncts(where.this):
            columns = list(cond.find_all(exp.Column))
            if columns and all(c.table == alias for c in columns) and self._null_rejecting(cond):
                movable.append(cond)
            else:
                kept.append(cond)
        if not movable:
            raise FixSkipped("No WHERE condition that only involves the LEFT-joined table can be moved safely.")

        join.set("on", exp.and_(join.args["on"].copy(), *[c.copy() for c in movable]))
        if kept:
            select.set("where", exp.Where(this=exp.and_(*[c.copy() for c in kept])))
        else:
            select.set("where", None)
        moved = " AND ".join(c.sql(dialect=d) for c in movable)
        return (
            f"Moved the WHERE condition(s) on '{alias}' ({moved}) into the LEFT JOIN's ON clause, so rows of "
            "the left table without a match are kept (with NULLs) instead of being filtered out.",
            {"warnings": [], "deterministic": None},
        )

    # ------------------------------------------------------------------ #
    # Strategy 3: wrap a join key whose two sides have incompatible types
    # ------------------------------------------------------------------ #
    def _cast_join_key(self, ctx: _Context) -> Tuple[str, Dict[str, Any]]:
        ev = ctx.bug.get("evidence") if isinstance(ctx.bug.get("evidence"), dict) else {}
        join = ctx.join
        if join.args.get("using") or join.args.get("on") is None:
            raise FixSkipped("A USING / ON-less join has no key condition to cast.")
        la, lc, ra, rc = ev.get("left_alias"), ev.get("left_column"), ev.get("right_alias"), ev.get("right_column")
        lt, rt = ev.get("left_type"), ev.get("right_type")
        lcat, rcat = ev.get("left_category"), ev.get("right_category")
        if not (la and lc and ra and rc and lt and rt):
            raise FixSkipped("The finding does not carry both key columns and their types.")
        left_col = right_col = None
        for cond in self._conjuncts(join.args["on"]):
            if isinstance(cond, exp.EQ) and isinstance(cond.this, exp.Column) and isinstance(cond.expression, exp.Column):
                for a, b in ((cond.this, cond.expression), (cond.expression, cond.this)):
                    if a.table == la and a.name.lower() == str(lc).lower() \
                            and b.table == ra and b.name.lower() == str(rc).lower():
                        left_col, right_col = a, b
                        break
            if left_col is not None:
                break
        if left_col is None:
            raise FixSkipped("The key equality named in the finding was not found in the ON clause.")

        def to_type(type_name):
            return exp.DataType.build(str(type_name), dialect=_INTERNAL_DIALECT)

        if lcat == "text" and rcat != "text":
            new_left = exp.TryCast(this=left_col.copy(), to=to_type(rt))
            left_col.replace(new_left)
            what = f"TRY_CAST({la}.{lc} AS {rt})"
        elif rcat == "text" and lcat != "text":
            new_right = exp.TryCast(this=right_col.copy(), to=to_type(lt))
            right_col.replace(new_right)
            what = f"TRY_CAST({ra}.{rc} AS {lt})"
        else:  # e.g. number vs date: compare as text on both sides
            left_col.replace(exp.Cast(this=left_col.copy(), to=to_type("VARCHAR")))
            right_col.replace(exp.Cast(this=right_col.copy(), to=to_type("VARCHAR")))
            what = f"CAST({la}.{lc} AS VARCHAR) = CAST({ra}.{rc} AS VARCHAR)"
        return (
            f"The join keys {la}.{lc} ({lt}) and {ra}.{rc} ({rt}) have different types, so values can fail to "
            f"match or fail to convert. The key is now compared as {what}; values that cannot be converted "
            "become NULL (no match) instead of raising an error.",
            {"warnings": ["Review the result: TRY_CAST turns unconvertible key values into NULL."], "deterministic": None},
        )

    # ------------------------------------------------------------------ #
    # Strategies 4 + 5: repair the TEXT of a statement (syntax, unknown names)
    # ------------------------------------------------------------------ #
    def _statement_is_valid(self, sql: str) -> bool:
        node, _, _ = _parser._parse_statement(sql, _parser._dialect_chain(self.options.dialect, FALLBACK_DIALECTS))
        if node is None:
            return False
        info = _parser._classify(node)
        return info is not None and _parser._validate(node, info) is None

    @staticmethod
    def _explain(conn: Any, sql: str) -> Tuple[Optional[bool], str]:
        """(True, '') planned, (False, message) rejected, (None, '') not checkable."""
        if conn is None:
            return None, ""
        try:
            conn.execute("EXPLAIN " + sql).fetchall()
            return True, ""
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _is_name_error(message: str) -> bool:
        lowered = message.lower()
        return "catalog" in lowered or "binder" in lowered

    def _repair_names(self, conn: Any, sql: str, max_rounds: int = 5) -> Tuple[str, List[str], Optional[bool]]:
        """Replace unknown names by the database's own suggestions until the statement plans."""
        notes: List[str] = []
        current = sql
        for _ in range(max_rounds):
            ok, message = self._explain(conn, current)
            if ok is None or ok:
                return current, notes, ok
            parsed = repair.parse_reference_error(message)
            if not parsed:
                return current, notes, False
            bad, suggestions = parsed
            for good in suggestions:
                if not repair.acceptable_suggestion(bad, good):
                    continue
                candidate = repair.replace_identifier(current, bad, good)
                if candidate != current:
                    notes.append(f"'{bad}' -> '{good}'")
                    current = candidate
                    break
            else:
                return current, notes, False
        return current, notes, False

    def _fix_text_level(self, result, strategy, original, texts, bug, step_map, conn, seen) -> FixResult:
        evidence = bug.get("evidence") if isinstance(bug.get("evidence"), dict) else {}
        try:
            step = step_map.get(bug.get("suspicious_step_id")) or {}
            index = (step.get("statement_index") or evidence.get("statement_index") or 0) - 1
            if not 0 <= index < len(texts):
                raise FixSkipped("Could not map the finding to a statement of the script.")
            key = ("text", index, strategy)
            if key in seen:
                raise FixSkipped("This statement was already repaired by an earlier finding.")
            lead = _leading_trivia_len(texts[index])
            prefix_text, body = texts[index][:lead], texts[index][lead:]
            if not body.strip():
                raise FixSkipped("The statement is empty.")

            alternatives: List[str] = []
            if strategy == "repair_syntax":
                new_body, notes, planned, alternatives = self._repair_syntax(body, conn)
                what = "Repaired the syntax"
            else:
                new_body, notes, planned = self._repair_names(conn, body)
                if not notes or planned is not True:
                    raise FixSkipped(
                        "No close match for the unknown name was found on the database, or the repaired "
                        "statement still does not plan; rewrite it manually."
                    )
                what = "Replaced unknown names by the closest existing ones"

            texts[index] = (prefix_text + self._sanitize(new_body)).rstrip()
            seen.add(key)
            result.applied = True
            result.strategy_used = strategy
            result.explanation = self._sanitize(f"{what}: " + "; ".join(notes) + ".")
            result.validation = {"parsed": True, "ast_sane": True, "db_planned": planned,
                                 "needs_review": True, "alternatives": alternatives}
            result.warnings.append("This repair is a best guess verified by parsing and planning, not by intent: review it.")
            result.deterministic = None
            result.fixed_sql = texts[index]
        except FixSkipped as skip:
            result.warnings.append(str(skip))
        except Exception as exc:  # noqa: BLE001
            log.exception("Strategy %s failed", strategy)
            result.warnings.append(f"Repair abandoned safely ({type(exc).__name__}: {exc}).")
        return result

    def _repair_syntax(self, body: str, conn: Any) -> Tuple[str, List[str], Optional[bool], List[str]]:
        found = repair.repair_candidates(body, self._statement_is_valid)
        if not found:
            raise FixSkipped(
                "No safe automatic repair was found for the syntax error "
                "(tried quotes, parentheses, commas, repeated / mistyped keywords, == and smart quotes)."
            )
        ranked = []
        for candidate in found:
            ok, message = self._explain(conn, candidate.sql)
            # 0 plans, 1 not checkable, 2 parses but names are unknown (repairable), 3 rejected by the database
            kind = 0 if ok else 1 if ok is None else 2 if self._is_name_error(message) else 3
            ranked.append((candidate.edits, kind, -candidate.ratio, candidate, message))
        ranked.sort(key=lambda item: item[:3])
        edits, kind, _, best, message = ranked[0]
        if kind == 3:
            raise FixSkipped(f"The closest repair was rejected by the database ({message.splitlines()[0]}).")
        sql, notes = best.sql, list(best.labels)
        planned: Optional[bool] = True if kind == 0 else None
        if kind == 2:
            sql, name_notes, ok = self._repair_names(conn, best.sql)
            if ok is not True:
                raise FixSkipped("The syntax could be repaired but the statement still refers to unknown names.")
            notes += [f"replaced {n}" for n in name_notes]
            planned = True
        alternatives = [r[3].sql for r in ranked[1:4] if r[0] == edits and r[3].sql != sql]
        return sql, notes, planned, alternatives

    # ------------------------------------------------------------------ #
    # AST navigation
    # ------------------------------------------------------------------ #
    def _parse_chunk(self, chunk: str):
        dialects = [self.options.dialect] + [x for x in FALLBACK_DIALECTS if x != self.options.dialect]
        for dialect in dialects:
            try:
                node = sqlglot.parse_one(chunk, read=dialect)
            except Exception:  # noqa: BLE001
                continue
            if node is not None:
                return node.unnest()
        raise FixSkipped("The statement could not be parsed.")

    def _locate_select(self, statement, scope):
        """Resolve a parser scope ('main', 'stmt_2/cte:x', 'main/branch_2', 'main/source' ...) to its SELECT."""
        tokens = [t for t in str(scope or "main").split("/") if t]
        if tokens and (tokens[0] == "main" or tokens[0].startswith("stmt_")):
            tokens = tokens[1:]
        node = statement
        for token in tokens:
            if token == "source":
                node = node.args.get("expression")
            elif token.startswith("cte:"):
                name = token[4:]
                node = next((c.this for c in statement.find_all(exp.CTE) if c.alias == name), None)
            elif token.startswith("branch_"):
                leaves = self._leaves(node)
                position = int(token[7:]) - 1 if token[7:].isdigit() else -1
                node = leaves[position] if 0 <= position < len(leaves) else None
            else:
                node = None
            if node is None:
                raise FixSkipped(f"Could not locate scope '{scope}' in the statement.")
            node = node.unnest()
        node = node.unnest()
        if _SET_OPERATIONS and isinstance(node, _SET_OPERATIONS):
            raise FixSkipped("The scope resolves to a set operation, not to a single SELECT.")
        if not isinstance(node, exp.Select):
            raise FixSkipped("The scope does not resolve to a SELECT.")
        return node

    def _leaves(self, node):
        leaves = []

        def walk(current):
            current = current.unnest()
            if _SET_OPERATIONS and isinstance(current, _SET_OPERATIONS):
                walk(current.this)
                walk(current.expression)
            else:
                leaves.append(current)

        walk(node)
        return leaves

    def _find_join(self, select, alias):
        matches = []
        for join in select.args.get("joins") or []:
            target = join.this
            name = target.alias or (target.name if isinstance(target, exp.Table) else "")
            if name == alias:
                matches.append(join)
        if len(matches) != 1:
            raise FixSkipped(f"Could not uniquely identify the JOIN with alias '{alias}'.")
        return matches[0]

    def _conjuncts(self, node):
        node = node.unnest()
        if isinstance(node, exp.And):
            return self._conjuncts(node.this) + self._conjuncts(node.expression)
        return [node]

    @staticmethod
    def _null_rejecting(cond) -> bool:
        cond = cond.unnest()
        if isinstance(cond, exp.Not):
            inner = cond.this.unnest()
            return isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null)
        for risky in ("Coalesce", "Case", "Or"):
            klass = getattr(exp, risky, None)
            if klass is not None and cond.find(klass):
                return False
        return isinstance(cond, _NULL_REJECTING)

    # ------------------------------------------------------------------ #
    # Validation, sanitising, rendering
    # ------------------------------------------------------------------ #
    def _validate(self, new_sql: str, strategy: str, conn) -> Dict[str, Any]:
        checks: Dict[str, Any] = {"parsed": False, "ast_sane": False, "db_planned": None}
        try:
            tree = sqlglot.parse_one(new_sql, read=self.options.dialect)
        except Exception as exc:  # noqa: BLE001
            raise FixSkipped(f"The rewritten SQL failed validation (parse error: {exc}).")
        if tree is None or isinstance(tree, exp.Command):
            raise FixSkipped("The rewritten SQL failed validation (not a recognised statement).")
        checks["parsed"] = True
        if strategy == "dedupe_right_side_row_number" and not (tree.find(exp.Qualify) and tree.find(exp.Window)):
            raise FixSkipped("The rewritten SQL failed validation (deduplication window missing).")
        checks["ast_sane"] = True

        if conn is not None and self.options.validate_on_db:
            try:
                conn.execute("EXPLAIN " + new_sql).fetchall()
                checks["db_planned"] = True
            except Exception as exc:  # noqa: BLE001
                kind = type(exc).__name__.lower()
                if "catalog" in kind:  # a table of an earlier script step may no longer exist: AST check stands
                    checks["db_planned"] = None
                    checks["db_note"] = "Could not plan on the DB (missing objects); AST validation only."
                else:
                    raise FixSkipped(f"The rewritten SQL was rejected by the database ({type(exc).__name__}: {exc}).")
        return checks

    @staticmethod
    def _sanitize(text: Any) -> str:
        return _ANSI_ESCAPES.sub("", str(text if text is not None else ""))

    def _clean_report(self, report: FixReport) -> FixReport:
        report.fixed_sql = self._sanitize(report.fixed_sql)
        report.warnings = [self._sanitize(w) for w in report.warnings]
        for fix in report.fixes:
            fix.fixed_sql = self._sanitize(fix.fixed_sql)
            fix.explanation = self._sanitize(fix.explanation)
            fix.warnings = [self._sanitize(w) for w in fix.warnings]
        return report