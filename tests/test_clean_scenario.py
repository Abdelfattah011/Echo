import duckdb
from Engine.parser.parser import parse_sql_to_steps
from Engine.runner.runner import EchoEngineRunner

def run_clean_test():
    print("🚀 Starting Clean Query Test...")
    
    db = duckdb.connect(database=":memory:")
    cursor = db.cursor()
    
    cursor.execute("CREATE TABLE users (id INT, name VARCHAR, status VARCHAR);")
    cursor.execute("INSERT INTO users VALUES (1, 'Ahmed', 'active'), (2, 'Mohamed', 'active'), (3, 'Ali', 'inactive');")
    
    test_query = """
    SELECT id, name 
    FROM users 
    WHERE status = 'active'
    """
    
    print(f"\n🔍 Parsing and Running Clean Query:\n{test_query}")
    
    parsed_steps = parse_sql_to_steps(test_query)
    runner = EchoEngineRunner(db)
    analysis_result = runner.analyze_query(test_query, parsed_steps)
    
    print("\n📊 --- Step Trace Results ---")
    for step in analysis_result.get("step_trace", []):
        print(f"Step: {step.get('step_id')} | Type: {step.get('step_type')} | Rows: {step.get('row_count')} | Status: {step.get('status')}")
        
    print("\n🚨 --- Detected Bugs Report ---")
    bugs = analysis_result.get("detected_bugs", [])
    if not bugs:
        print("✨ Clean! No bugs or anomalies detected.")
    else:
        for bug in bugs:
            print(f"Bug Type: {bug.get('bug_type')}")
            print(f"Severity: {bug.get('severity')}")
            print(f"Description: {bug.get('description')}")
            print("-" * 50)

if __name__ == "__main__":
    run_clean_test()