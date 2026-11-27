"""Run the real ECHO engine on one SQL query and save the result to PostgreSQL.

This is plan step 3: "connect the loader to the real engine output".

Put this file in the project root, next to loader.py and the Engine/ folder, then:

    python run_and_load.py                       # demo query (orders x addresses fan-out)
    python run_and_load.py "SELECT ... FROM ..." # your own query on the demo tables

The demo tables are built in memory with DuckDB (like the API's sandbox), so nothing
here touches real data. Only the RESULT of the analysis is saved to PostgreSQL.
"""

import json
import sys

import duckdb

from Engine.parser.parser import parse_sql_to_steps, split_statements
from Engine.runner.runner import EchoEngineRunner
from loader import load_result

# 1000 customers, 3 addresses each (so joining on customer_id multiplies rows 3x),
# and 3000 orders.
DEMO_SETUP = """
CREATE TABLE customers AS
SELECT range AS customer_id, 'Customer_' || range AS customer_name FROM range(1000);

CREATE TABLE customer_addresses AS
SELECT c.customer_id, 'Street ' || (c.customer_id * 10 + a.addr_id) AS address_line
FROM customers c CROSS JOIN (SELECT * FROM range(1, 4) AS t(addr_id)) a;

CREATE TABLE orders AS
SELECT range AS order_id, (range * 7) % 1000 AS customer_id,
       CAST(((range * 37) % 500) + 1 AS DECIMAL(10, 2)) AS amount
FROM range(1, 3001);
"""

DEFAULT_QUERY = (
    "SELECT SUM(o.amount) AS total_sales "
    "FROM orders o JOIN customer_addresses a ON o.customer_id = a.customer_id;"
)


def run_engine(sql):
    """Analyze one query on a fresh in-memory DuckDB and return the engine result (a dict)."""
    conn = duckdb.connect(":memory:")
    try:
        for statement in split_statements(DEMO_SETUP):
            conn.execute(statement)
        runner = EchoEngineRunner(conn)
        return runner.analyze_query(sql, parse_sql_to_steps(sql))
    finally:
        conn.close()


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]  # options start with --
    sql = args[0] if args else DEFAULT_QUERY
    result = run_engine(sql)

    print("verdict:", result.get("verdict"))
    for step in result.get("step_trace", []):
        print(f'  {step.get("step_id")}  {step.get("step_type"):<12} rows={step.get("row_count")}')
    for bug in result.get("detected_bugs", []):
        print(f'  BUG: {bug.get("bug_type")} ({bug.get("severity")}) at {bug.get("suspicious_step_id")}')

    run_id = load_result(result)
    print("Saved to PostgreSQL as run", run_id)

    if "--save-json" in sys.argv:  # optional: keep a copy of the raw result
        with open("last_result.json", "w", encoding="utf-8") as f:
            json.dump(result, f, default=str, indent=2)
