"""ECHO engine runner.

Executes the parser's steps on DuckDB (SELECT analysis, DML, DDL, admin commands),
profiles every step, collects multi-column metrics and runs every detector plugin.
"""

import difflib
import importlib
import inspect
import logging
import pkgutil
import re
import time
from decimal import Decimal

from Engine import detectors
from Engine.detectors.base_detector import SEVERITY_RANK, BaseBugDetector
from Engine.parser.parser import DEFAULT_DIALECT, check_query_is_read_only, parse_sql_to_steps

log = logging.getLogger("echo.runner")

FATAL_STEP_TYPES = ("SYNTAX_ERROR", "SECURITY_ERROR")

# Statements that are valid SQL but often unsupported by an in-memory DuckDB session
# (database-level commands). For these, ANY engine-side error degrades to a warning.
FALLBACK_KINDS = {"CREATE_DATABASE", "DROP_DATABASE", "USE", "TRUNCATE"}
DML_KINDS = {"INSERT", "UPDATE", "DELETE", "MERGE", "COPY"}
# Engine error classes that mean "this engine/environment cannot do it", never "your SQL is wrong".
_LIMITATION_TAGS = (
    "notimplemented", "parser", "syntax", "catalog", "binder", "invalidinput",
    "io", "permission", "runtime", "transaction", "dependency",
)


class EchoEngineRunner:
    """Runs the step trace, then every detector plugin, and returns one report.

    Parameters
    ----------
    allow_writes        Execute INSERT/UPDATE/DELETE/DDL/admin statements. Set False to
                        analyse read-only (non-SELECT steps become SECURITY_ERROR).
    stop_on_error       Stop executing the script after the first failed step.
    collect_column_metrics  Compute per-column numeric sums (``columns_metrics``).
    metrics_row_limit   Skip column metrics for steps with more rows than this
                        (metrics need the rows in Python; count/sum stay exact in SQL).
    """

    def __init__(
        self,
        db_connection,
        dialect=DEFAULT_DIALECT,
        allow_writes=True,
        stop_on_error=False,
        collect_column_metrics=True,
        metrics_row_limit=200_000,
    ):
        self.db = db_connection
        self.dialect = dialect
        self.allow_writes = allow_writes
        self.stop_on_error = stop_on_error
        self.collect_column_metrics = collect_column_metrics
        self.metrics_row_limit = metrics_row_limit
        self.load_errors = []
        self._detectors = None
        self._session = None

    # ------------------------------------------------------------------ #
    # Plugins (loaded once, cached)
    # ------------------------------------------------------------------ #
    def load_all_detectors(self, force=False):
        """Discover every concrete BaseBugDetector subclass in Engine/detectors/."""
        if self._detectors is not None and not force:
            return self._detectors
        plugins, seen = [], set()
        self.load_errors = []
        modules = sorted(pkgutil.iter_modules(detectors.__path__), key=lambda m: m.name)
        for module_info in modules:
            name = module_info.name
            if name.startswith("_") or name == "base_detector":
                continue
            try:
                module = importlib.import_module(f"{detectors.__name__}.{name}")
            except Exception as exc:  # noqa: BLE001 - one broken plugin must not hide the others
                self.load_errors.append({"detector": name, "error": f"import failed: {exc!r}"})
                log.warning("Could not import detector module %s: %s", name, exc)
                continue
            for _, cls in inspect.getmembers(module, inspect.isclass):
                if (
                    issubclass(cls, BaseBugDetector)
                    and cls is not BaseBugDetector
                    and not inspect.isabstract(cls)
                    and cls.__module__ == module.__name__
                    and cls not in seen
                ):
                    plugins.append(cls())
                    seen.add(cls)
        self._detectors = plugins
        return plugins

    # ------------------------------------------------------------------ #
    # Step normalisation / session
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_steps(parsed_steps):
        """Accept the parser's list of dicts (preferred) or a legacy {step_id: sql} dict."""
        steps = []
        if isinstance(parsed_steps, list):
            steps = [s for s in parsed_steps if isinstance(s, dict)]
        elif isinstance(parsed_steps, dict):
            for step_id, sql in parsed_steps.items():
                steps.append({"step_id": step_id, "step_type": "QUERY", "name": str(step_id),
                              "scope": "main", "executable_query": sql})
        return steps

    def _open_session(self):
        """One session for the whole script so USE / ATTACH / temp objects persist across steps."""
        try:
            return self.db.cursor()
        except Exception:  # noqa: BLE001 - connection types without cursor()
            return self.db

    def _close_session(self):
        session, self._session = self._session, None
        if session is not None and session is not self.db:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    # Step trace
    # ------------------------------------------------------------------ #
    def run_step_trace(self, parsed_steps):
        """Execute every step in order; return one trace entry per step."""
        steps = self._normalize_steps(parsed_steps)
        if self._session is None:
            self._session = self._open_session()
        trace = []
        for step in steps:
            entry = self._execute_step(self._session, step)
            trace.append(entry)
            if entry["status"] == "failed" and self.stop_on_error:
                break
        return trace

    def _base_entry(self, step, sql):
        return {
            "step_id": step.get("step_id", "unknown_step"),
            "step_type": step.get("step_type", "QUERY"),
            "name": step.get("name"),
            "scope": step.get("scope", "main"),
            "statement_index": step.get("statement_index"),
            "statement_kind": step.get("statement_kind"),
            "query": sql,
            "row_count": None,
            "sum_value": None,
            "columns_metrics": {},
            "execution_time_ms": 0.0,
            "status": "success",
            "error": None,
        }

    def _is_read_only(self, step, sql):
        if "read_only" in step:
            return bool(step["read_only"])
        return check_query_is_read_only(sql or "", self.dialect)[0]  # legacy dict mode

    def _execute_step(self, session, step):
        sql = step.get("executable_query") or step.get("query")
        entry = self._base_entry(step, sql)
        step_type = entry["step_type"]

        if step_type in FATAL_STEP_TYPES:
            entry.update(status="failed", error=step.get("error") or "Invalid query structure.")
            return entry
        if not sql and not step.get("count_query"):
            entry.update(status="skipped")
            return entry
        if not self.allow_writes and not self._is_read_only(step, sql):
            entry.update(
                step_type="SECURITY_ERROR",
                status="failed",
                error="Blocked: write / DDL / admin statements are disabled (allow_writes=False).",
            )
            return entry

        if step.get("count_query"):
            return self._execute_analytic_step(session, step, entry, sql)
        return self._execute_statement(session, step, entry, sql)

    # ---- SELECT analysis steps ------------------------------------------- #
    def _execute_analytic_step(self, session, step, entry, sql):
        try:
            started = time.perf_counter()
            entry["row_count"] = int(session.execute(step["count_query"]).fetchone()[0])
            entry["execution_time_ms"] = round((time.perf_counter() - started) * 1000, 2)
        except Exception as exc:  # noqa: BLE001
            entry.update(status="failed", error=str(exc), error_type=type(exc).__name__)
            return entry

        entry["measure_values"] = {}
        measure_queries = step.get("measure_queries") or {}

        for key, query in measure_queries.items():
            try:
                value = session.execute(query).fetchone()[0]
                entry["measure_values"][key] = (
                    float(value) if value is not None else None
                )
            except Exception:  # noqa: BLE001 - measure columns may not exist yet at this step
                entry["measure_values"][key] = None

        # Keep backward compatibility with existing SUM-based detectors.
        measure_sql = step.get("measure_query")
        if measure_sql:
            try:
                value = session.execute(measure_sql).fetchone()[0]
                entry["sum_value"] = float(value) if value is not None else None
            except Exception:  # noqa: BLE001
                entry["sum_value"] = None
    
        if (self.collect_column_metrics and sql and entry["row_count"]
                and entry["row_count"] <= self.metrics_row_limit):
            try:
                cursor = session.execute(sql)
                columns = [c[0] for c in cursor.description] if cursor.description else []
                entry["columns_metrics"] = self._column_metrics(columns, cursor.fetchall())
                if entry.get("sum_value") is None and entry["columns_metrics"]:
                    entry["sum_value"] = next(iter(entry["columns_metrics"].values()))
            except Exception as exc:  # noqa: BLE001
                entry["columns_metrics"] = {}
                if self._is_reference_error(exc):  # unknown column / table is a real error, not a metrics glitch
                    entry.update(status="failed", error=str(exc), error_type=type(exc).__name__)
        elif entry["row_count"] and entry["row_count"] > self.metrics_row_limit:
            entry["columns_metrics_skipped"] = True
        return entry

    @staticmethod
    def _is_reference_error(exc):
        name = type(exc).__name__.lower()
        return "binder" in name or "catalog" in name

    @staticmethod
    def _column_metrics(columns, rows):
        """Sum of every numeric column (duplicate column names get a _2, _3 ... suffix)."""
        metrics, used = {}, {}
        for index, name in enumerate(columns):
            used[name] = used.get(name, 0) + 1
            key = name if used[name] == 1 else f"{name}_{used[name]}"
            total, seen = 0, False
            for row in rows:
                value = row[index]
                if value is None or isinstance(value, bool):
                    continue
                if isinstance(value, (int, float, Decimal)):
                    total += float(value) if isinstance(value, Decimal) else value
                    seen = True
                else:
                    break  # non-numeric column
            else:
                if seen:
                    metrics[key] = total
        return metrics

    # ---- DDL / DML / admin statements ------------------------------------- #
    def _run_unit(self, session, queries):
        """Run a list of SQL strings in order; return (last_rows, last_columns)."""
        rows, columns = [], []
        for query in queries:
            cursor = session.execute(query)
            description = getattr(cursor, "description", None)
            columns = [c[0] for c in description] if description else []
            rows = cursor.fetchall() if description else []
        return rows, columns

    def _execute_statement(self, session, step, entry, sql):
        attempts = [("native", [sql])]
        attempts.extend(("emulated", list(alt)) for alt in step.get("alternative_queries") or [])
        kind = (step.get("statement_kind") or "").upper()
        first_error = None
        started = time.perf_counter()

        for mode, queries in attempts:
            try:
                rows, columns = self._run_unit(session, queries)
            except Exception as exc:  # noqa: BLE001
                first_error = first_error or exc
                continue
            entry["execution_time_ms"] = round((time.perf_counter() - started) * 1000, 2)
            entry["execution_mode"] = mode
            if mode == "emulated":
                entry["warning"] = f"Ran an equivalent command because the original was not supported: {queries[0]}"
            if kind in DML_KINDS and len(rows) == 1 and len(rows[0]) == 1 \
                    and isinstance(rows[0][0], int) and not isinstance(rows[0][0], bool):
                entry["rows_affected"] = entry["row_count"] = rows[0][0]
            elif step.get("step_type") in ("DDL", "ADMIN", "DML"):
                entry["row_count"] = 0
            else:
                entry["row_count"] = len(rows)
                if self.collect_column_metrics and rows and len(rows) <= self.metrics_row_limit:
                    entry["columns_metrics"] = self._column_metrics(columns, rows)
            return entry

        entry["execution_time_ms"] = round((time.perf_counter() - started) * 1000, 2)
        if self._is_environment_limitation(step, kind, first_error):
            entry.update(
                row_count=0,
                execution_mode="fallback",
                fallback=True,
                fallback_reason=str(first_error),
                warning=(
                    "Statement is valid SQL but this in-memory DuckDB session cannot run it; "
                    "it was skipped (no data was changed)."
                ),
            )
            return entry
        entry.update(status="failed", error=str(first_error), error_type=type(first_error).__name__)
        return entry

    @staticmethod
    def _is_environment_limitation(step, kind, exc):
        """True when the engine (not the SQL) is the problem. Syntax was already validated by the parser."""
        if exc is None:
            return False
        name = type(exc).__name__.lower()
        text = str(exc).lower()
        if "notimplemented" in name or "not implemented" in text:
            return True
        if kind in FALLBACK_KINDS or (step.get("step_type") == "ADMIN" and not step.get("read_only")):
            return any(tag in name for tag in _LIMITATION_TAGS)
        return False

    # ------------------------------------------------------------------ #
    # Analysis
    # ------------------------------------------------------------------ #
    @staticmethod
    def _failure_bug_type(entry):
        error_type = (entry.get("error_type") or "").lower()
        message = (entry.get("error") or "").lower()
        if entry.get("step_type") == "SYNTAX_ERROR" or "parser" in error_type \
                or "syntax" in error_type or message.startswith("invalid sql syntax"):
            return "SQL Syntax Error"
        if entry.get("step_type") == "SECURITY_ERROR":
            return "Blocked Statement"
        if "binder" in error_type or "catalog" in error_type:
            return "SQL Reference Error (unknown table/column or incompatible types)"
        if "conversion" in error_type:
            return "Data Type Conversion Error"
        return "Query Runtime Failure"

    @staticmethod
    def _reference_hints(message, tables):
        """(unknown_name, [close matches]) from a binder / catalog message (best effort)."""
        text = str(message or "")
        found = re.search(r'(?:column|Table|View)(?: with name)? "?([\w.$]+)"?(?: not found| does not exist)', text, re.I)
        unknown = found.group(1) if found else None
        pool = []
        for marker in (r"Did you mean\s+(.*)", r"Candidate bindings:\s*(.*)"):
            match = re.search(marker, text, re.I | re.S)
            if match:
                pool += re.findall(r'"([^"]+)"', match.group(1).split("\n")[0])
        if not pool and unknown and re.search(r"table|view", text, re.I):
            pool = list(tables or [])
        if unknown:
            bare = unknown.split(".")[-1].lower()
            pool = [p for p in dict.fromkeys(pool)
                    if difflib.get_close_matches(bare, [p.split(".")[-1].lower()], 1, 0.5)]
        return unknown, pool[:5]

    def _available_tables(self, limit=30):
        """Names of the tables / views the script can use (best effort, never raises)."""
        try:
            rows = (self._session or self.db).execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema NOT IN ('information_schema', 'pg_catalog') "
                "ORDER BY table_name LIMIT " + str(int(limit))
            ).fetchall()
            return [str(r[0]) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def analyze_sql(self, query):
        """Convenience: parse + analyze in one call."""
        return self.analyze_query(query, parse_sql_to_steps(query, self.dialect))

    def analyze_query(self, query, parsed_steps):
        started = time.perf_counter()
        steps = self._normalize_steps(parsed_steps)
        order = {s.get("step_id"): i for i, s in enumerate(steps)}
        self._close_session()
        self._session = self._open_session()
        try:
            step_trace = self.run_step_trace(steps)
            trace_ms = round((time.perf_counter() - started) * 1000, 2)
            bugs, detector_errors, detector_ms = [], [], {}
            detector_plugins = self.load_all_detectors()
            detector_errors.extend(self.load_errors)
            available_tables = self._available_tables()  # while the session (temp tables) is still open

            for detector in detector_plugins:
                t0 = time.perf_counter()
                try:
                    found = detector.detect(steps, step_trace, self._session)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Detector %s crashed: %s", detector.detector_name, exc)
                    detector_errors.append({"detector": detector.detector_name, "error": repr(exc)})
                    continue
                finally:
                    detector_ms[detector.detector_name] = round((time.perf_counter() - t0) * 1000, 2)
                if isinstance(found, dict):
                    bugs.append(found)
                elif isinstance(found, list):
                    bugs.extend(b for b in found if isinstance(b, dict))
        finally:
            self._close_session()

        # Failed steps: syntax / blocked statements always; others only if no detector explained them.
        explained = {b.get("suspicious_step_id") for b in bugs}
        reported_scopes = set()
        for entry in step_trace:
            if entry.get("status") != "failed":
                continue
            fatal = entry["step_type"] in FATAL_STEP_TYPES
            if not fatal and entry["scope"] in reported_scopes:
                continue  # later steps of the same scope fail as a cascade
            reported_scopes.add(entry["scope"])
            if entry["step_id"] in explained:
                continue
            evidence = {"error_type": entry.get("error_type"),
                        "statement_index": entry.get("statement_index")}
            suggested = "Fix the statement text, or the table/column names and types it uses."
            error_kind = (entry.get("error_type") or "").lower()
            close = []
            if "catalog" in error_kind or "binder" in error_kind:
                unknown, close = self._reference_hints(entry.get("error"), available_tables)
                if unknown:
                    evidence["unknown_reference"] = unknown
                if close:
                    evidence["did_you_mean"] = close
                    suggested = "Unknown reference '%s'. Did you mean: %s?" % (unknown, ", ".join(close))
            if available_tables and ("catalog" in error_kind or "binder" in error_kind):
                evidence["available_tables"] = available_tables
                suggested = (suggested + " " if close else "") + (
                    "A table or column does not exist. Read the database message in the description "
                    "(it often says 'Did you mean ...'). Tables available here: "
                    + ", ".join(available_tables)
                    + ". If your table is not listed, create it in the script (CREATE TABLE ...; INSERT ...) "
                    "before the query."
                )
            bugs.append({
                "bug_type": self._failure_bug_type(entry),
                "severity": "High",
                "confidence": "high",
                "suspicious_step_id": entry["step_id"],
                "scope": entry["scope"],
                "description": f"Step '{entry['step_id']}' failed: {entry.get('error')}",
                "evidence": evidence,
                "suggested_fix": suggested,
                "detector": "EchoEngineRunner",
            })

        bugs.sort(key=lambda b: (SEVERITY_RANK.get(b.get("severity"), 3),
                                 order.get(b.get("suspicious_step_id"), 10**6)))
        if any(t["step_type"] in FATAL_STEP_TYPES for t in step_trace):
            verdict = "error"
        else:
            verdict = "issues_found" if bugs else "no_issues_found"

        located = [b for b in bugs if b.get("suspicious_step_id") in order]
        first = min(located, key=lambda b: order[b["suspicious_step_id"]], default=None)
        return {
            "query": query,
            "verdict": verdict,
            "first_suspicious_step": first["suspicious_step_id"] if first else None,
            "step_trace": step_trace,
            "detected_bugs": bugs,
            "warnings": [
                {"step_id": t["step_id"], "message": t["warning"], "execution_mode": t.get("execution_mode")}
                for t in step_trace if t.get("warning")
            ],
            "detector_errors": detector_errors,
            "profiling": {
                "total_ms": round((time.perf_counter() - started) * 1000, 2),
                "steps_ms": trace_ms,
                "detectors_ms": detector_ms,
            },
        }