import duckdb
from Engine.parser.parser import parse_sql_to_steps
from Engine.runner.runner import EchoEngineRunner


def run_sara_end_to_end_test():
    print("🚀 Starting Sara's End-to-End Test (M1)...")

    # 1. إعداد قاعدة بيانات DuckDB مؤقتة في الذاكرة (In-memory) لبيانات سارة
    conn = duckdb.connect(database=":memory:")

    # إنشاء جدول تجريبي يمثل حالة سارة (10,000 صف أساسي)
    conn.execute("CREATE TABLE users AS SELECT range AS id FROM range(10000);")
    # جدول فرعي هيدخل يضرب الدنيا ويعمل تضخم (30,000 صف متكرر)
    conn.execute(
        "CREATE TABLE orders AS SELECT range % 3 as user_id FROM range(30000);"
    )

    print("✅ Test tables created in DuckDB.")

    # 2. كويري فيها خطأ JOIN Fan-out متعمد (من 10,000 لـ 30,000 صف)
    buggy_query = """
        SELECT u.id, o.user_id 
        FROM users u 
        JOIN orders o ON u.id = o.user_id
    """

    print(f"\n🔍 Parsing and Running Buggy Query:\n{buggy_query}")

    # تحليل الكويري بالـ Parser
    parsed_steps = parse_sql_to_steps(buggy_query)

    # تجهيز الخطوات للـ Runner باستخدام الـ step_id النظيف مباشرة بدون تكرار
    formatted_steps = {}
    for step in parsed_steps:
        step_id = step['step_id']  # تم إزالة f"step_..." الزائدة لتفادي تكرار step_step
        eq = step.get("executable_query")
        formatted_steps[step_id] = eq if eq else buggy_query

    # ج. تشغيل المحرك والـ Runner على DuckDB
    runner = EchoEngineRunner(db_connection=conn)
    analysis_result = runner.analyze_query(buggy_query, formatted_steps)

    # 3. عرض النتائج والتقارير للتأكد من نجاح الكاشف
    print("\n📊 --- Step Trace Results ---")
    for trace in analysis_result.get("step_trace", []):
        print(
            f"Step: {trace.get('step_id')} | Rows: {trace.get('row_count')} | Status:"
            f" {trace.get('status')}"
        )

    print("\n🚨 --- Detected Bugs Report ---")
    bugs = analysis_result.get("detected_bugs", [])
    for bug in bugs:
        print(f"Bug Type: {bug.get('bug_type')}")
        print(f"Severity: {bug.get('severity')}")
        print(f"Description: {bug.get('description')}")

    # التأكد من نجاح اختبار سارة واكتشاف الـ Fan-out بنجاح
    fan_out_detected = any(
        "Fan-out" in str(bug.get("bug_type", "")) for bug in bugs
    )
    if fan_out_detected:
        print(
            "\n✨ [SUCCESS] Sara's Test passed successfully! The detector caught"
            " the row inflation."
        )
    else:
        print("\n⚠️ [WARNING] Fan-out was not caught, check thresholds.")

    conn.close()


if __name__ == "__main__":
    run_sara_end_to_end_test()