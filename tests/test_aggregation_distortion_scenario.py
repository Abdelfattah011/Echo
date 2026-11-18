import duckdb
from Engine.runner.runner import EchoEngineRunner
from Engine.parser.parser import parse_sql_to_steps

print("🚀 Starting Aggregation Data Distortion & Multi-Table Test...")

# 1. إنشاء اتصال مؤقت بـ DuckDB
conn = duckdb.connect(database=":memory:")

# 2. إنشاء الجداول
conn.execute("""
    CREATE TABLE customers (
        customer_id INT,
        customer_tier VARCHAR
    );
""")

conn.execute("""
    CREATE TABLE transactions (
        tx_id INT,
        customer_id INT,
        amount DECIMAL
    );
""")

# إدخال بيانات: عميل واحد (ID: 1) ليه 3 معاملات ضخمة
conn.execute("INSERT INTO customers VALUES (1, 'VIP');")
conn.execute("INSERT INTO transactions VALUES (10, 1, 500), (11, 1, 300), (12, 1, 200);")

print("✅ Aggregation distortion test tables created in DuckDB.")

# 3. كويري فيها تشوه في التجميع (تجميع مباشر بعد JOIN بيؤدي لتكرار الحسابات الخاطئة)
distortion_query = """
    SELECT c.customer_tier, SUM(t.amount) as total_spent
    FROM customers c
    JOIN transactions t ON c.customer_id = t.customer_id
    GROUP BY c.customer_tier
"""

print(f"\n🔍 Parsing and Running Aggregation Distortion Query:\n{distortion_query}")

# 4. تشغيل البارسنج والـ Runner
engine = EchoEngineRunner(conn)
parsed_steps = parse_sql_to_steps(distortion_query)
analysis_result = engine.analyze_query(distortion_query, parsed_steps)

# 5. طباعة النتائج والتقرير النهائي
print("\n📊 --- Step Trace Results ---")
for step in analysis_result["step_trace"]:
    print(f"Step: {step['step_id']} | Type: {step['step_type']} | Rows: {step.get('row_count')} | Status: {step['status']}")

print("\n🚨 --- Detected Bugs Report ---")
for bug in analysis_result["detected_bugs"]:
    print(f"Bug Type: {bug.get('bug_type')}")
    print(f"Severity: {bug.get('severity')}")
    print(f"Description: {bug.get('description')}")
    print("-" * 40)