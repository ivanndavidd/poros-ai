"""Monthly Business Review engine: governed queries in, a report model out.

    resolve scope -> index reachable tables -> discover order statuses -> run every
    section's template queries through registry.execute_governed -> compute figures in
    Python -> one JSON-able report model the writers (Markdown now, XLSX/PPTX later) read.

Rules this module keeps:
  * every query goes through ``execute_governed`` (govern() + dry-run cost guard), inside
    the caller's business slice and role policy — the report can read nothing the same
    person could not ask the chat for;
  * no model call anywhere: figures are query cells or arithmetic on them;
  * a missing table, column or input never fails the report. The section degrades to
    ``partial`` / ``placeholder`` and says what is missing and why it matters.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

from .. import business_scope, store
from ..skills import registry
from ..skills.registry import Ctx
from . import mbr_config
from .sqlkit import SUPPORTED_DIALECTS, Month, ReportInputError, clean_brand

ENGINE_VERSION = "mbr-1"
_log = logging.getLogger(__name__)


@dataclass
class TableRef:
    table: str
    datasource: dict
    columns: set
    dialect: str


@dataclass
class Run:
    """Everything one report build shares: scope, inputs, and what each query returned."""
    tenant_id: str
    role: str
    brand: str
    month: Month
    config: mbr_config.MbrConfig
    business: dict | None
    ctx: Ctx
    tables: dict                                   # table name -> TableRef
    queries: dict = field(default_factory=dict)    # key -> query record
    statuses: dict = field(default_factory=dict)   # group -> [values]
    status_rows: list = field(default_factory=list)
    bytes_used: int = 0
    data_as_of: str | None = None                  # latest order date seen for the brand
    progress: object = None
    today: date | None = None                      # "so far" pages count through yesterday
    target_source: dict = field(default_factory=dict)   # "YYYY-MM" -> request | settings
    uploads: dict | None = None                    # attached files: what was read, replaced
    cleanup: list = field(default_factory=list)    # callables run when the build ends

    def close(self) -> None:
        for fn in self.cleanup:
            try:
                fn()
            except Exception:                      # cleanup never masks the report outcome
                _log.exception("mbr run cleanup failed")
        self.cleanup.clear()

    def from_files(self, table: str) -> bool:
        ref = self.tables.get(table)
        return bool(ref and str(ref.datasource.get("id") or "").startswith("upload_"))

    # ---- table access ----
    def ref(self, table: str) -> TableRef | None:
        return self.tables.get(table)

    def missing_columns(self, table: str, columns) -> list:
        ref = self.ref(table)
        if not ref:
            return list(columns)
        return [c for c in columns if c.lower() not in ref.columns]

    def _report_source(self, ref: TableRef) -> dict:
        """The datasource with the REPORT per-query byte ceiling; a copy, so the shared
        datasource (and every chat on it) keeps its own limit."""
        conn = ref.datasource.get("conn")
        if ref.dialect != "bigquery" or not isinstance(conn, dict):
            return ref.datasource
        return {**ref.datasource,
                "conn": {**conn, "max_bytes_billed": self.config.max_query_bytes}}

    def _skip(self, ref, table: str, columns) -> tuple[str, str] | None:
        """(code, error) when this query cannot run at all, else None."""
        if not ref:
            return "TABLE_NOT_IN_SCOPE", f"table {table} is not reachable in this business slice"
        missing = self.missing_columns(table, columns)
        if missing:
            return "COLUMN_MISSING", f"{table} has no column(s) {', '.join(missing)}"
        if self.bytes_used >= self.config.max_report_bytes:
            return "REPORT_BYTES_LIMIT", "report scan budget reached; later queries skipped"
        return None

    def _execute(self, rec: dict, ref, build) -> None:
        """Build and run the SQL through the governed path; the outcome lands in ``rec``."""
        try:
            sql = build(ref.dialect)
        except ReportInputError as exc:
            rec.update(code=exc.code, error=exc.message)
            return
        if not sql:
            return
        out = registry.execute_governed(self.ctx, self._report_source(ref), sql, "report")
        rec["sql"] = out.get("safe_sql") or sql
        rec["estimated_bytes"] = out.get("estimated_bytes")
        self.bytes_used += int(out.get("estimated_bytes") or 0)
        if not out.get("ok"):
            rec.update(code=out.get("code") or "ERROR",
                       error=str(out.get("error") or "query failed")[:500])
            return
        cols = [str(c) for c in out.get("columns") or []]
        rec.update(ok=True, columns=cols,
                   rows=[dict(zip(cols, [plain(v) for v in r])) for r in out.get("rows") or []])

    def query(self, key: str, table: str, columns, build) -> dict:
        """Run one template query; never raises. ``build(dialect)`` returns the SQL."""
        if key in self.queries:
            return self.queries[key]
        ref = self.ref(table)
        rec = {"key": key, "table": table, "ok": False, "rows": [], "columns": [],
               "datasource": ref.datasource.get("name") if ref else None}
        skip = self._skip(ref, table, columns)
        if skip:
            rec.update(code=skip[0], error=skip[1])
            if skip[0] == "COLUMN_MISSING":        # what the data team can map instead
                rec["scanned"] = sorted(ref.columns)
        else:
            self._execute(rec, ref, build)
        self.queries[key] = rec
        if self.progress:
            self.progress(key, rec)
        return rec


def plain(value):
    """JSON-safe scalar."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return value


def _index_tables(datasources: list) -> dict:
    out = {}
    for ds in datasources:
        if ds.get("type") not in SUPPORTED_DIALECTS:
            continue
        mdl = ds.get("mdl") or {}
        scanned = mdl.get("tables") or {}
        for table in mdl.get("allowed_tables") or []:
            cols = scanned.get(table) or []
            names = {(c if isinstance(c, str) else (c.get("name") or "")).lower() for c in cols}
            # first datasource wins; Acme table names do not repeat across datasets
            out.setdefault(table, TableRef(table, ds, names, ds.get("type")))
    return out


def _scope(tenant_id: str, role: str, business_ref):
    """(business, ctx, tables) for this caller: the business slice they may read, a
    governed context with their role policy, and the tables that slice reaches."""
    business = business_scope.resolve(tenant_id, role, business_ref)
    role_row = store.get_role(tenant_id, role)
    if not role_row:
        raise ReportInputError("ROLE_NOT_FOUND", f"role '{role}' does not exist")
    ctx = Ctx(tenant_id=tenant_id, role=role, role_policy=role_row.get("policy") or {},
              question="", business=business,
              trace_id="mbr-" + secrets.token_hex(6))
    tables = _index_tables(business_scope.visible_datasources(tenant_id, business))
    return business, ctx, tables


# ---- the brands a person can pick (the request form's combo box) ----
BRAND_MONTHS = 13            # a brand with orders in the last 13 months is offered
BRAND_LIMIT = 1000
# Brands change slowly and the read scans 13 months of orders (~10 s on BigQuery), so the
# order part is kept for hours; past that age it is still served at once while ONE
# background read replaces it. Only the first request after a restart waits.
BRAND_CACHE_SEC = 6 * 3600
_brand_cache: dict = {}
_brand_refreshing: set = set()
_brand_lock = threading.Lock()


def has_brand_orders(tenant_id: str, role: str, business_ref: str | None = None) -> bool:
    """Whether the caller's slice reaches an order table the brand list and the brand
    baseline read. Schema only, no query: cheap enough to ask on every chat turn."""
    from .mbr_sections import L2_ORDERS, L3_ORDERS   # sections import this module
    try:
        _business, _ctx, tables = _scope(tenant_id, role, business_ref)
    except Exception:                          # no role, no business: nothing to offer
        return False
    return any(ref and not ({"brand", "month_id"} - ref.columns)
               for ref in (tables.get(L2_ORDERS), tables.get(L3_ORDERS)))


def brand_choices(tenant_id: str, role: str, business_ref: str | None = None,
                  today: date | None = None) -> dict:
    """Brands the order history in the caller's slice holds, busiest first, plus brands
    saved in the MBR settings. A picked name is spelled the way the report filters it,
    so a typo can no longer queue a report that finds no orders.

    Never fails on the data side: when the order table is out of reach or the read is
    refused, the configured brands are still offered and ``source`` says why. The
    settings are read on every call, so a brand added there shows at once."""
    business, ctx, tables = _scope(tenant_id, role, business_ref)
    raw = store.get_tenant_config(tenant_id, mbr_config.SECTION) or {}
    key = (tenant_id, role, (business or {}).get("id"))
    hit = _brand_cache.get(key)
    if hit and time.time() - hit[0] >= BRAND_CACHE_SEC:
        _refresh_brands_later(key, ctx, tables, raw, today)
    orders = hit[1] if hit else _read_brands(ctx, tables, raw, today)
    if orders["source"] == "orders" and not hit:
        _brand_cache[key] = (time.time(), orders)
    configured = set()
    for name in (raw.get("brands") or {}):
        try:
            configured.add(clean_brand(name))
        except ReportInputError:
            continue
    brands = [{**b, "configured": b["name"] in configured} for b in orders["brands"]]
    listed = {b["name"] for b in brands}
    brands += [{"name": n, "orders": 0, "last_month": None, "configured": True}
               for n in sorted(configured - listed)]
    result = {"brands": brands, "source": orders["source"], "since": orders["since"],
              "business": ({"id": business["id"], "name": business["name"]}
                           if business else None)}
    if orders.get("note"):
        result["note"] = orders["note"]
    return result


def _refresh_brands_later(key, ctx, tables, raw, today) -> None:
    """Replace a stale brand list in the background; one refresh per slice at a time."""
    with _brand_lock:
        if key in _brand_refreshing:
            return
        _brand_refreshing.add(key)

    def run():
        try:
            fresh = _read_brands(ctx, tables, raw, today)
            if fresh["source"] == "orders":
                _brand_cache[key] = (time.time(), fresh)
        except Exception:                      # the stale list keeps serving
            _log.exception("brand list refresh failed")
        finally:
            with _brand_lock:
                _brand_refreshing.discard(key)

    threading.Thread(target=run, name="mbr-brands", daemon=True).start()


def _max_query_bytes(raw: dict) -> int:
    try:
        return int(raw.get("max_query_bytes") or mbr_config.DEFAULT_MAX_QUERY_BYTES)
    except (TypeError, ValueError):
        return mbr_config.DEFAULT_MAX_QUERY_BYTES


def _brand_rows(out: dict) -> dict:
    """name -> {name, orders, last_month} from the brand query; names that are blank or
    not a plain name are dropped (the report could not filter them)."""
    found = {}
    cols = [str(c).lower() for c in out.get("columns") or []]
    for row in out.get("rows") or []:
        rec = dict(zip(cols, row))
        try:
            name = clean_brand(rec.get("brand"))
        except ReportInputError:
            continue
        last = int(rec.get("last_month") or 0)
        prev = found.get(name)
        found[name] = {"name": name,
                       "orders": int(rec.get("orders") or 0) + (prev or {}).get("orders", 0),
                       "last_month": f"{last // 100:04d}-{last % 100:02d}" if last else None}
    return found


def _read_brands(ctx, tables, raw, today) -> dict:
    """The order part of the brand list: one governed read of the order history."""
    from .mbr_sections import L2_ORDERS, L3_ORDERS   # sections import this module
    now = today or datetime.now(timezone.utc).date()
    since = Month(now.year, now.month).shift(-(BRAND_MONTHS - 1))
    max_query = _max_query_bytes(raw)
    found, source, note = {}, "none", "no order table in this business slice"
    for table in (L2_ORDERS, L3_ORDERS):
        ref = tables.get(table)
        if not ref or {"brand", "month_id"} - ref.columns:
            continue
        sql = (f"SELECT UPPER(TRIM(brand)) AS brand, COUNT(*) AS orders, "
               f"MAX(month_id) AS last_month FROM {table} "
               f"WHERE month_id >= {since.mid} AND brand IS NOT NULL "
               f"GROUP BY UPPER(TRIM(brand)) ORDER BY orders DESC LIMIT {BRAND_LIMIT}")
        ds = ref.datasource
        if ref.dialect == "bigquery" and isinstance(ds.get("conn"), dict):
            ds = {**ds, "conn": {**ds["conn"], "max_bytes_billed": max_query}}
        out = registry.execute_governed(ctx, ds, sql, "report")
        if not out.get("ok"):
            note = f"the order history could not be read ({out.get('code') or 'ERROR'})"
            continue
        found = _brand_rows(out)
        source, note = "orders", None
        break
    out = {"brands": sorted(found.values(), key=lambda b: (-b["orders"], b["name"])),
           "source": source, "since": since.key}
    if note:
        out["note"] = note
    return out


def prepare(tenant_id: str, role: str, brand: str, period: str,
            business_ref: str | None = None, progress=None, targets: dict | None = None,
            inputs: str | None = None) -> Run:
    """Resolve everything a build needs; raises ReportInputError / BusinessError early.

    ``targets`` = the NMV targets the person typed when asking for the report
    ({"YYYY-MM": amount}); they win over the brand's stored settings for those months.
    ``inputs`` = the id of the exports attached for this report (``file_source``); call
    ``Run.close()`` when the build ends."""
    month = Month.parse(period)
    brand = clean_brand(brand)
    config = mbr_config.load(tenant_id, brand)
    source = {k: "settings" for k in config.brand.targets}
    for key, spec in mbr_config.request_targets(targets, month).items():
        config.brand.targets[key] = {**config.brand.targets.get(key, {}), **spec}
        source[key] = "request"
    business, ctx, tables = _scope(tenant_id, role, business_ref)
    run = Run(tenant_id=tenant_id, role=role, brand=brand, month=month, config=config,
              business=business, ctx=ctx, tables=tables, progress=progress,
              target_source=source)
    if inputs:
        from . import file_source
        file_source.attach(run, tenant_id, inputs)
    return run


def build(run: Run) -> dict:
    """Run every section and return the report model."""
    from . import mbr_sections               # sections import this module's helpers
    started = time.time()
    pulled_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sections = mbr_sections.build_all(run)
    queries = list(run.queries.values())
    ok = sum(1 for q in queries if q["ok"])
    statuses = [s["status"] for s in sections]
    return {
        "kind": "mbr",
        "engine_version": ENGINE_VERSION,
        "data_pulled_at": pulled_at,         # the warehouse is live: later runs can differ
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tenant_id": run.tenant_id,
        "brand": run.brand,
        "period": run.month.key,
        "period_label": run.month.label,
        "business": ({"id": run.business["id"], "name": run.business["name"]}
                     if run.business else None),
        "role": run.role,
        "limits": {"max_query_bytes": run.config.max_query_bytes,
                   "max_report_bytes": run.config.max_report_bytes},
        "data_as_of": run.data_as_of,
        "inputs": _inputs_summary(run),
        "uploads": run.uploads,
        "status_groups": run.statuses,
        "status_values": run.status_rows,
        "sections": sections,
        "queries": queries,
        "stats": {
            "queries": len(queries), "queries_ok": ok, "queries_failed": len(queries) - ok,
            "estimated_bytes": run.bytes_used,
            "sections": len(sections),
            "complete": statuses.count("complete"), "partial": statuses.count("partial"),
            "placeholder": statuses.count("placeholder"), "failed": statuses.count("failed"),
            "seconds": round(time.time() - started, 1),
        },
    }


def _inputs_summary(run: Run) -> dict:
    b = run.config.brand
    return {
        "brand_configured": b.configured,
        "takeover_date": b.takeover_date.isoformat() if b.takeover_date else None,
        "target": b.targets.get(run.month.key),
        "next_month_target": b.targets.get(run.month.shift(1).key),
        "target_source": {k: v for k, v in run.target_source.items()
                          if k in (run.month.key, run.month.shift(1).key)},
        "campaigns": [{"name": c["name"], "type": c["type"], "start": c["start"].isoformat(),
                       "end": c["end"].isoformat()} for c in b.campaigns],
        "focus_skus": len(b.focus_skus),
        "focus": b.focus,
        "client_names": b.client_names,
        "inventory_filter": {"column": b.inventory_column, "values": b.inventory_values},
        "client_codes": b.client_codes,
    }
