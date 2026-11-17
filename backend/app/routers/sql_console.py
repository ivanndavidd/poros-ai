"""SQL console: an admin types SELECT statements and reads the rows, like a desktop SQL client.

It is NOT a side door around governance. Every statement runs as a tenant role the admin
picks (optionally inside one of that role's businesses) through the same
`registry.execute_governed` path the chat agent uses: read-only, the connection and business
allowlists, the role's authorized tables, PII columns refused for a role that may not see
them, row filters, the row cap and the BigQuery cost guard. What the console shows is
therefore exactly what that role could read through the API.

No business named = the connection allowlist (the tenant's "business required" switch is a
rule for chat questions; an operator checking coverage across the warehouse still reads
only allowed tables as the chosen role).
"""
import secrets
import time
from datetime import date, datetime
from decimal import Decimal

import sqlglot
from fastapi import APIRouter, Depends, HTTPException
from sqlglot.tokens import TokenType

from .. import business_scope, store
from ..console_auth import console_guard, current_principal
from ..models import SqlConsoleIn
from ..skills import registry

router = APIRouter(prefix="/admin", tags=["sql console"], dependencies=[Depends(console_guard)])

MAX_STATEMENTS = 10          # one Run = at most this many statements
MAX_SQL_CHARS = 100_000
SHOW_ROWS = 2_000            # rows sent to the browser per result set (govern caps the fetch)
AUDIT_SQL_CHARS = 4_000


def _plain(value):
    """JSON-safe cell."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() and abs(value) < 2 ** 53 \
            else float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def split_statements(sql: str, dialect: str) -> list[str]:
    """The statements of a script, split at top-level semicolons (a semicolon inside a
    string or comment is not a separator). Empty and comment-only pieces are dropped."""
    try:
        tokens = sqlglot.Dialect.get_or_raise(dialect).tokenize(sql)
    except Exception:  # noqa: BLE001 - govern() reports the parse error per statement
        return [sql.strip()] if sql.strip() else []
    pieces, start = [], 0
    for tok in tokens:
        if tok.token_type == TokenType.SEMICOLON:
            pieces.append(sql[start:tok.start])
            start = tok.end + 1
    pieces.append(sql[start:])
    out = []
    for piece in pieces:
        try:
            meaningful = bool(sqlglot.Dialect.get_or_raise(dialect).tokenize(piece))
        except Exception:  # noqa: BLE001
            meaningful = bool(piece.strip())
        if meaningful:
            out.append(piece.strip())
    return out


def _role(tid: str, name: str | None) -> dict:
    roles = store.list_roles(tid)
    if not roles:
        raise HTTPException(409, {"code": "NO_ROLES", "message": "this tenant has no roles yet"})
    if not name:
        return roles[0]
    for r in roles:
        if r["name"].lower() == name.strip().lower():
            return r
    raise HTTPException(404, {"code": "ROLE_NOT_FOUND",
                              "message": f"role '{name}' does not exist in this tenant"})


def _business(tid: str, role: str, ref: str | None):
    """The business the console runs in, or None (connection allowlist). A named business
    must be one this role was granted; resolve() raises BusinessError (409 + menu) else."""
    if not (ref or "").strip():
        return None
    return business_scope.resolve(tid, role, ref)


def _dialect(ds: dict) -> str:
    return {"bigquery": "bigquery", "duckdb": "duckdb", "postgres": "postgres",
            "mysql": "mysql", "sqlserver": "tsql"}.get(ds.get("type"), "")


def _role_tables(policy: dict) -> set | None:
    """The tables a restricted role may read, or None when the role is not restricted."""
    if policy.get("full_access") or policy.get("authorized_tables") is None:
        return None
    return {t.lower() for t in policy.get("authorized_tables") or []}


def _column(col, pii: set, blocked: set) -> dict:
    if isinstance(col, str):
        name, ctype = col, ""
    else:
        name, ctype = col.get("name") or "", col.get("type") or ""
    low = name.lower()
    return {"name": name, "type": ctype, "pii": low in pii, "blocked": low in blocked}


def _tables(ds: dict, policy: dict) -> list[dict]:
    mdl = ds.get("mdl") or {}
    scanned = mdl.get("tables") or {}
    pii = {str(c).lower() for c in mdl.get("pii_columns") or []}
    role_tables = _role_tables(policy)
    blocked = {str(c).lower() for c in policy.get("denied_columns") or []}
    if not policy.get("can_see_pii", False):
        blocked |= pii
    out = []
    for name in sorted(mdl.get("allowed_tables") or [], key=str.lower):
        out.append({"name": name,
                    "columns": [_column(col, pii, blocked) for col in scanned.get(name) or []],
                    "role_denied": role_tables is not None and name.lower() not in role_tables})
    return out


@router.get("/tenants/{tid}/sql/catalog")
def sql_catalog(tid: str, role: str | None = None, business: str | None = None):
    """What the console can query as this role (+ business): roles, the role's businesses,
    and per datasource the allowed tables with their columns (PII/blocked flagged)."""
    if not store.get_tenant(tid):
        raise HTTPException(404, {"code": "TENANT_NOT_FOUND", "message": "tenant not found"})
    roles = store.list_roles(tid)
    if not roles:
        return {"roles": [], "role": None, "businesses": [], "business": None,
                "datasources": []}
    r = _role(tid, role)
    biz = _business(tid, r["name"], business)
    sources = business_scope.visible_datasources(tid, biz)
    return {
        "roles": [{"name": x["name"], "can_see_pii": bool((x.get("policy") or {}).get("can_see_pii")),
                   "full_access": bool((x.get("policy") or {}).get("full_access"))} for x in roles],
        "role": r["name"],
        "businesses": business_scope.choices(tid, r["name"]),
        "business": {"id": biz["id"], "name": biz["name"]} if biz else None,
        "datasources": [{"id": ds["id"], "name": ds.get("name") or ds["id"], "type": ds.get("type"),
                         "dialect": _dialect(ds), "tables": _tables(ds, r.get("policy") or {})}
                        for ds in sources if _dialect(ds)],
    }


def _datasource(tid: str, biz, datasource_id: str | None) -> dict:
    """The SQL data source this run reads: the one named, else the first visible one."""
    sources = {ds["id"]: ds for ds in business_scope.visible_datasources(tid, biz)}
    ds = sources.get(datasource_id) if datasource_id else next(iter(sources.values()), None)
    if not ds:
        where = f" in business {biz['name']}" if biz else ""
        raise HTTPException(404, {"code": "DATASOURCE_NOT_FOUND",
                                  "message": "that data source is not reachable as this role" + where})
    if not _dialect(ds):
        raise HTTPException(400, {"code": "NOT_SQL", "message": "this data source does not run SQL"})
    return ds


def _statements(sql: str, dialect: str) -> list[str]:
    if len(sql) > MAX_SQL_CHARS:
        raise HTTPException(413, {"code": "SQL_TOO_LONG",
                                  "message": f"the script is over {MAX_SQL_CHARS:,} characters"})
    statements = split_statements(sql, dialect)
    if not statements:
        raise HTTPException(400, {"code": "NO_SQL", "message": "type a SELECT statement first"})
    if len(statements) > MAX_STATEMENTS:
        raise HTTPException(400, {"code": "TOO_MANY_STATEMENTS",
                                  "message": f"run at most {MAX_STATEMENTS} statements at a time"})
    return statements


def _run_statement(ctx, ds: dict, index: int, stmt: str) -> dict:
    """One statement through the governed path, shaped for the result grid."""
    t0 = time.monotonic()
    out = registry.execute_governed(ctx, ds, stmt, "console")
    res = {"index": index, "sql": stmt, "ok": bool(out.get("ok")),
           "elapsed_ms": int((time.monotonic() - t0) * 1000),
           "safe_sql": out.get("safe_sql"), "estimated_bytes": out.get("estimated_bytes")}
    if not res["ok"]:
        res.update(code=out.get("code") or "ERROR", error=str(out.get("error") or "")[:2000])
        return res
    rows = out.get("rows") or []
    res.update(columns=[str(c) for c in out.get("columns") or []],
               rows=[[_plain(v) for v in row] for row in rows[:SHOW_ROWS]],
               row_count=len(rows), truncated=len(rows) > SHOW_ROWS)
    return res


@router.post("/tenants/{tid}/sql")
def run_sql(tid: str, b: SqlConsoleIn, p: dict = Depends(current_principal)):
    """Run one SQL script (statements split at `;`) as a role; one result per statement."""
    if not store.get_tenant(tid):
        raise HTTPException(404, {"code": "TENANT_NOT_FOUND", "message": "tenant not found"})
    r = _role(tid, b.role)
    biz = _business(tid, r["name"], b.business)
    ds = _datasource(tid, biz, b.datasource_id)
    statements = _statements(b.sql or "", _dialect(ds))

    who = p.get("email") or p.get("name") or "console"
    trace = "sql-" + secrets.token_hex(6)
    ctx = registry.Ctx(tenant_id=tid, role=r["name"], role_policy=r.get("policy") or {},
                       question="", user_id=who, business=biz, trace_id=trace)
    biz_name = biz["name"] if biz else None
    results = []
    for i, stmt in enumerate(statements):
        res = _run_statement(ctx, ds, i, stmt)
        results.append(res)
        store.log_audit(tid, trace, "console_sql", role=r["name"], user_id=who,
                        status="ok" if res["ok"] else "error", code=res.get("code", ""),
                        detail={"datasource_id": ds["id"], "business": biz_name,
                                "sql": stmt[:AUDIT_SQL_CHARS], "rows": res.get("row_count", 0),
                                "estimated_bytes": res.get("estimated_bytes"),
                                "elapsed_ms": res["elapsed_ms"]})
    return {"trace_id": trace, "role": r["name"], "business": biz_name,
            "datasource": {"id": ds["id"], "name": ds.get("name") or ds["id"], "type": ds.get("type")},
            "results": results}
