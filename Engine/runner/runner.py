import importlib
import pkgutil
import time
from Engine.detectors import base_detector
from Engine import detectors


class EchoEngineRunner:
    """Enterprise-Grade Engine that handles Step Tracing on DuckDB,
    Performance Profiling, Multi-Column Metrics, and Dynamic Detector Plugins.
    """

    def __init__(self, db_connection):
        self.db = db_connection  

    def load_all_detectors(self):
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

            if step_type in ["SYNTAX_ERROR", "SECURITY_ERROR"]:
                error_msg = raw_step.get("error", "Invalid query structure or security violation.")
                step_trace.append({
                    "step_id": step_id,
                    "step_type": step_type,
                    "query": sql_query,
                    "error": error_msg,
                    "status": "failed",
                })
                continue

            try:
                start_time = time.perf_counter()
                cursor.execute(sql_query)
                result = cursor.fetchall()
                end_time = time.perf_counter()
                execution_time_ms = round((end_time - start_time) * 1000, 2)

                columns = [col[0] for col in cursor.description] if cursor.description else []
                row_count = len(result)
                
                columns_metrics = {}
                sum_value = None

                if row_count > 0 and columns:
                    for i, col_name in enumerate(columns):
                        try:
                            values = [row[i] for row in result if row[i] is not None]
                            if values and isinstance(values[0], (int, float)):
                                col_sum = sum(values)
                                columns_metrics[col_name] = col_sum
                                if sum_value is None:
                                    sum_value = col_sum
                        except Exception:
                            pass

                step_trace.append({
                    "step_id": step_id,          
                    "step_type": step_type,      
                    "query": sql_query,
                    "row_count": row_count,
                    "sum_value": sum_value,
                    "columns_metrics": columns_metrics,
                    "execution_time_ms": execution_time_ms,
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
        step_trace = self.run_step_trace(parsed_steps)
        detected_bugs = []

        failed_steps = [st for st in step_trace if st.get("status") == "failed"]
        if failed_steps:
            for fs in failed_steps:
                err_msg = fs.get("error", "Unknown execution failure.")
                bug_label = "SQL Syntax/Execution Error" if ("Parser Error" in err_msg or "syntax" in err_msg.lower() or "Security" in err_msg) else "Query Runtime Failure"
                
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

        return {
            "query": query,
            "step_trace": step_trace,
            "detected_bugs": detected_bugs,
        }