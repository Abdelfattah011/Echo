from typing import Any, Dict, List, Optional

from Engine.detectors.base_detector import SEVERITY_RANK, BaseBugDetector


class FanOutDetector(BaseBugDetector):
    """Bulletproof JOIN Fan-out Detector.

    Monitors sequential row inflation on JOIN steps to catch data explosions without
    triggering false JOIN fan-out alerts on non-JOIN steps (DML, DDL, UNION, filters,
    aggregations, CTE results ...).

    How it decides
    --------------
    1. A step is a JOIN only if the parser metadata says so (step_type / join_details),
       or - for legacy steps without metadata - if it is a plain SELECT whose SQL
       contains a JOIN. UPDATE / INSERT / DELETE / MERGE and UNION / INTERSECT / EXCEPT
       statements are never JOINs, even when their text contains the word JOIN.
    2. Row counts are compared only with the previous step *of the same scope*, so
       statement boundaries, CTE bodies and UNION branches are never mixed up.
    3. If a DB connection and join metadata are available, the cause is proven
       (is the right-hand key repeated?) and an expansion whose aggregates all sit on
       the "many" side is downgraded to Low as likely intentional.
    """

    # Legacy thresholds (unchanged).
    MIN_INFLATION_RATIO = 1.05
    HIGH_SEVERITY_RATIO = 1.5
    # Set True if a dedicated Cartesian-product detector is installed, to avoid double alerts.
    DEFER_CARTESIAN = False
    # False = stay completely silent when the expansion is proven intentional (aggregates only on the many side).
    REPORT_LIKELY_INTENTIONAL = True
    MAX_RELATED = 10

    # ------------------------------------------------------------------ #
    def detect(
        self,
        parsed_steps: List[Dict[str, Any]],
        step_trace: List[Dict[str, Any]],
        db_connection: Any = None,
        *args,
        **kwargs,
    ) -> Optional[Dict[str, Any]]:
        """Strictly compares sequential row counts to catch fan-out on JOIN steps."""
        if not isinstance(step_trace, list) or len(step_trace) < 2:
            return None

        flow = self.collect_row_flow(parsed_steps, step_trace)
        candidates = self.find_join_inflations(flow, self.MIN_INFLATION_RATIO)
        if not candidates:
            return None

        aggregates = {}
        for parsed in self.as_dict_list(parsed_steps):
            if self.classify_step(parsed) != "AGGREGATE":
                continue
            measures = parsed.get("measures") or []
            if not measures:
                continue
            scope = str(parsed.get("scope") or "main")
            if scope not in aggregates:
                aggregates[scope] = {"measures": []}
            aggregates[scope]["measures"].extend(measures)

        findings = [f for f in (self._build_finding(c, aggregates, db_connection) for c in candidates) if f]
        if not findings:
            return None

        # One report (contract): the most severe, earliest join. The others ride along as evidence.
        findings.sort(key=lambda f: (SEVERITY_RANK.get(f["severity"], 9), f["_index"]))
        primary, others = findings[0], findings[1:]
        related = [
            {"suspicious_step_id": f["suspicious_step_id"], "severity": f["severity"], **f["_summary"]}
            for f in others[: self.MAX_RELATED]
        ]
        for finding in findings:
            finding.pop("_index", None)
            finding.pop("_summary", None)
        if related:
            primary["evidence"]["related_findings"] = related
        return primary

    # ------------------------------------------------------------------ #
    def _build_finding(self, item, aggregates, conn) -> Optional[Dict[str, Any]]:
        step = item["step"]
        details = step.get("join_details") or {}
        duplicate_key_details = []
        pairs = details.get("key_pairs") or []
        right_columns = list(dict.fromkeys(
        pair["right_column"]
        for pair in pairs
        if pair.get("right_column")
        ))

        if right_columns:
          duplicate_key_details = self._get_duplicate_key_details(
            conn=conn,
            prefix=step.get("with_prefix", ""),
            relation_sql=details.get("target_relation_sql"),
            key_columns=right_columns,
        )
        cartesian = bool(details.get("implicit_cross") or details.get("explicit_cross")
                         or details.get("links_both_sides") is False)
        if self.DEFER_CARTESIAN and cartesian:
            return None

        before, after, ratio, step_id = item["prev_rows"], item["rows"], item["ratio"], item["step_id"]
        proof = self._prove_with_db(step, details, conn)
        impact = self._aggregate_impact(step, details, aggregates.get(item["scope"]), proof, conn)
        if impact == "benign" and not self.REPORT_LIKELY_INTENTIONAL:
            return None

        severity = "High" if ratio >= self.HIGH_SEVERITY_RATIO else "Medium"
        if impact == "impacted":
            severity = "High"
        elif impact == "benign":
            severity = "Low"

        if impact == "benign":
          description = (
            f"Expected one-to-many expansion detected at '{step_id}'. "
            f"Row count increased from {before} to {after} "
            f"(x{ratio:.2f}). The available aggregation metadata "
            "suggests this expansion is likely intentional."
            )

        elif impact == "impacted":
          description = (
            f"Potentially harmful JOIN fan-out detected at '{step_id}'. "
            f"Row count increased from {before} to {after} "
            f"(x{ratio:.2f}). The expansion may inflate aggregate results."
            )

        else:
          description = (
            f"Possible JOIN fan-out detected at '{step_id}'. "
            f"Row count increased from {before} to {after} "
            f"(x{ratio:.2f}). Further investigation is recommended."
            )

        evidence = {
            "join_step": step_id,
            "previous_step": item["prev_step_id"],
            "rows_before": before,
            "rows_after": after,
            "multiplication_factor": ratio,
            "join_type": details.get("join_type"),
            "on_condition": details.get("on_condition"),
            "confirmed": bool(proof.get("confirmed")),
            "likely_intentional": impact == "benign",
            "cartesian_suspected": cartesian,
            "duplicate_key_details": duplicate_key_details,
        }
        if proof.get("stats"):
            evidence["right_key_stats"] = proof["stats"]

        table = proof.get("table") or details.get("target_table") or "the joined table"
        if impact == "benign":
            suggested_fix = (
                "No fix is required if this one-to-many expansion is expected. "
                "Verify that the aggregation is performed at the intended level."
            )

        elif impact == "impacted":
            suggested_fix = (
                f"Review the JOIN with {table}. Check whether the join key should be unique. "
                "If multiple rows per key are expected, pre-aggregate the data before joining "
                "or aggregate at the correct level to avoid inflated totals."
            )

        else:
            suggested_fix = (
                f"Review the JOIN condition with {table}. Check whether duplicate keys or "
                "an incomplete ON condition are causing unexpected row multiplication."
            )

        report = self.report(
            bug_type="Possible JOIN Fan-out",
            severity=severity,
            description=description,
            suspicious_step_id=step_id,
            evidence=evidence,
            suggested_fix=suggested_fix,
            scope=item["scope"],
            confidence="high" if proof.get("confirmed") else "medium",
        )
        report["_index"] = item["index"]
        report["_summary"] = {"rows_before": before, "rows_after": after, "multiplication_factor": ratio}
        return report

    # ------------------------------------------------------------------ #
    # Optional DB proof (silently skipped when metadata or connection is missing)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _q(name: str) -> str:
        return '"' + str(name).replace('"', '""') + '"'

    @staticmethod
    def _scalar(conn, sql):
        try:
            row = conn.execute(sql).fetchone()
            return row[0] if row else None
        except Exception:  # noqa: BLE001 - the proof is best-effort and must never fail detection
            return None

    def _key_stats(self, conn, prefix, relation_sql, columns):
        cols = ", ".join(self._q(c) for c in columns)
        not_null = " AND ".join(f"{self._q(c)} IS NOT NULL" for c in columns)
        src = f"{relation_sql} AS _echo_rel"
        rows = self._scalar(conn, f"{prefix}SELECT COUNT(*) FROM {src} WHERE {not_null}")
        distinct = self._scalar(
            conn,
            f"{prefix}SELECT COUNT(*) FROM (SELECT DISTINCT {cols} FROM {src} WHERE {not_null}) AS _echo_d",
        )
        if rows is None or distinct is None:
            return None
        return {"key_rows": int(rows), "distinct_keys": int(distinct),
                "duplicate_rows": int(rows) - int(distinct)}

    def _prove_with_db(self, step, details, conn) -> Dict[str, Any]:
        """Is the right-hand join key repeated? ({} when it cannot be checked)."""
        pairs = details.get("key_pairs") or []
        relation = details.get("target_relation_sql")
        right_cols = [p["right_column"] for p in pairs if p.get("right_column")]
        if conn is None or not relation or not right_cols:
            return {}
        stats = self._key_stats(conn, step.get("with_prefix") or "", relation, right_cols)
        if stats is None:
            return {}
        duplicated = stats["duplicate_rows"] > 0
        return {
            "stats": stats,
            "confirmed": duplicated,
            "table": details.get("target_table") or details.get("target_alias"),
            "key_text": ", ".join(right_cols) if duplicated else None,
            "key_rows": stats["key_rows"],
            "distinct_keys": stats["distinct_keys"],
        }

    def _left_key_duplicated(self, step, details, conn) -> Optional[bool]:
        """True / False when the LEFT key's uniqueness can be checked, else None."""
        pairs = details.get("key_pairs") or []
        alias_map = step.get("alias_map") or {}
        left_aliases = {p.get("left_alias") for p in pairs}
        if conn is None or len(left_aliases) != 1 or None in left_aliases:
            return None
        info = alias_map.get(next(iter(left_aliases)))
        if not info or not info.get("relation_sql"):
            return None
        stats = self._key_stats(conn, step.get("with_prefix") or "", info["relation_sql"],
                                [p["left_column"] for p in pairs])
        return None if stats is None else stats["duplicate_rows"] > 0

    def _aggregate_impact(self, step, details, aggregate, proof, conn) -> str:
        """Classify whether JOIN row multiplication can affect aggregates."""
        measures = (aggregate or {}).get("measures") or []
        if not measures:
            return "unknown"
        left_aliases = set(details.get("left_aliases") or [])
        target_alias = details.get("target_alias")
        relevant_measures = []
        for measure in measures:
            func = str(measure.get("func") or "").upper()
            # Duplicating rows alone does not change MIN or MAX.
            if func in {"MIN", "MAX"}:
                continue
            # These aggregate functions can be affected by duplicated rows.
            if func in {"SUM", "AVG", "COUNT"}:
                relevant_measures.append(measure)
        # No aggregate in this scope is affected by row duplication alone.
        if not relevant_measures:
            return "benign"

        owners = set()
        for measure in relevant_measures:
            tables = measure.get("tables")
            if not tables:
                return "unknown"
            owners.update(tables)

        # A measure using a left-side table can be multiplied by the JOIN.
        if owners & left_aliases:
            return "impacted"
        # A measure using the right-side table needs additional checks.
        if target_alias and target_alias in owners:
            if not proof.get("confirmed"):
                return "unknown"
            left_dup = self._left_key_duplicated(step, details, conn)
            if left_dup is False:
                return "benign"
            elif left_dup is True:
                return "impacted"
            return "unknown"
        return "unknown"

 ##   
    
    def _get_duplicate_key_details(self, conn, prefix, relation_sql, key_columns):
        """Return repeated join-key values and their counts."""
        if conn is None or not relation_sql or not key_columns:
            return []

        cols = ", ".join(self._q(c) for c in key_columns)
        not_null = " AND ".join(
            f"{self._q(c)} IS NOT NULL" for c in key_columns
        )
        query = f"""
            {prefix or ""}
            SELECT
                {cols},
                COUNT(*) AS occurrence_count
            FROM ({relation_sql}) AS _echo_duplicates
            WHERE {not_null}
            GROUP BY {cols}
            HAVING COUNT(*) > 1
            ORDER BY occurrence_count DESC
            LIMIT 10
        """

        try:
            cursor = conn.execute(query)
            rows = cursor.fetchall()
            names = [desc[0] for desc in cursor.description]
            return [dict(zip(names, row)) for row in rows]
        except Exception:
            return []

    