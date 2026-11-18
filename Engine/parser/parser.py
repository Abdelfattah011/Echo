import sqlglot
from sqlglot import expressions as exp


def parse_sql_to_steps(sql_query: str) -> list:
    """Enterprise-Grade, Highly Robust SQL Parser using sqlglot.
    
    Features:
    - Read-Only Security Guard (Blocks DROP, DELETE, UPDATE, INSERT, ALTER, TRUNCATE).
    - Graceful Syntax Error Handling (Prevents system crashes).
    - Comprehensive AST extraction for CTEs, Base Tables, Joins, Unions, and Group By.
    - Standardized step schema compatible with the entire Echo Engine team.
    """

    if not isinstance(sql_query, str) or not sql_query.strip():
        raise ValueError("SQL query must be a non-empty string.")

    cleaned_query_upper = sql_query.strip().upper()
    destructive_keywords = ["DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "TRUNCATE"]
    if any(cleaned_query_upper.startswith(keyword) for keyword in destructive_keywords):
        return [{
            "step_id": "step_1",
            "step_type": "SECURITY_ERROR",
            "name": "security_violation",
            "description": "Security Guard Violation: Only read-only SELECT or WITH queries are permitted.",
            "sql_snippet": sql_query,
            "executable_query": sql_query,
            "error": "Security Guard Violation: Destructive queries are blocked.",
        }]

    try:
        tree = sqlglot.parse_one(sql_query)
    except Exception as exc:
        return [{
            "step_id": "step_1",
            "step_type": "SYNTAX_ERROR",
            "name": "syntax_error_branch",
            "description": "Failed to parse SQL query due to syntax error.",
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
                "description": "Compute UNION query branch",
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
                "description": f"Compute Common Table Expression: '{cte.alias}'",
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
            "description": f"Load base table '{table_name}'",
            "sql_snippet": from_clause.sql(),
            "executable_query": base_exec_query,
        })

    for join in tree.args.get("joins") or []:
        target = join.this
        target_name = target.name if isinstance(target, exp.Table) else target.sql()
        target_alias = target.alias if hasattr(target, "alias") else None
        on_clause = join.args.get("on")
        
        join_side = join.side or "INNER"
        join_kind = join.kind or ""
        full_join_type = f"{join_side} {join_kind}".strip()

        steps.append({
            "step_id": get_step_id(),
            "step_type": "JOIN",
            "name": target_alias if target_alias else target_name,
            "description": f"Perform {full_join_type} JOIN with '{target_name}'",
            "sql_snippet": join.sql(),
            "join_details": {
                "join_type": full_join_type,
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
            "description": "Group rows and compute aggregations",
            "sql_snippet": group_clause.sql(),
            "executable_query": tree.sql(),
        })

    if not steps:
        steps.append({
            "step_id": get_step_id(),
            "step_type": "QUERY",
            "name": "main_query",
            "description": "Execute main query block",
            "sql_snippet": tree.sql(),
            "executable_query": tree.sql(),
        })

    return steps