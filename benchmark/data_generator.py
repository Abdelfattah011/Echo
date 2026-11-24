"""Generate deterministic and synthetic data for the ECHO benchmark."""

import logging
import duckdb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def generate_benchmark_data(db_path: str = "echo_benchmark.duckdb") -> None:
    """Create benchmark tables, including deterministic detector fixtures."""
    conn = None
    try:
        logger.info("Generating benchmark data in '%s'", db_path)
        conn = duckdb.connect(db_path)
        conn.execute("BEGIN TRANSACTION")

        # Main synthetic dataset.
        conn.execute("""
            CREATE OR REPLACE TABLE customers AS
            SELECT
                range AS customer_id,
                'Customer_' || range AS customer_name,
                CASE WHEN range % 2 = 0 THEN 'Active'
                     ELSE 'Inactive' END AS status
            FROM range(3334)
        """)

        conn.execute("""
            CREATE OR REPLACE TABLE customer_addresses AS
            SELECT
                c.customer_id,
                'Address_' || a.addr_id AS address_id,
                'Street ' || (c.customer_id * 10 + a.addr_id) AS address_line
            FROM customers c
            CROSS JOIN (SELECT * FROM range(1, 4) AS t(addr_id)) a
        """)

        conn.execute("""
            CREATE OR REPLACE TABLE orders AS
            SELECT
                range AS order_id,
                CAST(RANDOM() * 3333 AS INTEGER) AS customer_id,
                ROUND(CAST(RANDOM() * 500 AS DECIMAL(10, 2)), 2) AS amount,
                CASE WHEN RANDOM() > 0.3 THEN 'Completed'
                     ELSE 'Pending' END AS status
            FROM range(1, 10001)
        """)

        # Deterministic NULL-key fixture: NULLs exist on both join sides,
        # alongside ordinary matching and unmatched keys.
        conn.execute("""
            CREATE OR REPLACE TABLE null_key_orders (
                order_id INTEGER,
                sales_rep_id INTEGER,
                amount DECIMAL(10, 2)
            )
        """)
        conn.execute("""
            INSERT INTO null_key_orders VALUES
                (1, 101, 150.00),
                (2, 102, 200.00),
                (3, NULL, 300.00),
                (4, 103, 250.00),
                (5, NULL, 175.00),
                (6, 104, 125.00),
                (7, 105, 400.00),
                (8, 999, 90.00),
                (9, 101, 220.00),
                (10, 106, 310.00),
                (11, NULL, 80.00),
                (12, 102, 450.00)
        """)
        conn.execute("""
            CREATE OR REPLACE TABLE null_key_employees (
                employee_id INTEGER,
                employee_name VARCHAR
            )
        """)
        conn.execute("""
            INSERT INTO null_key_employees VALUES
                (101, 'Ahmed'),
                (102, 'Mona'),
                (NULL, 'Unassigned'),
                (103, 'Omar'),
                (104, 'Sara'),
                (105, 'Youssef'),
                (106, 'Nour'),
                (107, 'Hana')
        """)

        # Type mismatch fixture: INTEGER order keys correspond to VARCHAR
        # customer keys, so type difference is tested independently of
        # accidental overlap in the randomly generated main orders table.
        conn.execute("""
            CREATE OR REPLACE TABLE customers_text_keys (
                customer_id VARCHAR,
                customer_name VARCHAR
            )
        """)
        conn.execute("""
            INSERT INTO customers_text_keys VALUES
                ('101', 'Ahmed'),
                ('102', 'Mona'),
                ('103', 'Omar'),
                ('104', 'Sara'),
                ('105', 'Youssef'),
                ('106', 'Nour')
        """)
        conn.execute("""
            CREATE OR REPLACE TABLE type_mismatch_orders (
                order_id INTEGER,
                customer_id INTEGER,
                amount DECIMAL(10, 2),
                status VARCHAR
            )
        """)
        conn.execute("""
            INSERT INTO type_mismatch_orders VALUES
                (1, 101, 150.00, 'Completed'),
                (2, 102, 200.00, 'Pending'),
                (3, 103, 300.00, 'Completed'),
                (4, 104, 250.00, 'Completed'),
                (5, 105, 175.00, 'Pending'),
                (6, 106, 125.00, 'Completed'),
                (7, 101, 400.00, 'Pending'),
                (8, 102, 90.00, 'Completed'),
                (9, 103, 220.00, 'Pending'),
                (10, 104, 310.00, 'Completed'),
                (11, 105, 80.00, 'Completed'),
                (12, 106, 450.00, 'Pending')
        """)

        # Deterministic LEFT JOIN fixture: customers 3, 5, 7, 10 and 12
        # intentionally have no matching order.
        conn.execute("""
            CREATE OR REPLACE TABLE left_join_customers (
                customer_id INTEGER,
                customer_name VARCHAR
            )
        """)
        conn.execute("""
            INSERT INTO left_join_customers VALUES
                (1, 'Customer_1'), (2, 'Customer_2'),
                (3, 'Customer_3'), (4, 'Customer_4'),
                (5, 'Customer_5'), (6, 'Customer_6'),
                (7, 'Customer_7'), (8, 'Customer_8'),
                (9, 'Customer_9'), (10, 'Customer_10'),
                (11, 'Customer_11'), (12, 'Customer_12')
        """)
        conn.execute("""
            CREATE OR REPLACE TABLE left_join_orders (
                order_id INTEGER,
                customer_id INTEGER,
                amount DECIMAL(10, 2),
                status VARCHAR
            )
        """)
        conn.execute("""
            INSERT INTO left_join_orders VALUES
                (1001, 1, 150.00, 'Completed'),
                (1002, 2, 200.00, 'Pending'),
                (1003, 4, 300.00, 'Completed'),
                (1004, 6, 250.00, 'Cancelled'),
                (1005, 8, 175.00, 'Pending'),
                (1006, 9, 125.00, 'Completed'),
                (1007, 11, 400.00, 'Completed'),
                (1008, 1, 90.00, 'Pending'),
                (1009, 4, 220.00, 'Completed')
        """)

        conn.execute("COMMIT")

        tables_to_verify = [
            "orders",
            "customers",
            "customer_addresses",
            "null_key_orders",
            "null_key_employees",
            "customers_text_keys",
            "type_mismatch_orders",
            "left_join_customers",
            "left_join_orders",
        ]
        logger.info("Benchmark data generated successfully:")
        for table_name in tables_to_verify:
            row_count = conn.execute(
                f"SELECT COUNT(*) FROM {table_name}"
            ).fetchone()[0]
            logger.info("  - %s: %s rows", table_name, row_count)

    except Exception:
        if conn:
            conn.execute("ROLLBACK")
        logger.exception("Failed to generate benchmark data.")
        raise
    finally:
        if conn:
            conn.close()
            logger.info("DuckDB connection closed.")


if __name__ == "__main__":
    generate_benchmark_data()