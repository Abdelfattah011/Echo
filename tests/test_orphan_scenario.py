import duckdb
from Engine.runner.runner import EchoEngineRunner

print("🚀 Starting Orphan Records & Aggregation Anomaly Test...")
# 1. إنشاء اتصال مؤقت بـ DuckDB
conn = duckdb.connect(database=":memory:")
# 2. إنشاء جداول فيها بيانات يتيمة (Orphan Records) - مبيعات لمنتجات غير مسجلة في جدول الـ Products
conn.execute("""
    CREATE TABLE products (
        product_id INT,
        product_name VARCHAR
    );
""")

conn.execute("""
    CREATE TABLE sales (
        sale_id INT,
        product_id INT,
        quantity INT
    );
""")
# إدخال بيانات: منتج رقم 1 و 2 موجودين، لكن فيه مبيعات لمنتج رقم 99 (مش موجود في جدول المنتجات!)
conn.execute("INSERT INTO products VALUES (1, 'Laptop'), (2, 'Mouse');")
conn.execute("INSERT INTO sales VALUES (101, 1, 5), (102, 2, 3), (103, 99, 10);")  # منتج 99 يتيم (Orphan)

print("✅ Orphan test tables created in DuckDB.")

# 3. كويري فيها خطر الـ Orphan Records (LEFT JOIN مع جدول غير متطابق يؤدي لبيانات مفقودة أو غير دقيقة في الـ Summary)
orphan_query = """
    SELECT p.product_name, SUM(s.quantity) as total_qty
    FROM sales s
    LEFT JOIN products p ON s.product_id = p.product_id
    GROUP BY p.product_name
"""

print(f"\n🔍 Parsing and Running Orphan / Aggregation Query:\n{orphan_query}")

# 4. تحليل الكويري عبر الـ Runner
engine = EchoEngineRunner(conn)

# محاكاة خطوات البارسنج لهذه الكويري (أو استدعاء الـ Parser مباشرة)
from Engine.parser.parser import parse_sql_to_steps
parsed_steps = parse_sql_to_steps(orphan_query)

# تشغيل التحليل الشامل
analysis_result = engine.analyze_query(orphan_query, parsed_steps)

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