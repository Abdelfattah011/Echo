"""ECHO benchmark query generator.

Generates 60 intentionally broken SQL cases and 10 clean cases.
The NULL-key, LEFT JOIN, and type-mismatch cases use deterministic fixture
tables created by benchmark/data_generator.py.
"""

import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from Engine.parser.parser import parse_sql_to_steps


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

OUTPUT_FILE = Path(__file__).with_name("generated_cases.json")

BUG_TYPES = [
    "JOIN Fan-out",
    "Inflated Totals",
    "Lost Rows in a LEFT JOIN",
    "NULL Join Keys",
    "Key Type Mismatch",
]

CASES_PER_BUG = 12
CLEAN_CASES_COUNT = 10
EXPECTED_TOTAL_CASES = len(BUG_TYPES) * CASES_PER_BUG + CLEAN_CASES_COUNT


def get_expected_join_step(sql: str, join_index: int = 0) -> str:
    """Return the expected JOIN step by its position in the parsed query."""
    parsed_steps = parse_sql_to_steps(sql)
    join_steps = [
        step for step in parsed_steps
        if step.get("step_type") == "JOIN"
    ]

    if not 0 <= join_index < len(join_steps):
        raise ValueError(
            f"JOIN index {join_index} not found in query:\n{sql}"
        )

    step_id = join_steps[join_index].get("step_id")

    if not step_id:
        raise ValueError(
            f"Missing step_id for JOIN index {join_index}:\n{sql}"
        )

    return step_id


def build_case(
    case_id: str,
    bug_type: str,
    sql: str,
    is_clean: bool = False,
    expected_join_index: int = 0,
) -> Dict[str, Any]:
    clean_sql = sql.strip()
    expected_step = (
        None
        if is_clean
        else get_expected_join_step(clean_sql, expected_join_index)
    )

    return {
        "case_id": case_id,
        "bug_type": bug_type,
        "expected_step": expected_step,
        "sql": clean_sql,
        "is_clean": is_clean,
    }


def _append_join_cases(
    cases: List[Dict[str, Any]],
    prefix: str,
    bug_type: str,
    queries: List[str],
    expected_indexes: Optional[Dict[int, int]] = None,
) -> None:
    expected_indexes = expected_indexes or {}

    for index, sql in enumerate(queries, start=1):
        cases.append(
            build_case(
                f"{prefix}_{index:03d}",
                bug_type,
                sql,
                expected_join_index=expected_indexes.get(index, 0),
            )
        )


def generate_query_cases() -> List[Dict[str, Any]]:
    """Generate 60 broken SQL queries and 10 clean queries."""
    cases: List[Dict[str, Any]] = []

    # 1. JOIN Fan-out: customer_addresses has three rows per customer.
    fanout_queries = [
        """SELECT o.order_id, o.amount, a.address_line
           FROM orders o JOIN customer_addresses a
             ON o.customer_id = a.customer_id;""",
        """SELECT o.order_id, o.amount, a.address_id
           FROM orders o JOIN customer_addresses a
             ON o.customer_id = a.customer_id
           WHERE o.status = 'Completed' AND o.amount > 200;""",
        """SELECT o.order_id, o.amount, a.address_line
           FROM orders o JOIN customer_addresses a
             ON o.customer_id = a.customer_id
           WHERE o.status = 'Completed';""",
        """SELECT o.order_id, o.amount, a.address_line
           FROM orders o JOIN customer_addresses a
             ON o.customer_id = a.customer_id
           WHERE o.amount > 250;""",
        """SELECT c.customer_name, a.address_line
           FROM customers c JOIN customer_addresses a
             ON c.customer_id = a.customer_id;""",
        """SELECT c.customer_name, c.status, a.address_line
           FROM customers c JOIN customer_addresses a
             ON c.customer_id = a.customer_id
           WHERE c.status = 'Inactive';""",
        """SELECT o.order_id, c.customer_name, a.address_line
           FROM orders o
           JOIN customers c ON o.customer_id = c.customer_id
           JOIN customer_addresses a ON c.customer_id = a.customer_id;""",
        """SELECT o.order_id, o.amount, a.address_id
           FROM orders o JOIN customer_addresses a
             ON o.customer_id = a.customer_id
           WHERE o.order_id < 3000;""",
        """SELECT o.order_id, o.amount, a.address_line
           FROM orders o JOIN customer_addresses a
             ON o.customer_id = a.customer_id
           WHERE o.status = 'Pending';""",
        """SELECT c.status, a.address_line
           FROM customers c JOIN customer_addresses a
             ON c.customer_id = a.customer_id
           WHERE c.status = 'Active';""",
        """SELECT o.order_id, o.status, a.address_line
           FROM orders o JOIN customer_addresses a
             ON o.customer_id = a.customer_id
           WHERE o.status = 'Pending' AND o.amount > 100;""",
        """SELECT o.order_id, o.status, c.customer_name, a.address_line
           FROM orders o
           JOIN customers c ON o.customer_id = c.customer_id
           JOIN customer_addresses a ON c.customer_id = a.customer_id
           WHERE o.amount > 300;""",
    ]
    _append_join_cases(
        cases, "FANOUT", "JOIN Fan-out", fanout_queries,
        expected_indexes={7: 1, 12: 1},
    )

    # 2. Inflated Totals: aggregate values after joining to a 3:1 relation.
    totals_queries = [
        """SELECT SUM(o.amount) AS total_revenue
           FROM orders o JOIN customer_addresses a
             ON o.customer_id = a.customer_id
           WHERE o.order_id BETWEEN 1 AND 100;""",
        """SELECT SUM(o.amount) AS total_revenue
           FROM orders o JOIN customer_addresses a
             ON o.customer_id = a.customer_id
           WHERE o.order_id BETWEEN 101 AND 500;""",
        """SELECT SUM(o.amount) AS total_revenue
           FROM orders o JOIN customer_addresses a
             ON o.customer_id = a.customer_id
           WHERE o.status = 'Completed';""",
        """SELECT SUM(o.amount) AS pending_revenue
           FROM orders o JOIN customer_addresses a
             ON o.customer_id = a.customer_id
           WHERE o.status = 'Pending';""",
        """SELECT COUNT(o.order_id) AS order_count
           FROM orders o JOIN customer_addresses a
             ON o.customer_id = a.customer_id;""",
        """SELECT SUM(o.amount) AS total_revenue
            FROM orders o JOIN customer_addresses a
             ON o.customer_id = a.customer_id;""",
        """SELECT o.customer_id, SUM(o.amount) AS customer_revenue
           FROM orders o JOIN customer_addresses a
             ON o.customer_id = a.customer_id
           GROUP BY o.customer_id;""",
        """SELECT o.status, SUM(o.amount) AS revenue
           FROM orders o JOIN customer_addresses a
             ON o.customer_id = a.customer_id
           GROUP BY o.status;""",
        """SELECT COUNT(DISTINCT o.order_id) AS distinct_orders,
                  COUNT(o.order_id) AS joined_order_rows
           FROM orders o JOIN customer_addresses a
             ON o.customer_id = a.customer_id;""",
        """SELECT SUM(o.amount) AS high_value_revenue
           FROM orders o JOIN customer_addresses a
             ON o.customer_id = a.customer_id
           WHERE o.amount >= 400;""",
        """SELECT o.customer_id, SUM(o.amount) AS total_revenue
           FROM orders o JOIN customer_addresses a
             ON o.customer_id = a.customer_id
           WHERE o.status = 'Completed'
           GROUP BY o.customer_id;""",
        """SELECT SUM(o.amount) AS revenue, COUNT(*) AS joined_rows
           FROM orders o
           JOIN customers c ON o.customer_id = c.customer_id
           JOIN customer_addresses a ON c.customer_id = a.customer_id
           WHERE o.order_id <= 1200;""",
    ]
    _append_join_cases(cases, "TOTALS", "Inflated Totals", totals_queries,
                       expected_indexes={12: 1})

    # 3. Lost rows: right-side WHERE predicates reject NULL-extended rows.
    # Fixture data guarantees customers without matching orders.
    lost_rows_queries = [
        """SELECT c.customer_id, o.order_id, o.amount
           FROM left_join_customers c LEFT JOIN left_join_orders o
             ON c.customer_id = o.customer_id
           WHERE o.status = 'Completed';""",
        """SELECT c.customer_id, o.order_id
           FROM left_join_customers c LEFT JOIN left_join_orders o
             ON c.customer_id = o.customer_id
           WHERE o.status = 'Pending';""",
        """SELECT c.customer_id, o.amount
           FROM left_join_customers c LEFT JOIN left_join_orders o
             ON c.customer_id = o.customer_id
           WHERE o.amount > 100;""",
        """SELECT c.customer_id, o.amount
           FROM left_join_customers c LEFT JOIN left_join_orders o
             ON c.customer_id = o.customer_id
           WHERE o.amount IS NOT NULL;""",
        """SELECT c.customer_id, o.order_id
           FROM left_join_customers c LEFT JOIN left_join_orders o
             ON c.customer_id = o.customer_id
           WHERE o.order_id > 0;""",
        """SELECT c.customer_id, COUNT(o.order_id) AS order_count
           FROM left_join_customers c LEFT JOIN left_join_orders o
             ON c.customer_id = o.customer_id
           WHERE o.status <> 'Cancelled'
           GROUP BY c.customer_id;""",
        """SELECT c.customer_id, o.status
           FROM left_join_customers c LEFT JOIN left_join_orders o
             ON c.customer_id = o.customer_id
           WHERE LOWER(o.status) = 'completed';""",
        """SELECT c.customer_id, o.amount
           FROM left_join_customers c LEFT JOIN left_join_orders o
             ON c.customer_id = o.customer_id
           WHERE COALESCE(o.amount, 0) > 0;""",
        """SELECT c.customer_id, o.order_id
           FROM left_join_customers c LEFT JOIN left_join_orders o
             ON c.customer_id = o.customer_id
           WHERE o.status IN ('Completed', 'Pending');""",
        """SELECT c.customer_id, o.amount
           FROM left_join_customers c LEFT JOIN left_join_orders o
             ON c.customer_id = o.customer_id
           WHERE o.amount BETWEEN 100 AND 300;""",
        """SELECT c.customer_id, o.order_id
           FROM left_join_customers c LEFT JOIN left_join_orders o
             ON c.customer_id = o.customer_id
           WHERE NOT (o.status IS NULL);""",
        """SELECT c.customer_id, SUM(o.amount) AS revenue
           FROM left_join_customers c LEFT JOIN left_join_orders o
             ON c.customer_id = o.customer_id
           WHERE o.amount >= 150
           GROUP BY c.customer_id;""",
    ]
    _append_join_cases(cases, "LOST_ROWS", "Lost Rows in a LEFT JOIN",
                       lost_rows_queries)

    # 4. NULL Join Keys: deterministic fixture contains NULLs on both sides.
    null_key_queries = [
        """SELECT o.order_id, e.employee_name
           FROM null_key_orders o JOIN null_key_employees e
             ON o.sales_rep_id = e.employee_id;""",
        """SELECT o.order_id, o.sales_rep_id, e.employee_name
           FROM null_key_orders o LEFT JOIN null_key_employees e
             ON o.sales_rep_id = e.employee_id
           WHERE o.sales_rep_id IS NULL;""",
        """SELECT o.order_id, o.sales_rep_id, e.employee_name
           FROM null_key_orders o LEFT JOIN null_key_employees e
           ON o.sales_rep_id = e.employee_id
           WHERE e.employee_id IS NULL;""",
        """SELECT o.order_id, e.employee_name
           FROM null_key_orders o INNER JOIN null_key_employees e
             ON o.sales_rep_id = e.employee_id
           WHERE o.sales_rep_id IS NOT NULL;""",
        """SELECT COUNT(*) AS matched_rows
           FROM null_key_orders o JOIN null_key_employees e
             ON o.sales_rep_id = e.employee_id;""",
        """SELECT o.sales_rep_id, COUNT(*) AS order_count
           FROM null_key_orders o JOIN null_key_employees e
             ON o.sales_rep_id = e.employee_id
           GROUP BY o.sales_rep_id;""",
        """SELECT o.order_id, e.employee_name
           FROM null_key_orders o JOIN null_key_employees e
             ON o.sales_rep_id = e.employee_id
           WHERE o.order_id BETWEEN 1 AND 6;""",
        """SELECT o.order_id, e.employee_name
           FROM null_key_orders o JOIN null_key_employees e
             ON o.sales_rep_id = e.employee_id
           WHERE o.amount >= 150;""",
        """SELECT o.order_id, e.employee_name
           FROM null_key_orders o LEFT JOIN null_key_employees e
             ON o.sales_rep_id = e.employee_id
           WHERE o.sales_rep_id IS NOT NULL;""",
        """SELECT o.order_id, e.employee_name
           FROM null_key_orders o JOIN null_key_employees e
             ON o.sales_rep_id = e.employee_id
           WHERE e.employee_name <> 'Unassigned';""",
        """SELECT o.order_id, o.sales_rep_id, e.employee_id
           FROM null_key_orders o FULL OUTER JOIN null_key_employees e
             ON o.sales_rep_id = e.employee_id;""",
        """SELECT o.sales_rep_id, SUM(o.amount) AS total_amount
           FROM null_key_orders o JOIN null_key_employees e
             ON o.sales_rep_id = e.employee_id
           GROUP BY o.sales_rep_id;""",
    ]
    _append_join_cases(cases, "NULL_KEYS", "NULL Join Keys", null_key_queries)

    # 5. Key Type Mismatch: integer keys joined to VARCHAR keys.
    # Dedicated fixture has deterministic, logically matching key values.
    mismatch_queries = [
        """SELECT o.order_id, o.customer_id, c.customer_name
           FROM type_mismatch_orders o JOIN customers_text_keys c
             ON o.customer_id = c.customer_id;""",
        """SELECT o.order_id, c.customer_name
           FROM type_mismatch_orders o INNER JOIN customers_text_keys c
             ON o.customer_id = c.customer_id
           WHERE o.amount > 100;""",
        """SELECT o.customer_id, COUNT(*) AS order_count
           FROM type_mismatch_orders o JOIN customers_text_keys c
             ON o.customer_id = c.customer_id
           GROUP BY o.customer_id;""",
        """SELECT c.customer_name, SUM(o.amount) AS revenue
           FROM type_mismatch_orders o JOIN customers_text_keys c
             ON o.customer_id = c.customer_id
           GROUP BY c.customer_name;""",
        """SELECT o.order_id, c.customer_name
           FROM type_mismatch_orders o LEFT JOIN customers_text_keys c
             ON o.customer_id = c.customer_id
           WHERE o.order_id <= 5;""",
        """SELECT o.order_id, c.customer_name
           FROM type_mismatch_orders o JOIN customers_text_keys c
             ON o.customer_id = c.customer_id
           WHERE c.customer_name LIKE 'A%';""",
        """SELECT o.order_id, o.customer_id
           FROM type_mismatch_orders o JOIN customers_text_keys c
             ON o.customer_id = c.customer_id
           WHERE o.amount BETWEEN 100 AND 300;""",
        """SELECT COUNT(DISTINCT o.customer_id) AS matched_customers
           FROM type_mismatch_orders o JOIN customers_text_keys c
             ON o.customer_id = c.customer_id;""",
        """SELECT o.order_id, c.customer_name
           FROM type_mismatch_orders o JOIN customers_text_keys c
             ON o.customer_id = c.customer_id
           WHERE o.status = 'Completed';""",
        """SELECT o.customer_id, AVG(o.amount) AS average_amount
           FROM type_mismatch_orders o JOIN customers_text_keys c
             ON o.customer_id = c.customer_id
           GROUP BY o.customer_id;""",
        """SELECT o.order_id, c.customer_name
           FROM type_mismatch_orders o JOIN customers_text_keys c
             ON o.customer_id = c.customer_id
           WHERE o.order_id > 3 AND o.amount < 400;""",
        """SELECT c.customer_name, COUNT(o.order_id) AS orders_count
           FROM type_mismatch_orders o JOIN customers_text_keys c
             ON o.customer_id = c.customer_id
           GROUP BY c.customer_name
           HAVING COUNT(o.order_id) > 0;""",
    ]
    _append_join_cases(cases, "TYPE_MISMATCH", "Key Type Mismatch",
                       mismatch_queries)

    # 6. Clean queries: include safe LEFT JOIN predicates and intentional
    # one-to-many detail queries without unsafe aggregation.
    clean_queries = [
        """SELECT order_id, customer_id, amount
           FROM orders WHERE status = 'Completed';""",
        """SELECT customer_id, customer_name
           FROM customers WHERE status = 'Active';""",
        """SELECT COUNT(*) AS total_orders FROM orders;""",
        """SELECT SUM(amount) AS total_revenue FROM orders;""",
        """SELECT AVG(amount) AS average_order FROM orders;""",
        """SELECT o.order_id, c.customer_name
           FROM orders o JOIN customers c
             ON o.customer_id = c.customer_id;""",
        """SELECT c.customer_id, COUNT(o.order_id) AS order_count
           FROM customers c LEFT JOIN orders o
             ON c.customer_id = o.customer_id
           GROUP BY c.customer_id;""",
        """SELECT o.order_id, c.customer_name
           FROM orders o LEFT JOIN customers c
             ON o.customer_id = c.customer_id
           WHERE o.status = 'Completed';""",
        """SELECT customer_id, COUNT(*) AS address_count
           FROM customer_addresses GROUP BY customer_id;""",
        """SELECT order_id, amount FROM orders
           WHERE amount > 100 ORDER BY amount DESC;""",
    ]

    for index, sql in enumerate(clean_queries, start=1):
        cases.append(
            build_case(
                f"CLEAN_{index:03d}",
                "Clean",
                sql,
                is_clean=True,
            )
        )

    return cases


def validate_cases(cases: List[Dict[str, Any]]) -> None:
    """Validate counts, unique IDs, SQL uniqueness, labels, and JOIN steps."""
    counts = Counter(case["bug_type"] for case in cases)

    for bug_type in BUG_TYPES:
        if counts[bug_type] != CASES_PER_BUG:
            raise ValueError(
                f"{bug_type}: expected {CASES_PER_BUG}, "
                f"but generated {counts[bug_type]}"
            )

    if counts["Clean"] != CLEAN_CASES_COUNT:
        raise ValueError(
            f"Expected {CLEAN_CASES_COUNT} clean cases, "
            f"but generated {counts['Clean']}"
        )

    if len(cases) != EXPECTED_TOTAL_CASES:
        raise ValueError(
            f"Expected {EXPECTED_TOTAL_CASES} cases, generated {len(cases)}"
        )

    for case in cases:
      case_id = case["case_id"]

      if not case_id.strip():
        raise ValueError("Empty case_id detected.")
      
      if not isinstance(case["is_clean"], bool):
        raise ValueError(f"Invalid is_clean value in {case_id}")
      
      if case["is_clean"] != (case["bug_type"] == "Clean"):
        raise ValueError(
            f"Inconsistent bug_type/is_clean in {case_id}"
        )
      
      if not case["sql"].strip():
        raise ValueError(f"Empty SQL in case {case_id}")
      
      if not case["is_clean"] and not case["expected_step"]:
        raise ValueError(f"Missing expected step in {case_id}")
      
      if case["is_clean"] and case["expected_step"] is not None:
        raise ValueError(
            f"Clean case {case_id} must not have expected_step"
        )

    case_ids = [case["case_id"] for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("Duplicate benchmark case IDs detected.")

    # Exact duplicate SQL is a strong signal that the set lacks diversity.
    normalized_sql = [
        " ".join(case["sql"].lower().split()) for case in cases
    ]
    if len(normalized_sql) != len(set(normalized_sql)):
        raise ValueError("Duplicate SQL queries detected.")

    logger.info("Benchmark case validation passed.")


def save_cases(
    cases: List[Dict[str, Any]],
    output_file: Path = OUTPUT_FILE,
) -> None:
    with output_file.open("w", encoding="utf-8") as file:
        json.dump(cases, file, indent=4, ensure_ascii=False)
    logger.info("Saved %d benchmark cases to: %s", len(cases), output_file)


def main() -> None:
    logger.info("Starting Echo benchmark query generation...")
    cases = generate_query_cases()
    validate_cases(cases)
    save_cases(cases)

    counts = Counter(case["bug_type"] for case in cases)
    logger.info("Benchmark dataset generated successfully.")
    for bug_type in BUG_TYPES:
        logger.info("%s: %d queries", bug_type, counts[bug_type])
    logger.info("Clean queries: %d", counts["Clean"])
    logger.info("Total queries: %d", len(cases))


if __name__ == "__main__":
    main()
