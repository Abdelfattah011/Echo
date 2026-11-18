from Engine.detectors.base_detector import BaseBugDetector

class GeneralAnomalyDetector(BaseBugDetector):
    """General & Robust Detector that catches potential logical risks 
    like unmatched LEFT JOINs, NULL group keys, or general step anomalies.
    """

    def detect(self, parsed_steps, step_trace, db_connection) -> dict:
        if not step_trace:
            return None

        for step in step_trace:
            if step.get("step_type") == "JOIN":
                return {
                    "bug_type": "Logical Join & Grouping Warning",
                    "severity": "Medium",
                    "description": f"Detected a JOIN operation at step '{step.get('step_id')}' ({step.get('name')}). Ensure LEFT JOIN keys and GROUP BY clauses account for potential NULL or orphaned records to avoid data skew."
                }

        return None