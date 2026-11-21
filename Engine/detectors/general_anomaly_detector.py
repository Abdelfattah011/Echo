import logging
from typing import Any, Dict, List, Optional

from Engine.detectors.base_detector import SEVERITY_RANK, BaseBugDetector

log = logging.getLogger("echo.detectors.general")


class GeneralAnomalyDetector(BaseBugDetector):
    """Silent & Enterprise-Grade General Anomaly Detector.

    Acts as a smart guard:
    - Catches performance bottlenecks, data drops, and complex join workflows.
    - Returns None quietly when the query is clean and error-free to prevent noise.

    Checks (all configurable through the class constants below, or per call via kwargs):
      1. Performance Bottleneck / Heavy Step Warning   (slowest step above the threshold)
      2. Silent Data Drop / Empty Result Set            (rows -> 0 inside one pipeline)
      3. Severe Row Loss at JOIN                        (new: >= 95% of rows lost at a JOIN)
      4. Empty Source Table                             (new: a base table with 0 rows)
      5. Complex Aggregation & Join Workflow            (Low; only when nothing else is wrong)

    Noise control
    -------------
    * Row counts are compared only inside one scope of a SELECT pipeline. DML (UPDATE / DELETE
      touching 0 rows), DDL (TRUNCATE ...), admin commands, CTE / UNION results and statement
      boundaries are never treated as "data drops".
    * Row INFLATION belongs to FanOutDetector: check 5 stays silent when a JOIN inflated rows,
      so the two detectors never raise conflicting alerts for the same problem.
    * One report is returned (the most severe, earliest). Every other finding is attached
      to it as ``evidence["additional_findings"]`` instead of being lost.

    Robustness
    ----------
    * Every check runs in isolation: a failure inside one check (unexpected types, missing keys,
      partial / failed execution traces) is logged and skipped, it never breaks the analysis and
      never hides the findings of the other checks.
    """

    PERFORMANCE_THRESHOLD_MS = 3000
    SEVERE_LOSS_MIN_ROWS = 100       # ignore tiny samples
    SEVERE_LOSS_RATIO = 0.05         # rows_after / rows_before at or below this = severe loss
    REPORT_SEVERE_ROW_LOSS = True
    REPORT_EMPTY_SOURCE = True
    REPORT_COMPLEX_WORKFLOW = True
    MIN_INFLATION_RATIO = 1.05       # keep equal to FanOutDetector.MIN_INFLATION_RATIO
    MAX_ADDITIONAL = 10

    # Legacy names kept for compatibility (classification now lives in the base class).
    ignored_types = {"DDL", "TRUNCATE", "DROP", "CREATE", "ALTER"}
    ignored_kinds = {"TRUNCATE", "TRUNCATE_TABLE", "DROP_TABLE", "DROP_VIEW", "CREATE_TABLE", "ALTER_TABLE"}

    def detect(
        self,
        parsed_steps: List[Dict[str, Any]],
        step_trace: List[Dict[str, Any]],
        db_connection: Any = None,
        *args,
        **kwargs,
    ) -> Optional[Dict[str, Any]]:
        """Scans for general anomalies, returns None if everything is clean."""
        if not isinstance(step_trace, list) or not step_trace:
            return None
        parsed_steps = parsed_steps if isinstance(parsed_steps, list) else []

        threshold = self.to_number(kwargs.get("performance_threshold_ms"))
        if threshold is None or threshold < 0:
            threshold = self.PERFORMANCE_THRESHOLD_MS

        flow = self._safe(lambda: self.collect_row_flow(parsed_steps, step_trace), default=[])
        flow = [item for item in (flow if isinstance(flow, list) else []) if isinstance(item, dict)]

        findings: List[Dict[str, Any]] = []
        findings += self._safe(lambda: self._check_performance(step_trace, threshold), default=[])
        findings += self._safe(lambda: self._check_data_drop(flow), default=[])
        if self.REPORT_SEVERE_ROW_LOSS:
            findings += self._safe(lambda: self._check_severe_join_loss(flow), default=[])
        if self.REPORT_EMPTY_SOURCE:
            findings += self._safe(lambda: self._check_empty_source(flow), default=[])

        # The workflow note is informational: only when nothing else is wrong and FanOutDetector
        # has nothing to say about row inflation.
        if not findings and self.REPORT_COMPLEX_WORKFLOW:
            inflated = self._safe(
                lambda: self.find_join_inflations(flow, self.MIN_INFLATION_RATIO), default=None
            )
            if not inflated:
                findings += self._safe(
                    lambda: self._check_complex_workflow(parsed_steps, step_trace, db_connection), default=[]
                )

        findings = [f for f in findings if isinstance(f, dict)]
        if not findings:
            return None

        findings.sort(key=lambda f: (SEVERITY_RANK.get(f.get("severity"), 9), f.get("_index", 0)))
        primary, others = findings[0], findings[1:]
        extras = [
            {"bug_type": f.get("bug_type"), "severity": f.get("severity"),
             "suspicious_step_id": f.get("suspicious_step_id"), "description": f.get("description")}
            for f in others[: self.MAX_ADDITIONAL]
        ]
        for finding in findings:
            finding.pop("_index", None)
        if extras:
            evidence = primary.get("evidence")
            if not isinstance(evidence, dict):
                evidence = {}
                primary["evidence"] = evidence
            evidence["additional_findings"] = extras
        return primary

    # ------------------------------------------------------------------ #
    @staticmethod
    def _safe(func, default=None):
        """Run one check; log and swallow unexpected errors so the other checks still run."""
        try:
            result = func()
        except Exception:  # noqa: BLE001
            log.exception("GeneralAnomalyDetector check failed")
            return default
        return default if result is None else result

    def _finding(self, index, **fields) -> Dict[str, Any]:
        report = self.report(**fields)
        report["_index"] = index if isinstance(index, int) else 0
        return report

    # 1) Performance ---------------------------------------------------- #
    def _check_performance(self, step_trace, threshold) -> List[Dict[str, Any]]:
        slow = []
        for index, step in enumerate(step_trace):
            if not isinstance(step, dict) or str(step.get("status") or "").lower() == "skipped":
                continue
            elapsed = self.to_number(step.get("execution_time_ms"))
            if elapsed is not None and elapsed > threshold:
                slow.append((elapsed, index, step))
        if not slow:
            return []
        elapsed, index, step = max(slow, key=lambda item: item[0])
        step_id = step.get("step_id", "unknown")
        return [self._finding(
            index,
            bug_type="Performance Bottleneck / Heavy Step Warning",
            severity="Medium",
            description=(
                f"Performance lag detected at step '{step_id}'. "
                f"Execution took {elapsed}ms. Consider optimizing table scans or adding indexes."
            ),
            suspicious_step_id=step_id,
            scope=step.get("scope"),
            evidence={"execution_time_ms": elapsed, "threshold_ms": threshold, "slow_step_count": len(slow)},
            suggested_fix="Filter early, select only needed columns, and index or pre-aggregate the large join keys.",
        )]

    # 2) Silent data drop ----------------------------------------------- #
    def _check_data_drop(self, flow) -> List[Dict[str, Any]]:
        findings = []
        for item in flow:
            if item.get("category") == "BASE" or not item.get("prev_rows") or item.get("rows") != 0:
                continue
            step_id = item.get("step_id")
            findings.append(self._finding(
                item.get("index"),
                bug_type="Silent Data Drop / Empty Result Set",
                severity="High",
                description=(
                    f"Data flow dropped to 0 rows at step '{step_id}' "
                    f"(Type: {item.get('step_type') or 'UNKNOWN'}). "
                    f"Check for overly restrictive filters or mismatched join conditions."
                ),
                suspicious_step_id=step_id,
                scope=item.get("scope"),
                evidence={"rows_before": item.get("prev_rows"), "rows_after": 0,
                          "previous_step": item.get("prev_step_id"), "category": item.get("category")},
                suggested_fix="Inspect sample key values on both sides of the join and the filter constants.",
            ))
        return findings

    # 3) Severe row loss at a JOIN (new) -------------------------------- #
    def _check_severe_join_loss(self, flow) -> List[Dict[str, Any]]:
        findings = []
        for item in flow:
            before, after = item.get("prev_rows"), item.get("rows")
            if item.get("category") != "JOIN":
                continue
            if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
                continue
            if before < self.SEVERE_LOSS_MIN_ROWS:
                continue
            if after == 0 or after > before * self.SEVERE_LOSS_RATIO:  # 0 rows is the data-drop check
                continue
            lost = 100.0 * (1 - after / before)
            findings.append(self._finding(
                item.get("index"),
                bug_type="Severe Row Loss at JOIN",
                severity="Medium",
                description=(
                    f"Step '{item.get('step_id')}' kept only {after:,} of {before:,} rows ({lost:.1f}% lost). "
                    f"Almost no key values found a partner: check key formats, types, case and whitespace."
                ),
                suspicious_step_id=item.get("step_id"),
                scope=item.get("scope"),
                evidence={"rows_before": before, "rows_after": after, "rows_lost_pct": round(lost, 1)},
                suggested_fix="Compare sample key values from both tables; normalise types, trimming and case.",
                confidence="medium",
            ))
        return findings

    # 4) Empty source table (new) --------------------------------------- #
    def _check_empty_source(self, flow) -> List[Dict[str, Any]]:
        findings = []
        for item in flow:
            if item.get("category") != "BASE" or item.get("rows") != 0:
                continue
            step = item.get("step") if isinstance(item.get("step"), dict) else {}
            name = step.get("name") or step.get("step_id") or item.get("step_id")
            findings.append(self._finding(
                item.get("index"),
                bug_type="Empty Source Table",
                severity="Low",
                description=(
                    f"The source relation '{name}' returned 0 rows at step '{item.get('step_id')}', "
                    f"so every later step is trivially empty. Check that the table/sample was loaded."
                ),
                suspicious_step_id=item.get("step_id"),
                scope=item.get("scope"),
                evidence={"rows": 0},
                suggested_fix="Load data into the table (or use a representative sample) before analysing.",
                confidence="medium",
            ))
        return findings

    # 5) Complex aggregation & join workflow ---------------------------- #
    def _check_complex_workflow(self, parsed_steps, step_trace, db_connection=None) -> List[Dict[str, Any]]:
        """JOIN + GROUP BY / AGGREGATE inside the SAME scope (original check, scope-aware)."""
        parsed_list = self.as_dict_list(parsed_steps)
        parsed_by_id = {p.get("step_id"): p for p in parsed_list if p.get("step_id") is not None}
        scopes: Dict[str, Dict[str, Any]] = {}
        for index, entry in enumerate(step_trace):
            if not isinstance(entry, dict) or not self.is_step_ok(entry):
                continue
            parsed = parsed_by_id.get(entry.get("step_id"))
            if parsed is None and len(parsed_list) == len(step_trace):
                parsed = parsed_list[index]
            step = self._merge(entry, parsed)
            category = self.classify_step(step)
            scope = str(step.get("scope") or "main")
            info = scopes.setdefault(scope, {"join": None, "group": None, "joins": []})
            if category == "JOIN":
                info["joins"].append(step)
            if category == "JOIN" and info["join"] is None:
                info["join"] = (index, entry.get("step_id"))
            elif category == "AGGREGATE" and info["group"] is None:
                info["group"] = (index, entry.get("step_id"))
        for scope, info in scopes.items():
            if info["join"] and info["group"]:
                # Proven safe: every joined table is unique on its join key, so no JOIN can
                # multiply rows and the grain warning would only be noise.
                if all(self.join_right_key_unique(j, db_connection) is True for j in info["joins"]):
                    continue
                return [self._finding(
                    info["group"][0],
                    bug_type="Complex Aggregation & Join Workflow",
                    severity="Low",
                    description=(
                        "The query combines table JOIN operations with GROUP BY aggregations. "
                        "Ensure that grain alignment and aggregation keys are correctly structured "
                        "to prevent calculation distortion."
                    ),
                    suspicious_step_id=info["group"][1],
                    scope=scope,
                    evidence={"join_step": info["join"][1], "aggregate_step": info["group"][1]},
                    suggested_fix="Confirm each joined table is unique at the grain you aggregate on.",
                    confidence="low",
                )]
        return []