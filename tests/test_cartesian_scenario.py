import duckdb
from Engine.runner.runner import EchoEngineRunner
from Engine.parser.parser import parse_sql_to_steps

print("🚀 Starting Cartesian Product / Unfiltered Join Test...")

# 1. إنشاء اتصال مؤقت بـ DuckDB
conn = duckdb.connect(database=":memory:")

# 2. إنشاء جداول بيانات
conn.execute("""
    CREATE TABLE users (
        user_id INT,
        user_name VARCHAR
    );
""")

conn.execute("""
    CREATE TABLE orders (
        order_id INT,
        user_id INT,
        amount DECIMAL
    );
""")

# إدخال بيانات تجريبية
conn.execute("INSERT INTO users VALUES (1, 'Ali'), (2, 'Sara'), (3, 'Omar');")
conn.execute("INSERT INTO orders VALUES (501, 1, 100), (502, 2, 250);")

print("✅ Cartesian test tables created in DuckDB.")

# 3. كويري كارثية فيها CROSS JOIN / Unfiltered Join (مفيهاش ON clause حقيقي)
cartesian_query = """
    SELECT u.user_name, o.order_id
    FROM users u
    JOIN orders o
"""

print(f"\n🔍 Parsing and Running Cartesian Query:\n{cartesian_query}")

# 4. تشغيل البارسنج والـ Runner
engine = EchoEngineRunner(conn)
parsed_steps = parse_sql_to_steps(cartesian_query)
analysis_result = engine.analyze_query(cartesian_query, parsed_steps)

# 5. طباعة النتائج والتقرير
print("\n📊 --- Step Trace Results ---")
for step in analysis_result["step_trace"]:
    print(f"Step: {step['step_id']} | Type: {step['step_type']} | Rows: {step.get('row_count')} | Status: {step['status']}")

print("\n🚨 --- Detected Bugs Report ---")
for bug in analysis_result["detected_bugs"]:
    print(f"Bug Type: {bug.get('bug_type')}")
    print(f"Severity: {bug.get('severity')}")
    print(f"Description: {bug.get('description')}")
    print("-" * 40)