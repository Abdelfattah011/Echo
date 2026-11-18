from Engine.detectors.base_detector import BaseBugDetector
from typing import List, Dict, Any, Optional


class GeneralAnomalyDetector(BaseBugDetector):
    """Silent & Enterprise-Grade General Anomaly Detector.
    
    Acts as a smart guard:
    - Catches performance bottlenecks, data drops, and complex join workflows.
    - Returns None quietly when the query is clean and error-free to prevent noise.
    """

    def detect(
        self, 
        parsed_steps: List[Dict[str, Any]], 
        step_trace: List[Dict[str, Any]], 
        db_connection: Any = None, 
        *args, 
        **kwargs
    ) -> Optional[Dict[str, Any]]:
        """Scans for general anomalies, returns None if everything is clean."""
        if not isinstance(step_trace, list) or not step_trace:
            return None

        for step in step_trace:
            exec_time = step.get("execution_time_ms", 0)
            if isinstance(exec_time, (int, float)) and exec_time > 3000:
                return self._format_bug_report(
                    bug_type="Performance Bottleneck / Heavy Step Warning",
                    severity="Medium",
                    description=(
                        f"Performance lag detected at step '{step.get('step_id', 'unknown')}'. "
                        f"Execution took {exec_time}ms. Consider optimizing table scans or adding indexes."
                    ),
                )

        for i in range(1, len(step_trace)):
            curr_step = step_trace[i]
            prev_step = step_trace[i - 1]
            curr_rows = curr_step.get("row_count")
            prev_rows = prev_step.get("row_count")

            if isinstance(curr_rows, (int, float)) and isinstance(prev_rows, (int, float)):
                if prev_rows > 0 and curr_rows == 0:
                    return self._format_bug_report(
                        bug_type="Silent Data Drop / Empty Result Set",
                        severity="High",
                        description=(
                            f"Data flow dropped to 0 rows at step '{curr_step.get('step_id', 'unknown')}' "
                            f"(Type: {curr_step.get('step_type', 'UNKNOWN')}). "
                            f"Check for overly restrictive filters or mismatched join conditions."
                        ),
                    )

        has_join = any(step.get("step_type") == "JOIN" for step in step_trace)
        has_group_by = any(step.get("step_type") == "GROUP_BY" for step in step_trace)

        if has_join and has_group_by:
            return self._format_bug_report(
                bug_type="Complex Aggregation & Join Workflow",
                severity="Low",
                description=(
                    "The query combines table JOIN operations with GROUP BY aggregations. "
                    "Ensure that grain alignment and aggregation keys are correctly structured "
                    "to prevent calculation distortion."
                ),
            )

        return None