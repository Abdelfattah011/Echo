from Engine.detectors.base_detector import BaseBugDetector
from typing import List, Dict, Any, Optional


class FanOutDetector(BaseBugDetector):
    """Bulletproof JOIN Fan-out Detector.
    
    Monitors sequential row inflation on JOIN steps to catch data explosions
    without triggering false JOIN fan-out alerts on non-JOIN steps.
    """

    def _is_join_step(
        self,
        step: Dict[str, Any],
        parsed_steps: List[Dict[str, Any]],
        step_index: int,
    ) -> bool:
        step_type = str(step.get("step_type") or "").upper()
        stmt_kind = str(step.get("statement_kind") or "").upper()
        operation = str(step.get("operation") or "").upper()
        sql_text = str(step.get("sql") or step.get("query") or "").upper()

        if (
            step_type == "JOIN"
            or "JOIN" in stmt_kind
            or "JOIN" in operation
            or " JOIN " in f" {sql_text} "
        ):
            return True

        step_id = step.get("step_id")
        if isinstance(parsed_steps, list):
            for p_step in parsed_steps:
                if isinstance(p_step, dict):
                    if step_id and p_step.get("step_id") == step_id:
                        p_type = str(p_step.get("step_type") or p_step.get("type") or "").upper()
                        p_sql = str(p_step.get("sql") or p_step.get("query") or "").upper()
                        if "JOIN" in p_type or " JOIN " in f" {p_sql} ":
                            return True

            if 0 <= step_index < len(parsed_steps) and isinstance(parsed_steps[step_index], dict):
                p_step = parsed_steps[step_index]
                p_type = str(p_step.get("step_type") or p_step.get("type") or "").upper()
                p_sql = str(p_step.get("sql") or p_step.get("query") or "").upper()
                if "JOIN" in p_type or " JOIN " in f" {p_sql} ":
                    return True

        return False

    def detect(
        self, 
        parsed_steps: List[Dict[str, Any]], 
        step_trace: List[Dict[str, Any]], 
        db_connection: Any = None, 
        *args, 
        **kwargs
    ) -> Optional[Dict[str, Any]]:
        """Strictly compares sequential row counts to catch fan-out on JOIN steps."""
        if not isinstance(step_trace, list) or len(step_trace) < 2:
            return None

        for i in range(len(step_trace) - 1):
            curr_step = step_trace[i]
            next_step = step_trace[i + 1]

            curr_rows = curr_step.get("row_count")
            next_rows = next_step.get("row_count")
            next_id = next_step.get("step_id", f"step_{i+2}")

            if not isinstance(curr_rows, (int, float)) or not isinstance(next_rows, (int, float)):
                continue

            if curr_rows <= 0:
                continue

            if next_rows > curr_rows:
                if not self._is_join_step(next_step, parsed_steps, i + 1):
                    continue

                inflation_ratio = round(next_rows / curr_rows, 2)
                
                if inflation_ratio > 1.05:
                    severity = "High" if inflation_ratio >= 1.5 else "Medium"
                    
                    return self._format_bug_report(
                        bug_type="Possible JOIN Fan-out",
                        severity=severity,
                        description=(
                            f"Critical row inflation detected at '{next_id}'. "
                            f"Row count surged from {curr_rows:,} to {next_rows:,} "
                            f"(x{inflation_ratio} multiplier increase). This strongly indicates an unintended "
                            f"one-to-many data duplication or a missing/flawed ON condition in the JOIN operation."
                        ),
                    )

        return None