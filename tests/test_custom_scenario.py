import duckdb
from Engine.parser.parser import parse_sql_to_steps
from Engine.runner.runner import EchoEngineRunner

def run_custom_test():
    print("🚀 Starting Custom End-to-End Test...")
    
    db = duckdb.connect(database=":memory:")
    cursor = db.cursor()
    
    cursor.execute("CREATE TABLE employees (id INT, name VARCHAR, department_id INT);")
    cursor.execute("CREATE TABLE departments (id INT, dept_name VARCHAR);")
    
    # إدخال بيانات تجريبية
    cursor.execute("INSERT INTO employees VALUES (1, 'Alice', 10), (2, 'Bob', 10), (3, 'Charlie', 20);")
    cursor.execute("INSERT INTO departments VALUES (10, 'Engineering'), (20, 'HR');")
    
    test_query = """
    SELECT d.dept_name, COUNT(e.id) as emp_count
    FROM employees e
    JOIN departments d ON e.department_id = d.id
    GROUP BY d.dept_name
    """
    
    print(f"\n🔍 Parsing and Running Query:\n{test_query}")
    
    parsed_steps = parse_sql_to_steps(test_query)
    runner = EchoEngineRunner(db)
    analysis_result = runner.analyze_query(test_query, parsed_steps)
    
    print("\n📊 --- Step Trace Results ---")
    for step in analysis_result.get("step_trace", []):
        print(f"Step: {step.get('step_id')} | Type: {step.get('step_type')} | Rows: {step.get('row_count')} | Status: {step.get('status')}")
        
    print("\n🚨 --- Detected Bugs Report ---")
    for bug in analysis_result.get("detected_bugs", []):
        print(f"Bug Type: {bug.get('bug_type')}")
        print(f"Severity: {bug.get('severity')}")
        print(f"Description: {bug.get('description')}")
        print("-" * 50)

if __name__ == "__main__":
    run_custom_test()