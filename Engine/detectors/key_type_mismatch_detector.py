import logging
from typing import Any, Dict, List, Optional

from Engine.detectors.base_detector import BaseBugDetector

log = logging.getLogger("echo.detectors.key_type")

_NUMERIC = {
    "TINYINT", "SMALLINT", "INTEGER", "INT", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER",
    "UBIGINT", "UHUGEINT", "INT1", "INT2", "INT4", "INT8", "SHORT", "LONG", "FLOAT", "FLOAT4", "FLOAT8",
    "DOUBLE", "REAL", "DECIMAL", "NUMERIC",
}
_TEXT = {"VARCHAR", "TEXT", "STRING", "CHAR", "BPCHAR", "NVARCHAR"}
_DATE = {"DATE", "TIMESTAMP", "TIMESTAMPTZ", "TIMESTAMP WITH TIME ZONE", "DATETIME", "TIME"}
_BOOL = {"BOOLEAN", "BOOL"}


def type_category(type_name: Any) -> Optional[str]:
    """numeric | text | date | bool | None (unknown / not comparable, e.g. UUID, LIST, STRUCT)."""
    base = str(type_name or "").upper().split("(")[0].strip()
    if base in _NUMERIC:
        return "numeric"
    if base in _TEXT:
        return "text"
    if base in _DATE or base.startswith("TIMESTAMP"):
        return "date"
    if base in _BOOL:
        return "bool"
    return None


class KeyTypeMismatchDetector(BaseBugDetector):
    """Finds JOINs whose key columns have incompatible types (e.g. INTEGER = VARCHAR).

    Needs a database connection: the types are read from the real relations. Without one (or
    without join metadata) it stays silent. One report is returned (the earliest mismatching
    JOIN); the others are attached as ``evidence["related_findings"]`` with their full evidence
    so the fixer can repair every one of them in one run.
    """

    detector_name = "KeyTypeMismatchDetector"
    MAX_RELATED = 10

    def detect(
        self,
        parsed_steps: List[Dict[str, Any]],
        step_trace: List[Dict[str, Any]],
        db_connection: Any = None,
        *args,
        **kwargs,
    ) -> Optional[Dict[str, Any]]:
        if db_connection is None or not isinstance(step_trace, list) or not step_trace:
            return None
        flow = self.collect_row_flow(parsed_steps, step_trace)
        findings = []
        for item in flow:
            if item.get("category") != "JOIN":
                continue
            try:
                finding = self._check_join(item, db_connection)
            except Exception:  # noqa: BLE001 - a failing check must never hide other findings
                log.exception("KeyTypeMismatchDetector check failed")
                continue
            if finding:
                findings.append(finding)
        if not findings:
            return None
        primary, others = findings[0], findings[1:]
        if others:
            primary["evidence"]["related_findings"] = [
                {"suspicious_step_id": f["suspicious_step_id"], "severity": f["severity"], **f["_pair"]}
                for f in others[: self.MAX_RELATED]
            ]
        for finding in findings:
            finding.pop("_pair", None)
        return primary

    # ------------------------------------------------------------------ #
    @staticmethod
    def _q(name: str) -> str:
        return '"' + str(name).replace('"', '""') + '"'

    def _column_type(self, conn, prefix, relation_sql, column) -> Optional[str]:
        try:
            rows = conn.execute(
                f"DESCRIBE SELECT * FROM ({prefix}SELECT {self._q(column)} FROM {relation_sql} AS _echo_t) AS _echo_q"
            ).fetchall()
            return str(rows[0][1]) if rows else None
        except Exception:  # noqa: BLE001
            return None

    def _check_join(self, item, conn) -> Optional[Dict[str, Any]]:
        step = item["step"]
        details = step.get("join_details") if isinstance(step.get("join_details"), dict) else {}
        if details.get("join_type") not in ("INNER", "LEFT", "RIGHT", "FULL"):
            return None
        alias_map = step.get("alias_map") if isinstance(step.get("alias_map"), dict) else {}
        prefix = step.get("with_prefix") or ""
        right_relation = details.get("target_relation_sql")
        for pair in details.get("key_pairs") or []:
            left_alias, right_alias = pair.get("left_alias"), pair.get("right_alias")
            left_col, right_col = pair.get("left_column"), pair.get("right_column")

            target_alias = details.get("target_alias")
            if right_alias != target_alias:
                continue
            left_info = alias_map.get(left_alias) if left_alias is not None else None
            if left_info is None:
                left_aliases = details.get("left_aliases") or []

                # Safely support USING when there is exactly one left relation.
                if len(left_aliases) == 1:
                    left_alias = left_aliases[0]
                    left_info = alias_map.get(left_alias)
            if not (left_col and right_col and right_relation and isinstance(left_info, dict)
                    and left_info.get("relation_sql")):
                continue
            left_type = self._column_type(conn, prefix, left_info["relation_sql"], left_col)
            right_type = self._column_type(conn, prefix, right_relation, right_col)
            left_cat, right_cat = type_category(left_type), type_category(right_type)
            if not left_cat or not right_cat or left_cat == right_cat:
                continue
            step_id = item["step_id"]
            pair_evidence = {
                "join_step": step_id,
                "left_alias": left_alias, "left_column": left_col, "left_type": left_type,
                "left_category": left_cat,
                "right_alias": right_alias, "right_column": right_col, "right_type": right_type,
                "right_category": right_cat,
                "join_type": details.get("join_type"),
                "on_condition": details.get("on_condition"),
                "confirmed": True,
            }
            report = self.report(
                bug_type="Join Key Type Mismatch",
                severity="Medium",
                description=(
                    f"The join keys at '{step_id}' have incompatible types: {left_alias}.{left_col} is "
                    f"{left_type} but {right_alias}.{right_col} is {right_type}. Values may silently fail "
                    "to match (spaces, leading zeros, formats) or raise a conversion error on some rows."
                ),
                suspicious_step_id=step_id,
                evidence=pair_evidence,
                suggested_fix=(
                    f"Compare the keys with an explicit, safe conversion, e.g. TRY_CAST on the text side, "
                    f"or store both columns with the same type."
                ),
                scope=item.get("scope"),
                confidence="high",
            )
            report["_pair"] = {k: v for k, v in pair_evidence.items() if k != "confirmed"}
            return report
        return None