"""The seven old scenario scripts, rewritten as real tests.

Each old script built a small database, ran ECHO and PRINTED the result. These tests do
the same thing but CHECK the result, so a pull request fails if ECHO's behaviour breaks.

Scenario -> test:
    test_clean_scenario.py                 -> test_clean_filter_query_has_no_issues
    test_custom_scenario.py                -> test_normal_join_and_group_by_has_no_issues
    test_sara_scenario.py                  -> test_row_inflation_after_join_is_detected
    test_cartesian_scenario.py             -> test_join_without_on_clause_is_flagged
    test_aggregation_distortion_scenario   -> test_one_to_many_join_before_group_by_is_flagged
    test_complex_scenario.py               -> test_cte_join_inflation_is_detected
                                              test_typo_in_sql_is_reported_not_crashed
    test_orphan_scenario.py                -> test_orphan_records_are_detected (known gap, xfail)
"""

import duckdb
import pytest

from Engine.parser.parser import parse_sql_to_steps
from Engine.runner.runner import EchoEngineRunner

FAN_OUT = "Possible JOIN Fan-out"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def analyze(setup_statements, query):
    """Build a fresh in-memory database, run ECHO on `query`, return its report."""
    conn = duckdb.connect(":memory:")
    try:
        for statement in setup_statements:
            conn.execute(statement)
        return EchoEngineRunner(conn).analyze_query(query, parse_sql_to_steps(query))
    finally:
        conn.close()


def bug_types(report):
    return [bug.get("bug_type") for bug in report["detected_bugs"]]


def step_rows(report, step_type):
    """Row count of the first step of this type (BASE_TABLE, JOIN, WHERE, GROUP_BY...)."""
    return next(s["row_count"] for s in report["step_trace"] if s["step_type"] == step_type)


# --------------------------------------------------------------------------- #
# Queries that are fine: ECHO must stay quiet
# --------------------------------------------------------------------------- #
def test_clean_filter_query_has_no_issues():
    report = analyze(
        [
            "CREATE TABLE users (id INT, name VARCHAR, status VARCHAR)",
            "INSERT INTO users VALUES (1,'Ahmed','active'),(2,'Mohamed','active'),(3,'Ali','inactive')",
        ],
        "SELECT id, name FROM users WHERE status = 'active'",
    )
    assert report["verdict"] == "no_issues_found"
    assert bug_types(report) == []
    assert step_rows(report, "BASE_TABLE") == 3
    assert step_rows(report, "WHERE") == 2


def test_normal_join_and_group_by_has_no_issues():
    report = analyze(
        [
            "CREATE TABLE employees (id INT, name VARCHAR, department_id INT)",
            "CREATE TABLE departments (id INT, dept_name VARCHAR)",
            "INSERT INTO employees VALUES (1,'Alice',10),(2,'Bob',10),(3,'Charlie',20)",
            "INSERT INTO departments VALUES (10,'Engineering'),(20,'HR')",
        ],
        "SELECT d.dept_name, COUNT(e.id) AS emp_count "
        "FROM employees e JOIN departments d ON e.department_id = d.id "
        "GROUP BY d.dept_name",
    )
    assert report["verdict"] == "no_issues_found"
    assert bug_types(report) == []
    assert step_rows(report, "JOIN") == 3  # many employees to one department: no inflation
    assert step_rows(report, "GROUP_BY") == 2


# --------------------------------------------------------------------------- #
# Queries with a bug: ECHO must catch them
# --------------------------------------------------------------------------- #
def test_row_inflation_after_join_is_detected():
    """Sara's case: 10,000 users join 30,000 orders and become 30,000 rows."""
    report = analyze(
        [
            "CREATE TABLE users AS SELECT range AS id FROM range(10000)",
            "CREATE TABLE orders AS SELECT range % 3 AS user_id FROM range(30000)",
        ],
        "SELECT u.id, o.user_id FROM users u JOIN orders o ON u.id = o.user_id",
    )
    assert step_rows(report, "BASE_TABLE") == 10000
    assert step_rows(report, "JOIN") == 30000
    assert FAN_OUT in bug_types(report)


def test_join_without_on_clause_is_flagged():
    """A JOIN with no ON condition multiplies every row by every row (3 x 2 = 6)."""
    report = analyze(
        [
            "CREATE TABLE users (user_id INT, user_name VARCHAR)",
            "CREATE TABLE orders (order_id INT, user_id INT, amount DECIMAL)",
            "INSERT INTO users VALUES (1,'Ali'),(2,'Sara'),(3,'Omar')",
            "INSERT INTO orders VALUES (501,1,100),(502,2,250)",
        ],
        "SELECT u.user_name, o.order_id FROM users u JOIN orders o",
    )
    assert step_rows(report, "JOIN") == 6
    flagged = [b for b in report["detected_bugs"] if b["bug_type"] == FAN_OUT]
    assert flagged and flagged[0]["severity"] == "High"


def test_one_to_many_join_before_group_by_is_flagged():
    """One customer with 3 transactions: the JOIN turns 1 row into 3 before the SUM."""
    report = analyze(
        [
            "CREATE TABLE customers (customer_id INT, customer_tier VARCHAR)",
            "CREATE TABLE transactions (tx_id INT, customer_id INT, amount DECIMAL)",
            "INSERT INTO customers VALUES (1,'VIP')",
            "INSERT INTO transactions VALUES (10,1,500),(11,1,300),(12,1,200)",
        ],
        "SELECT c.customer_tier, SUM(t.amount) AS total_spent "
        "FROM customers c JOIN transactions t ON c.customer_id = t.customer_id "
        "GROUP BY c.customer_tier",
    )
    assert step_rows(report, "BASE_TABLE") == 1
    assert step_rows(report, "JOIN") == 3
    assert step_rows(report, "GROUP_BY") == 1
    assert FAN_OUT in bug_types(report)


def test_cte_join_inflation_is_detected():
    """A join inside a CTE inflates 1,000 products into 15,000 rows; the GROUP BY brings it back."""
    report = analyze(
        [
            "CREATE TABLE products AS SELECT range AS product_id, 'Product_' || range AS name FROM range(1000)",
            "CREATE TABLE sales AS SELECT range % 1000 AS product_id, (range % 5) + 1 AS quantity FROM range(15000)",
        ],
        "WITH regional_sales AS ("
        "  SELECT p.product_id, s.quantity FROM products p JOIN sales s ON p.product_id = s.product_id"
        ") "
        "SELECT product_id, SUM(quantity) AS total_qty FROM regional_sales GROUP BY product_id",
    )
    assert step_rows(report, "JOIN") == 15000
    assert step_rows(report, "GROUP_BY") == 1000
    assert FAN_OUT in bug_types(report)


def test_typo_in_sql_is_reported_not_crashed():
    """The old complex scenario had 'FORM' instead of 'FROM'. ECHO must report it, not crash."""
    report = analyze(
        [
            "CREATE TABLE products AS SELECT range AS product_id FROM range(10)",
        ],
        "SELECT product_id FORM products",
    )
    assert report["verdict"] == "error"
    assert "SQL Syntax Error" in bug_types(report)
    assert report["step_trace"][0]["status"] == "failed"


# --------------------------------------------------------------------------- #
# Known gap
# --------------------------------------------------------------------------- #
@pytest.mark.xfail(
    strict=True,
    reason="Known gap: ECHO has no detector for orphan rows (keys with no partner after a "
    "LEFT JOIN). The benchmark shows the same hole (LEFT JOIN row loss and NULL keys at 0%). "
    "When a detector is added this test starts passing: strict=True then makes it fail "
    "loudly so someone removes this xfail marker.",
)
def test_orphan_records_are_detected():
    """Sale 103 points at product 99, which does not exist. ECHO should warn about it."""
    report = analyze(
        [
            "CREATE TABLE products (product_id INT, product_name VARCHAR)",
            "CREATE TABLE sales (sale_id INT, product_id INT, quantity INT)",
            "INSERT INTO products VALUES (1,'Laptop'),(2,'Mouse')",
            "INSERT INTO sales VALUES (101,1,5),(102,2,3),(103,99,10)",
        ],
        "SELECT p.product_name, SUM(s.quantity) AS total_qty "
        "FROM sales s LEFT JOIN products p ON s.product_id = p.product_id "
        "GROUP BY p.product_name",
    )
    assert report["verdict"] == "issues_found"
