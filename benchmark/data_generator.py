import logging
import duckdb

# Configure professional logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def generate_benchmark_data(db_path: str = "echo_benchmark.duckdb") -> None:
  """Generates high-fidelity synthetic benchmark data using DuckDB for Echo Engine.

  Creates structured datasets (10,000 orders, customers, and multi-address
  relations)
  specifically engineered to simulate real-world SQL bugs such as JOIN Fan-out,
  Inflated Totals, and LEFT JOIN filtering anomalies.

  Args:
      db_path (str): The file path or connection string for the DuckDB database.
  """
  conn = None
  try:
    logger.info(
        f"🚀 Initializing DuckDB connection and starting data generation at:"
        f" '{db_path}'"
    )
    conn = duckdb.connect(db_path)

    # Use transactions for high-performance batch execution
    conn.execute("BEGIN TRANSACTION;")

    # 1. Create Customers Table (~3,334 rows)
    logger.info("Creating 'customers' table...")
    conn.execute("""
            CREATE OR REPLACE TABLE customers AS 
            SELECT 
                range AS customer_id,
                'Customer_' || range AS customer_name,
                CASE 
                    WHEN range % 2 = 0 THEN 'Active' 
                    ELSE 'Inactive' 
                END AS status
            FROM range(3334);
        """)

    # 2. Create Customer Addresses Table (3 addresses per customer to force Fan-out)
    logger.info(
        "Creating 'customer_addresses' table (3:1 One-to-Many relation)..."
    )
    conn.execute("""
            CREATE OR REPLACE TABLE customer_addresses AS
            SELECT 
                c.customer_id,
                'Address_' || a.addr_id AS address_id,
                'Street ' || (c.customer_id * 10 + a.addr_id) AS address_line
            FROM customers c
            CROSS JOIN (SELECT * FROM range(1, 4) AS t(addr_id)) AS a;
        """)

    # 3. Create Orders Table (Exactly 10,000 rows with randomized states)
    logger.info("Creating 'orders' table (10,000 benchmark rows)...")
    conn.execute("""
            CREATE OR REPLACE TABLE orders AS
            SELECT 
                range AS order_id,
                CAST((RANDOM() * 3333) AS INTEGER) AS customer_id,
                ROUND(CAST(RANDOM() * 500 AS DECIMAL(10,2)), 2) AS amount,
                CASE 
                    WHEN RANDOM() > 0.3 THEN 'Completed' 
                    ELSE 'Pending' 
                END AS status
            FROM range(1, 10001);
        """)

    # Commit the transaction
    conn.execute("COMMIT;")

    # Verification & Metrics Collection
    orders_count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    customers_count = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[
        0
    ]
    addresses_count = conn.execute(
        "SELECT COUNT(*) FROM customer_addresses"
    ).fetchone()[0]

    logger.info("✅ Benchmark dataset successfully generated and verified:")
    logger.info(f"   - Orders Table: {orders_count:,} rows")
    logger.info(f"   - Customers Table: {customers_count:,} rows")
    logger.info(f"   - Customer Addresses Table: {addresses_count:,} rows")

  except Exception as e:
    if conn:
      conn.execute("ROLLBACK;")
    logger.error(f"❌ Failed to generate benchmark data: {str(e)}")
    raise
  finally:
    if conn:
      conn.close()
      logger.info("🔒 DuckDB connection securely closed.")


if __name__ == "__main__":
  generate_benchmark_data()