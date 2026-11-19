"""ECHO universal SQL parser.

Accepts ANY SQL script (SELECT / INSERT / UPDATE / DELETE / MERGE / CREATE / ALTER /
DROP / TRUNCATE / USE / SET / PRAGMA / ATTACH ...), single statement or many, and
returns an ordered list of executable steps (``step_1``, ``step_2`` ...).

Design
------
* Scripts are split into statements by our own quote/comment-aware splitter, so ONE
  broken statement never hides the others (``sqlglot.parse`` would fail the whole script).
* Every statement is parsed with a dialect chain (DuckDB first, then generic /
  Postgres / T-SQL / MySQL). A statement is a *syntax error* only if it is invalid
  in every dialect, is not a statement at all, or is structurally incomplete
  (e.g. ``CREATE TABLE`` with no name/columns).
* SELECT statements are analysed step by step (base table -> JOIN 1 -> JOIN 2 ... ->
  WHERE -> GROUP BY / AGGREGATE) with CUMULATIVE queries, so the runner can count rows
  after each stage. CTEs, UNION branches and the SELECT inside CTAS / INSERT..SELECT /
  CREATE VIEW are analysed in their own ``scope``.
* Every other statement becomes one step carrying the ORIGINAL text, plus optional
  ``alternative_queries`` the runner may try when DuckDB lacks the exact syntax
  (e.g. ``CREATE DATABASE x`` -> ``ATTACH ':memory:' AS x``).
"""

import re

import sqlglot
from sqlglot import expressions as exp

DEFAULT_DIALECT = "duckdb"
EXECUTION_DIALECT = "duckdb"
FALLBACK_DIALECTS = (None, "postgres", "tsql", "mysql")


def _types(*names):
    """Resolve expression classes by name (tolerates sqlglot versions that lack some)."""
    return tuple(getattr(exp, name) for name in names if hasattr(exp, name))


SELECT_TYPES = _types("Select")
SET_OPERATIONS = _types("Union", "Intersect", "Except")
QUERY_TYPES = SELECT_TYPES + SET_OPERATIONS
_NULL_REJECTING = _types("EQ", "NEQ", "GT", "GTE", "LT", "LTE", "Like", "ILike", "In", "Between")

_DDL_COMMANDS = {"CREATE", "ALTER", "DROP", "TRUNCATE", "RENAME", "COMMENT"}
_DML_COMMANDS = {"INSERT", "UPDATE", "DELETE", "MERGE", "COPY", "REPLACE"}
_READ_ONLY_COMMANDS = {"SHOW", "DESCRIBE", "DESC", "SUMMARIZE", "EXPLAIN"}
_NEEDS_BODY_COMMANDS = {"CREATE", "ALTER", "DROP", "TRUNCATE", "INSERT", "UPDATE", "DELETE", "USE"}
_ADMIN_NODES = (
    "Use", "Set", "SetItem", "Pragma", "Attach", "Detach", "Transaction", "Commit", "Rollback",
    "Grant", "Revoke", "Analyze", "Cache", "Uncache", "Refresh", "Kill", "Execute", "Declare",
    "Export", "Install", "LoadData",
)
_READ_ONLY_ADMIN_NODES = ("Show", "Describe", "Summarize", "Explain")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _arg(node, *names):
    """Read an arg under any of several names (sqlglot renamed 'from' -> 'from_', 'with' -> 'with_')."""
    for name in names:
        value = node.args.get(name)
        if value is not None:
            return value
    return None


def _without_with(node):
    clone = node.copy()
    for key in ("with_", "with"):
        if key in clone.args:
            clone.set(key, None)
    return clone


def _quote(name):
    return '"' + str(name).replace('"', '""') + '"'


def _wrap_count(prefix, sql):
    return f"{prefix}SELECT COUNT(*) FROM ({sql}) AS _echo_q"


def _conjuncts(node):
    node = node.unnest()
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    return [node]


def _is_null_rejecting(cond):
    """True when the predicate turns NULL (an unmatched LEFT JOIN row) into 'not true'."""
    cond = cond.unnest()
    if isinstance(cond, exp.Not):
        inner = cond.this.unnest()
        return isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null)
    for risky in _types("Coalesce", "Case", "Or"):
        if cond.find(risky):
            return False
    return isinstance(cond, _NULL_REJECTING)


def _is(node, *names):
    types = _types(*names)
    return bool(types) and isinstance(node, types)


def _object_name(node):
    if node is None:
        return None
    if isinstance(node, str):
        cleaned = node.strip().strip('"').strip("`")
        return cleaned or None
    if _is(node, "Schema", "Table"):
        if getattr(node, "name", None):
            return node.name
        if getattr(node, "this", None) is not None and node.this is not node:
            found = _object_name(node.this)
            if found:
                return found
    name = getattr(node, "name", None)
    if name:
        return name
    this_node = getattr(node, "this", None)
    if this_node is not None and this_node is not node:
        found = _object_name(this_node)
        if found:
            return found
    # Fallback أخير: لو الـ node له نص/sql ومش فاضي
    sql_val = getattr(node, "sql", None)
    if callable(sql_val):
        try:
            text = str(sql_val()).strip().strip('"').strip("`")
            if text and text.upper() not in ("TABLE", "VIEW", "INDEX", "DATABASE", "SCHEMA"):
                return text
        except Exception:
            pass
    return None

def _command_verb(node):
    verb = node.this if isinstance(node.this, str) else getattr(node, "name", "")
    return str(verb or "").strip().upper()


def _command_rest(node):
    rest = node.args.get("expression")
    return (getattr(rest, "name", "") or "").strip() if rest is not None else ""


# --------------------------------------------------------------------------- #
# Statement splitting (quote / comment / dollar-quote aware)
# --------------------------------------------------------------------------- #
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_]*\$")
_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_GO_LINE = re.compile(r"(?im)^[ \t]*GO[ \t]*;?[ \t]*$")


def _is_blank(chunk):
    return not _COMMENTS.sub("", chunk).strip()


def split_statements(sql):
    """Split a script on top-level semicolons (and T-SQL ``GO`` lines).

    Semicolons inside '...', "...", `...`, $$...$$, -- comments and /* */ comments are
    ignored. Comment-only / empty pieces are dropped. Unterminated quotes simply run to
    the end of the script, so the broken statement is reported as a syntax error later.
    """
    pieces, start, i, n = [], 0, 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if ch == "-" and nxt == "-":
            end = sql.find("\n", i)
            i = n if end == -1 else end
        elif ch == "/" and nxt == "*":
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif ch in ("'", '"', "`"):
            j = i + 1
            while j < n:
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:  # doubled quote = escaped quote
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
        elif ch == "$":
            match = _DOLLAR_TAG.match(sql, i)
            if match:
                close = sql.find(match.group(0), match.end())
                i = n if close == -1 else close + len(match.group(0))
            else:
                i += 1
        elif ch == ";":
            pieces.append(sql[start:i])
            start = i + 1
            i += 1
        else:
            i += 1
    pieces.append(sql[start:])

    statements = []
    for piece in pieces:
        for part in _GO_LINE.split(piece):
            if part.strip() and not _is_blank(part):
                statements.append(part.strip())
    return statements


# --------------------------------------------------------------------------- #
# Per-statement parsing, classification, validation
# --------------------------------------------------------------------------- #
def _dialect_chain(dialect, fallbacks):
    chain = [dialect]
    chain.extend(d for d in (fallbacks or ()) if d not in chain)
    return chain


def _parse_statement(chunk, dialects):
    """Try each dialect. Returns (node, dialect_used, first_error)."""
    first_error = None
    for dialect in dialects:
        try:
            node = sqlglot.parse_one(chunk, read=dialect)
        except Exception as exc:  # noqa: BLE001
            first_error = first_error or exc
            continue
        if node is not None:
            return node.unnest(), dialect, None
    return None, None, first_error


def _info(kind, step_type, read_only):
    return {"kind": kind, "step_type": step_type, "read_only": read_only}


def _classify(node):
    """Map a parsed node to {kind, step_type, read_only}; None = not a SQL statement."""
    if isinstance(node, QUERY_TYPES):
        return _info("SELECT", "QUERY", True)
    if _is(node, "Insert"):
        return _info("INSERT", "DML", False)
    if _is(node, "Update"):
        return _info("UPDATE", "DML", False)
    if _is(node, "Delete"):
        return _info("DELETE", "DML", False)
    if _is(node, "Merge"):
        return _info("MERGE", "DML", False)
    if _is(node, "Copy"):
        return _info("COPY", "DML", False)
    if _is(node, "Create"):
        return _info("CREATE_" + str(node.args.get("kind") or "OBJECT").upper(), "DDL", False)
    if _is(node, "Drop"):
        return _info("DROP_" + str(node.args.get("kind") or "OBJECT").upper(), "DDL", False)
    if _is(node, "Alter", "AlterTable"):
        return _info("ALTER", "DDL", False)
    if _is(node, "TruncateTable"):
        return _info("TRUNCATE", "DDL", False)
    if _is(node, "Command"):
        verb = _command_verb(node)
        if verb in _DDL_COMMANDS:
            return _info(verb, "DDL", False)
        if verb in _DML_COMMANDS:
            return _info(verb, "DML", False)
        return _info(verb or "COMMAND", "ADMIN", verb in _READ_ONLY_COMMANDS)
    if _is(node, *_READ_ONLY_ADMIN_NODES):
        return _info(type(node).__name__.upper(), "ADMIN", True)
    if _is(node, *_ADMIN_NODES):
        return _info(type(node).__name__.upper(), "ADMIN", False)
    return None


def _validate(node, info):
    """Return a message when the statement is structurally incomplete, else None."""
    kind = info["kind"]
    if _is(node, "Command"):
        verb = _command_verb(node)
        if verb in _NEEDS_BODY_COMMANDS and not _command_rest(node):
            return f"Incomplete {verb} statement."
        return None
    if _is(node, "Create"):
        target = node.this
        if target is None or not _object_name(target):
            return "CREATE requires an object name."
        if kind == "CREATE_TABLE":
            has_columns = _is(target, "Schema") and any(
                _is(e, "ColumnDef") for e in target.expressions
            )
            has_source = node.args.get("expression") is not None
            has_extras = bool(node.args.get("properties") or node.args.get("clone"))
            if not (has_columns or has_source or has_extras):
                return "CREATE TABLE requires a column list or AS SELECT."
        if kind == "CREATE_VIEW" and node.args.get("expression") is None:
            return "CREATE VIEW requires AS SELECT."
    elif _is(node, "Drop"):
        targets = [node.this] if node.this is not None else []
        targets.extend(node.args.get("tables") or [])
        targets.extend(node.args.get("expressions") or [])
        if not any(_object_name(t) for t in targets):
            return "DROP requires an object name."
    elif _is(node, "Insert"):
        if node.this is None:
            return "INSERT requires a target table."
        if node.args.get("expression") is None and not node.args.get("default"):
            return "INSERT requires VALUES or a SELECT."
    elif _is(node, "Update"):
        if node.this is None or not node.args.get("expressions"):
            return "UPDATE requires a table and a SET clause."
    elif _is(node, "Delete"):
        if node.this is None:
            return "DELETE requires a target table."
    elif _is(node, "Use"):
        if node.this is None:
            return "USE requires a database name."
    elif _is(node, "Alter", "AlterTable"):
        if node.this is None:
            return "ALTER requires an object name."
    return None


def _alternatives(node, info, original_sql, parsed_dialect, write_dialect):
    """Other ways to run the statement when DuckDB rejects the original text.

    Returns a list of alternatives; each alternative is a list of SQL strings that the
    runner executes in order as one unit.
    """
    alternatives = []
    kind = info["kind"]
    try:
        if _is(node, "Create") and kind in ("CREATE_DATABASE",):
            name = _object_name(node.this)
            if name:
                exists = "IF NOT EXISTS " if node.args.get("exists") else ""
                alternatives.append([f"ATTACH {exists}':memory:' AS {_quote(name)}"])
        elif _is(node, "Drop") and kind == "DROP_DATABASE":
            name = _object_name(node.this)
            if name:
                exists = "IF EXISTS " if node.args.get("exists") else ""
                alternatives.append([f"DETACH DATABASE {exists}{_quote(name)}"])
        elif kind == "TRUNCATE":
            tables = []
            if _is(node, "TruncateTable"):
                tables = [t.sql(dialect=write_dialect) for t in node.expressions]
            elif _is(node, "Command"):
                rest = re.sub(r"(?i)^\s*TABLE\s+", "", _command_rest(node))
                tables = [t.strip() for t in rest.split(",") if t.strip()]
            if tables:
                alternatives.append([f"DELETE FROM {table}" for table in tables])
        if parsed_dialect != write_dialect and not _is(node, "Command"):
            transpiled = node.sql(dialect=write_dialect)
            if transpiled and transpiled.strip().rstrip(";") != original_sql.strip().rstrip(";"):
                alternatives.append([transpiled])
    except Exception:  # noqa: BLE001 - alternatives are best-effort only
        pass
    return alternatives


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def check_query_is_read_only(sql, dialect=DEFAULT_DIALECT):
    """Return ``(ok, reason)``: True only for one read-only statement (SELECT / SHOW / ...)."""
    chunks = split_statements(sql)
    if len(chunks) != 1:
        return False, "Exactly one SQL statement is allowed."
    node, _, error = _parse_statement(chunks[0], _dialect_chain(dialect, FALLBACK_DIALECTS))
    if node is None:
        return False, f"Invalid SQL syntax: {error}"
    info = _classify(node)
    if info and info["read_only"]:
        return True, ""
    return False, "Only read-only statements are allowed."


def parse_sql_to_steps(
    sql_query,
    dialect=DEFAULT_DIALECT,
    fallback_dialects=FALLBACK_DIALECTS,
    write_dialect=EXECUTION_DIALECT,
):
    """Break a script into ordered, executable steps. Never raises on bad SQL.

    Raises ValueError only for empty / non-string input.
    """
    if not isinstance(sql_query, str) or not sql_query.strip():
        raise ValueError("SQL query must be a non-empty string.")

    statements = split_statements(sql_query)
    builder = _StepBuilder(write_dialect, multi=len(statements) > 1)
    if not statements:
        builder.begin_statement(1, "")
        builder.syntax_error(sql_query.strip(), "No SQL statement found (only comments / whitespace).")
        return builder.steps

    dialects = _dialect_chain(dialect, fallback_dialects)
    for index, chunk in enumerate(statements, 1):
        builder.begin_statement(index, chunk)
        node, used, error = _parse_statement(chunk, dialects)
        if node is None:
            builder.syntax_error(chunk, f"Invalid SQL syntax: {error}")
            continue
        info = _classify(node)
        if info is None:
            builder.syntax_error(chunk, "Not a recognised SQL statement.")
            continue
        problem = _validate(node, info)
        if problem:
            builder.syntax_error(chunk, problem)
            continue
        builder.add_statement(node, chunk, info, used)
    return builder.steps


# --------------------------------------------------------------------------- #
# Step builder
# --------------------------------------------------------------------------- #
class _StepBuilder:
    def __init__(self, write_dialect, multi):
        self.dialect = write_dialect
        self.multi = multi
        self.steps = []
        self.recursive = False
        self.statement_index = 1
        self.statement_kind = None
        self.main_scope = "main"
        self._counter = 0

    # ---- plumbing --------------------------------------------------------- #
    def begin_statement(self, index, chunk):
        self.statement_index = index
        self.statement_kind = None
        self.recursive = False
        self.main_scope = f"stmt_{index}" if self.multi else "main"

    def _cte_scope(self, name):
        return f"{self.main_scope}/cte:{name}" if self.multi else f"cte:{name}"

    def _sql(self, node):
        return node.sql(dialect=self.dialect)

    def _add(self, **step):
        self._counter += 1
        record = {
            "step_id": f"step_{self._counter}",
            "statement_index": self.statement_index,
            "statement_kind": self.statement_kind,
            "status": "success",
            "read_only": True,
        }
        record.update(step)
        self.steps.append(record)
        return record

    def _prefix(self, ctes):
        if not ctes:
            return ""
        keyword = "WITH RECURSIVE " if self.recursive else "WITH "
        return keyword + ", ".join(self._sql(c) for c in ctes) + " "

    def syntax_error(self, sql, message):
        self.statement_kind = "SYNTAX_ERROR"
        return self._add(
            step_type="SYNTAX_ERROR",
            name="syntax_error",
            scope=self.main_scope,
            description="Failed to parse the statement.",
            sql_snippet=sql,
            executable_query=sql,
            error=message,
            status="failed",
            read_only=False,
        )

    # ---- statements ------------------------------------------------------- #
    def add_statement(self, node, sql, info, parsed_dialect):
        self.statement_kind = info["kind"]
        if info["kind"] == "SELECT":
            self.analyze(node, self.main_scope, [])
            return

        # CTAS / INSERT ... SELECT / CREATE VIEW: analyse the embedded query first.
        if _is(node, "Create", "Insert"):
            source = node.args.get("expression")
            source = source.unnest() if source is not None else None
            if source is not None and isinstance(source, QUERY_TYPES):
                with_node = _arg(node, "with_", "with")
                ctes = list(with_node.expressions) if with_node is not None else []
                self.recursive = bool(with_node is not None and with_node.args.get("recursive"))
                self.statement_kind = "SELECT"
                self.analyze(source, f"{self.main_scope}/source", ctes)
                self.statement_kind = info["kind"]

        self._add(
            step_type=info["step_type"],
            name=info["kind"].lower(),
            scope=self.main_scope,
            description=f"Execute {info['kind'].replace('_', ' ')} statement",
            sql_snippet=sql,
            executable_query=sql,
            read_only=info["read_only"],
            parsed_dialect=parsed_dialect,
            alternative_queries=_alternatives(node, info, sql, parsed_dialect, self.dialect),
        )

    # ---- SELECT entry points ---------------------------------------------- #
    def analyze(self, node, scope, ctes):
        node = node.unnest()
        ctes = list(ctes)
        with_node = _arg(node, "with_", "with")
        if with_node is not None:
            if with_node.args.get("recursive"):
                self.recursive = True
            for cte in with_node.expressions:
                cte_scope = self._cte_scope(cte.alias)
                body = cte.this.unnest()
                # Recursive CTEs reference themselves, so only their result is traced.
                if not self.recursive and isinstance(body, QUERY_TYPES):
                    self.analyze(body, cte_scope, ctes)
                ctes.append(cte)
                prefix = self._prefix(ctes)
                name = _quote(cte.alias)
                self._add(
                    step_type="CTE",
                    name=cte.alias,
                    scope=cte_scope,
                    description=f"Result of Common Table Expression '{cte.alias}'",
                    sql_snippet=self._sql(cte),
                    executable_query=f"{prefix}SELECT * FROM {name}",
                    count_query=f"{prefix}SELECT COUNT(*) FROM {name}",
                    with_prefix=prefix,
                )
        if isinstance(node, SET_OPERATIONS):
            self._analyze_set_operation(node, scope, ctes)
        else:
            self._analyze_select(node, scope, ctes)

    def _flatten_set_operation(self, node):
        branches = []

        def walk(current):
            current = current.unnest()
            if isinstance(current, SET_OPERATIONS):
                walk(current.this)
                walk(current.expression)
            else:
                branches.append(current)

        walk(node)
        return branches

    def _analyze_set_operation(self, node, scope, ctes):
        prefix = self._prefix(ctes)
        for index, branch in enumerate(self._flatten_set_operation(node), 1):
            branch_scope = f"{scope}/branch_{index}"
            if isinstance(branch, SELECT_TYPES):
                self._analyze_select(branch, branch_scope, ctes)
            sql = self._sql(_without_with(branch))
            self._add(
                step_type="UNION_PART",
                name=f"branch_{index}",
                scope=branch_scope,
                description=f"Result of set-operation branch {index}",
                sql_snippet=sql,
                executable_query=f"{prefix}{sql}",
                count_query=_wrap_count(prefix, sql),
                with_prefix=prefix,
            )
        full = self._sql(_without_with(node))
        self._add(
            step_type="SET_OPERATION",
            name="set_operation",
            scope=scope,
            description="Combined result of UNION / INTERSECT / EXCEPT",
            sql_snippet=full,
            executable_query=f"{prefix}{full}",
            count_query=_wrap_count(prefix, full),
            with_prefix=prefix,
        )

    # ---- SELECT analysis -------------------------------------------------- #
    def _analyze_select(self, select, scope, ctes):
        prefix = self._prefix(ctes)
        from_node = _arg(select, "from_", "from")
        joins = list(select.args.get("joins") or [])
        where = select.args.get("where")
        group = select.args.get("group")
        measures = self._measures(select)
        has_aggregate = bool(measures) or self._has_aggregate(select)
        primary = next((m for m in measures if m["func"] == "SUM"), None)
        measure_expr = primary["expression"] if primary else None

        if from_node is None:  # e.g. SELECT 1
            sql = self._sql(_without_with(select))
            self._add(
                step_type="QUERY",
                name="main_query",
                scope=scope,
                description="Query without a FROM clause",
                sql_snippet=sql,
                executable_query=f"{prefix}{sql}",
                count_query=_wrap_count(prefix, sql),
                with_prefix=prefix,
            )
            return

        # ---- base table ---------------------------------------------------- #
        base = from_node.this
        base_info = self._relation_info(base)
        alias_map = {base_info["alias"]: base_info}
        from_sql = self._sql(base)
        self._flow_step(
            "BASE_TABLE", base_info["alias"] or base_info["table"] or "base",
            f"Load base relation '{base_info['alias'] or base_info['table']}'",
            self._sql(from_node), scope, prefix, from_sql, measure_expr, alias_map,
        )

        # ---- joins (cumulative) --------------------------------------------- #
        for join in joins:
            target = join.this
            info = self._relation_info(target)
            join_alias = info["alias"]
            left_aliases = list(alias_map.keys())
            alias_map[join_alias] = info

            on = join.args.get("on")
            using = join.args.get("using")
            side = (join.side or "").upper()
            kind = (join.kind or "").upper()
            method = (join.method or "").upper()

            explicit_cross = kind == "CROSS"
            implicit_cross = (not explicit_cross and on is None and not using and not method
                              and isinstance(target, (exp.Table, exp.Subquery)))
            if explicit_cross or implicit_cross:
                join_type = "CROSS"
            elif side:
                join_type = side
            elif kind in ("SEMI", "ANTI"):
                join_type = kind
            else:
                join_type = "INNER"

            if implicit_cross:
                join_sql = f"CROSS JOIN {self._sql(target)}"
            else:
                join_sql = self._sql(join)
            from_sql = f"{from_sql} {join_sql}"

            pairs, complex_condition = self._key_pairs(on, using, join_alias)
            details = {
                "join_type": join_type,
                "side": side,
                "kind": kind,
                "target_table": info["table"] or join_alias,
                "target_alias": join_alias,
                "target_relation_sql": info["relation_sql"],
                "on_condition": self._sql(on) if on is not None else None,
                "using": [u.name for u in using] if using else [],
                "key_pairs": pairs,
                "complex_condition": complex_condition,
                "left_aliases": left_aliases,
                "explicit_cross": explicit_cross,
                "implicit_cross": implicit_cross,
                "links_both_sides": self._links_both_sides(on, using, method, target, join_alias),
            }
            self._flow_step(
                "JOIN", join_alias or info["table"] or "join",
                f"Perform {join_type} JOIN with '{info['table'] or join_alias}'",
                self._sql(join), scope, prefix, from_sql, measure_expr, alias_map,
                join_details=details,
            )

        # ---- where ---------------------------------------------------------- #
        if where is not None:
            conjuncts = [
                {
                    "sql": self._sql(c),
                    "aliases": sorted({col.table for col in c.find_all(exp.Column)}),
                    "null_rejecting": _is_null_rejecting(c),
                }
                for c in _conjuncts(where.this)
            ]
            self._flow_step(
                "WHERE", "filter", "Filter rows with the WHERE clause",
                self._sql(where), scope, prefix, from_sql, measure_expr, alias_map,
                where_sql=self._sql(where.this),
                where_details={"condition": self._sql(where.this), "conjuncts": conjuncts},
            )

        # ---- group by / aggregate -------------------------------------------- #
        if group is not None or has_aggregate:
            full = self._sql(_without_with(select))
            final_alias = primary["alias"] if primary else None
            measure_query = None
            if final_alias:
                measure_query = (
                    f"{prefix}SELECT SUM({_quote(final_alias)}) FROM ({full}) AS _echo_final"
                )
            group_exprs = list(group.expressions) if group is not None else []
            self._add(
                step_type="GROUP_BY" if group is not None else "AGGREGATE",
                name="aggregation",
                scope=scope,
                description="Group rows and compute aggregates" if group is not None
                else "Compute aggregates over all rows",
                sql_snippet=self._sql(group) if group is not None else self._sql(select),
                executable_query=f"{prefix}{full}",
                count_query=_wrap_count(prefix, full),
                measure_query=measure_query,
                with_prefix=prefix,
                alias_map=dict(alias_map),
                measures=measures,
                group_details={
                    "group_by": [self._sql(g) for g in group_exprs],
                    "group_aliases": sorted(
                        {c.table for g in group_exprs for c in g.find_all(exp.Column)}
                    ),
                },
            )

    # ---- builders --------------------------------------------------------- #
    def _flow_step(self, step_type, name, description, snippet, scope, prefix, from_sql,
                   measure_expr, alias_map, where_sql=None, **extra):
        tail = f" WHERE {where_sql}" if where_sql else ""
        return self._add(
            step_type=step_type,
            name=name,
            scope=scope,
            description=description,
            sql_snippet=snippet,
            executable_query=f"{prefix}SELECT * FROM {from_sql}{tail}",
            count_query=f"{prefix}SELECT COUNT(*) FROM {from_sql}{tail}",
            measure_query=(
                f"{prefix}SELECT SUM({measure_expr}) FROM {from_sql}{tail}" if measure_expr else None
            ),
            with_prefix=prefix,
            alias_map=dict(alias_map),
            **extra,
        )

    def _relation_info(self, rel):
        if isinstance(rel, exp.Table):
            bare = rel.copy()
            bare.set("alias", None)
            return {
                "alias": rel.alias or rel.name,
                "relation_sql": self._sql(bare),
                "kind": "table",
                "table": rel.name or None,
            }
        if isinstance(rel, exp.Subquery):
            return {
                "alias": rel.alias or "",
                "relation_sql": "(" + self._sql(rel.this) + ")",
                "kind": "subquery",
                "table": None,
            }
        return {"alias": rel.alias or "", "relation_sql": self._sql(rel), "kind": "other", "table": None}

    def _key_pairs(self, on, using, join_alias):
        pairs, complex_condition = [], False
        for column in using or []:
            pairs.append({"left_alias": None, "left_column": column.name,
                          "right_alias": join_alias, "right_column": column.name})
        if on is None:
            return pairs, complex_condition
        for cond in _conjuncts(on):
            if (isinstance(cond, exp.EQ) and isinstance(cond.this, exp.Column)
                    and isinstance(cond.expression, exp.Column)):
                a, b = cond.this, cond.expression
                a_right, b_right = a.table == join_alias, b.table == join_alias
                if a_right and not b_right:
                    left, right = b, a
                elif b_right and not a_right:
                    left, right = a, b
                else:  # ambiguous or unrelated to the joined table
                    complex_condition = True
                    continue
                pairs.append({"left_alias": left.table or None, "left_column": left.name,
                              "right_alias": join_alias, "right_column": right.name})
            else:
                complex_condition = True
        return pairs, complex_condition

    def _links_both_sides(self, on, using, method, target, join_alias):
        if using or method:
            return True
        if not isinstance(target, (exp.Table, exp.Subquery)):
            return True  # LATERAL / UNNEST: not judged here
        if on is None:
            return False
        qualifiers = {c.table for c in on.find_all(exp.Column)}
        if not qualifiers:
            return False  # e.g. ON 1 = 1
        if "" in qualifiers:
            return True  # unqualified columns: cannot tell, do not accuse
        touches_right = join_alias in qualifiers
        touches_left = any(q != join_alias for q in qualifiers)
        return touches_right and touches_left

    def _direct_aggs(self, select, *types):
        found = []
        for expression in select.expressions:
            for agg in expression.find_all(*types):
                if agg.find_ancestor(exp.Window) is not None:
                    continue
                if agg.find_ancestor(exp.Select) is not select:
                    continue  # belongs to a sub-query
                found.append((expression, agg))
        return found

    def _has_aggregate(self, select):
        return bool(self._direct_aggs(select, exp.AggFunc))

    def _measures(self, select):
        """Duplication-sensitive aggregates: SUM(x), AVG(x), COUNT(x) (not COUNT(*)/DISTINCT)."""
        measures = []
        for expression, agg in self._direct_aggs(select, exp.Sum, exp.Avg, exp.Count):
            argument = agg.this
            if argument is None or isinstance(argument, (exp.Star, exp.Distinct)):
                continue
            columns = list(argument.find_all(exp.Column))
            measures.append({
                "func": type(agg).__name__.upper(),
                "expression": self._sql(argument),
                "tables": sorted({c.table for c in columns}),
                "columns": [{"table": c.table, "name": c.name} for c in columns],
                # The output alias is only reliable when the select item IS the aggregate.
                "alias": (expression.alias or None)
                if (expression is agg or getattr(expression, "this", None) is agg) else None,
            })
        return measures