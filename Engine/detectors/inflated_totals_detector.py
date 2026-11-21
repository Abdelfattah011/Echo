from typing import Any, Dict, List, Optional
from Engine.detectors.base_detector import BaseBugDetector


class InflatedTotalsDetector(BaseBugDetector):
    """Detect aggregate measures that may be duplicated by JOIN expansion."""

    MIN_INFLATION_RATIO = 1.05

    def _execute_measure_query(self, db_connection, query):
        """Execute a parser-generated aggregate query and return its value."""
        if db_connection is None or not query:
            return None
        try:
            result = db_connection.execute(query).fetchone()
            if not result:
                return None
            return result[0]
        except Exception:
            return None

    @staticmethod
    def _get_measure_value(step):
        """Get the aggregate value collected by the Runner."""
        if not step:
            return None

        value = step.get("measure_value")
        if value is None:
            value = step.get("sum_value")
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def detect(
        self,
        parsed_steps: List[Dict[str, Any]],
        step_trace: List[Dict[str, Any]],
        db_connection=None,
        *args,
        **kwargs,
    ) -> Optional[Dict[str, Any]]:
        
        print("### INFLATED TOTALS DETECTOR RUNNING ###", flush=True)
        flow = self.collect_row_flow(parsed_steps, step_trace)

        if not flow:
            return None

        # Collect aggregate measures by scope.
        aggregates_by_scope = {}

        for step in self.as_dict_list(parsed_steps):
            scope = str(step.get("scope") or "main")
            group_details = step.get("group_details") or {}
            measures = step.get("measures") or group_details.get("measures") or []

            if measures:
                aggregates_by_scope.setdefault(scope, []).extend(measures)

        findings = []

        flow_by_id = {
            item["step_id"]: item
            for item in flow
            if item.get("step_id")
        }

        for item in flow:
            if item["category"] != "JOIN":
                continue
            before_rows = item.get("prev_rows")
            after_rows = item.get("rows")

            if before_rows is None or after_rows is None or before_rows == 0:
                continue
            scope = item["scope"]
            measures = aggregates_by_scope.get(scope, [])

            if not measures:
                continue

            step = item["step"]
            details = step.get("join_details") or {}
            left_aliases = set(details.get("left_aliases") or [])
            previous_item = flow_by_id.get(item.get("prev_step_id"))

            if previous_item is None:
                continue

            before_value = self._get_measure_value(previous_item["step"])
            after_value = self._get_measure_value(step)

            if before_value is None or after_value is None:
                continue

            # Current Parser generates a measure_query for the primary SUM.
            measure = next(
                (
                m for m in measures
                if str(m.get("func") or "").upper() == "SUM"
                ),
                None,
            )

            if measure is None:
                continue

            measure_tables = set(measure.get("tables") or [])

            if not measure_tables.intersection(left_aliases):
                continue

            if before_value == 0:
                continue

            ratio = abs(after_value) / abs(before_value)

            if ratio < self.MIN_INFLATION_RATIO:
                continue

            findings.append({
                "item": item,
                "measure": measure,
                "ratio": ratio,
                "before_value": before_value,
                "after_value": after_value,
                "before_rows": before_rows,
                "after_rows": after_rows,
            })
        if not findings:
            return None
        # Select the finding with the largest observed expansion.
        finding = max(findings, key=lambda x: x["ratio"])

        item = finding["item"]
        measure = finding["measure"]
        ratio = finding["ratio"]
        step_id = item["step_id"]

        before = finding["before_rows"]
        after = finding["after_rows"]
        before_value = finding["before_value"]
        after_value = finding["after_value"]

        func = measure.get("func", "AGGREGATE")
        expression = measure.get("expression", "unknown expression")

        description = (
            f"Actual aggregate inflation detected at '{step_id}'. "
            f"{func}({expression}) increased from {before_value} "
            f"to {after_value} (x{ratio:.2f}) after the JOIN. "
            f"Row count changed from {before} to {after}."
        )

        suggested_fix = (
            "Review the JOIN cardinality and aggregation grain. "
            "If this JOIN duplicates rows containing the measure, "
            "pre-aggregate the measure before joining or aggregate "
            "at the correct level."
        )

        return self.report(
            bug_type="Inflated Aggregate",
            severity="High" if ratio >= 2 else "Medium",
            description=description,
            suspicious_step_id=step_id,
            evidence={
                "aggregate_function": func,
                "aggregate_expression": expression,
                "aggregate_alias": measure.get("alias"),
                "measure_tables": measure.get("tables"),
                "aggregate_before": before_value,
                "aggregate_after": after_value,
                "aggregate_inflation_factor": ratio,
                "rows_before": before,
                "rows_after": after,
                "row_multiplication_factor": after / before,
                "previous_step": item["prev_step_id"],
            },
            suggested_fix=suggested_fix,
            scope=item["scope"],
            confidence="high",
        )
