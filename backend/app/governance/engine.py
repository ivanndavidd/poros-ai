"""Governed query engine: reuse Wren policy.py (Apache-2.0) + RLAC/CLAC per tenant/role.

This is the single choke-point every consumer query MUST pass through (hard rule).
It enforces, in order:
  (a) Wren strict policy  — only MDL tables, no denied functions  (reused, Apache-2.0)
  (b) CLAC                — block PII / denied columns for roles without permission
  (c) RLAC                — inject per-role row filters
"""
import sqlglot
from sqlglot import exp
from ..limits import governed_max_rows
from .policy import validate_sql_policy
from ._compat import WrenConfig, WrenError
from .functions import validate_function_allowlist

# Read-only enforcement (hard rule). Wren's policy.py only validates table references +
# denied functions — it does NOT reject DML/DDL, and only the DuckDB connector opens
# read-only, so `INSERT INTO <mdl-table> ...` would otherwise reach Postgres/MySQL/BigQuery.
# Version-tolerant: node classes are looked up by name (sqlglot renamed some across majors).
_READONLY_ROOTS = tuple(t for t in (getattr(exp, n, None) for n in ("Select", "SetOperation", "Union", "Subquery"))
                        if isinstance(t, type))
_WRITE_NODES = tuple(t for t in (getattr(exp, n, None) for n in
                                 ("Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "AlterTable",
                                  "TruncateTable", "Command", "Grant", "Set", "Use", "Call", "Transaction",
                                  "Rollback", "Commit", "Attach", "Detach", "Copy", "Export", "LoadData"))
                     if isinstance(t, type))


# Clauses a governed SELECT may carry. This is an ALLOWLIST on purpose: `SELECT ... INTO t`
# parses as an ordinary exp.Select holding an `into` arg, so it clears both the root-type
# check and the _WRITE_NODES walk — and sqlglot's generator then renders it as
# `CREATE TABLE t AS SELECT ...` for duckdb/mysql. Denying unknown clause names instead of
# naming the bad one also covers whatever side-effect clause a future sqlglot release adds.
# `locks` (SELECT ... FOR UPDATE) is deliberately absent: a governed analytics read must
# not take row locks on a customer's production database.
_SAFE_SELECT_ARGS = frozenset({
    "expressions", "from_", "from", "joins", "where", "group", "having", "order",
    "limit", "offset", "distinct", "qualify", "windows", "with_", "with", "hint", "kind",
})


def _enforce_read_only(ast: exp.Expression):
    """Reject anything that is not a pure read: the root must be a SELECT / set
    operation / subquery, NO write/DDL/command node may appear anywhere in the tree
    (covers `WITH x AS (...) INSERT ...`, CTAS, multi-part commands, etc.), and no
    SELECT may carry a clause outside the read-only allowlist."""
    if not isinstance(ast, _READONLY_ROOTS):
        raise WrenError("READ_ONLY", f"only SELECT statements are allowed; got {type(ast).__name__}")
    for node in ast.walk():
        n = node[0] if isinstance(node, tuple) else node   # sqlglot<20 walk() yields tuples
        if isinstance(n, _WRITE_NODES):
            raise WrenError("READ_ONLY", f"only SELECT statements are allowed; found {type(n).__name__} in query")
        if isinstance(n, exp.Select):
            unexpected = sorted(k for k, v in n.args.items()
                                if v is not None and k not in _SAFE_SELECT_ARGS)
            if unexpected:
                raise WrenError("READ_ONLY",
                                f"unsupported SELECT clause(s) {unexpected} in governed SQL")


def _referenced_tables(ast: exp.Expression, mdl: dict) -> list[str]:
    """MDL table names referenced by the query, resolved case-insensitively."""
    by_lower = {t.lower(): t for t in mdl.get("tables", {})}
    out: list[str] = []
    for t in ast.find_all(exp.Table):
        name = (t.name or "").lower()
        if name in by_lower and by_lower[name] not in out:
            out.append(by_lower[name])
    return out


def _selected_columns(ast: exp.Expression, mdl: dict) -> set[str]:
    """Lower-cased physical columns the query reads.

    Crucially this expands ``SELECT *`` / ``SELECT t.*`` against the MDL schema so a
    star projection cannot smuggle PII columns past CLAC (the original code missed
    this because ``SELECT *`` produces no ``exp.Column`` nodes).

    The star of ``COUNT(*)`` is not a projection: it counts rows and reads no column
    value, so it does not expand (expanding it refused every row count on a table that
    has a PII column for a role that may not see PII). Any other star still does.
    """
    cols = {c.name.lower() for c in ast.find_all(exp.Column) if c.name}
    if any(not isinstance(s.parent, exp.Count) for s in ast.find_all(exp.Star)):
        for t in _referenced_tables(ast, mdl):
            for col in mdl.get("tables", {}).get(t, []):
                cols.add(col.lower())
    return cols


def _enforce_model_table(ast: exp.Expression, mdl: dict):
    """A governed analytics query must read at least one real MDL table.

    This rejects constant-only SELECTs and CTEs built solely from constants. Besides
    being outside the product's data contract, those shapes are a common carrier for
    database side-effect functions that are callable from SELECT.
    """
    if not _referenced_tables(ast, mdl):
        raise WrenError("TABLE_REQUIRED",
                        "governed SQL must reference at least one table defined in the MDL")


def _reject_foreign_qualifiers(ast: exp.Expression, namespace: dict | None):
    """A qualified reference may only name the datasource's OWN schema/dataset/project.

    The MDL is keyed by bare table names and every allowlist check reads
    ``exp.Table.name`` — the leaf identifier — so ``other_schema.customers`` and
    ```other-project.other_ds.customers``` resolve against the allowlisted ``customers``
    while actually reading somewhere else entirely. sqlglot keeps the qualifier in the
    ``db``/``catalog`` args, which nothing else inspects.
    """
    # No namespace supplied (a connector that predates the method) is the STRICTEST
    # case, not a bypass: every qualifier then differs from "" and is refused, so bare
    # MDL names still work and nothing can point outside the datasource.
    own_schema = str((namespace or {}).get("schema") or "").strip().lower()
    own_catalog = str((namespace or {}).get("catalog") or "").strip().lower()
    for table in ast.find_all(exp.Table):
        db = (table.text("db") or "").strip().lower()
        catalog = (table.text("catalog") or "").strip().lower()
        if db and db != own_schema:
            raise WrenError("TABLE_QUALIFIER_DENIED",
                            f"schema/dataset '{db}' is outside this datasource")
        if catalog and catalog != own_catalog:
            raise WrenError("TABLE_QUALIFIER_DENIED",
                            f"catalog/project '{catalog}' is outside this datasource")


def _cte_names(ast: exp.Expression) -> set[str]:
    """Lower-cased names bound by any WITH clause in the query."""
    names: set[str] = set()
    for cte in ast.find_all(exp.CTE):
        alias = cte.args.get("alias")
        if alias is None:
            continue
        name = alias.this.name if isinstance(alias.this, exp.Identifier) else str(alias.this)
        if name:
            names.add(name.lower())
    return names


def _apply_row_filters(ast: exp.Expression, mdl: dict, role_policy: dict) -> exp.Expression:
    """RLAC: bind each row filter to the PHYSICAL table, at every scope that reads it.

    Appending a bare predicate to the query's ROOT (the previous approach) was defeated
    two ways. An enclosing scope can redefine the column —
    ``SELECT * FROM (SELECT 'Jakarta' AS region FROM orders) x`` makes the injected
    ``WHERE region = 'Jakarta'`` compare a constant with itself, so every row passes —
    and ``.where()`` never descends, so a CTE, derived table or scalar subquery read the
    table completely unfiltered.

    Rewriting every reference into ``(SELECT * FROM t WHERE col = 'val') AS t`` is
    scope-correct by construction: the predicate is applied BEFORE any outer scope can
    re-alias the column, and each nested read carries its own copy. It also fixes an
    ambiguous-column error that the root-level predicate caused when two joined tables
    both had the filter column.

    Filter VALUES stay AST literals, never interpolated text — that property is what
    keeps a value like ``x' OR '1'='1`` a single literal, and it is preserved here.
    """
    filters = role_policy.get("row_filters") or {}
    if not filters:
        return ast
    strict = role_policy.get("rlac_strict", True)
    by_lower = {t.lower(): t for t in mdl.get("tables", {})}
    cte_names = _cte_names(ast)

    for table_node in list(ast.find_all(exp.Table)):
        mdl_name = by_lower.get((table_node.name or "").lower())
        if mdl_name is None:
            continue                       # CTE alias or non-MDL reference
        replacement = _row_filtered_ref(table_node, mdl_name, mdl, filters, strict, cte_names)
        if replacement is not None:
            table_node.replace(replacement)
    return ast


def _row_filtered_ref(table_node: exp.Table, mdl_name: str, mdl: dict,
                      filters: dict, strict: bool, cte_names: set):
    """The filtered subquery that replaces ONE reference to a filtered MDL table.

    Returns None when no filter in the role policy applies to this table. Raises
    RLAC_UNENFORCEABLE rather than returning an unfiltered reference — see the two
    fail-closed cases below.
    """
    name = table_node.name or ""
    columns = {c.lower() for c in mdl.get("tables", {}).get(mdl_name, [])}
    unenforceable = sorted(col for col in filters if col.lower() not in columns)
    if unenforceable and strict:
        # Fail CLOSED. Dropping the predicate silently turned "Jakarta only" into
        # "everything in tables without a region column". Set the role policy's
        # `rlac_strict: false` to keep the old permissive behaviour while a
        # per-table filter mapping is defined.
        raise WrenError(
            "RLAC_UNENFORCEABLE",
            f"row filter(s) {unenforceable} cannot be applied to table '{mdl_name}' "
            "(column absent) — define a per-table filter or restrict the role's "
            "authorized_tables")
    applicable = [(col, val) for col, val in filters.items() if col.lower() in columns]
    if not applicable:
        return None
    if name.lower() in cte_names:
        # A CTE shadowing a filtered MDL table makes "is this the base table or the
        # CTE?" unanswerable without full scope resolution; refuse rather than guess.
        raise WrenError(
            "RLAC_UNENFORCEABLE",
            f"CTE name '{name}' shadows filtered table '{mdl_name}' — rename the CTE")
    inner_table = table_node.copy()
    inner_table.set("alias", None)
    inner = exp.select("*").from_(inner_table)
    for col, val in applicable:
        inner = inner.where(
            exp.EQ(this=exp.column(col), expression=exp.Literal.string(str(val))))
    return inner.subquery(table_node.alias or name)


def _apply_row_cap(ast: exp.Expression) -> exp.Expression:
    """Add a LIMIT when the query has none, or tighten one that exceeds the ceiling."""
    cap = governed_max_rows()
    if not cap:
        return ast
    existing = ast.args.get("limit")
    if existing is not None:
        try:
            asked = int(existing.expression.this)
        except (AttributeError, TypeError, ValueError):
            return ast              # non-literal LIMIT (parameter/expression): leave as-is
        if asked <= cap:
            return ast
    return ast.limit(cap)


def _enforce_business_scope(ast, mdl: dict) -> None:
    """(a0) Business scope. The MDL handed in is already narrowed to the chosen slice, so
    the strict guardrail below would reject an out-of-slice table anyway — but as "not in
    the model", which is misleading when the table plainly exists in that database. Checked
    first so the refusal names the business instead."""
    business = mdl.get("business")
    if not business:
        return
    in_scope = {str(t).lower() for t in (business.get("tables") or [])}
    known = {str(t).lower() for t in (mdl.get("tables") or {})}
    used = {t.lower() for t in _referenced_tables(ast, mdl)}
    outside = sorted((used & known) - in_scope)
    if outside:
        raise WrenError(
            "BUSINESS_TABLE_DENIED",
            f"business '{business.get('name')}' does not cover tables {outside} — "
            "ask inside a business that does, or add them to this one")


def _enforce_role_tables(ast, mdl: dict, role_policy: dict) -> None:
    """(a2) Per-role table authorization (TenantOS Role tab). Only roles that declare
    authorized_tables are restricted; full_access bypasses. Backward-compatible."""
    if role_policy.get("full_access", False) or role_policy.get("authorized_tables") is None:
        return
    allowed_role = {t.lower() for t in role_policy.get("authorized_tables", [])}
    used_tables = {t.lower() for t in _referenced_tables(ast, mdl)}
    forbidden = sorted(used_tables - allowed_role)
    if forbidden:
        raise WrenError("ROLE_TABLE_DENIED", f"role is not authorized for tables {forbidden}")


def govern(sql: str, dialect: str, mdl: dict, role_policy: dict,
           namespace: dict | None = None, tenant_id: str = "") -> str:
    if not sql or not sql.strip():
        # NL->SQL produced nothing usable; fail with a clear code instead of letting
        # sqlglot raise the cryptic "No expression was parsed from ''".
        raise WrenError("NO_SQL", "the model did not produce a valid SQL query for this "
                        "question — try rephrasing, or add table/column descriptions in the Database tab")
    try:
        ast = sqlglot.parse_one(sql, dialect=dialect)
    except Exception as e:
        raise WrenError("SQL_PARSE_ERROR", f"could not parse the SQL: {e}")

    # Defence in depth: governance is handed an `mdl` by its caller and has no tenant
    # concept of its own, so it cannot tell whose metadata it is enforcing. When both
    # sides are known, verify them rather than trusting every present and future caller.
    if tenant_id and mdl.get("tenant_id") and mdl["tenant_id"] != tenant_id:
        raise WrenError("MDL_TENANT_MISMATCH",
                        "governance metadata does not belong to the requesting tenant")

    # (0) read-only: SELECT-shaped root, no DML/DDL/command nodes anywhere (hard rule).
    _enforce_read_only(ast)
    _reject_foreign_qualifiers(ast, namespace)

    _enforce_business_scope(ast, mdl)

    # (a) Wren strict guardrail: only tables in the MDL + no denied functions.
    denied_fns = frozenset(mdl.get("denied_functions", []))
    validate_sql_policy(ast, set(mdl.get("allowed_tables", [])),
                        WrenConfig(strict_mode=True, denied_functions=denied_fns))
    validate_function_allowlist(ast, dialect, mdl.get("allowed_functions", []))
    _enforce_model_table(ast, mdl)

    _enforce_role_tables(ast, mdl, role_policy)

    # (b) CLAC: block PII / denied columns when the role may not see them.
    if not role_policy.get("can_see_pii", False):
        used = _selected_columns(ast, mdl)
        blocked = {p.lower() for p in mdl.get("pii_columns", [])} \
                  | {d.lower() for d in role_policy.get("denied_columns", [])}
        hit = sorted(used & blocked)
        if hit:
            raise WrenError("CLAC_DENIED", f"restricted columns {hit} not allowed for this role")

    # (c) RLAC: inject per-role row filters using safe sqlglot nodes (NEVER f-string
    #     interpolation — that was SQL-injectable), bound to each physical table at
    #     every scope that reads it. A set-operation root needs no special case now:
    #     each branch's tables are rewritten in place.
    ast = _apply_row_filters(ast, mdl, role_policy)

    # (d) Hard row cap. Governance bounded WHAT may be read but never HOW MUCH: the
    #     connector then did fetchall() into a list of lists and /v1/query serialised it
    #     whole, so one `SELECT * FROM <big table>` over non-PII columns could walk a
    #     shared multi-tenant pod into its memory limit. Applied at the choke point so
    #     /v1/query, the tool loop and every future caller inherit it.
    ast = _apply_row_cap(ast)

    return ast.sql(dialect=dialect)
