import math
import re
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any, Dict, List, Optional

SEVERITY_RANK = {
    "Critical": 0,
    "High": 1,
    "Medium": 2,
    "Low": 3,
}

_SEVERITY_ALIASES = {name.lower(): name for name in SEVERITY_RANK}

# --- step classification vocabulary (shared by every detector) ----------------------------
# Analytic steps produced by the parser for SELECT queries.
_ANALYTIC_TYPES = {
    "JOIN": "JOIN",
    "WHERE": "FILTER",
    "BASE_TABLE": "BASE",
    "GROUP_BY": "AGGREGATE",
    "AGGREGATE": "AGGREGATE",
    "UNION_PART": "SET_OP",
    "SET_OPERATION": "SET_OP",
    "CTE": "CTE",
}
_ERROR_TYPES = {"SYNTAX_ERROR", "SECURITY_ERROR", "ENGINE_ERROR"}
DML_KINDS = {"INSERT", "UPDATE", "DELETE", "MERGE", "COPY", "REPLACE"}
DDL_TYPES = {"DDL", "TRUNCATE", "DROP", "CREATE", "ALTER"}
DDL_KINDS = {"TRUNCATE", "TRUNCATE_TABLE", "DROP_TABLE", "DROP_VIEW", "CREATE_TABLE", "ALTER_TABLE"}
DDL_KIND_PREFIXES = ("CREATE_", "DROP_", "ALTER", "TRUNCATE")
# Categories whose row_count describes "rows flowing through a SELECT pipeline".
ROW_FLOW_CATEGORIES = {"BASE", "JOIN", "FILTER", "QUERY"}

_LITERALS_AND_COMMENTS = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|--[^\n]*|/\*.*?\*/", re.DOTALL)
_SET_OPERATION = re.compile(r"\b(UNION|INTERSECT|EXCEPT)\b", re.IGNORECASE)
_DML_WORD = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE)\b", re.IGNORECASE)
_JOIN_WORD = re.compile(r"\bJOIN\b", re.IGNORECASE)
_LEADING_WORD = re.compile(r"[A-Za-z]+")


class BaseBugDetector(ABC):
    """Abstract Base Class for all SQL Bug Detectors.

    Contract (unchanged): ``detect(parsed_steps, step_trace, db_connection=None, *args, **kwargs)``
    returns ``None`` or a bug-report ``dict``. A runner that also accepts a ``list`` of
    reports keeps working, but returning one dict is always safe.

    Everything below ``detect`` is optional tooling: safe number handling, one shared
    definition of what a JOIN / DML / DDL step is, and a ``collect_row_flow`` helper that
    walks the row counts of a SELECT pipeline per scope, so detectors cannot disagree.
    """

    detector_name: str = ""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # Look only at the class's OWN attributes: a subclass of a subclass must not
        # silently inherit its parent's name.
        if not cls.__dict__.get("detector_name"):
            cls.detector_name = cls.__name__

    @abstractmethod
    def detect(
        self,
        parsed_steps: List[Dict[str, Any]],
        step_trace: List[Dict[str, Any]],
        db_connection: Any = None,
        *args,
        **kwargs,
    ) -> Optional[Dict[str, Any]]:
        """Analyzes query steps and execution data to find anomalies."""
        pass

    # ------------------------------------------------------------------ #
    # Report formatting
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_severity(severity: Any) -> str:
        """Map any severity spelling onto Critical / High / Medium / Low (unknown -> Medium)."""
        text = str(severity or "").strip().lower()
        if text in _SEVERITY_ALIASES:
            return _SEVERITY_ALIASES[text]
        for alias, canonical in _SEVERITY_ALIASES.items():  # e.g. "Low/Unknown"
            if text.startswith(alias):
                return canonical
        return "Medium"

    def report(
        self,
        bug_type: str,
        severity: str,
        description: str,
        suspicious_step_id: Optional[str] = None,
        evidence: Optional[Dict[str, Any]] = None,
        suggested_fix: Optional[str] = None,
        scope: Optional[str] = None,
        confidence: str = "high",
    ) -> Dict[str, Any]:
        """Full-featured report. Always contains bug_type, severity and description."""
        return {
            "bug_type": str(bug_type),
            "severity": self._normalize_severity(severity),
            "confidence": confidence,
            "suspicious_step_id": suspicious_step_id,
            "scope": scope,
            "description": str(description),
            "evidence": dict(evidence) if isinstance(evidence, dict) else {},
            "suggested_fix": suggested_fix,
            "detector": self.detector_name or self.__class__.__name__,
        }

    def _format_bug_report(
        self, bug_type: str, severity: str, description: str, **extra: Any
    ) -> Dict[str, Any]:
        """Legacy helper (same three positional arguments). Optional keyword extras:
        suspicious_step_id, evidence, suggested_fix, scope, confidence; any other keyword
        is added to the report as-is.
        """
        known = ("suspicious_step_id", "evidence", "suggested_fix", "scope", "confidence")
        report = self.report(
            bug_type,
            severity,
            description,
            **{key: extra.pop(key) for key in known if key in extra},
        )
        for key, value in extra.items():
            report.setdefault(key, value)
        return report

    # ------------------------------------------------------------------ #
    # Safe value handling
    # ------------------------------------------------------------------ #
    @staticmethod
    def to_number(value: Any) -> Optional[float]:
        """Finite number or None. Rejects bool, NaN/inf, empty and non-numeric text."""
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
            return value if math.isfinite(value) else None
        return None

    @classmethod
    def to_row_count(cls, value: Any) -> Optional[int]:
        number = cls.to_number(value)
        if number is None or number < 0:
            return None
        return int(number)

    @staticmethod
    def as_dict_list(value: Any) -> List[Dict[str, Any]]:
        if not isinstance(value, (list, tuple)):
            return []
        return [item for item in value if isinstance(item, dict)]

    @staticmethod
    def is_step_ok(entry: Dict[str, Any]) -> bool:
        """True for executed-and-successful trace entries (entries without a status count as ok)."""
        status = str(entry.get("status") or "").lower()
        if status:
            return status == "success"
        return not entry.get("error")

    @staticmethod
    def step_sql(step: Dict[str, Any]) -> str:
        for key in ("sql", "query", "executable_query", "sql_snippet"):
            value = step.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""

    # ------------------------------------------------------------------ #
    # Step classification (single source of truth)
    # ------------------------------------------------------------------ #
    @staticmethod
    def sql_is_select_with_join(sql: str) -> bool:
        """Text fallback for legacy steps without metadata: a plain SELECT/WITH query that
        contains a JOIN. UPDATE / INSERT / DELETE / MERGE and UNION / INTERSECT / EXCEPT
        statements never qualify, and JOIN inside strings or comments is ignored.
        """
        cleaned = _LITERALS_AND_COMMENTS.sub(" ", sql or "").strip().lstrip("(").lstrip()
        first = _LEADING_WORD.match(cleaned)
        if not first or first.group(0).upper() not in ("SELECT", "WITH", "FROM"):
            return False
        if _SET_OPERATION.search(cleaned) or _DML_WORD.search(cleaned):
            return False
        return bool(_JOIN_WORD.search(cleaned))

    def classify_step(self, step: Dict[str, Any]) -> str:
        """JOIN | FILTER | BASE | AGGREGATE | SET_OP | CTE | DML | DDL | ADMIN | ERROR | QUERY | UNKNOWN."""
        step_type = str(step.get("step_type") or step.get("type") or "").upper()
        kind = str(step.get("statement_kind") or "").upper()
        operation = str(step.get("operation") or "").upper()

        if step_type in _ERROR_TYPES:
            return "ERROR"
        if step_type in _ANALYTIC_TYPES:  # structured parser output wins
            return _ANALYTIC_TYPES[step_type]
        if kind in DML_KINDS or step_type == "DML":
            return "DML"
        if (step_type in DDL_TYPES or kind in DDL_KINDS
                or (kind and kind.startswith(DDL_KIND_PREFIXES))):
            return "DDL"
        if step_type == "ADMIN":
            return "ADMIN"
        if step.get("join_details") or "JOIN" in kind or "JOIN" in operation:
            return "JOIN"
        if step_type in ("QUERY", "") or kind == "SELECT":
            return "JOIN" if self.sql_is_select_with_join(self.step_sql(step)) else "QUERY"
        return "UNKNOWN"

    def _is_join_step(
        self,
        step: Dict[str, Any],
        parsed_steps: List[Dict[str, Any]],
        step_index: int,
    ) -> bool:
        """Compatibility wrapper: is this trace step a JOIN (using parsed_steps metadata too)?"""
        parsed_list = self.as_dict_list(parsed_steps)
        parsed = self._lookup_parsed(step, step_index, parsed_list, {p.get("step_id"): p for p in parsed_list})
        return self.classify_step(self._merge(step, parsed)) == "JOIN"

    # ------------------------------------------------------------------ #
    # Row-flow walk (shared by FanOut / General anomaly detectors)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _lookup_parsed(entry, index, parsed_list, parsed_by_id):
        """Find the parser step for a trace entry: by step_id, else by position when that is unambiguous."""
        step_id = entry.get("step_id")
        if step_id is not None and step_id in parsed_by_id:
            return parsed_by_id[step_id]
        if 0 <= index < len(parsed_list):
            candidate = parsed_list[index]
            if candidate.get("step_id") in (None, step_id):
                return candidate
        return None

    @staticmethod
    def _merge(entry, parsed):
        merged = dict(parsed) if isinstance(parsed, dict) else {}
        merged.update({k: v for k, v in entry.items() if v is not None})
        return merged

    def collect_row_flow(
        self,
        parsed_steps: List[Dict[str, Any]],
        step_trace: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Successful row-producing SELECT steps, each linked to its predecessor IN THE SAME SCOPE.

        Statement boundaries, CTE bodies and UNION branches are separate scopes, and DML / DDL /
        admin / aggregate steps are not part of the flow, so row counts of unrelated steps are
        never compared with each other.
        """
        parsed_list = self.as_dict_list(parsed_steps)
        aligned = len(parsed_list) == len(step_trace) if isinstance(step_trace, list) else False
        parsed_by_id = {p.get("step_id"): p for p in parsed_list if p.get("step_id") is not None}
        previous: Dict[str, Dict[str, Any]] = {}
        flow: List[Dict[str, Any]] = []

        for index, entry in enumerate(step_trace if isinstance(step_trace, list) else []):
            if not isinstance(entry, dict):
                continue
            step_id = entry.get("step_id")
            parsed = parsed_by_id.get(step_id)
            if parsed is None and aligned:
                parsed = parsed_list[index]
            step = self._merge(entry, parsed)
            category = self.classify_step(step)
            rows = self.to_row_count(entry.get("row_count"))
            if category not in ROW_FLOW_CATEGORIES or rows is None or not self.is_step_ok(entry):
                continue
            scope = str(step.get("scope") or "main")
            before = previous.get(scope)
            flow.append({
                "index": index,
                "step_id": step_id if step_id is not None else f"step_{index + 1}",
                "scope": scope,
                "category": category,
                "step_type": step.get("step_type"),
                "rows": rows,
                "prev_rows": before["rows"] if before else None,
                "prev_step_id": before["step_id"] if before else None,
                "step": step,
                "parsed": parsed,
            })
            previous[scope] = {"rows": rows, "step_id": flow[-1]["step_id"]}
        return flow

    def join_right_key_unique(self, step: Dict[str, Any], db_connection: Any) -> Optional[bool]:
        """True when the JOIN's right-hand key is unique (the join cannot multiply rows),
        False when it repeats, None when that cannot be checked (no connection / metadata,
        RIGHT / FULL / CROSS joins, no plain key equality)."""
        details = step.get("join_details") if isinstance(step.get("join_details"), dict) else {}
        pairs = details.get("key_pairs") or []
        relation = details.get("target_relation_sql")
        columns = [p.get("right_column") for p in pairs if isinstance(p, dict) and p.get("right_column")]
        if db_connection is None or not relation or not columns:
            return None
        if details.get("join_type") not in ("INNER", "LEFT"):
            return None
        quote = lambda name: '"' + str(name).replace('"', '""') + '"'  # noqa: E731
        cols = ", ".join(quote(c) for c in columns)
        not_null = " AND ".join(f"{quote(c)} IS NOT NULL" for c in columns)
        prefix = step.get("with_prefix") or ""
        src = f"{relation} AS _echo_rel"
        try:
            rows = db_connection.execute(f"{prefix}SELECT COUNT(*) FROM {src} WHERE {not_null}").fetchone()[0]
            distinct = db_connection.execute(
                f"{prefix}SELECT COUNT(*) FROM (SELECT DISTINCT {cols} FROM {src} WHERE {not_null}) AS _echo_d"
            ).fetchone()[0]
        except Exception:  # noqa: BLE001 - best-effort proof
            return None
        return int(rows) == int(distinct)

    def find_join_inflations(self, flow: List[Dict[str, Any]], min_ratio: float = 1.05) -> List[Dict[str, Any]]:
        """JOIN steps whose row count grew by more than ``min_ratio`` versus the previous step."""
        found = []
        for item in flow:
            before, after = item["prev_rows"], item["rows"]
            if item["category"] != "JOIN" or not before or before <= 0 or after <= before:
                continue
            ratio = round(after / before, 2)
            if ratio > min_ratio:
                found.append({**item, "ratio": ratio})
        return found