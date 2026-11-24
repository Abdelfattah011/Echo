
import logging
from typing import Any, Dict, List, Optional

from Engine.detectors.base_detector import BaseBugDetector

log = logging.getLogger("echo.detectors.null_join_keys")


class NullJoinKeysDetector(BaseBugDetector):
    """Detect NULL values in columns used as JOIN keys."""

    detector_name = "NullJoinKeysDetector"
    MAX_RELATED = 10

    @staticmethod
    def _q(name: str) -> str:
        return '"' + str(name).replace('"', '""') + '"'

    def _count_nulls(self, conn, prefix, relation_sql, column):
        """Return the number of NULL values in a JOIN key, or None if unavailable."""
        if not relation_sql or not column:
            return None

        try:
            query = (
                f"{prefix}SELECT COUNT(*) "
                f"FROM {relation_sql} AS _echo_null "
                f"WHERE {self._q(column)} IS NULL"
            )
            row = conn.execute(query).fetchone()
            return int(row[0]) if row else None
        except Exception:  # noqa: BLE001
            log.exception("Failed to inspect NULL values in JOIN key")
            return None

    def _check_join(self, item, conn):
        step = item["step"]
        details = step.get("join_details") or {}

        join_type = str(details.get("join_type") or "").upper()
        if join_type not in {"INNER", "LEFT", "RIGHT", "FULL"}:
            return []

        alias_map = step.get("alias_map") or {}
        prefix = step.get("with_prefix") or ""
        right_relation = details.get("target_relation_sql")
        target_alias = details.get("target_alias")
        findings = []

        for pair in details.get("key_pairs") or []:
            if not isinstance(pair, dict):
                continue

            left_alias = pair.get("left_alias")
            right_alias = pair.get("right_alias")
            left_col = pair.get("left_column")
            right_col = pair.get("right_column")

            if right_alias != target_alias:
                continue

            left_info = alias_map.get(left_alias) if left_alias else None

            # USING may omit the left alias. Resolve only when unambiguous.
            if left_info is None:
                left_aliases = details.get("left_aliases") or []
                if len(left_aliases) == 1:
                    left_alias = left_aliases[0]
                    left_info = alias_map.get(left_alias)

            if not (
                isinstance(left_info, dict)
                and left_info.get("relation_sql")
                and right_relation
                and left_col
                and right_col
            ):
                continue

            left_nulls = self._count_nulls(
                conn, prefix, left_info["relation_sql"], left_col
            )
            right_nulls = self._count_nulls(
                conn, prefix, right_relation, right_col
            )

            if left_nulls is None or right_nulls is None:
                continue

            if left_nulls == 0 and right_nulls == 0:
                continue

            findings.append({
                "join_step": item["step_id"],
                "join_type": join_type,
                "left_alias": left_alias,
                "left_column": left_col,
                "left_null_count": left_nulls,
                "right_alias": right_alias,
                "right_column": right_col,
                "right_null_count": right_nulls,
                "on_condition": details.get("on_condition"),
                "confirmed": True,
            })

        return findings

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
                findings.extend(self._check_join(item, db_connection))
            except Exception:  # noqa: BLE001
                log.exception("NullJoinKeysDetector check failed")

        if not findings:
            return None

        primary, others = findings[0], findings[1:]

        report = self.report(
            bug_type="NULL Join Keys",
            severity="Medium",
            confidence="medium",
            suspicious_step_id=primary["join_step"],
            scope=next(
                (
                    item.get("scope")
                    for item in flow
                    if item.get("step_id") == primary["join_step"]
                ),
                None,
            ),
            description=(
                "One or more JOIN key columns contain NULL values. "
                "NULL values do not match through ordinary SQL equality "
                "conditions, which may cause rows to be excluded from the JOIN."
            ),
            evidence={
                **primary,
                "related_findings": others[:self.MAX_RELATED],
            },
            suggested_fix=(
                "Review whether NULL join keys are expected. If they represent "
                "missing relationships, clean or handle them explicitly before "
                "joining. Do not replace NULL values with arbitrary keys."
            ),
        )

        return report
