
import duckdb
import pytest

from Engine.detectors.key_type_mismatch_detector import KeyTypeMismatchDetector
from Engine.parser.parser import parse_sql_to_steps


@pytest.fixture
def db_connection():
    conn = duckdb.connect(":memory:")

    conn.execute("CREATE TABLE orders (customer_id INTEGER)")
    conn.execute("CREATE TABLE customers (customer_id VARCHAR)")

    conn.execute("INSERT INTO orders VALUES (1)")
    conn.execute("INSERT INTO customers VALUES ('1')")

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

    return KeyTypeMismatchDetector().detect(steps, trace, conn)


def test_detects_on_join_type_mismatch(db_connection):
    sql = """
    SELECT *
    FROM orders o
    JOIN customers c
    ON o.customer_id = c.customer_id
    """

    result = run_detector(db_connection, sql)

    assert result is not None
    assert result["bug_type"] == "Join Key Type Mismatch"
    assert result["evidence"]["left_type"] == "INTEGER"
    assert result["evidence"]["right_type"] == "VARCHAR"


def test_detects_using_join_type_mismatch(db_connection):
    sql = """
    SELECT *
    FROM orders o
    JOIN customers c
    USING (customer_id)
    """

    result = run_detector(db_connection, sql)

    assert result is not None
    assert result["bug_type"] == "Join Key Type Mismatch"
    assert result["evidence"]["left_alias"] == "o"
    assert result["evidence"]["right_alias"] == "c"


def test_ignores_compatible_numeric_types(db_connection):
    db_connection.execute("DROP TABLE orders")
    db_connection.execute("DROP TABLE customers")

    db_connection.execute("CREATE TABLE orders (customer_id BIGINT)")
    db_connection.execute("CREATE TABLE customers (customer_id INTEGER)")

    db_connection.execute("INSERT INTO orders VALUES (1)")
    db_connection.execute("INSERT INTO customers VALUES (1)")

    sql = """
    SELECT *
    FROM orders o
    JOIN customers c
    ON o.customer_id = c.customer_id
    """

    result = run_detector(db_connection, sql)

    assert result is None
