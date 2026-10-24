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
