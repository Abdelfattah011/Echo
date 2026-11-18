import duckdb
from Engine.parser.parser import parse_sql_to_steps
from Engine.runner.runner import EchoEngineRunner


def run_complex_scenario_test():
    print("🚀 Starting Complex Scenario End-to-End Test (CTE & Aggregation)...")

    # 1. إعداد قاعدة بيانات DuckDB مؤقتة في الذاكرة
    conn = duckdb.connect(database=":memory:")

    # إنشاء جداول اختبارية (مثلاً جدول منتجات وجدول مبيعات بتفاصيل معقدة)
    conn.execute("CREATE TABLE products AS SELECT range AS product_id, 'Product_' || range AS name FROM range(1000);")
    conn.execute("CREATE TABLE sales AS SELECT range % 1000 as product_id, (range % 5) + 1 as quantity FROM range(15000);")

    print("✅ Complex test tables created in DuckDB.")

    # 2. كويري معقدة فيها CTE و JOIN بيعمل تضخم في الصفوف
    complex_query = """
        WITH regional_sales AS (
            SELECT p.product_id, s.quantity 
            FROM products p 
            JOIN sales s ON p.product_id = s.product_id
        )
        SELECT product_id, SUM(quantity) as total_qty
        FORM regional_sales
        GROUP BY product_id
    """

    print(f"\n🔍 Parsing and Running Complex Query:\n{complex_query}")

    # تحليل الكويري بالـ Parser العبقري
    parsed_steps = parse_sql_to_steps(complex_query)

    # تجهيز الخطوات للـ Runner
    formatted_steps = {}
    for step in parsed_steps:
        step_id = step['step_id']
        eq = step.get("executable_query")
        formatted_steps[step_id] = eq if eq else complex_query

    # تشغيل المحرك والـ Runner
    runner = EchoEngineRunner(db_connection=conn)
    analysis_result = runner.analyze_query(complex_query, formatted_steps)

    # 3. عرض النتائج والتقارير
    print("\n📊 --- Step Trace Results ---")
    for trace in analysis_result.get("step_trace", []):
        print(
            f"Step: {trace.get('step_id')} | Type: {trace.get('step_type')} | Rows: {trace.get('row_count')} | Status:"
            f" {trace.get('status')}"
        )

    print("\n🚨 --- Detected Bugs Report ---")
    bugs = analysis_result.get("detected_bugs", [])
    for bug in bugs:
        print(f"Bug Type: {bug.get('bug_type')}")
        print(f"Severity: {bug.get('severity')}")
        print(f"Description: {bug.get('description')}")

    conn.close()


if __name__ == "__main__":
    run_complex_scenario_test()