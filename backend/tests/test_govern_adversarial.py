"""Adversarial unit tests for governance/engine.govern() — the single choke point.

Pure unit tests: govern() is called directly with a small fixture MDL, no HTTP/app.
Every case asserts either the exact WrenError code or a provably-safe output.

Run:  cd backend && python -m pytest tests/test_govern_adversarial.py -q
(or:  python tests/test_govern_adversarial.py  standalone without pytest)

FIX NOTED (found by this suite): govern() previously did NOT reject non-SELECT
statements — Wren's policy.py only validates table references + denied functions, and
only the DuckDB connector opens read-only, so `INSERT INTO <mdl-table> ...` would have
executed on Postgres/MySQL/BigQuery tenants. engine.py now enforces read-only up front
(`_enforce_read_only`: SELECT/set-op root + no DML/DDL/command node anywhere) and
raises READ_ONLY. It also parses inside try/except (SQL_PARSE_ERROR instead of a raw
sqlglot exception) and applies RLAC to set-operation roots by wrapping them in a
subquery (exp.Union has no .where()).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402
from app.governance.engine import govern  # noqa: E402
from app.governance._compat import WrenError  # noqa: E402

MDL = {
    "tables": {
        "customers": ["customer_id", "name", "email", "region"],
        "orders": ["order_id", "customer_id", "amount", "region"],
        "internal_costs": ["cost_id", "item", "amount"],   # in MDL but outside allowlist
    },
    "allowed_tables": ["customers", "orders"],
    "pii_columns": ["email", "name"],
    "denied_functions": ["version", "current_user"],
}
ROLE = {"can_see_pii": False, "denied_columns": ["amount"], "row_filters": {}}
ROLE_RLS = {"can_see_pii": False, "denied_columns": [], "row_filters": {"region": "Jakarta"}}
ROLE_TBL = {"can_see_pii": False, "denied_columns": [], "row_filters": {}, "authorized_tables": ["orders"]}


def _denied(sql, role=ROLE, code=None, dialect="duckdb"):
    try:
        out = govern(sql, dialect, MDL, role)
    except WrenError as e:
        if code:
            assert e.code == code, f"expected {code}, got {e.code} for: {sql}"
        return e
    raise AssertionError(f"NOT BLOCKED (returned: {out}) for: {sql}")


# ---- star projections cannot smuggle PII / denied columns ----
def test_select_star_blocked():
    _denied("SELECT * FROM customers", code="CLAC_DENIED")

def test_table_star_blocked():
    _denied("SELECT c.* FROM customers c", code="CLAC_DENIED")

def test_count_star_reads_no_column_and_passes():
    # COUNT(*) is a row count, not a projection: it must not expand to the PII columns
    out = govern("SELECT region, COUNT(*) AS n FROM customers GROUP BY 1", "duckdb", MDL, ROLE)
    assert "COUNT(*)" in out.upper()

def test_count_star_does_not_cover_a_real_star_or_a_pii_filter():
    _denied("SELECT COUNT(*), * FROM customers GROUP BY ALL", code="CLAC_DENIED")
    _denied("SELECT COUNT(*) FROM customers WHERE email = 'a@b.com'", code="CLAC_DENIED")
    _denied("SELECT n FROM (SELECT *, COUNT(*) OVER () AS n FROM customers) t", code="CLAC_DENIED")

def test_star_via_join_blocked():
    _denied("SELECT * FROM orders o JOIN customers c ON o.customer_id = c.customer_id", code="CLAC_DENIED")


# ---- UNION smuggling ----
def test_union_smuggles_pii_column():
    _denied("SELECT region FROM orders UNION ALL SELECT email FROM customers", code="CLAC_DENIED")

def test_union_smuggles_forbidden_table():
    # internal_costs is outside allowed_tables -> Wren policy rejects (MODEL_NOT_FOUND)
    _denied("SELECT region FROM orders UNION ALL SELECT item FROM internal_costs", code="MODEL_NOT_FOUND")


# ---- CTEs and nested subqueries ----
def test_cte_smuggles_pii():
    _denied("WITH x AS (SELECT email FROM customers) SELECT * FROM x", code="CLAC_DENIED")

def test_nested_subquery_smuggles_pii():
    _denied("SELECT r FROM (SELECT email AS r FROM customers) t", code="CLAC_DENIED")


# ---- identifier quoting / case tricks ----
def test_quoted_mixedcase_identifiers_blocked():
    # whatever layer rejects it (policy's case-sensitive table match OR CLAC), it must NOT pass
    _denied('SELECT "EMAIL" FROM "Customers"')

def test_backticked_mysql_identifiers_blocked():
    _denied("SELECT `Email` FROM `customers`", dialect="mysql")

def test_mixedcase_plain_identifiers_blocked():
    _denied("SELECT EMAIL FROM CUSTOMERS")


# ---- comments cannot hide identifiers ----
def test_block_comment_around_pii():
    _denied("SELECT /* harmless */ email /* :) */ FROM customers", code="CLAC_DENIED")

def test_line_comment_around_pii():
    _denied("SELECT email -- just metadata\nFROM customers", code="CLAC_DENIED")


# ---- per-role table authorization ----
def test_join_outside_authorized_tables():
    _denied("SELECT o.region FROM orders o JOIN customers c ON o.customer_id = c.customer_id",
            role=ROLE_TBL, code="ROLE_TABLE_DENIED")


# ---- non-SELECT statements (the bypass this suite exposed; now READ_ONLY) ----
def test_insert_rejected():
    _denied("INSERT INTO customers VALUES (9,'x','x@x.x','Jakarta')", code="READ_ONLY")

def test_update_rejected():
    _denied("UPDATE orders SET amount = 0", code="READ_ONLY")

def test_delete_rejected():
    _denied("DELETE FROM orders", code="READ_ONLY")

def test_ddl_rejected():
    _denied("DROP TABLE orders", code="READ_ONLY")
    _denied("CREATE TABLE evil AS SELECT region FROM orders", code="READ_ONLY")

def test_cte_wrapped_dml_rejected():
    _denied("WITH x AS (SELECT region FROM orders) DELETE FROM orders", code="READ_ONLY")


# ---- denied functions still blocked (Wren policy, Apache-2.0, reused untouched) ----
def test_denied_function_blocked():
    _denied("SELECT version()", code="BLOCKED_FUNCTION")


# ---- RLAC row filters: AST predicates, injection-proof literals ----
def test_row_filter_is_parenthesized_ast_predicate():
    out = govern("SELECT region, amount FROM orders", "duckdb", MDL, ROLE_RLS)
    parsed = sqlglot.parse_one(out, dialect="duckdb")
    where = parsed.find(exp.Where)
    assert where is not None, out
    eq = where.this
    assert isinstance(eq, exp.EQ), f"expected a single EQ predicate, got: {out}"
    assert eq.this.name.lower() == "region"
    lit = eq.expression
    assert isinstance(lit, exp.Literal) and lit.is_string and lit.this == "Jakarta", out

def test_malicious_row_filter_value_stays_a_literal():
    evil = {"can_see_pii": False, "denied_columns": [], "row_filters": {"region": "x' OR '1'='1"}}
    out = govern("SELECT region, amount FROM orders", "duckdb", MDL, evil)
    parsed = sqlglot.parse_one(out, dialect="duckdb")
    where = parsed.find(exp.Where)
    eq = where.this
    # the WHOLE payload must be ONE string literal — no OR predicate escaped into the tree
    assert isinstance(eq, exp.EQ) and isinstance(eq.expression, exp.Literal), out
    assert eq.expression.is_string and eq.expression.this == "x' OR '1'='1", out
    assert parsed.find(exp.Or) is None, f"injection escaped the literal: {out}"

def test_row_filter_applies_to_union_result():
    out = govern("SELECT region FROM orders UNION ALL SELECT region FROM customers",
                 "duckdb", MDL, ROLE_RLS)
    parsed = sqlglot.parse_one(out, dialect="duckdb")
    where = parsed.find(exp.Where)
    assert where is not None and "region = 'Jakarta'" in out, out


# ---- degenerate inputs ----
def test_empty_sql_is_no_sql():
    _denied("", code="NO_SQL")
    _denied("   \n\t  ", code="NO_SQL")

def test_garbage_sql_is_parse_error_not_crash():
    e = _denied("this is not sql at all ;;;")
    assert e.code in ("SQL_PARSE_ERROR", "READ_ONLY"), e.code

def test_multi_statement_rejected():
    try:
        out = govern("SELECT region FROM orders; DROP TABLE orders", "duckdb", MDL, ROLE_RLS)
    except Exception:
        return                       # rejected (parse error or READ_ONLY) — safe
    # if a permissive parser accepted it, the output must NOT contain the second statement
    assert "drop" not in out.lower(), out


# ---- sanity: a legitimate governed query still works end-to-end ----
def test_legitimate_query_passes_with_filters():
    out = govern("SELECT region, SUM(amount) AS total FROM orders GROUP BY region",
                 "duckdb", MDL, ROLE_RLS)
    assert "region = 'Jakarta'" in out and out.upper().startswith("SELECT")


# ---- `SELECT ... INTO` is a write wearing a SELECT root ----
# It parses as exp.Select carrying an `into` arg, so it clears the root-type check AND
# the write-node walk, and sqlglot renders it as CREATE TABLE ... AS SELECT on
# duckdb/mysql. Naming an ALREADY-ALLOWLISTED table as the destination also clears
# _check_tables, so the allowlist is not a backstop here.
def test_select_into_allowlisted_table_is_rejected():
    for dialect in ("duckdb", "tsql", "postgres", "mysql"):
        _denied("SELECT * INTO customers FROM orders", code="READ_ONLY", dialect=dialect)

def test_select_into_unknown_table_is_rejected():
    e = _denied("SELECT * INTO exfil FROM orders", dialect="tsql")
    assert e.code in ("READ_ONLY", "MODEL_NOT_FOUND"), e.code

def test_select_into_hidden_behind_a_cte_name_is_rejected():
    # policy._check_tables forgives any name matching a visible CTE — that allowance was
    # written for READ references and must not extend to a write destination.
    _denied("WITH exfil AS (SELECT 1 AS x) SELECT * INTO exfil FROM orders",
            code="READ_ONLY", dialect="tsql")

def test_select_for_update_is_rejected():
    # A governed analytics read must not take row locks on a customer's database.
    _denied("SELECT amount FROM orders FOR UPDATE", code="READ_ONLY", dialect="postgres")


# ---- RLAC must bind to the physical table at EVERY scope ----
def _assert_every_read_is_filtered(out, dialect="duckdb"):
    """Every physical reference to a filtered table must carry the predicate itself.

    Checking the rendered SQL for one `region = 'Jakarta'` is not enough: the bug this
    guards against left the predicate on the OUTER select while a nested scope read the
    table unfiltered, and that shape still contains the string.
    """
    parsed = sqlglot.parse_one(out, dialect=dialect)
    tables = [t for t in parsed.find_all(exp.Table)
              if (t.name or "").lower() in ("orders", "customers")]
    assert tables, f"no physical table reference found in: {out}"
    for table in tables:
        scope = table.find_ancestor(exp.Select)
        where = scope.args.get("where") if scope is not None else None
        assert where is not None and "region = 'Jakarta'" in where.sql(dialect=dialect), (
            f"unfiltered read of {table.name} in: {out}")
    return parsed

def test_row_filter_survives_alias_shadowing():
    # Re-projecting the filtered column as a constant made the injected predicate compare
    # 'Jakarta' with itself — true for every row, so the whole table leaked.
    out = govern("SELECT * FROM (SELECT order_id, amount, 'Jakarta' AS region FROM orders) x",
                 "duckdb", MDL, ROLE_RLS)
    _assert_every_read_is_filtered(out)

def test_row_filter_reaches_scalar_subquery():
    out = govern("SELECT region, (SELECT SUM(amount) FROM orders) AS total "
                 "FROM orders GROUP BY region", "duckdb", MDL, ROLE_RLS)
    _assert_every_read_is_filtered(out)

def test_row_filter_reaches_in_subquery():
    out = govern("SELECT amount FROM orders WHERE customer_id IN "
                 "(SELECT customer_id FROM customers)", "duckdb", MDL, ROLE_RLS)
    _assert_every_read_is_filtered(out)

def test_row_filter_reaches_cte_body():
    out = govern("WITH g AS (SELECT SUM(amount) AS s FROM orders) "
                 "SELECT g.s FROM g, orders o WHERE o.order_id > 0", "duckdb", MDL, ROLE_RLS)
    _assert_every_read_is_filtered(out)

def test_row_filter_reaches_both_sides_of_a_join():
    # Also a correctness fix: the old root-level predicate raised "ambiguous column"
    # whenever two joined tables both carried the filter column.
    out = govern("SELECT o.amount FROM orders o JOIN customers c "
                 "ON o.customer_id = c.customer_id", "duckdb", MDL, ROLE_RLS)
    _assert_every_read_is_filtered(out)
    assert " AS o" in out and " AS c" in out, out   # both aliases survive the rewrite

def test_row_filter_reaches_every_union_branch():
    out = govern("SELECT region FROM orders UNION ALL SELECT region FROM customers",
                 "duckdb", MDL, ROLE_RLS)
    _assert_every_read_is_filtered(out)

def test_table_aliases_are_preserved_by_the_rewrite():
    out = govern("SELECT o.amount FROM orders o WHERE o.order_id > 0",
                 "duckdb", MDL, ROLE_RLS)
    assert " AS o" in out, out          # outer references o.amount — the alias must survive


# ---- RLAC fails CLOSED when it cannot be applied ----
_MDL_DIM = {
    "tables": {"orders": ["order_id", "amount", "region"],
               "product_dim": ["product_id", "product_name"]},   # no `region` column
    "allowed_tables": ["orders", "product_dim"],
    "pii_columns": [], "denied_functions": [],
}

def test_unenforceable_row_filter_denies_instead_of_dropping():
    # Silently skipping the predicate turned "Jakarta only" into "everything in tables
    # that happen to lack a region column".
    try:
        govern("SELECT product_name FROM product_dim", "duckdb", _MDL_DIM, ROLE_RLS)
    except WrenError as e:
        assert e.code == "RLAC_UNENFORCEABLE", e.code
        return
    raise AssertionError("unenforceable row filter was silently dropped")

def test_rlac_strict_false_keeps_the_legacy_permissive_behaviour():
    role = {**ROLE_RLS, "rlac_strict": False}
    out = govern("SELECT product_name FROM product_dim", "duckdb", _MDL_DIM, role)
    assert "region" not in out, out

def test_cte_shadowing_a_filtered_table_is_refused():
    _denied("WITH orders AS (SELECT 'x' AS region) SELECT region FROM orders",
            role=ROLE_RLS, code="RLAC_UNENFORCEABLE")


# ---- table allowlist must not be satisfied by a foreign schema/catalog ----
# Every check reads exp.Table.name (the leaf), so `other_schema.customers` matches the
# allowlisted `customers` while reading somewhere else entirely.
_NS_PG = {"catalog": "appdb", "schema": "public"}
_NS_BQ = {"catalog": "my-proj", "schema": "my_ds"}

def _denied_qualified(sql, namespace, dialect):
    try:
        out = govern(sql, dialect, MDL, ROLE, namespace=namespace)
    except WrenError as e:
        assert e.code == "TABLE_QUALIFIER_DENIED", f"got {e.code} for: {sql}"
        return
    raise AssertionError(f"NOT BLOCKED (returned: {out}) for: {sql}")

def test_foreign_schema_qualifier_is_denied():
    _denied_qualified("SELECT region FROM other_schema.orders", _NS_PG, "postgres")

def test_foreign_catalog_qualifier_is_denied():
    _denied_qualified("SELECT region FROM otherdb.public.orders", _NS_PG, "postgres")

def test_foreign_bigquery_project_is_denied():
    _denied_qualified("SELECT region FROM `other-proj.other_ds.orders`", _NS_BQ, "bigquery")

def test_own_namespace_qualifier_is_allowed():
    out = govern("SELECT region FROM `my-proj.my_ds.orders`", "bigquery", MDL, ROLE,
                 namespace=_NS_BQ)
    assert "orders" in out, out

def test_missing_namespace_denies_every_qualifier():
    # A connector without namespace() gets the STRICTEST treatment, never a bypass.
    _denied_qualified("SELECT region FROM other_schema.orders", None, "postgres")


# ---- governance metadata must belong to the requesting tenant ----
def test_mdl_from_another_tenant_is_refused():
    try:
        govern("SELECT region FROM orders", "duckdb", {**MDL, "tenant_id": "t_other"},
               ROLE, tenant_id="t_mine")
    except WrenError as e:
        assert e.code == "MDL_TENANT_MISMATCH", e.code
        return
    raise AssertionError("mismatched tenant metadata was accepted")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    p = f = 0
    for fn in fns:
        try:
            fn(); print(f"  PASS  {fn.__name__}"); p += 1
        except Exception as e:
            print(f"  FAIL  {fn.__name__}: {e}"); f += 1
    print(f"\n{p} passed, {f} failed")
    sys.exit(1 if f else 0)
