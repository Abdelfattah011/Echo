
import duckdb
import pytest

from Engine.detectors.null_join_keys_detector import NullJoinKeysDetector
from Engine.parser.parser import parse_sql_to_steps


@pytest.fixture
def db_connection():
    conn = duckdb.connect(":memory:")

    conn.execute("CREATE TABLE orders (customer_id INTEGER)")
    conn.execute("CREATE TABLE customers (customer_id INTEGER)")

    conn.execute("INSERT INTO orders VALUES (1), (2), (NULL)")
    conn.execute("INSERT INTO customers VALUES (1), (2), (NULL)")

    yield conn
    conn.close()


def run_detector(conn, sql):
    steps = parse_sql_to_steps(sql)
    conn.execute(sql)

    trace = [
        {
            "step_id": step["step_id"],
            "step_type": step.get("step_type"),
            "row_count": 1,
            "status": "success",
            "scope": step.get("scope", "main"),
        }
        for step in steps
    ]

    return NullJoinKeysDetector().detect(steps, trace, conn)


def test_detects_null_join_keys(db_connection):
    sql = """
    SELECT *
    FROM orders o
    JOIN customers c
    ON o.customer_id = c.customer_id
    """

    result = run_detector(db_connection, sql)

    assert result is not None
    assert result["bug_type"] == "NULL Join Keys"
    assert result["evidence"]["left_null_count"] == 1
    assert result["evidence"]["right_null_count"] == 1


def test_ignores_join_keys_without_nulls(db_connection):
    db_connection.execute("UPDATE orders SET customer_id = 3 WHERE customer_id IS NULL")
    db_connection.execute("UPDATE customers SET customer_id = 3 WHERE customer_id IS NULL")

    sql = """
    SELECT *
    FROM orders o
    JOIN customers c
    ON o.customer_id = c.customer_id
    """

    result = run_detector(db_connection, sql)

    assert result is None


def test_detects_null_keys_with_using(db_connection):
    sql = """
    SELECT *
    FROM orders o
    JOIN customers c
    USING (customer_id)
    """

    result = run_detector(db_connection, sql)

    assert result is not None
    assert result["bug_type"] == "NULL Join Keys"
    assert result["evidence"]["left_null_count"] == 1
    assert result["evidence"]["right_null_count"] == 1
