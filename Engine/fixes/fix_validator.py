
def validate_fanout_fix(
    connection,
    fixed_sql: str,
    source_table: str = "orders",
    id_column: str = "order_id",
    total_column: str = "amount",
) -> dict:
    """
    Validates a generated SQL fix against its original source table.

    Checks:
    1. Fixed row count equals source row count.
    2. Fixed total equals source total.
    3. No duplicate IDs exist in the fixed result.
    """

    source_count = connection.execute(
        f"SELECT COUNT(*) FROM {source_table}"
    ).fetchone()[0]

    fixed_count = connection.execute(
        f"SELECT COUNT(*) FROM ({fixed_sql}) AS fixed"
    ).fetchone()[0]

    source_total = connection.execute(
        f"SELECT COALESCE(SUM({total_column}), 0) FROM {source_table}"
    ).fetchone()[0]

    fixed_total = connection.execute(
        f"SELECT COALESCE(SUM({total_column}), 0) "
        f"FROM ({fixed_sql}) AS fixed"
    ).fetchone()[0]

    duplicate_count = connection.execute(f"""
        SELECT COUNT(*)
        FROM (
            SELECT {id_column}
            FROM ({fixed_sql}) AS fixed
            GROUP BY {id_column}
            HAVING COUNT(*) > 1
        ) AS duplicates
    """).fetchone()[0]

    checks = {
        "row_count_matches": fixed_count == source_count,
        "total_matches": fixed_total == source_total,
        "no_duplicate_ids": duplicate_count == 0,
    }

    return {
        "source_row_count": source_count,
        "fixed_row_count": fixed_count,
        "source_total": source_total,
        "fixed_total": fixed_total,
        "duplicate_count": duplicate_count,
        "checks": checks,
        "passed": all(checks.values()),
    }
