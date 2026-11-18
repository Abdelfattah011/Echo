<<<<<<< HEAD
from Engine.detectors.base_detector import BaseBugDetector


class FanOutDetector(BaseBugDetector):
    """Production-grade detector for identifying JOIN Fan-out anomalies

    by analyzing step-by-step execution row counts and tracing structural inflation.
    """

    def detect(self, parsed_steps, execution_results, db_connection=None, *args, **kwargs):
        """Analyzes execution trace results step-by-step to detect

        unexpected row count expansions caused by flawed JOIN operations.
        """
        if not isinstance(execution_results, list) or not execution_results:
            return None

        previous_rows = None
        suspicious_step_id = None
        inflation_ratio = 1.0

        for result in execution_results:
            if not isinstance(result, dict):
                continue

            step_type = str(result.get("step_type", "")).upper()
            step_id = result.get("step_id", "unknown_step")
            rows = result.get("row_count")

            if not isinstance(rows, (int, float)):
                continue

            if step_type in {"BASE_TABLE", "TABLE"} or previous_rows is None:
                previous_rows = rows
                continue

            if previous_rows is not None and rows > previous_rows:
                ratio = rows / previous_rows if previous_rows > 0 else 0

                if ratio > 1.0:
                    inflation_ratio = ratio
                    suspicious_step_id = step_id
                    
                    return {
                        "bug_type": "Possible JOIN Fan-out",
                        "severity": "High" if inflation_ratio >= 2.0 else "Medium",
                        "suspicious_step_id": suspicious_step_id,
                        "description": (
                            f"Row count exploded from {previous_rows} "
                            f"to {rows} at step '{suspicious_step_id}' after a JOIN "
                            f"(x{inflation_ratio:.2f} increase). This indicates potential "
                            "unintended row duplication due to one-to-many "
                            "or duplicate keys in the joined table."
                        ),
                    }

            previous_rows = rows

        return None
=======
from Engine.base_detector import BaseBugDetector


class FanOutDetector(BaseBugDetector):
  """Detects JOIN Fan-out anomalies where row counts unexpectedly multiply

  due to One-to-Many table relationships without proper aggregation or keys.
  """

  def detect(self, parsed_steps: list, execution_results: list) -> dict:

    bug_report = {
        "bug_type": "JOIN Fan-out",
        "severity": "High",
        "description": (
            "Detected unexpected row multiplication during table join."
        ),
    }

    return bug_report
>>>>>>> 11cfc2f119062be38a47a4b5278be27c2172ba14
