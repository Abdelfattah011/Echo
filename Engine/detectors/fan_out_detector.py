from Engine.detectors.base_detector import BaseBugDetector
from typing import List, Dict, Any, Optional


class FanOutDetector(BaseBugDetector):
    """Bulletproof JOIN Fan-out Detector.
    
    Ignores strict step types and strictly monitors raw sequential row inflation 
    across any step trace to instantly catch data explosions.
    """

    def detect(
        self, 
        parsed_steps: List[Dict[str, Any]], 
        step_trace: List[Dict[str, Any]], 
        db_connection: Any = None, 
        *args, 
        **kwargs
    ) -> Optional[Dict[str, Any]]:
        """Strictly compares sequential row counts to catch fan-out unconditionally."""
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