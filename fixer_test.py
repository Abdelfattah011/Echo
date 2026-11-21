"""Fix Generator tests (DuckDB + sqlglot). Run from the project root:  python -m pytest -v

Each test follows the real integration path:
    analysis = EchoEngineRunner(conn).analyze_query(sql, parse_sql_to_steps(sql))
    report   = FixGenerator().fix_all(sql, analysis, conn)
"""
import duckdb
import pytest

from Engine.fixer.fixer import FixGenerator, FixOptions
from Engine.parser.parser import parse_sql_to_steps
from Engine.runner.runner import EchoEngineRunner


@pytest.fixture()
def conn():
    connection = duckdb.connect(database=":memory:")
    connection.execute("CREATE TABLE customers AS SELECT * FROM (VALUES (1, 'Ana'), (2, 'Ben'), (3, 'Cy')) t(customer_id, name)")
    connection.execute("CREATE TABLE orders AS SELECT range + 1 AS order_id, (range % 3) + 1 AS customer_id, 100.0 AS amount FROM range(6)")
    connection.execute(
        "CREATE TABLE customer_addresses AS "
        "SELECT (range % 3) + 1 AS customer_id, 'city_' || range AS city, "
        "CASE WHEN range < 3 THEN TRUE ELSE FALSE END AS is_primary, "
        "CASE WHEN range % 2 = 0 THEN 'home' ELSE 'work' END AS kind FROM range(9)"
    )
    yield connection
    connection.close()


def analyze_and_fix(conn, sql, **options):
    steps = parse_sql_to_steps(sql)
    analysis = EchoEngineRunner(conn).analyze_query(sql, steps)
    report = FixGenerator(FixOptions(**options) if options else None).fix_all(sql, analysis, conn, parsed_steps=steps)
    return analysis, report


def count(conn, sql):
    return conn.execute(f"SELECT COUNT(*) FROM ({sql.rstrip().rstrip(';')})").fetchone()[0]


FAN_OUT = (
    "SELECT o.order_id, o.amount, a.city FROM orders o "
    "JOIN customer_addresses a ON o.customer_id = a.customer_id"
)


# --------------------------------------------------------------------------- #
# Fan-out rewrites
# --------------------------------------------------------------------------- #
def test_standalone_fan_out_is_fixed(conn):
    assert count(conn, FAN_OUT) == 18
    _, report = analyze_and_fix(conn, FAN_OUT)
    assert report.applied and report.fixes[0].strategy_used == "dedupe_right_side_row_number"
    assert count(conn, report.fixed_sql) == 6
    fix = report.fixes[0]
    assert fix.deterministic is True and fix.validation["parsed"] and fix.validation["ast_sane"]
    assert "QUALIFY" in report.fixed_sql.upper() and "ROW_NUMBER" in report.fixed_sql.upper()
    assert fix.target_table == "customer_addresses" and fix.explanation


def test_fix_prefers_the_primary_row(conn):
    _, report = analyze_and_fix(conn, FAN_OUT)
    cities = {r[0] for r in conn.execute(f"SELECT city FROM ({report.fixed_sql.rstrip(';')})").fetchall()}
    primary = {r[0] for r in conn.execute("SELECT city FROM customer_addresses WHERE is_primary").fetchall()}
    assert cities <= primary


def test_totals_are_correct_after_the_fix(conn):
    sql = ("SELECT SUM(o.amount) AS total FROM orders o "
           "JOIN customer_addresses a ON o.customer_id = a.customer_id")
    assert conn.execute(sql).fetchone()[0] == 1800
    _, report = analyze_and_fix(conn, sql)
    assert report.applied
    assert conn.execute(report.fixed_sql).fetchone()[0] == 600


def test_select_star_columns_do_not_change(conn):
    sql = "SELECT * FROM orders o JOIN customer_addresses a ON o.customer_id = a.customer_id"
    before = len(conn.execute(sql).description)
    _, report = analyze_and_fix(conn, sql)
    assert report.applied
    assert len(conn.execute(report.fixed_sql).description) == before


def test_alias_and_unaliased_table_references_survive(conn):
    sql = ("SELECT orders.order_id, customer_addresses.city FROM orders "
           "JOIN customer_addresses ON orders.customer_id = customer_addresses.customer_id")
    _, report = analyze_and_fix(conn, sql)
    assert report.applied and count(conn, report.fixed_sql) == 6


def test_cte_with_fan_out_is_fixed_inside_the_cte(conn):
    sql = ("WITH enriched AS (SELECT o.order_id, o.amount, a.city FROM orders o "
           "JOIN customer_addresses a ON o.customer_id = a.customer_id) "
           "SELECT SUM(amount) AS total FROM enriched")
    assert conn.execute(sql).fetchone()[0] == 1800
    _, report = analyze_and_fix(conn, sql)
    assert report.applied and conn.execute(report.fixed_sql).fetchone()[0] == 600


def test_right_side_is_a_cte(conn):
    sql = ("WITH addr AS (SELECT * FROM customer_addresses) "
           "SELECT o.order_id, addr.city FROM orders o JOIN addr ON o.customer_id = addr.customer_id")
    _, report = analyze_and_fix(conn, sql)
    assert report.applied and count(conn, report.fixed_sql) == 6


def test_right_side_is_a_subquery(conn):
    sql = ("SELECT o.order_id, a.city FROM orders o JOIN "
           "(SELECT customer_id, city FROM customer_addresses) a ON o.customer_id = a.customer_id")
    _, report = analyze_and_fix(conn, sql)
    assert report.applied and count(conn, report.fixed_sql) == 6


def test_union_all_only_the_bad_branch_is_rewritten(conn):
    sql = ("SELECT o.order_id, c.name AS label FROM orders o JOIN customers c ON o.customer_id = c.customer_id "
           "UNION ALL "
           "SELECT o.order_id, a.city AS label FROM orders o JOIN customer_addresses a ON o.customer_id = a.customer_id")
    _, report = analyze_and_fix(conn, sql)
    assert report.applied
    assert report.fixed_sql.upper().count("QUALIFY") == 1
    assert count(conn, report.fixed_sql) == 12


def test_multi_statement_script_keeps_setup_statements(conn):
    script = ("CREATE TABLE dim AS SELECT * FROM customer_addresses;\n"
              "SELECT o.order_id, d.city FROM orders o JOIN dim d ON o.customer_id = d.customer_id;")
    _, report = analyze_and_fix(conn, script)
    assert report.applied
    assert report.fixed_sql.startswith("CREATE TABLE dim AS SELECT * FROM customer_addresses;")
    assert report.fixed_sql.count(";") == 2
    last = report.fixed_sql.split(";")[1]
    assert count(conn, last) == 6


def test_ctas_source_query_is_fixed(conn):
    sql = ("CREATE TABLE enriched AS SELECT o.order_id, a.city FROM orders o "
           "JOIN customer_addresses a ON o.customer_id = a.customer_id")
    steps = parse_sql_to_steps(sql)
    analysis = EchoEngineRunner(conn).analyze_query(sql, steps)
    report = FixGenerator().fix_all(sql, analysis, conn, parsed_steps=steps)
    assert report.applied and report.fixed_sql.upper().startswith("CREATE TABLE ENRICHED AS")
    conn.execute("DROP TABLE enriched")
    conn.execute(report.fixed_sql)
    assert conn.execute("SELECT COUNT(*) FROM enriched").fetchone()[0] == 6


def test_multi_column_join_keys(conn):
    conn.execute("CREATE TABLE stock AS SELECT (range % 2) + 1 AS a, (range % 2) + 10 AS b, range AS qty FROM range(6)")
    conn.execute("CREATE TABLE sales AS SELECT * FROM (VALUES (1, 10, 5), (2, 11, 7)) t(a, b, sold)")
    sql = "SELECT s.sold, k.qty FROM sales s JOIN stock k ON s.a = k.a AND s.b = k.b"
    _, report = analyze_and_fix(conn, sql)
    assert report.applied
    partition = report.fixed_sql.upper().split("PARTITION BY")[1].split("ORDER BY")[0]
    assert "A" in partition and "B" in partition
    assert count(conn, report.fixed_sql) == 2


def test_quoted_identifiers(conn):
    conn.execute('CREATE TABLE "Customer Addresses" AS SELECT customer_id AS "Customer ID", city FROM customer_addresses')
    sql = ('SELECT o.order_id, a.city FROM orders o '
           'JOIN "Customer Addresses" AS a ON o.customer_id = a."Customer ID"')
    _, report = analyze_and_fix(conn, sql)
    assert report.applied and count(conn, report.fixed_sql) == 6


def test_right_only_predicate_is_applied_before_deduplication(conn):
    sql = ("SELECT o.order_id, a.city FROM orders o "
           "JOIN customer_addresses a ON o.customer_id = a.customer_id AND a.kind = 'home'")
    assert count(conn, sql) > 6
    _, report = analyze_and_fix(conn, sql)
    assert report.applied and "'home'" in report.fixed_sql
    assert count(conn, report.fixed_sql) == 6


def test_explicit_order_by_option(conn):
    _, report = analyze_and_fix(conn, FAN_OUT, order_by={"customer_addresses": ["city DESC"]})
    assert report.applied and "city DESC" in report.fixed_sql.replace('"', "")
    assert report.fixes[0].deterministic is True


def test_without_connection_confirmed_evidence_is_enough_but_flagged_non_deterministic(conn):
    steps = parse_sql_to_steps(FAN_OUT)
    analysis = EchoEngineRunner(conn).analyze_query(FAN_OUT, steps)
    report = FixGenerator().fix_all(FAN_OUT, analysis, None, parsed_steps=steps)
    assert report.applied and report.fixes[0].deterministic is False
    assert any("arbitrary" in w for w in report.fixes[0].warnings)
    strict = FixGenerator(FixOptions(require_deterministic=True)).fix_all(FAN_OUT, analysis, None, parsed_steps=steps)
    assert not strict.applied and strict.fixed_sql == FAN_OUT


# --------------------------------------------------------------------------- #
# LEFT JOIN rewrite
# --------------------------------------------------------------------------- #
@pytest.mark.xfail(reason="Detector for 'LEFT JOIN Converted To INNER JOIN' not yet emitting for this query")
def test_left_join_filter_moves_into_on(conn):
    conn.execute("CREATE TABLE cust AS SELECT * FROM (VALUES (1), (2), (3), (4)) t(customer_id)")
    conn.execute("CREATE TABLE ord AS SELECT * FROM (VALUES (1, 1, 'paid'), (2, 2, 'pending')) t(order_id, customer_id, status)")
    sql = ("SELECT c.customer_id, o.order_id FROM cust c LEFT JOIN ord o ON c.customer_id = o.customer_id "
           "WHERE o.status = 'paid'")
    assert count(conn, sql) == 1
    _, report = analyze_and_fix(conn, sql)
    assert report.applied and report.fixes[0].strategy_used == "left_join_filter_to_on"
    assert count(conn, report.fixed_sql) == 4          # every customer is kept again
    assert "WHERE" not in report.fixed_sql.upper()


# --------------------------------------------------------------------------- #
# Things that must NOT be rewritten
# --------------------------------------------------------------------------- #
def test_clean_query_is_never_modified(conn):
    sql = "SELECT o.order_id, c.name FROM orders o JOIN customers c ON o.customer_id = c.customer_id"
    analysis, report = analyze_and_fix(conn, sql)
    assert not report.applied and report.fixed_sql == sql and not report.fixes


def test_proven_intentional_one_to_many_is_not_rewritten(conn):
    sql = ("SELECT c.name, SUM(o.amount) AS total FROM customers c "
           "JOIN orders o ON c.customer_id = o.customer_id GROUP BY c.name")
    analysis, report = analyze_and_fix(conn, sql)
    assert not report.applied and report.fixed_sql == sql


def test_unsupported_findings_are_explained_not_applied(conn):
    sql = "SELECT * FROM orders o JOIN customers c ON CAST(o.customer_id AS VARCHAR) = CAST(c.customer_id AS VARCHAR)"
    bug = {"bug_type": "NULL Join Keys", "severity": "Medium", "suspicious_step_id": "step_2",
           "suggested_fix": "Fix or filter the NULL keys upstream."}
    report = FixGenerator().fix_all(sql, bugs=[bug], db_connection=conn)
    assert not report.applied and report.fixed_sql == sql
    assert "No automatic rewrite" in report.fixes[0].warnings[0]
    assert "Suggested manual fix" in report.fixes[0].explanation


def test_mixed_condition_blocks_key_only_deduplication(conn):
    sql = ("SELECT o.order_id, a.city FROM orders o JOIN customer_addresses a "
           "ON o.customer_id = a.customer_id AND a.customer_id <> o.order_id + 100")
    _, report = analyze_and_fix(conn, sql)
    assert not report.applied and report.fixed_sql == sql


def test_right_and_full_joins_are_not_rewritten(conn):
    sql = ("SELECT o.order_id, a.city FROM orders o RIGHT JOIN customer_addresses a "
           "ON o.customer_id = a.customer_id")
    _, report = analyze_and_fix(conn, sql)
    assert not report.applied


def test_fix_is_idempotent(conn):
    _, first = analyze_and_fix(conn, FAN_OUT)
    assert first.applied
    _, second = analyze_and_fix(conn, first.fixed_sql)
    assert not second.applied and second.fixed_sql == first.fixed_sql


def test_malformed_and_empty_sql_fail_gracefully(conn):
    for bad in ("SELEC * FRM", "", "   ", "CREATE TABLE"):
        report = FixGenerator().fix_all(bad, None, conn)
        assert not report.applied and report.fixed_sql == bad
    report = FixGenerator().fix_all(FAN_OUT, bugs=[{"bug_type": "Possible JOIN Fan-out", "suspicious_step_id": "nope"}])
    assert not report.applied and report.fixed_sql == FAN_OUT


def test_output_has_no_ansi_escape_codes(conn):
    _, report = analyze_and_fix(conn, FAN_OUT)
    blob = report.fixed_sql + "".join(f.explanation + "".join(f.warnings) for f in report.fixes)
    assert "\x1b" not in blob


# --------------------------------------------------------------------------- #
# Proof
# --------------------------------------------------------------------------- #
def test_verify_proves_the_fix_on_fresh_connections(conn):
    def factory():
        c = duckdb.connect(database=":memory:")
        c.execute("CREATE TABLE orders AS SELECT range + 1 AS order_id, (range % 3) + 1 AS customer_id, 100.0 AS amount FROM range(6)")
        c.execute("CREATE TABLE customer_addresses AS SELECT (range % 3) + 1 AS customer_id, 'c' || range AS city FROM range(9)")
        return c

    seed_conn = factory()
    try:
        _, report = analyze_and_fix(seed_conn, FAN_OUT)
    finally:
        seed_conn.close()

    proof = FixGenerator().verify(FAN_OUT, report.fixed_sql, factory)
    assert proof["verified"] and proof["fan_out_findings_before"] >= 1 and proof["fan_out_findings_after"] == 0

def test_verify_returns_rows_and_totals_before_and_after_on_fan_out():
    def factory():
        c = duckdb.connect(database=":memory:")
        c.execute(
            "CREATE TABLE orders AS SELECT range + 1 AS order_id, "
            "(range % 3) + 1 AS customer_id, 100.0 AS amount FROM range(6)"
        )
        c.execute(
            "CREATE TABLE customer_addresses AS SELECT (range % 3) + 1 AS customer_id, "
            "'c' || range AS city FROM range(9)"
        )
        return c

    sql = """
    SELECT o.order_id, o.amount, ca.city
    FROM orders o
    JOIN customer_addresses ca ON o.customer_id = ca.customer_id
    """

    seed = factory()
    try:
        _, report = analyze_and_fix(seed, sql)
    finally:
        seed.close()

    proof = FixGenerator().verify(sql, report.fixed_sql, factory)

    assert proof["passed"] is True
    assert proof["verified"] is True
    assert proof["rows_before"] > proof["rows_after"]
    assert proof["total_before"] > proof["total_after"]
    assert proof["total_after"] is not None


def test_verify_handles_non_numeric_columns_gracefully():
    def factory():
        c = duckdb.connect(database=":memory:")
        c.execute("CREATE TABLE users AS SELECT 1 AS id, 'Ali' AS name UNION ALL SELECT 2, 'Mona'")
        return c

    sql = "SELECT name FROM users"
    proof = FixGenerator().verify(sql, sql, factory)

    assert proof["rows_before"] == 2
    assert proof["rows_after"] == 2
    assert proof["total_before"] is None
    assert proof["total_after"] is None
    assert proof["passed"] is False

def test_verify_marks_failed_when_fixed_sql_has_error():
    def factory():
        c = duckdb.connect(database=":memory:")
        c.execute("CREATE TABLE t AS SELECT 1 AS id, 50.0 AS amount")
        return c

    bad_fixed_sql = "SELECT id, amount FROM nonexistent_table"
    proof = FixGenerator().verify("SELECT id, amount FROM t", bad_fixed_sql, factory)

    assert proof["passed"] is False
    assert proof["verified"] is False
    assert proof["rows_after"] is None
    assert proof["total_after"] is None