
from typing import Any, Dict, List, Optional

from Engine.detectors.base_detector import BaseBugDetector


class LostRowsDetector(BaseBugDetector):
    """Detect LEFT JOIN operations effectively converted to INNER JOIN by WHERE filters."""

    detector_name = "LostRowsDetector"

    def detect(
        self,
        parsed_steps: List[Dict[str, Any]],
        step_trace: List[Dict[str, Any]],
        db_connection: Any = None,
        *args,
        **kwargs,
    ) -> Optional[Dict[str, Any]]:

        steps = self.as_dict_list(parsed_steps)

        successful_steps = [
            step for step in steps
            if str(step.get("status", "success")).lower() == "success"
        ]

        for join_step in successful_steps:
            join_details = join_step.get("join_details") or {}

            if str(join_details.get("join_type", "")).upper() != "LEFT":
                continue

            target_alias = join_details.get("target_alias")
            if not target_alias:
                continue

            join_scope = join_step.get("scope", "main")
            join_statement = join_step.get("statement_index")

            for where_step in successful_steps:
                if where_step.get("step_type") != "WHERE":
                    continue

                if where_step.get("scope", "main") != join_scope:
                    continue

                if (
                    join_statement is not None
                    and where_step.get("statement_index") != join_statement
                ):
                    continue

                where_details = where_step.get("where_details") or {}
                conjuncts = where_details.get("conjuncts") or []

                for conjunct in conjuncts:
                    if not isinstance(conjunct, dict):
                        continue

                    aliases = conjunct.get("aliases") or []

                    if (
                        target_alias in aliases
                        and conjunct.get("null_rejecting") is True
                    ):
                        return self.report(
                            bug_type="Lost Rows in a LEFT JOIN",
                            severity="High",
                            confidence="high",
                            suspicious_step_id=join_step.get("step_id"),
                            scope=join_scope,
                            description=(
                                "A WHERE condition references the right-hand "
                                "table of a LEFT JOIN and rejects NULL values. "
                                "This can effectively convert the LEFT JOIN "
                                "into an INNER JOIN, unintentionally dropping "
                                "unmatched rows."
                            ),
                            evidence={
                                "join_step_id": join_step.get("step_id"),
                                "where_step_id": where_step.get("step_id"),
                                "join_type": "LEFT",
                                "target_table": join_details.get("target_table"),
                                "target_alias": target_alias,
                                "where_condition": conjunct.get("sql"),
                                "null_rejecting": True,
                            },
                            suggested_fix=(
                                "If unmatched left-side rows must be preserved, "
                                "move the right-table filter from WHERE into "
                                "the LEFT JOIN ON condition."
                            ),
                        )

        return None
