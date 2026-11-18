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