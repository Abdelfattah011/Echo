<<<<<<< HEAD
import importlib
import pkgutil
from Engine.detectors import base_detector
from Engine import detectors


class EchoEngineRunner:
    """The core general engine that handles Step Tracing on DuckDB,
    runs dynamic detectors, and falls back to general behavioral checks.
    Supports both list and dict formats for parsed steps dynamically,
    with robust general error handling for both parsing and database execution.
    """

    def __init__(self, db_connection):
        self.db = db_connection  

    def load_all_detectors(self):
        """Automatically loads all available Python detector plugins dynamically."""
        detector_plugins = []
        try:
            for _, module_name, _ in pkgutil.iter_modules(detectors.__path__):
                module = importlib.import_module(f"Engine.detectors.{module_name}")
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if (
                        isinstance(attr, type)
                        and issubclass(attr, base_detector.BaseBugDetector)
                        and attr is not base_detector.BaseBugDetector
                    ):
                        detector_plugins.append(attr())
        except Exception as e:
            print(f"Warning: Failed to load some detectors: {e}")
        return detector_plugins

    def run_step_trace(self, parsed_steps) -> list:
        """M1 Core Task: Runs each parsed step on DuckDB and records
        row count, SUM, and step metadata dynamically (supports list or dict).
        Handles both parsing syntax errors and database execution failures gracefully.
        """
        step_trace = []
        cursor = self.db.cursor()

        steps_iterable = []
        if isinstance(parsed_steps, list):
            for step in parsed_steps:
                if isinstance(step, dict):
                    sid = step.get("step_id", "unknown_step")
                    squery = step.get("executable_query") or step.get("query")
                    stype = step.get("step_type", "QUERY")
                    steps_iterable.append((sid, squery, stype, step))
        elif isinstance(parsed_steps, dict):
            for sid, squery in parsed_steps.items():
                steps_iterable.append((sid, squery, "QUERY", {"step_id": sid, "executable_query": squery, "step_type": "QUERY"}))

        for step_id, sql_query, step_type, raw_step in steps_iterable:
            if not sql_query:
                continue

            if step_type == "SYNTAX_ERROR":
                error_msg = raw_step.get("error", "Invalid SQL syntax detected.")
                step_trace.append({
                    "step_id": step_id,
                    "step_type": step_type,
                    "query": sql_query,
                    "error": error_msg,
                    "status": "failed",
                })
                continue

            try:
                cursor.execute(sql_query)
                result = cursor.fetchall()
                columns = [col[0] for col in cursor.description] if cursor.description else []
                
                row_count = len(result)
                
                sum_value = None
                if row_count > 0 and columns:
                  for i, col_name in enumerate(columns):
                    try:
                      values = [row[i] for row in result if row[i] is not None]
                      if values and isinstance(values[0], (int, float)):
                        sum_value = sum(values)
                        break
                    except Exception:
                      pass

                step_trace.append({
                    "step_id": step_id,          
                    "step_type": step_type,      
                    "query": sql_query,
                    "row_count": row_count,
                    "sum_value": sum_value,
                    "status": "success",
                })
            except Exception as e:
                step_trace.append({
                    "step_id": step_id,
                    "step_type": step_type,
                    "query": sql_query,
                    "error": str(e),
                    "status": "failed",
                })

        return step_trace

    def analyze_query(self, query: str, parsed_steps) -> dict:
        """Analyzes the query by first generating the Step Trace,
        then passing it to detector plugins, and applying general heuristics.
        General & Robust: Catches both explicit parser errors and runtime DB failures early.
        """
        step_trace = self.run_step_trace(parsed_steps)

        detected_bugs = []

        failed_steps = [st for st in step_trace if st.get("status") == "failed"]
        if failed_steps:
            for fs in failed_steps:
                err_msg = fs.get("error", "Unknown execution failure.")
                bug_label = "SQL Syntax/Execution Error" if ("Parser Error" in err_msg or "Binder Error" in err_msg or "Catalog Error" in err_msg or "syntax" in err_msg.lower() or "form" in err_msg.lower()) else "Query Runtime Failure"
                
                detected_bugs.append({
                    "bug_type": bug_label,
                    "severity": "High",
                    "description": f"Step '{fs.get('step_id')}' failed. Details: {err_msg}",
                })
            return {
                "query": query,
                "step_trace": step_trace,
                "detected_bugs": detected_bugs,
            }

        detectors_list = self.load_all_detectors()

        for detector in detectors_list:
          try:
            report = detector.detect(parsed_steps, step_trace, self.db)
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
                  " through all step traces."
              ),
          })

        return {
            "query": query,
            "step_trace": step_trace,
            "detected_bugs": detected_bugs,
        }
=======
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
>>>>>>> 11cfc2f119062be38a47a4b5278be27c2172ba14
