import sqlglot
from sqlglot import expressions as exp


def parse_sql_to_steps(sql_query: str) -> list:
  """Advanced Production-Ready SQL Parser based on sqlglot.

  Parses any complex SQL query and breaks it down into an ordered
  sequence of executable steps (CTEs -> Base Table -> JOINs -> GROUP BY).
  """
  # Validate input query
  if not sql_query or not isinstance(sql_query, str):
    raise ValueError("Invalid SQL query provided. Must be a non-empty string.")

  try:
    # Convert raw SQL text into an Abstract Syntax Tree (AST)
    parsed_ast = sqlglot.parse_one(sql_query)
  except Exception as e:
    raise SyntaxError(f"Failed to parse SQL query due to syntax error: {str(e)}")

  steps = []
  step_counter = 1

  # 1. Extract CTEs (Common Table Expressions) first
  for cte in parsed_ast.find_all(exp.CTE):
    cte_name = cte.alias
    steps.append({
        "step_id": step_counter,
        "step_type": "CTE",
        "name": cte_name,
        "description": f"Compute Common Table Expression: '{cte_name}'",
        "sql_snippet": cte.sql(),
        # Executable query snippet for the Step Runner
        "executable_query": f"SELECT * FROM ({cte.sql()})",
    })
    step_counter += 1

  # 2. Extract the Base Table from the FROM clause
  from_expr = parsed_ast.find(exp.From)
  if from_expr:
    table_node = from_expr.find(exp.Table)
    table_name = table_node.name if table_node else "unknown_table"

    steps.append({
        "step_id": step_counter,
        "step_type": "BASE_TABLE",
        "name": table_name,
        "description": f"Load base table '{table_name}'",
        "sql_snippet": from_expr.sql(),
        "executable_query": f"SELECT * FROM {from_expr.sql()}",
    })
    step_counter += 1

  # 3. Extract all JOINs sequentially with their ON conditions
  for join in parsed_ast.find_all(exp.Join):
    joined_table = join.find(exp.Table)
    joined_name = joined_table.name if joined_table else "table"
    join_side = join.side or "INNER"
    join_kind = join.kind or ""
    full_join_type = f"{join_side} {join_kind}".strip()

    # Extract the ON condition if available for detectors
    on_expr = join.args.get("on")
    on_condition = on_expr.sql() if on_expr else None

    steps.append({
        "step_id": step_counter,
        "step_type": "JOIN",
        "name": joined_name,
        "description": f"Perform {full_join_type} JOIN with '{joined_name}'",
        "sql_snippet": join.sql(),
        "join_details": {
            "join_type": full_join_type,
            "target_table": joined_name,
            "on_condition": on_condition,
        },
        "executable_query": f"SELECT * FROM {joined_name}",
    })
    step_counter += 1

  # 4. Extract GROUP BY and Aggregations
  group_expr = parsed_ast.find(exp.Group)
  if group_expr:
    steps.append({
        "step_id": step_counter,
        "step_type": "GROUP_BY",
        "name": "aggregation",
        "description": "Group rows and compute aggregations",
        "sql_snippet": group_expr.sql(),
        "executable_query": parsed_ast.sql(),
    })
    step_counter += 1

  # Ensure meaningful steps were extracted
  if not steps:
    raise ValueError(
        "Could not extract meaningful execution steps from the given SQL"
        " query."
    )

  return steps


# --- Test block ---
if __name__ == "__main__":
  test_query = """
    WITH regional_sales AS (
        SELECT region, amount FROM orders
    )
    SELECT r.region, SUM(r.amount) as total
    FROM regional_sales r
    JOIN employees e ON r.region = e.region
    GROUP BY r.region
    """

  print("🔥 Testing Next-Level Production SQL Parser...\n")
  steps = parse_sql_to_steps(test_query)
  for s in steps:
    print(
        f"Step {s['step_id']} | Type: {s['step_type']} | Name:"
        f" {s.get('name', 'N/A')}"
    )
    print(f"   Desc: {s['description']}")
    if "join_details" in s:
      print(f"   Join Condition: {s['join_details']['on_condition']}")
    print(f"   Executable: {s['executable_query']}\n")