
import duckdb
from Engine.runner.runner import EchoEngineRunner

print("🚀 Starting Full Integration Test...")

# 1. Create database
conn = duckdb.connect(database=":memory:")

# 2. Create tables
conn.execute("""
    CREATE TABLE customers (
        customer_id INT,
        customer_tier VARCHAR
    )
""")

conn.execute("""
    CREATE TABLE transactions (
        transaction_id INT,
        customer_id INT,
        amount INT
    )
""")

conn.execute("""
    CREATE TABLE customer_tags (
        customer_id INT,
        tag VARCHAR
    )
""")

# 3. Insert test data
conn.execute("""
    INSERT INTO customers VALUES
    (1, 'Gold')
""")

conn.execute("""
    INSERT INTO transactions VALUES
    (101, 1, 500),
    (102, 1, 300),
    (103, 1, 200)
""")

conn.execute("""
    INSERT INTO customer_tags VALUES
    (1, 'VIP'),
    (1, 'Loyal')
""")

# 4. Query with actual JOIN multiplication
query = """
SELECT c.customer_tier, SUM(t.amount) AS total_spent
FROM customers c
JOIN transactions t
    ON c.customer_id = t.customer_id
JOIN customer_tags ct
    ON c.customer_id = ct.customer_id
GROUP BY c.customer_tier
"""

# 5. Run all detectors
runner = EchoEngineRunner(db_connection=conn)
result = runner.analyze_sql(query)

# 6. Display results
print("\n===== STEP TRACE =====")
for step in result["step_trace"]:
    print(
        f'{step["step_id"]} | '
        f'Rows: {step["row_count"]} | '
        f'Status: {step["status"]} | '
        f'Measure: {step.get("measure_value")}'
    )

print("\n===== DETECTED BUGS =====")
for bug in result["detected_bugs"]:
    print(
        f'Bug: {bug["bug_type"]} | '
        f'Severity: {bug["severity"]} | '
        f'Step: {bug["suspicious_step_id"]}'
    )

print("\n===== DETECTOR ERRORS =====")
print(result["detector_errors"])

print("\n===== VERDICT =====")
print(result["verdict"])

# 7. Integration assertions
assert not result["detector_errors"], (
    f"Detector errors found: {result['detector_errors']}"
)

assert all(
    step["status"] == "success"
    for step in result["step_trace"]
), "One or more execution steps failed."

assert any(
    bug["bug_type"] == "Possible JOIN Fan-out"
    for bug in result["detected_bugs"]
), "Fan-Out finding was not detected."

assert any(
    bug["bug_type"] == "Inflated Aggregate"
    for bug in result["detected_bugs"]
), "Inflated Aggregate finding was not detected."

print("\n✅ FULL INTEGRATION TEST PASSED!")

conn.close()
