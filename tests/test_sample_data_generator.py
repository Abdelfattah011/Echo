
import duckdb

from Engine.fixes.sample_data_generator import generate_sample_data


def test_generate_sample_data():
    connection = duckdb.connect(":memory:")

    try:
        result = generate_sample_data(connection)

        # Check table counts
        assert connection.execute(
            "SELECT COUNT(*) FROM orders"
        ).fetchone()[0] == 10000

        assert connection.execute(
            "SELECT COUNT(*) FROM customers"
        ).fetchone()[0] == 1000

        assert connection.execute(
            "SELECT COUNT(*) FROM customer_addresses"
        ).fetchone()[0] == 3000

        # Check every customer has exactly 3 addresses
        addresses_per_customer = connection.execute("""
            SELECT customer_id, COUNT(*)
            FROM customer_addresses
            GROUP BY customer_id
            HAVING COUNT(*) != 3
        """).fetchall()

        assert addresses_per_customer == []

        assert result == {
            "customers": 1000,
            "orders": 10000,
            "customer_addresses": 3000,
        }

    finally:
        connection.close()
