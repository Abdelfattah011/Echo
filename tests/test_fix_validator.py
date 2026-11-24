
import duckdb

from Engine.fixes.fix_generator import generate_fanout_fix
from Engine.fixes.fix_validator import validate_fanout_fix


def create_test_data():
    connection = duckdb.connect(":memory:")

    connection.execute("""
        CREATE TABLE orders AS
        SELECT
            order_id,
            ((order_id - 1) % 1000) + 1 AS customer_id,
            100.0 AS amount
        FROM generate_series(1, 10000) AS s(order_id)
    """)

    connection.execute("""
        CREATE TABLE customer_addresses AS
        SELECT
            customer_id,
            address_number,
            'City ' || address_number AS city
        FROM generate_series(1, 1000) AS c(customer_id)
        CROSS JOIN generate_series(1, 3) AS a(address_number)
    """)

    return connection


def test_validator_passes_for_fixed_query():
    connection = create_test_data()

    original_sql = """
        SELECT o.order_id, o.customer_id, o.amount, a.city
        FROM orders o
        JOIN customer_addresses a
        ON o.customer_id = a.customer_id
    """

    fixed_sql = generate_fanout_fix(
        sql_query=original_sql,
        join_table="customer_addresses",
        join_key="customer_id",
        order_by="address_number",
    )

    result = validate_fanout_fix(connection, fixed_sql)

    assert result["checks"]["row_count_matches"] is True
    assert result["checks"]["total_matches"] is True
    assert result["checks"]["no_duplicate_ids"] is True
    assert result["passed"] is True

    connection.close()


def test_validator_fails_for_original_query():
    connection = create_test_data()

    original_sql = """
        SELECT o.order_id, o.customer_id, o.amount, a.city
        FROM orders o
        JOIN customer_addresses a
        ON o.customer_id = a.customer_id
    """

    result = validate_fanout_fix(connection, original_sql)

    assert result["checks"]["row_count_matches"] is False
    assert result["checks"]["total_matches"] is False
    assert result["checks"]["no_duplicate_ids"] is False
    assert result["passed"] is False

    connection.close()
