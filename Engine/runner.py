from Engine.base_detector import BaseBugDetector
import importlib
import pkgutil
import detectors


class EchoEngineRunner:
  """The core general engine that runs all detectors and falls back

  to general behavioral checks if no specific detector catches the bug.
  """

  def __init__(self, db_connection):
    self.db = db_connection

  def load_all_detectors(self):
    """Automatically loads all available Python detector plugins dynamically."""
    detector_plugins = []
    try:
      for _, module_name, _ in pkgutil.iter_modules(detectors.__path__):
        module = importlib.import_module(f"detectors.{module_name}")
        for attr_name in dir(module):
          attr = getattr(module, attr_name)
          if (
              isinstance(attr, type)
              and issubclass(attr, BaseBugDetector)
              and attr is not BaseBugDetector
          ):
            detector_plugins.append(attr())
    except Exception:
      pass
    return detector_plugins

  def analyze_query(self, query: str, parsed_steps: dict) -> list:
    """Analyzes the query using plugins, and applies general heuristics

    if a bug falls outside the predefined specific detectors.
    """
    detected_bugs = []
    detectors_list = self.load_all_detectors()

    for detector in detectors_list:
      try:
        report = detector.detect(parsed_steps, self.db)
        if report:
          detected_bugs.append(report)
      except Exception as e:
        print(f"Warning: Detector failed gracefully: {e}")

    if not detected_bugs:
      detected_bugs.append({
          "bug_type": "General Behavioral Anomaly",
          "severity": "Low/Unknown",
          "description": (
              "No specific pattern matched, but query executed successfully"
              " or yielded unexpected structural flow."
          ),
      })

    return detected_bugs
