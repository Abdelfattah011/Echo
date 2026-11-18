<<<<<<< HEAD
import sqlglot
from sqlglot import expressions as exp


def parse_sql_to_steps(sql_query: str) -> list:
    """Production-grade, highly robust SQL Parser using sqlglot.

    Breaks complex queries (CTEs, Subqueries, Joins, Unions) into 
    clean, sequential execution steps for rigorous anomaly detection.
    Includes graceful handling for syntax errors to prevent system crashes.
    """

    if not isinstance(sql_query, str) or not sql_query.strip():
        raise ValueError("SQL query must be a non-empty string.")

    try:
        tree = sqlglot.parse_one(sql_query)
    except Exception as exc:
        return [{
            "step_id": "step_1",
            "step_type": "SYNTAX_ERROR",
            "name": "syntax_error_branch",
            "sql_snippet": sql_query,
            "executable_query": sql_query,
            "error": f"Invalid SQL syntax: {exc}",
        }]

    steps = []
    current_id = 1

    def get_step_id() -> str:
        nonlocal current_id
        sid = f"step_{current_id}"
        current_id += 1
        return sid

    if isinstance(tree, exp.Union):
        for sub_query in tree.flatten():
            steps.append({
                "step_id": get_step_id(),
                "step_type": "UNION_PART",
                "name": "union_query_branch",
                "sql_snippet": sub_query.sql(),
                "executable_query": sub_query.sql(),
            })
        return steps

    with_clause = tree.args.get("with_")
    if with_clause:
        for cte in with_clause.expressions:
            steps.append({
                "step_id": get_step_id(),
                "step_type": "CTE",
                "name": cte.alias,
                "sql_snippet": cte.this.sql(),
                "executable_query": cte.this.sql(),
            })

    from_clause = tree.args.get("from_")
    if from_clause:
        table = from_clause.this
        table_name = table.name if isinstance(table, exp.Table) else table.sql()
        table_alias = table.alias if hasattr(table, "alias") else None
        
        base_exec_query = f"SELECT * FROM {table_name}" + (f" AS {table_alias}" if table_alias else "")
        
        steps.append({
            "step_id": get_step_id(),
            "step_type": "BASE_TABLE",
            "name": table_alias if table_alias else table_name,
            "sql_snippet": from_clause.sql(),
            "executable_query": base_exec_query,
        })

    for join in tree.args.get("joins") or []:
        target = join.this
        target_name = target.name if isinstance(target, exp.Table) else target.sql()
        target_alias = target.alias if hasattr(target, "alias") else None
        on_clause = join.args.get("on")

        steps.append({
            "step_id": get_step_id(),
            "step_type": "JOIN",
            "name": target_alias if target_alias else target_name,
            "sql_snippet": join.sql(),
            "join_details": {
                "join_type": join.side or "INNER",
                "target_table": target_name,
                "target_alias": target_alias,
                "on_condition": on_clause.sql() if on_clause else None,
            },
            "executable_query": tree.sql(),
        })

    group_clause = tree.args.get("group")
    if group_clause:
        steps.append({
            "step_id": get_step_id(),
            "step_type": "GROUP_BY",
            "name": "aggregation",
            "sql_snippet": group_clause.sql(),
            "executable_query": tree.sql(),
        })

    if not steps:
        steps.append({
            "step_id": get_step_id(),
            "step_type": "QUERY",
            "name": "main_query",
            "sql_snippet": tree.sql(),
            "executable_query": tree.sql(),
        })

    return steps
=======
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
>>>>>>> 11cfc2f119062be38a47a4b5278be27c2172ba14
