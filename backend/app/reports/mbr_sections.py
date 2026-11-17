"""MBR sections: template SQL per section + the arithmetic that turns rows into figures.

Section ids, order and grouping follow the reference deck (A where are we now, B what
changed after Acme, C why it changed, D what is working, E synthesis, F the month ahead,
appendix). Every section returns the same shape:

    {id, group, title, status, headline, facts[], tables[], missing[], queries[]}

status: complete (all inputs present) | partial (some figures missing) | placeholder
(no data yet; ``missing`` says what, why it matters and where it comes from) | failed
(a query errored — the error is in the query record, the report still ships).

``headline`` is the finding, written the way the reference deck titles its slides ("Sep
closed at X GMV and Y NMV: NMV down a% on Aug, up b% on Sep 2025"): computed, never
narrated by a model. Every visible label is business language (``labels``); query keys
and table names stay in ``queries`` and in each missing item's technical ``detail``.

Definitions follow the reference package (prep_gmv2.py):
  * GMV = item value after seller discount, every order status. The order table holds
    NMV (seller discount applied, platform discount added back) and the platform
    discount, so GMV = NMV - platform discount. Net shipping, which the reference adds,
    is not in the order table; the GMV appendix says so and compares the table's own
    gmv column, which is not a gross figure in every month.
  * NMV = nmv over processed statuses.
  * A seller cancellation is one by the seller or fulfilment, or for a stock or
    fulfilment reason; a buyer who never paid is not a seller failure.

Source rule (owner, 13 Oct 2026): L3 tables are curated from L2 and only hold the months
Acme operates a brand, so history (MoM/YoY, before vs after Acme) is read from L2;
L3 is reconciled against L2 for the overlapping months.
"""
from __future__ import annotations

import math
import re
import statistics
from datetime import date, datetime, timedelta, timezone

from . import drafts
from . import labels as L
from . import mbr_config
from .sqlkit import (Month, as_date, d, day_of, days_between, lit, lit_list, month_key,
                     month_text, num_of)

L2_ORDERS = "acme_store_order_mp"
L3_ORDERS = "acme_store_order_mp_ai_curated"
TOP_N = 25
SKU_LIMIT = 2000
LOAD_LAG_DAYS = 2       # trailing empty days in the live month read as not loaded yet
GMV_EXPR = "COALESCE(nmv, 0) - COALESCE(discount_platform, 0)"
_COD_VALUES = ("bayar di tempat", "cod", "cash on delivery")
_LINE_COLUMNS = ("sub_brand_new", "sub_brand")
# column and row labels that several pages share
_MOM = "MoM %"
_SHARE = "Share %"
_CONVERSION = "Conversion %"
_NOT_RECORDED = "Not recorded"
_SAME_DAY = "Same day"


# ---------------------------------------------------------------- formatting helpers
def num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def idr(v) -> str:
    x = num(v)
    a = abs(x)
    if a >= 1e9:
        s = f"IDR {a / 1e9:,.0f} bn" if a % 1e9 == 0 else f"IDR {a / 1e9:,.2f} bn"
    elif a >= 1e6:
        s = f"IDR {a / 1e6:,.1f} m"
    else:
        s = f"IDR {a:,.0f}"
    return ("minus " + s) if x < 0 else s


def count(v) -> str:
    return f"{num(v):,.0f}"


def pct(part, whole) -> float | None:
    w = num(whole)
    return None if w == 0 else num(part) / w * 100


def change(cur, prev) -> float | None:
    p = num(prev)
    return None if p == 0 else (num(cur) - p) / abs(p) * 100


def share_text(v, digits=1) -> str:
    """A share as text that never reads 100% or 0% when it is not."""
    if v is None:
        return "n/a"
    s = f"{v:.{digits}f}"
    top, low = f"{100:.{digits}f}", f"{0:.{digits}f}"
    if s == top and v < 100:
        return f"{100 - 10 ** -digits:.{digits}f}%"
    if s == low and v > 0:
        return f"under {10 ** -digits:.{digits}f}%"
    return s + "%"


def updown(c) -> str:
    """'up 12%' / 'down 3.4%' / 'flat'."""
    if c is None:
        return "with no comparable base"
    if abs(c) < 0.05:
        return "flat"
    word = "up" if c > 0 else "down"
    return f"{word} {abs(c):.0f}%" if abs(c) >= 10 else f"{word} {abs(c):.1f}%"


def than(c) -> str:
    """'6.9% higher' / '12% lower' / 'the same'."""
    if c is None:
        return "not comparable"
    if abs(c) < 0.05:
        return "the same"
    word = "higher" if c > 0 else "lower"
    return f"{abs(c):.0f}% {word}" if abs(c) >= 10 else f"{abs(c):.1f}% {word}"


def change_text(cur, prev, fmt=idr) -> str:
    """'from X to Y (up 12.3%)' — the house style for every change, no dash characters."""
    c = change(cur, prev)
    if c is None:
        return f"{fmt(cur)} (no comparable base: {fmt(prev)})"
    word = "up" if c >= 0 else "down"
    return f"from {fmt(prev)} to {fmt(cur)} ({word} {abs(c):.1f}%)"


def fact(text, source, label="FACT") -> dict:
    return {"label": label, "text": text, "source": source}


def table(tid, title, columns, rows, source, formulas=None, chart=None, deck_columns=None) -> dict:
    """A result table. ``formulas`` makes derived columns LIVE in the workbook, each
    {"col": i, "kind": change|share|ratio|diff, "a": j, "b": k, "scale": n}; ``chart``
    asks for a native chart {"type": line|column|bar, "x": i, "y": [j..]};
    ``deck_columns`` = the columns a slide shows (identifiers stay in the workbook)."""
    out = {"id": tid, "title": title, "columns": columns, "rows": rows, "source": source}
    if formulas:
        out["formulas"] = formulas
    if chart:
        out["chart"] = chart
    if deck_columns:
        out["deck_columns"] = deck_columns
    return out


def chg(col, a, b):
    return {"col": col, "kind": "change", "a": a, "b": b}


def shr(col, a):
    return {"col": col, "kind": "share", "a": a}


def rat(col, a, b, scale=1):
    return {"col": col, "kind": "ratio", "a": a, "b": b, "scale": scale}


def section(sid, group, title) -> dict:
    return {"id": sid, "group": group, "title": title, "status": "complete", "headline": "",
            "facts": [], "tables": [], "missing": [], "queries": []}


def missing(what, why, needs, visual="", fallback="", updates="", detail="", key="") -> dict:
    """An open item in words an owner can act on; ``detail`` is the technical trace
    (table, column, error) for the data team, printed only in the open items file.
    ``key`` picks the item's own good/bad/neutral drafts when a page has several."""
    out = {"what": what, "why": why, "needs": needs, "visual": visual,
           "fallback": fallback, "updates": updates}
    if detail:
        out["detail"] = detail
    if key:
        out["key"] = key
    return out


# Query outcomes that are a data gap (documented, section degrades) rather than a defect.
_GAP_CODES = ("TABLE_NOT_IN_SCOPE", "COLUMN_MISSING", "COST_LIMIT", "REPORT_BYTES_LIMIT",
              "SCHEMA_DRIFT")
_NEEDS = {
    "TABLE_NOT_IN_SCOPE": "access to {label} for this business; the Poros admin adds the "
                          "table to the business the report runs in",
    "COLUMN_MISSING": "a field this page reads is not in Poros' copy of {label}; rescan "
                      "the data source in Poros, and if it is still missing the data team "
                      "adds it",
    "SCHEMA_DRIFT": "{label} changed in the warehouse; the Poros admin rescans the data "
                    "source so the report reads the new layout",
    "COST_LIMIT": "{label} is too large to read for one report; the data team provides a "
                  "daily or monthly summary table, or the MBR settings allow a larger read",
    "REPORT_BYTES_LIMIT": "the report used its data reading budget before this page; raise "
                          "the budget in the MBR settings or run the report again",
}


def _classify(rec) -> tuple[str, str, str]:
    """(code, what is needed in business words, technical detail) for a failed query."""
    code, err = rec.get("code") or "ERROR", str(rec.get("error") or "")
    label = L.source_label(rec.get("key")).lower()
    detail = f"{rec.get('key')} on {rec.get('table')}: {code}: {err}"
    if code == "DRY_RUN_ERROR" and "Unrecognized name" in err:
        col = err.split("Unrecognized name:")[-1].split(" at ")[0].strip()
        return ("SCHEMA_DRIFT", _NEEDS["SCHEMA_DRIFT"].format(label=label),
                f"{rec['table']} no longer has column {col} the catalog lists; rescan the "
                f"datasource ({err})")
    if code == "COLUMN_MISSING":
        detail += ("; the column list comes from the datasource's last scan: rescan it, and "
                   "only if the column is still absent is it missing in the warehouse")
        scanned = rec.get("scanned") or []
        if scanned:
            more = f" and {len(scanned) - 40} more" if len(scanned) > 40 else ""
            detail += f". The scan lists: {', '.join(scanned[:40])}{more}"
    if code == "COST_LIMIT":
        detail = (f"{rec['table']} is not partitioned, so this query scans more than the per "
                  f"query limit ({err}); a partitioned or pre-aggregated table, or a higher "
                  "max_query_bytes in the MBR settings")
    needs = _NEEDS.get(code, "the request for {label} failed; the technical detail is in the "
                             "open items for the data team")
    return code, needs.format(label=label), detail


def use(sec, rec) -> bool:
    """Attach a query to a section; returns whether it produced rows to use."""
    sec["queries"].append(rec["key"])
    if not rec["ok"]:
        code, needs, detail = _classify(rec)
        rec["gap_code"] = code
        sec["status"] = "partial" if code in _GAP_CODES else "failed"
        sec["missing"].append(missing(
            f"{L.source_label(rec['key'])} data",
            "the figures of this page that come from it are not shown", needs, detail=detail))
        return False
    return True


# fallback/updates of an open item whose figures are simply left off the page
_NOT_SHOWN = "not shown"
_ON_PAGE = "the figures appear on this page"


def _empty_feed(what, table_name, run, why, key="", field="") -> dict:
    """A query that ran but whose measure is zero everywhere: the feed is not loaded for
    this brand. Showing it as 0 would read as 'nothing happened', which is a claim.
    `field` names the one measure that is empty when the rest of the feed has data, so
    the page does not say "only zeros" next to figures it shows from the same feed."""
    label = L.source_label(key).lower() if key else what
    if field:
        needs = (f"the {label} feed has data for this brand but its {field} is zero "
                 "everywhere; the data team fills it, or confirms there were none")
    else:
        needs = (f"the {label} feed holds only zeros for this brand in these months; the "
                 "data team loads it, or confirms the brand truly had none")
    return missing(f"{what} for {run.brand}", why, needs,
                   fallback=_NOT_SHOWN, updates=_ON_PAGE,
                   detail=f"{table_name} returns only zero{f' {field}' if field else ''} for "
                          f"brand {run.brand}")


def degrade(sec):
    """A section with no usable figure at all is a placeholder, not a failure."""
    if not sec["facts"] and not sec["tables"] and sec["status"] != "failed":
        sec["status"] = "placeholder"
    elif sec["missing"] and sec["status"] == "complete":
        sec["status"] = "partial"
    return sec


def months_window(run):
    """13 months ending at the review month (YoY needs the same month last year)."""
    return [run.month.shift(-i) for i in range(12, -1, -1)]


def brand_eq(column="brand"):
    return f"UPPER({column}) = {{brand}}"


def _today(run) -> date:
    return getattr(run, "today", None) or datetime.now(timezone.utc).date()


def _has(run, table, column) -> bool:
    return not run.missing_columns(table, [column])


# ---------------------------------------------------------------- order status discovery
def discover_statuses(run) -> bool:
    start, end = run.month.shift(-12), run.month
    rec = run.query(
        "order_status_values", L2_ORDERS,
        ("order_status", "brand", "month_id", "nmv", "discount_platform"),
        lambda dl: (f"SELECT order_status, COUNT(*) AS line_count, "
                    f"SUM({GMV_EXPR}) AS gmv FROM {L2_ORDERS} "
                    f"WHERE UPPER(brand) = {lit(run.brand)} "
                    f"AND month_id BETWEEN {start.mid} AND {end.mid} GROUP BY order_status"))
    groups = {g: [] for g in mbr_config.STATUS_GROUPS}
    rows: dict = {}
    for row in rec["rows"]:
        value = row.get("order_status")
        group, configured = mbr_config.classify_status(value, run.config.status_map)
        if value is not None:
            groups[group].append(value)          # every stored spelling, for the SQL
        # dated variants of one status ("... hingga 2026-10-13.") are one row to confirm
        shown = rows.setdefault(mbr_config.status_key(value), {
            "value": mbr_config.DATE_IN_STATUS.sub("<date>", str(value).strip())
            if value is not None else None,
            "group": group, "configured": configured, "lines": 0, "gmv": 0.0, "variants": 0})
        shown["lines"] = num(shown["lines"]) + num(row.get("line_count"))
        shown["gmv"] = num(shown["gmv"]) + num(row.get("gmv"))
        shown["variants"] += 1
    run.statuses = groups
    run.status_rows = sorted(rows.values(), key=lambda r: -num(r["lines"]))
    return rec["ok"] and bool(rec["rows"])


_IN_FLIGHT = ("dikirim", "perlu dikirim", "shipped", "shipping", "to ship", "ready to ship",
              "ready_to_ship", "in transit", "diproses", "processing", "dalam pengiriman",
              "telah dikirim", "sedang dikirim")


def in_flight(run) -> list:
    """Processed statuses that are not final yet: the order can still complete, cancel or
    return, so the latest month moves after the report is pulled."""
    return [v for v in run.statuses.get("processed") or []
            if str(v).strip().lower() in _IN_FLIGHT]


def processed_list(run):
    """The statuses counted as sold, as an SQL list."""
    return lit_list(run.statuses.get("processed") or [])


def cancelled_list(run):
    """The statuses counted as cancelled, as an SQL list."""
    return lit_list(run.statuses.get("cancelled") or [])


# ---------------------------------------------------------------- shared order queries
def orders_monthly(run):
    w = months_window(run)
    p, c, b = processed_list(run), cancelled_list(run), lit(run.brand)
    f = lit_list(in_flight(run)) or lit("")
    return run.query(
        "orders_monthly", L2_ORDERS,
        ("month_id", "order_status", "gmv", "new_gmv", "nmv", "order_id", "quantity",
         "customer_id", "discount_platform", "order_date"),
        lambda dl: (
            f"SELECT month_id, "
            f"SUM({GMV_EXPR}) AS gmv, "
            f"SUM(COALESCE(new_gmv, gmv)) AS listed_gmv, "
            f"SUM(CASE WHEN order_status IN ({c}) AND COALESCE(nmv, 0) = 0 "
            f"THEN COALESCE(new_gmv, gmv, 0) ELSE 0 END) AS cancelled_unvalued, "
            f"SUM(CASE WHEN order_status IN ({p}) THEN nmv ELSE 0 END) AS nmv, "
            f"SUM(COALESCE(nmv, 0)) AS nmv_all, "
            f"SUM(CASE WHEN order_status IN ({f}) THEN nmv ELSE 0 END) AS nmv_in_flight, "
            f"COUNT(DISTINCT CASE WHEN order_status IN ({p}) THEN order_id END) AS orders, "
            f"SUM(CASE WHEN order_status IN ({p}) THEN quantity ELSE 0 END) AS units, "
            f"COUNT(DISTINCT CASE WHEN order_status IN ({p}) THEN customer_id END) AS buyers, "
            f"COUNT(DISTINCT order_id) AS all_orders, "
            f"COUNT(DISTINCT CASE WHEN order_status IN ({c}) THEN order_id END) AS cancelled_orders, "
            f"SUM(CASE WHEN order_status IN ({p}) THEN discount_platform ELSE 0 END) AS platform_discount, "
            f"MAX(order_date) AS last_order_date "
            f"FROM {L2_ORDERS} WHERE UPPER(brand) = {b} "
            f"AND month_id BETWEEN {w[0].mid} AND {w[-1].mid} GROUP BY month_id ORDER BY month_id"))


def _by_month(rec):
    return {int(num(r.get("month_id"))): r for r in rec["rows"]}


def orders_daily(run, key, first, last):
    p, b = processed_list(run), lit(run.brand)
    return run.query(
        key, L2_ORDERS, ("order_date", "order_status", "nmv", "order_id", "discount_platform"),
        lambda dl: (
            f"SELECT order_date, SUM({GMV_EXPR}) AS gmv, "
            f"SUM(CASE WHEN order_status IN ({p}) THEN nmv ELSE 0 END) AS nmv, "
            f"COUNT(DISTINCT CASE WHEN order_status IN ({p}) THEN order_id END) AS orders "
            f"FROM {L2_ORDERS} WHERE UPPER(brand) = {b} "
            f"AND order_date BETWEEN {d(first)} AND {d(last)} "
            f"GROUP BY order_date ORDER BY order_date"))


def sku_performance(run):
    """Per product, review month and the month before; every product section reads it."""
    p, b, cur, prev = processed_list(run), lit(run.brand), run.month, run.month.shift(-1)
    typ = "MAX(new_product_type) AS product_type, " if _has(run, L2_ORDERS, "new_product_type") \
        else ""
    if _has(run, L2_ORDERS, "marketplace"):
        typ += "MAX(marketplace) AS marketplace, "
    return run.query(
        "sku_performance", L2_ORDERS,
        ("sku_id", "product_name", "month_id", "order_status", "nmv", "quantity"),
        lambda dl: (
            f"SELECT sku_id, MAX(product_name) AS product_name, {typ}"
            f"SUM(CASE WHEN month_id = {cur.mid} AND order_status IN ({p}) THEN nmv ELSE 0 END) AS nmv_cur, "
            f"SUM(CASE WHEN month_id = {prev.mid} AND order_status IN ({p}) THEN nmv ELSE 0 END) AS nmv_prev, "
            f"SUM(CASE WHEN month_id = {cur.mid} AND order_status IN ({p}) THEN quantity ELSE 0 END) AS units_cur, "
            f"SUM(CASE WHEN month_id = {prev.mid} AND order_status IN ({p}) THEN quantity ELSE 0 END) AS units_prev "
            f"FROM {L2_ORDERS} WHERE UPPER(brand) = {b} AND month_id IN ({cur.mid}, {prev.mid}) "
            f"GROUP BY sku_id ORDER BY nmv_cur DESC LIMIT {SKU_LIMIT}"))


def sku_price_range(run):
    """Lowest and highest price per unit a product sold at, per month (processed lines)."""
    p, b, cur, prev = processed_list(run), lit(run.brand), run.month, run.month.shift(-1)
    return run.query(
        "sku_price_range", L2_ORDERS, ("sku_id", "month_id", "order_status", "nmv", "quantity"),
        lambda dl: (
            f"SELECT sku_id, month_id, MIN(nmv / quantity) AS price_low, "
            f"MAX(nmv / quantity) AS price_high FROM {L2_ORDERS} "
            f"WHERE UPPER(brand) = {b} AND month_id IN ({cur.mid}, {prev.mid}) "
            f"AND order_status IN ({p}) AND quantity > 0 AND nmv > 0 "
            f"GROUP BY sku_id, month_id"))


def product_traffic(run):
    """Page views, conversion and revenue per listing in the review month."""
    b, cur = lit(run.brand), run.month
    units = "SUM(total_selling_product) AS units, " if _has(
        run, "acme_store_product_mp", "total_selling_product") else ""
    return run.query(
        "product_funnel", "acme_store_product_mp",
        ("product_sku", "product_name", "total_views_page", "cvr", "sales_revenue", "month_id"),
        lambda dl: (
            f"SELECT product_sku, MAX(product_name) AS product_name, {units}"
            f"SUM(total_views_page) AS views, AVG(cvr) AS cvr, SUM(sales_revenue) AS revenue "
            f"FROM acme_store_product_mp WHERE UPPER(brand) = {b} AND month_id = {cur.mid} "
            f"GROUP BY product_sku ORDER BY revenue DESC LIMIT 200"))


def _skus(rec) -> list:
    return [r for r in rec["rows"] if num(r.get("nmv_cur")) or num(r.get("nmv_prev"))]


_ID_KEYS = (("sku_id", "sku_performance"), ("product_sku", "product_funnel"))


def _name(run, r, key="product_name") -> str:
    """The short product name a reader sees. Several listings often shorten to the same
    name (the same pack sold on two marketplaces, or listed twice); those get the
    marketplace and then the end of their SKU, so a table never shows one product three
    times with three different numbers."""
    base = L.short_product(r.get(key), run.brand)
    id_key, qkey = next(((k, q) for k, q in _ID_KEYS if r.get(k) is not None), (None, None))
    if id_key is None:
        return base
    dups = _dup_names(run, qkey, id_key, key)
    if dups is None or base not in dups[0]:
        return base
    tag = _dup_tag(r, base, id_key, dups[1])
    return f"{base[:-1]}, {tag})" if base.endswith(")") else f"{base} ({tag})"


def _dup_cache(run) -> dict:
    cache = getattr(run, "_dup_names", None)
    if cache is None:
        cache = {}
        try:
            run._dup_names = cache
        except AttributeError:
            pass
    return cache


def _dup_names(run, qkey, id_key, key):
    """(names several listings shorten to, (name, marketplace) pairs several listings
    share) over one query's rows, computed once per run; None when it has not run."""
    cache = _dup_cache(run)
    if qkey not in cache:
        rec = run.queries.get(qkey)
        if not rec:
            return None
        ids, places = {}, {}
        for x in rec.get("rows") or []:
            n = L.short_product(x.get(key), run.brand)
            ids.setdefault(n, set()).add(str(x.get(id_key)))
            places.setdefault((n, str(x.get("marketplace") or "")), set()).add(str(x.get(id_key)))
        cache[qkey] = ({n for n, v in ids.items() if len(v) > 1},
                       {k for k, v in places.items() if len(v) > 1})
    return cache[qkey]


def _dup_tag(r, base, id_key, dup_place) -> str:
    """The marketplace, then the end of the SKU, that tells a shared name apart."""
    tags = []
    place = str(r.get("marketplace") or "")
    if place:
        tags.append(L.platform_label(place))
    if not place or (base, place) in dup_place:
        tags.append(f"SKU ..{str(r.get(id_key))[-4:]}")
    return ", ".join(tags)


def _pack(run, r) -> int | None:
    cfg = (run.config.brand.focus_skus.get(str(r.get("sku_id"))) or {}).get("pack")
    return L.pack_of(r.get("product_name"), cfg)


# ---------------------------------------------------------------- A. where are we now
def sec_scorecard(run):
    sec = section("scorecard", "Summary", "Performance scorecard")
    rec = orders_monthly(run)
    if not use(sec, rec):
        return degrade(sec)
    m = _by_month(rec)
    _advance_data_as_of(run, (r.get("last_order_date") for r in rec["rows"]))
    cur, prev, ly = run.month, run.month.shift(-1), run.month.shift(-12)
    rc, rp, ry = m.get(cur.mid, {}), m.get(prev.mid, {}), m.get(ly.mid, {})
    rows = _scorecard_rows(rc, rp, ry)
    target = run.config.brand.targets.get(cur.key) or {}
    sec["tables"].append(table(
        "scorecard", f"{cur.label} scorecard",
        ["Measure", cur.label, prev.label, _MOM, ly.label, "YoY %"], rows, rec["key"],
        formulas=[chg(3, 1, 2), chg(5, 1, 4)]))
    if not rc:
        sec["missing"].append(missing(
            f"orders for {run.brand} in {cur.label}", "the scorecard has no current month",
            f"orders for brand {run.brand} in the order history",
            detail=f"no rows in {L2_ORDERS} for brand {run.brand} in {cur.mid}"))
        return degrade(sec)
    sec["headline"] = _scorecard_headline((cur, prev, ly), (rc, rp, ry))
    sec["facts"] += [
        fact(f"NMV {cur.label}: {change_text(rc.get('nmv'), rp.get('nmv'))} month on month.",
             rec["key"]),
        fact(f"NMV versus {ly.label}: {change_text(rc.get('nmv'), ry.get('nmv'))}.", rec["key"]),
        fact(f"GMV {cur.label}: {change_text(rc.get('gmv'), rp.get('gmv'))}.", rec["key"]),
        fact(f"Orders {cur.label}: {change_text(rc.get('orders'), rp.get('orders'), count)}; "
             f"buyers {change_text(rc.get('buyers'), rp.get('buyers'), count)}.", rec["key"]),
    ]
    flying = num(rc.get("nmv_in_flight"))
    if flying:
        sec["facts"].append(fact(
            f"{idr(flying)} ({share_text(pct(flying, rc.get('nmv')))}) of {cur.label} NMV is "
            f"still in a shipping status ({L.status_list(in_flight(run))}); those orders can "
            "still complete, cancel or return, so the latest month moves until they settle.",
            rec["key"], "INTERPRETATION"))
    _scorecard_target(sec, rec["key"], cur, rc, target)
    return degrade(sec)


def _advance_data_as_of(run, days) -> None:
    """The latest order date seen so far is the date the data runs to."""
    for day in days:
        if day and (run.data_as_of is None or str(day) > run.data_as_of):
            run.data_as_of = str(day)


def _aov(r):
    return (num(r.get("nmv")) / num(r.get("orders"))) if num(r.get("orders")) else 0


def _cancel_rate(r):
    return pct(r.get("cancelled_orders"), r.get("all_orders")) or 0


def _scorecard_rows(rc, rp, ry) -> list:
    """Measure, review month, month before, MoM, same month last year, YoY."""
    rows = []
    for key, name in (("gmv", "GMV"), ("nmv", "NMV"), ("orders", "Orders"),
                      ("units", "Units"), ("buyers", "Buyers")):
        rows.append([name, num(rc.get(key)), num(rp.get(key)), change(rc.get(key), rp.get(key)),
                     num(ry.get(key)), change(rc.get(key), ry.get(key))])
    rows.append(["AOV (NMV per order)", _aov(rc), _aov(rp), change(_aov(rc), _aov(rp)),
                 _aov(ry), change(_aov(rc), _aov(ry))])
    rows.append(["Cancellation rate %", _cancel_rate(rc), _cancel_rate(rp), None,
                 _cancel_rate(ry), None])
    return rows


def _scorecard_headline(months, figures) -> str:
    (cur, prev, ly), (rc, rp, ry) = months, figures
    moves = []
    if change(rc.get("nmv"), rp.get("nmv")) is not None:
        moves.append(f"NMV {updown(change(rc.get('nmv'), rp.get('nmv')))} on {prev.label}")
    if change(rc.get("nmv"), ry.get("nmv")) is not None:
        moves.append(f"{updown(change(rc.get('nmv'), ry.get('nmv')))} on {ly.label}")
    return (f"{cur.label} closed at {idr(rc.get('gmv'))} GMV and "
            f"{idr(rc.get('nmv'))} NMV" + (": " + ", ".join(moves) if moves else ""))


def _scorecard_target(sec, key, cur, rc, target) -> None:
    """Attainment against the NMV target, or the open item asking for it."""
    if target.get("nmv"):
        attain = pct(rc.get("nmv"), target["nmv"]) or 0
        sec["headline"] += f"; {attain:.0f}% of the {idr(target['nmv'])} target"
        sec["facts"].append(fact(
            f"NMV reached {attain:.1f}% of the {idr(target['nmv'])} target for {cur.label}.",
            key))
        return
    sec["missing"].append(missing(
        f"NMV target for {cur.label}",
        "attainment against target is the first line of the scorecard",
        "the NMV target, entered when the report is generated (or saved in the brand's "
        "MBR settings)",
        visual="target vs actual with attainment %",
        fallback="actuals only, no attainment line",
        updates="the scorecard gains attainment against target",
        detail=f"no targets[{cur.key}].nmv in the request or tenant config mbr.brands"))


def sec_trend(run):
    sec = section("monthly_trend", "A", "13 month trend")
    rec = orders_monthly(run)
    if not use(sec, rec):
        return degrade(sec)
    m = _by_month(rec)
    rows = []
    for mo in months_window(run):
        r = m.get(mo.mid, {})
        rows.append([mo.label, num(r.get("gmv")), num(r.get("nmv")), num(r.get("orders")),
                     num(r.get("units")), num(r.get("buyers"))])
    sec["tables"].append(table("trend", "GMV, NMV, orders, units and buyers by month",
                               ["Month", "GMV", "NMV", "Orders", "Units", "Buyers"], rows,
                               rec["key"], chart={"type": "line", "x": 0, "y": [1, 2],
                                                  "title": "GMV and NMV by month"}))
    active = [r for r in rows if r[2]]
    if not active:
        return degrade(sec)
    best = max(active, key=lambda r: r[2])
    cur, ly = rows[-1], rows[0]
    if cur[2]:
        if best is cur:
            head = f"{cur[0]} NMV of {idr(cur[2])} was the highest of the last 13 months"
        else:
            head = (f"{cur[0]} NMV of {idr(cur[2])} was {abs(change(cur[2], best[2])):.0f}% "
                    f"below the {best[0]} peak of {idr(best[2])}")
        if ly[2]:
            head += f", {updown(change(cur[2], ly[2]))} on {ly[0]}"
        sec["headline"] = head
    else:
        sec["headline"] = f"Highest NMV month in the window was {best[0]} at {idr(best[2])}"
    sec["facts"].append(fact(sec["headline"] + ".", rec["key"]))
    first = active[0]
    if first[0] != rows[0][0]:
        sec["facts"].append(fact(
            f"Order history for {run.brand} starts in {first[0]}; earlier months show zero.",
            rec["key"]))
    return degrade(sec)


def _breakpoint(values, min_side=5, min_change=25.0):
    """The day the daily level shifted: the split that best separates two means (mean
    gap weighted by sqrt(n1*n2/n)), kept only when the level moved at least 25%."""
    n = len(values)
    best = None
    for k in range(min_side, n - min_side + 1):
        a, b = values[:k], values[k:]
        m1, m2 = sum(a) / len(a), sum(b) / len(b)
        score = abs(m2 - m1) * math.sqrt(len(a) * len(b) / n)
        if best is None or score > best[0]:
            best = (score, k, m1, m2)
    if not best or not best[2]:
        return None
    c = change(best[3], best[2])
    return best[1:] if c is not None and abs(c) >= min_change else None


def _spike_text(rows, spikes, month) -> str:
    """'a 9 Sep (9.9) peak of IDR 41.3 m' / '3 peak days, the largest ...'."""
    top = max(spikes, key=lambda i: rows[i][2])
    day = str(rows[top][0])[:10]
    double = (f" ({month.month}.{month.month})"
              if day[8:10].lstrip("0") == str(month.month) else "")
    lead = "a" if len(spikes) == 1 else f"{len(spikes)} peak days, the largest"
    return f"{lead} {L.day_label(day)}{double} peak of {idr(rows[top][2])}"


def sec_daily(run):
    sec = section("daily", "A", "Daily run rate")
    cur = run.month
    rec = orders_daily(run, "orders_daily", cur.first, cur.last)
    if not use(sec, rec):
        return degrade(sec)
    rows = [[r.get("order_date"), num(r.get("gmv")), num(r.get("nmv")), num(r.get("orders"))]
            for r in rec["rows"]]
    _advance_data_as_of(run, (r[0] for r in rows))
    sec["tables"].append(table("daily", f"Daily GMV, NMV and orders, {cur.label}",
                               ["Date", "GMV", "NMV", "Orders"], rows, rec["key"],
                               chart={"type": "column", "x": 0, "y": [2],
                                      "title": f"Daily NMV, {cur.label}"}))
    if not rows:
        return degrade(sec)
    total = sum(r[2] for r in rows)
    peak = max(rows, key=lambda r: r[2])
    avg = total / len(rows)
    sec["facts"].append(fact(
        f"Average NMV per selling day was {idr(avg)} over {len(rows)} days; the peak day was "
        f"{L.day_label(peak[0])} at {idr(peak[2])} ({share_text(pct(peak[2], total))} of the "
        "month).", rec["key"]))
    _daily_level(sec, rec["key"], rows, cur, (avg, peak))
    camps = [c for c in run.config.brand.campaigns if c["start"] <= cur.last and c["end"] >= cur.first]
    if camps:
        _campaign_lift(sec, rec["key"], rows, camps)
    else:
        sec["missing"].append(missing(
            f"campaign calendar for {cur.label}",
            "campaign days (double date, payday) explain the daily shape and the uplift",
            "the campaign calendar in the brand's MBR settings",
            visual="daily NMV bars with campaign days highlighted",
            fallback="daily trend without campaign markers",
            updates="campaign versus other day uplift is computed",
            detail="tenant config mbr.brands.<brand>.campaigns"))
    return degrade(sec)


def _daily_level(sec, key, rows, month, plain) -> None:
    """The daily headline; ``plain`` = (average, peak row) for a month with no level
    shift and no spike."""
    # A campaign spike (9.9, payday) is one day, not a new level: the level split is
    # found and measured on the other days, and the spike is named on its own. Live
    # 13 Oct: the 9 Sep peak sat inside "15.8 m a day from 9 Sep"; without it, 14.6 m.
    vals = [r[2] for r in rows]
    med = statistics.median(vals)
    spikes = [i for i, v in enumerate(vals) if med and v > 2 * med]
    level = [med if i in spikes else v for i, v in enumerate(vals)]
    bp = _breakpoint(level)
    if bp:
        _daily_step(sec, key, rows, month, (vals, spikes, bp[0]))
    elif spikes:
        sec["headline"] = (f"{month.label} ran at about {idr(med)} NMV a day, with "
                           f"{_spike_text(rows, spikes, month)}")
    else:
        avg, peak = plain
        sec["headline"] = (f"{month.label} averaged {idr(avg)} NMV a day, peaking on "
                           f"{L.day_label(peak[0])} at {idr(peak[2])}")


def _daily_step(sec, key, rows, month, split) -> None:
    """Headline and fact for a daily level that shifted on day k, spikes left out."""
    vals, spikes, k = split
    a = [vals[i] for i in range(k) if i not in spikes]
    b_ = [vals[i] for i in range(k, len(vals)) if i not in spikes]
    m1, m2 = sum(a) / len(a), sum(b_) / len(b_)
    verb = "stepped up" if m2 > m1 else "dropped"
    sec["headline"] = (f"Daily NMV {verb} from {idr(m1)} a day ({L.day_label(rows[0][0])} "
                       f"to {L.day_label(rows[k - 1][0])}) to {idr(m2)} a day from "
                       f"{L.day_label(rows[k][0])}")
    if spikes:
        sec["headline"] += f", with {_spike_text(rows, spikes, month)}"
    sec["facts"].append(fact(
        sec["headline"] + f" ({updown(change(m2, m1))}"
        + ("; peak days left out of the daily level" if spikes else "") + ").",
        key, "INTERPRETATION"))


def _campaign_days(camps) -> set:
    out = set()
    for c in camps:
        x = c["start"]
        while x <= c["end"]:
            out.add(x.isoformat())
            x = x + timedelta(days=1)
    return out


def _campaign_lift(sec, key, rows, camps) -> None:
    """NMV per campaign day against the other days of the month."""
    days = {str(r[0])[:10]: r[2] for r in rows}
    camp_days = _campaign_days(camps)
    on = [v for k, v in days.items() if k in camp_days]
    off = [v for k, v in days.items() if k not in camp_days]
    if on and off:
        lift = change(sum(on) / len(on), sum(off) / len(off))
        sec["facts"].append(fact(
            f"Campaign days averaged {idr(sum(on) / len(on))} NMV per day against "
            f"{idr(sum(off) / len(off))} on other days ({updown(lift)}).",
            key, "INTERPRETATION"))


# ---------------------------------------------------------------- B. what changed after Acme
def sec_after_acme(run):
    sec = section("after_acme", "B", "Before and after the takeover")
    takeover = run.config.brand.takeover_date
    rec = orders_monthly(run)
    if not takeover:
        sec["missing"].append(missing(
            "Acme takeover date for this brand",
            "before versus after Acme is the core of the story the client hears",
            "the date Acme took over the store, in the brand's MBR settings",
            visual="monthly NMV with the takeover marked, averages before and after",
            fallback="the 13 month trend without an era line",
            updates="before versus after averages and the uplift are computed",
            detail="tenant config mbr.brands.<brand>.takeover_date"))
        return degrade(sec)
    if not use(sec, rec):
        return degrade(sec)
    tk = takeover.year * 100 + takeover.month
    before = [r for r in rec["rows"] if int(num(r.get("month_id"))) < tk and num(r.get("nmv"))]
    after = [r for r in rec["rows"] if int(num(r.get("month_id"))) >= tk and num(r.get("nmv"))]
    avg = lambda rows, k: sum(num(r.get(k)) for r in rows) / len(rows) if rows else 0
    sec["tables"].append(table(
        "eras", "Average month before and after Acme",
        ["Era", "Months", "Avg NMV", "Avg orders", "Avg buyers"],
        [["Before Acme", len(before), avg(before, "nmv"), avg(before, "orders"), avg(before, "buyers")],
         ["With Acme", len(after), avg(after, "nmv"), avg(after, "orders"), avg(after, "buyers")]],
        rec["key"]))
    if before and after:
        sec["headline"] = (f"An average month with Acme made {idr(avg(after, 'nmv'))} NMV, "
                           f"{updown(change(avg(after, 'nmv'), avg(before, 'nmv')))} on the "
                           f"{len(before)} months before the takeover")
        sec["facts"].append(fact(
            "Average monthly NMV with Acme versus before: "
            + change_text(avg(after, "nmv"), avg(before, "nmv")) + ".", rec["key"]))
        sec["facts"].append(fact(
            "Months before the takeover were not run by Acme; seasonality and price changes "
            "also differ between the eras.", rec["key"], "INTERPRETATION"))
    return degrade(sec)


# ---------------------------------------------------------------- C. why it changed
def sec_marketplace(run):
    sec = section("marketplace", "C", "Marketplaces")
    p, b, cur, prev = processed_list(run), lit(run.brand), run.month, run.month.shift(-1)
    rec = run.query(
        "orders_marketplace", L2_ORDERS, ("marketplace", "month_id", "order_status", "nmv"),
        lambda dl: (
            f"SELECT marketplace, month_id, "
            f"SUM(CASE WHEN order_status IN ({p}) THEN nmv ELSE 0 END) AS nmv, "
            f"COUNT(DISTINCT CASE WHEN order_status IN ({p}) THEN order_id END) AS orders "
            f"FROM {L2_ORDERS} WHERE UPPER(brand) = {b} AND month_id IN ({cur.mid}, {prev.mid}) "
            f"GROUP BY marketplace, month_id"))
    if not use(sec, rec):
        return degrade(sec)
    agg = {}
    for r in rec["rows"]:
        mp = r.get("marketplace") or _NOT_RECORDED
        slot = agg.setdefault(mp, {"cur": 0, "prev": 0, "orders": 0})
        if int(num(r.get("month_id"))) == cur.mid:
            slot["cur"] += num(r.get("nmv"))
            slot["orders"] += num(r.get("orders"))
        else:
            slot["prev"] += num(r.get("nmv"))
    total = sum(v["cur"] for v in agg.values())
    rows = sorted(([k, v["cur"], pct(v["cur"], total) or 0, v["prev"], change(v["cur"], v["prev"]),
                    v["orders"]] for k, v in agg.items()), key=lambda r: -r[1])
    sec["tables"].append(table("marketplace", "NMV by marketplace",
                               ["Marketplace", cur.label, _SHARE, prev.label, _MOM, "Orders"],
                               rows, rec["key"], formulas=[shr(2, 1), chg(4, 1, 3)],
                               chart={"type": "bar", "x": 0, "y": [1, 3]}))
    if rows and total:
        moves = [f"{r[0]} NMV {updown(r[4])}" for r in rows if r[4] is not None][:3]
        head = " and ".join(moves) + f" on {prev.label}" if moves else ""
        head += ("; " if head else "") + f"{rows[0][0]} made {rows[0][2]:.0f}% of {cur.label} NMV"
        sec["headline"] = head
        for r in rows[:4]:
            sec["facts"].append(fact(
                f"{r[0]}: {share_text(r[2])} of {cur.label} NMV, {change_text(r[1], r[3])}.",
                rec["key"]))
    sec["missing"].append(missing(
        "order channel per order (LIVE, video, product card, affiliate)",
        "channel mix explains where growth came from inside a marketplace",
        "the order channel on each order in the order data",
        visual="stacked NMV by channel per month", fallback="marketplace split only",
        updates="channel mix and its change month on month are added",
        detail=f"no channel column in {L2_ORDERS}"))
    return degrade(sec)


def sec_products(run):
    sec = section("products", "C", "Top products and pareto")
    cur, prev = run.month, run.month.shift(-1)
    rec = sku_performance(run)
    if not use(sec, rec):
        return degrade(sec)
    rows = _skus(rec)
    total = sum(num(r.get("nmv_cur")) for r in rows)
    focus = run.config.brand.focus_skus
    cols = ["#", "SKU", "Product"] + (["Focus flag"] if focus else []) + \
        ["NMV", _SHARE, f"NMV {prev.label}", _MOM, "Units", "NMV per unit"]
    i_nmv = cols.index("NMV")
    n80 = _rank_at_80(rows, total)
    out = [_top_sku_line(run, i, r, total, focus) for i, r in enumerate(rows[:TOP_N], 1)]
    sec["tables"].append(table(
        "top_skus", f"Top {TOP_N} products by NMV, {cur.label}", cols, out, rec["key"],
        formulas=[chg(i_nmv + 3, i_nmv, i_nmv + 2)],
        deck_columns=[j for j in range(len(cols)) if cols[j] != "SKU"]))
    if not out:
        return degrade(sec)
    sec["headline"] = _products_headline(rows, n80, cur, prev)
    sec["facts"].append(fact(sec["headline"] + ".", rec["key"]))
    movers = sorted((r for r in rows if num(r.get("nmv_prev"))),
                    key=lambda r: num(r.get("nmv_cur")) - num(r.get("nmv_prev")))
    if movers:
        up, down = movers[-1], movers[0]
        sec["facts"].append(fact(
            f"Largest gain: {_name(run, up)} ({change_text(up.get('nmv_cur'), up.get('nmv_prev'))}). "
            f"Largest drop: {_name(run, down)} "
            f"({change_text(down.get('nmv_cur'), down.get('nmv_prev'))}).", rec["key"]))
    _pareto_prices(run, sec, rows[:min(n80 or len(rows), TOP_N)])
    _focus_share(sec, rec["key"], rows, total, focus, cur)
    return degrade(sec)


def _rank_at_80(rows, total) -> int | None:
    """How many products, largest first, it takes to reach 80% of NMV."""
    cum = 0.0
    for i, r in enumerate(rows, 1):
        cum += num(r.get("nmv_cur"))
        if (pct(cum, total) or 0) >= 80:
            return i
    return None


def _unit_price(r, period) -> float | None:
    """NMV per unit sold in the period ("cur" or "prev"); None without units."""
    u = num(r.get(f"units_{period}"))
    return num(r.get(f"nmv_{period}")) / u if u else None


def _top_sku_line(run, i, r, total, focus) -> list:
    line = [i, r.get("sku_id"), _name(run, r)]
    if focus:
        line.append((focus.get(str(r.get("sku_id"))) or {}).get("flag", ""))
    line += [num(r.get("nmv_cur")), pct(r.get("nmv_cur"), total) or 0,
             num(r.get("nmv_prev")), change(r.get("nmv_cur"), r.get("nmv_prev")),
             num(r.get("units_cur")), _unit_price(r, "cur")]
    return line


def _products_headline(rows, n80, cur, prev) -> str:
    """The pareto, then how the top ten moved on price per unit and on units."""
    top10 = [r for r in rows[:10] if num(r.get("units_cur")) and num(r.get("units_prev"))
             and num(r.get("nmv_prev"))]
    dearer = sum(1 for r in top10 if num(r.get("nmv_cur")) / num(r.get("units_cur"))
                 > num(r.get("nmv_prev")) / num(r.get("units_prev")))
    fewer = sum(1 for r in top10 if num(r.get("units_cur")) < num(r.get("units_prev")))
    head = f"{n80 or len(rows)} of {len(rows)} products made 80% of {cur.label} NMV"
    if top10:
        head += (f"; {dearer} of the top {len(top10)} sold at a higher price per "
                 f"unit than in {prev.label} and {fewer} sold fewer units")
    return head


def _focus_share(sec, key, rows, total, focus, cur) -> None:
    """The focus products' share of NMV, or the open item asking for them."""
    if focus:
        fsum = sum(num(r.get("nmv_cur")) for r in rows if str(r.get("sku_id")) in focus)
        sec["facts"].append(fact(
            f"Focus products ({len(focus)} configured) made {share_text(pct(fsum, total))} of "
            f"{cur.label} NMV.", key))
        return
    sec["missing"].append(missing(
        "focus products and pack sizes",
        "the brand's priority (for example premium products) is how the client judges the mix",
        "the focus products, their flag and pack size, in the brand's MBR settings",
        visual="NMV share of focus products by month",
        fallback="top products without a focus flag; pack sizes read from the titles",
        updates="focus share appears", detail="tenant config mbr.brands.<brand>.focus_skus"))


def _pareto_prices(run, sec, pareto):
    """The standard reads the pareto with price per unit (average, lowest, highest) month on
    month, and with traffic. Traffic joins only when the product report's ids match the
    order SKUs; otherwise the gap is stated, never guessed through product names."""
    if not pareto:
        return
    cur, prev = run.month, run.month.shift(-1)
    rng = sku_price_range(run)
    lo_hi = _price_ranges(sec, rng)
    trf = product_traffic(run)
    views = {str(r.get("product_sku")): r for r in trf["rows"]} if trf["ok"] else {}
    joined = sum(1 for r in pareto if str(r.get("sku_id")) in views) * 2 >= len(pareto)
    if joined:
        sec["queries"].append(trf["key"])
    cols = _pareto_columns(cur, prev, lo_hi, joined)
    out = [_pareto_line(run, i, r, (cur, prev), lo_hi, views if joined else None)
           for i, r in enumerate(pareto, 1)]
    keep = {"#", "Product", f"Price per unit {cur.label}", f"Price per unit {prev.label}",
            _MOM, f"Lowest {cur.label}", f"Highest {cur.label}",
            f"Page views {cur.label}"}
    sources = [sec["queries"][0]] + ([rng["key"]] if lo_hi else []) \
        + ([trf["key"]] if joined else [])
    sec["tables"].append(table(
        "pareto_prices", f"Price per unit of the products that made 80% of NMV, {cur.label} "
        f"vs {prev.label}", cols, out, ",".join(sources),
        formulas=[chg(5, 3, 4)],
        deck_columns=[j for j, c in enumerate(cols) if c in keep]))
    _pareto_takeaway(sec, out, (cur, prev), _widest_spread(out) if lo_hi else None,
                     bool(lo_hi))
    if not joined:
        sec["missing"].append(missing(
            "page views per product in the sales data",
            "the standard reads the pareto with traffic next to price, to tell a demand "
            "change from a price change",
            "the product report keyed by the same product codes as the orders; today its "
            "listing ids do not match the order SKUs",
            visual="pareto table with page views and conversion per product, month on month",
            fallback="traffic by listing on the products by traffic and conversion page",
            updates="page views and conversion join the pareto table", key="traffic",
            detail="acme_store_product_mp.product_sku does not match acme_store_order_mp.sku_id"
                   " for most pareto products"))


def _price_ranges(sec, rng) -> dict:
    """{(sku, month id): (lowest, highest price per unit)}; {} when the query failed."""
    lo_hi = {}
    if rng["ok"]:
        sec["queries"].append(rng["key"])
        for r in rng["rows"]:
            lo_hi[(str(r.get("sku_id")), int(num(r.get("month_id"))))] = (
                num(r.get("price_low")), num(r.get("price_high")))
    return lo_hi


def _pareto_columns(cur, prev, lo_hi, joined) -> list:
    cols = ["#", "SKU", "Product", f"Price per unit {cur.label}", f"Price per unit {prev.label}",
            _MOM]
    if lo_hi:
        cols += [f"Lowest {cur.label}", f"Highest {cur.label}", f"Lowest {prev.label}",
                 f"Highest {prev.label}"]
    if joined:
        cols += [f"Page views {cur.label}", f"Conversion % {cur.label}"]
    return cols


def _range_cells(lo_hi, sid, cur, prev) -> list:
    lc = lo_hi.get((sid, cur.mid)) or (None, None)
    lp = lo_hi.get((sid, prev.mid)) or (None, None)
    return [lc[0], lc[1], lp[0], lp[1]]


def _traffic_cells(views, sid) -> list:
    v = views.get(sid) or {}
    if not v:
        return [None, None]
    return [num(v.get("views")), num(v.get("cvr"))]


def _pareto_line(run, i, r, months, lo_hi, traffic) -> list:
    """One pareto product: price per unit both months, the review month's lowest and
    highest when known, and page views and conversion when ``traffic`` (the product
    report by listing id) joins the order SKUs."""
    cur, prev = months
    sid = str(r.get("sku_id"))
    pc, pp = _unit_price(r, "cur"), _unit_price(r, "prev")
    line = [i, r.get("sku_id"), _name(run, r), pc, pp, change(pc, pp)]
    if lo_hi:
        line += _range_cells(lo_hi, sid, cur, prev)
    if traffic is not None:
        line += _traffic_cells(traffic, sid)
    return line


# A unit sold under a fifth of its product's average price is a voucher, a gift or a price
# error, not a price point (ACMESWEET Sep 2026: one Jumbo Vanilla 950g at IDR 100 against
# IDR 284,374 on average); it is named on its own instead of making the "widest spread".
_ODD_LOW = 0.2


def _odd_low(line) -> bool:
    lo, avg = line[6], line[3]
    return bool(lo and avg and lo < avg * _ODD_LOW)


def _widest_spread(out) -> tuple | None:
    """(product, lowest, highest) of the widest price range in the review month."""
    spread = None
    for line in out:
        lo, hi = line[6], line[7]
        if _odd_low(line):
            continue
        if lo and hi and (spread is None or hi - lo > spread[2] - spread[1]):
            spread = (line[2], lo, hi)
    return spread


def _pareto_takeaway(sec, out, months, spread, ranges=False) -> None:
    cur, prev = months
    both = sum(1 for r in out if r[3] and r[4])
    if not both:
        return
    dearer = sum(1 for r in out if r[3] and r[4] and r[3] > r[4])
    text = (f"{dearer} of the {both} products that made 80% of NMV and sold in both months "
            f"sold at a higher average price per unit than in {prev.label}")
    if spread:
        text += (f"; the widest spread in {cur.label} was {spread[0]}, from "
                 f"{idr(spread[1])} to {idr(spread[2])} a unit")
    sec["facts"].append(fact(text + ".", sec["tables"][-1]["source"]))
    sec["tables"][-1]["takeaway"] = text + "."
    odd = [line for line in out if _odd_low(line)] if ranges else []
    if odd:
        named = ", ".join(f"{line[2]} at {idr(line[6])} against {idr(line[3])} on average"
                          for line in odd[:3])
        sec["facts"].append(fact(
            f"{count(len(odd))} of these products sold at least one unit in {cur.label} for under "
            f"a fifth of its average price ({named}): a voucher, a gift or a price error to "
            "check, left out of the price spread.", sec["tables"][-1]["source"],
            "INTERPRETATION"))


def sec_nmv_bridge(run):
    """Why NMV moved: the same products selling more or fewer units, at a higher or lower
    price, and products that started or stopped selling. Exact for products sold in both
    months: units effect sum((u1 - u0) * p0) + price effect sum((p1 - p0) * u1) equals
    their NMV change."""
    sec = section("nmv_bridge", "C", "Units, price and listings behind NMV")
    cur, prev = run.month, run.month.shift(-1)
    rec = sku_performance(run)
    if not use(sec, rec):
        return degrade(sec)
    rows = _skus(rec)
    if not rows:
        return degrade(sec)
    (units_eff, price_eff, new, gone), movers = _bridge_effects(run, rows)
    t0 = sum(num(r.get("nmv_prev")) for r in rows)
    t1 = sum(num(r.get("nmv_cur")) for r in rows)
    other = (t1 - t0) - (units_eff + price_eff + new + gone)
    steps = [[f"NMV {prev.label}", t0], ["Units sold", units_eff],
             ["Price per unit", price_eff], ["New listings", new],
             ["Stopped selling", gone]]
    if abs(other) >= 1:
        steps.append(["Other", other])
    steps.append([f"NMV {cur.label}", t1])
    sec["tables"].append(table("bridge", f"NMV from {prev.label} to {cur.label}",
                               ["Step", "NMV"], steps, rec["key"],
                               chart={"type": "column", "x": 0, "y": [1]}))
    movers.sort(key=lambda m: -abs(m[3]))
    sec["tables"].append(table(
        "movers", f"Products that moved NMV most, {prev.label} to {cur.label}",
        ["Product", f"NMV {prev.label}", f"NMV {cur.label}", "NMV difference",
         f"Units {prev.label}", f"Units {cur.label}", "Price per unit change %"],
        movers[:15], rec["key"], formulas=[{"col": 3, "kind": "diff", "a": 2, "b": 1}]))
    parts = _bridge_parts(units_eff, price_eff, new, gone)
    diff = t1 - t0
    sec["headline"] = (f"NMV {'rose' if diff >= 0 else 'fell'} {idr(abs(diff))} from "
                       f"{prev.label}: " + ", ".join(parts))
    sec["facts"].append(fact(sec["headline"] + ".", rec["key"]))
    sec["facts"].append(fact(
        "Units sold = change in units times last month's price per unit; price per unit = "
        "change in price per unit times this month's units; both over products sold in both "
        "months, so together they equal those products' NMV change. New listings sold only "
        "this month, stopped selling only last month; other = sales without a unit count.",
        rec["key"],
        "INTERPRETATION"))
    return degrade(sec)


def _bridge_effects(run, rows) -> tuple:
    """((units, price, new listings, stopped selling), movers) over the products."""
    units_eff = price_eff = new = gone = 0.0
    movers = []
    for r in rows:
        n0, n1 = num(r.get("nmv_prev")), num(r.get("nmv_cur"))
        u0, u1 = num(r.get("units_prev")), num(r.get("units_cur"))
        if n0 and n1 and u0 and u1:
            p0, p1 = n0 / u0, n1 / u1
            units_eff += (u1 - u0) * p0
            price_eff += (p1 - p0) * u1
            movers.append([_name(run, r), n0, n1, n1 - n0, u0, u1, change(p1, p0)])
        elif n1 and not n0:
            new += n1
            movers.append([_name(run, r), n0, n1, n1 - n0, u0, u1, None])
        elif n0 and not n1:
            gone -= n0
            movers.append([_name(run, r), n0, n1, n1 - n0, u0, u1, None])
    return (units_eff, price_eff, new, gone), movers


def _bridge_parts(units_eff, price_eff, new, gone) -> list:
    """The three largest steps of the bridge in words, largest first."""
    parts = []
    for value, up, down in ((units_eff, "more units added", "fewer units took off"),
                            (price_eff, "higher prices added", "lower prices took off"),
                            (new, "new listings added", ""), (gone, "", "listings that stopped "
                                                              "selling took off")):
        if abs(value) >= 1:
            parts.append((abs(value), f"{up if value > 0 else down} {idr(abs(value))}"))
    parts.sort(key=lambda p: -p[0])
    return [p[1] for p in parts[:3]]


def sec_price_pack(run):
    sec = section("price_pack", "C", "Price and pack sizes")
    cur, prev = run.month, run.month.shift(-1)
    rec = sku_performance(run)
    if not use(sec, rec):
        return degrade(sec)
    rows = _skus(rec)
    both = [r for r in rows if num(r.get("units_cur")) and num(r.get("units_prev"))
            and num(r.get("nmv_cur")) and num(r.get("nmv_prev"))]
    lfl = _like_for_like(both)
    out = _pack_ladder(run, rows)
    sec["tables"].append(table("pack_ladder", f"NMV by pack size, {cur.label}",
                               ["Pack", "Products", "Units", "NMV", "NMV share %",
                                "NMV per piece"], out, rec["key"], formulas=[shr(4, 3)]))
    moves = sorted(both, key=lambda r: -num(r.get("nmv_cur")))[:15]
    sec["tables"].append(table(
        "price_moves", f"Price per unit of the largest products, {prev.label} and {cur.label}",
        ["Product", f"Price per unit {prev.label}", f"Price per unit {cur.label}",
         "Change %", f"Units {cur.label}"],
        [[_name(run, r), num(r["nmv_prev"]) / num(r["units_prev"]),
          num(r["nmv_cur"]) / num(r["units_cur"]),
          change(num(r["nmv_cur"]) / num(r["units_cur"]), num(r["nmv_prev"]) / num(r["units_prev"])),
          num(r["units_cur"])] for r in moves], rec["key"], formulas=[chg(3, 2, 1)]))
    head = []
    if lfl is not None:
        head.append(f"Like for like, price per unit was {updown(lfl)} on {prev.label}")
        sec["facts"].append(fact(
            f"Across the {len(both)} products sold in both months, price per unit moved "
            f"{updown(lfl)}, weighted by {cur.label} units.", rec["key"]))
    pack = _pack_price_finding(run, sec, rec["key"], rows, out)
    if pack:
        head.append(pack)
    if head:
        sec["headline"] = "; ".join(head)
    sec["facts"].append(fact(
        "Pack sizes are read from the listing titles (Isi 4, 4 x 225 g, Twin Pack); a value "
        "pack whose title gives no count is listed apart.", rec["key"], "INTERPRETATION"))
    return degrade(sec)


def _like_for_like(both) -> float | None:
    """Price per unit change over products sold in both months, weighted by this month's
    units (percent)."""
    if not both:
        return None
    num_ = sum((num(r["nmv_cur"]) / num(r["units_cur"]) - num(r["nmv_prev"]) / num(r["units_prev"]))
               * num(r["units_cur"]) for r in both)
    den = sum(num(r["nmv_prev"]) / num(r["units_prev"]) * num(r["units_cur"]) for r in both)
    return num_ / den * 100 if den else None


def _pack_ladder(run, rows) -> list:
    """Pack size, products, units, NMV, NMV share and NMV per piece, 1 piece first."""
    ladder = {}
    for r in rows:
        if not num(r.get("units_cur")):
            continue
        n = _pack(run, r)
        slot = ladder.setdefault(n, {"products": 0, "units": 0.0, "nmv": 0.0})
        slot["products"] += 1
        slot["units"] += num(r.get("units_cur"))
        slot["nmv"] += num(r.get("nmv_cur"))
    total = sum(v["nmv"] for v in ladder.values())
    out = []
    for n in sorted(ladder, key=lambda k: (k is None, k or 0)):
        v = ladder[n]
        per_piece = v["nmv"] / (v["units"] * n) if n and v["units"] else None
        out.append([L.pack_label(n), v["products"], v["units"], v["nmv"],
                    pct(v["nmv"], total) or 0, per_piece])
    return out


def _pack_price_finding(run, sec, key, rows, out) -> str | None:
    """The best selling pack size's share, then its price per piece against the SAME
    product sold as 1 piece. The ladder's own NMV per piece mixes products (a pack of 2
    wafers against a 950 g tin) and was read as 'a lower priced entry point' (package 6);
    only a like for like pair says what a pack costs a buyer. The fact goes on the page,
    the returned text into the headline."""
    one = next((r for r in out if r[0] == L.ONE_PIECE), None)
    packs = [r for r in out if r[0].startswith("Pack of")]
    if not (one and packs):
        return None
    big = max(packs, key=lambda r: r[3])
    sec["facts"].append(fact(
        f"{big[0]} listings made {share_text(big[4])} of NMV; 1 piece listings made "
        f"{share_text(one[4])}.", key))
    pairs = _pack_pairs(run, rows, int(big[0].split()[-1]))
    if not pairs:
        return f"{big[0].lower()} listings made {share_text(big[4])} of NMV"
    weight = sum(p[3] for p in pairs)
    gap = sum((p[2] / p[1] - 1) * p[3] for p in pairs) / weight * 100
    top = max(pairs, key=lambda p: p[3])
    side = "below" if gap < 0 else "above"
    sec["facts"].append(fact(
        f"Like for like, a {big[0].lower()} sold {abs(gap):.1f}% {side} the 1 piece price "
        f"per piece, over {count(len(pairs))} product(s) sold both ways; "
        f"{top[0][:1].upper()}{top[0][1:]}: {idr(top[2])} a piece in the pack against "
        f"{idr(top[1])} for 1 piece.", key))
    return f"a {big[0].lower()} sold {abs(gap):.1f}% {side} the 1 piece price per piece"


def _pack_pairs(run, rows, n) -> list:
    """[(product, 1 piece price, price per piece in a pack of n, pack NMV)] for products
    this month sold both as 1 piece and as a pack of n."""
    sides: dict = {}
    for r in rows:
        units, nmv = num(r.get("units_cur")), num(r.get("nmv_cur"))
        size = _pack(run, r)
        if not units or not nmv or size not in (1, n):
            continue
        slot = sides.setdefault(L.product_base(r.get("product_name"), run.brand),
                                {1: [0.0, 0.0], n: [0.0, 0.0]})
        slot[size][0] += nmv
        slot[size][1] += units * size
    return [(base, s[1][0] / s[1][1], s[n][0] / s[n][1], s[n][0])
            for base, s in sides.items() if s[1][1] and s[n][1]]


def _type_nmv(run, skus) -> tuple:
    """({product type: [nmv_cur, nmv_prev]}, the NMV whose type was read from the title)."""
    agg, derived = {}, {"cur": 0.0, "prev": 0.0}
    for r in skus:
        typ = str(r.get("product_type") or "").strip()
        if not typ:
            typ = "Single" if _pack(run, r) == 1 else "Bundle"
            derived["cur"] += num(r.get("nmv_cur"))
            derived["prev"] += num(r.get("nmv_prev"))
        slot = agg.setdefault(typ, [0.0, 0.0])
        slot[0] += num(r.get("nmv_cur"))
        slot[1] += num(r.get("nmv_prev"))
    return agg, derived


def _type_headline(sec, rows, prev_total, cur, prev, key) -> None:
    # the share and the NMV change are said apart: "51%, up 0.6%" read as a share move
    top = rows[0]
    sec["headline"] = f"{top[0]} products made {share_text(top[2])} of {cur.label} NMV"
    if prev_total:
        sec["headline"] += f", against {share_text(pct(top[3], prev_total))} in {prev.label}"
    moves = ", ".join(f"{r[0].lower()} NMV {updown(r[4])}" for r in rows if r[4] is not None)
    sec["facts"].append(fact(sec["headline"] + (f"; {moves}." if moves else "."), key))


def sec_product_type(run):
    sec = section("product_mix", "C", "Product mix")
    cur, prev = run.month, run.month.shift(-1)
    rec = sku_performance(run)
    if not use(sec, rec):
        return degrade(sec)
    agg, derived = _type_nmv(run, _skus(rec))
    total = sum(v[0] for v in agg.values())
    rows = sorted(([k, v[0], pct(v[0], total) or 0, v[1], change(v[0], v[1])]
                   for k, v in agg.items()), key=lambda r: -r[1])
    sec["tables"].append(table("product_type", "NMV by product type",
                               ["Product type", cur.label, _SHARE, prev.label, _MOM],
                               rows, rec["key"], formulas=[shr(2, 1), chg(4, 1, 3)],
                               chart={"type": "bar", "x": 0, "y": [1, 3]}))
    prev_total = sum(r[3] for r in rows)
    if derived["cur"] or derived["prev"]:
        sec["facts"].append(fact(
            f"Where the order data leaves the product type blank it is read from the listing "
            f"title (single or bundle): {share_text(pct(derived['prev'], prev_total))} of "
            f"{prev.label} NMV and {share_text(pct(derived['cur'], total))} of {cur.label} NMV.",
            rec["key"], "INTERPRETATION"))
    if rows and total:
        _type_headline(sec, rows, prev_total, cur, prev, rec["key"])
    _product_lines(run, sec)
    return degrade(sec)


def _merged_lines(rows) -> dict:
    """{label: [nmv_cur, nmv_prev]}; spellings that differ only in case or spacing are one
    line, and a blank line is "Not recorded"."""
    out: dict = {}
    for r in rows:
        name = " ".join(str(r.get("line") or "").split())
        label = name.title() if name else _NOT_RECORDED
        slot = out.setdefault(label, [0.0, 0.0])
        slot[0] += num(r.get("nmv_cur"))
        slot[1] += num(r.get("nmv_prev"))
    return out


def _product_lines(run, sec):
    """Optional product line split (sub brand), when the order data carries one and it is
    not a single value."""
    col = next((c for c in _LINE_COLUMNS if _has(run, L2_ORDERS, c)), None)
    if not col:
        return
    p, b, cur, prev = processed_list(run), lit(run.brand), run.month, run.month.shift(-1)
    rec = run.query(
        "product_line_mix", L2_ORDERS, (col, "month_id", "order_status", "nmv"),
        lambda dl: (
            f"SELECT {col} AS line, "
            f"SUM(CASE WHEN month_id = {cur.mid} AND order_status IN ({p}) THEN nmv ELSE 0 END) AS nmv_cur, "
            f"SUM(CASE WHEN month_id = {prev.mid} AND order_status IN ({p}) THEN nmv ELSE 0 END) AS nmv_prev "
            f"FROM {L2_ORDERS} WHERE UPPER(brand) = {b} AND month_id IN ({cur.mid}, {prev.mid}) "
            f"GROUP BY {col} ORDER BY nmv_cur DESC"))
    if not rec["ok"] or not rec["rows"]:
        return
    lines = _merged_lines(rec["rows"])
    total = sum(v[0] for v in lines.values())
    rows = sorted(([name, v[0], pct(v[0], total) or 0, v[1], change(v[0], v[1])]
                   for name, v in lines.items()), key=lambda r: -r[1])
    named = [r for r in rows if r[0] != _NOT_RECORDED]
    # one named line (staging DSOL: "Acmesweet" twice in two spellings, 57% blank) is
    # not a split worth a slide
    if not total or len(named) < 2 or rows[0][2] >= 95:
        return
    sec["queries"].append(rec["key"])
    sec["tables"].append(table("product_line", "NMV by product line",
                               ["Product line", cur.label, _SHARE, prev.label, _MOM],
                               rows[:15], rec["key"], formulas=[shr(2, 1), chg(4, 1, 3)]))
    sec["facts"].append(fact(f"{rows[0][0]} was the largest product line at "
                             f"{share_text(rows[0][2])} of NMV ({updown(rows[0][4])} on "
                             f"{prev.label}).", rec["key"]))


_TRAFFIC = "acme_customer_traffic_conversion"
# The traffic table was rebuilt in the warehouse (staging, Oct 2026: a fresh scan no longer
# lists order_month, total_orders or total_nmv); the first name the scan knows is read.
_TRAFFIC_MONTH = ("order_month", "month_id", "month", "period", "date")
_TRAFFIC_ORDERS = ("total_orders", "orders", "order_count", "total_order")
_TRAFFIC_NMV = ("total_nmv", "nmv")


def _first_known(run, table, names):
    return next((c for c in names if _has(run, table, c)), None)


def _traffic_sql(dl, w, where, month, orders, nmv, views):
    if month.endswith("_id"):                      # a YYYYMM number
        key, lo, hi = month, w[0].mid, w[-1].mid
    else:                                          # a date or 'YYYY-MM' text
        key, lo, hi = month_text(dl, month), lit(w[0].key), lit(w[-1].key)
    picks = [f"{key} AS month_key", "SUM(visitors) AS visitors", f"SUM({orders}) AS orders"]
    picks += ["SUM(views) AS views"] if views else []
    picks += [f"SUM({nmv}) AS nmv"] if nmv else []
    return (f"SELECT {', '.join(picks)} FROM {_TRAFFIC} WHERE {where} "
            f"AND {key} BETWEEN {lo} AND {hi} GROUP BY {key} ORDER BY 1")


# When no client name is set and the brand name finds no traffic rows (staging DSOL, Oct
# 2026), the brand is looked for in these columns, most exact first.
_TRAFFIC_MATCH = (("brand", "the brand column", "="), ("client", "client names", "in"),
                  ("store", "store names", "in"), ("store_name", "store names", "in"))


def _traffic_rows(run, columns, build):
    """(query record, how the brand was matched or None): the configured client names
    first, then the brand's own name in the columns of _TRAFFIC_MATCH the scan lists."""
    names = run.config.brand.client_names
    rec = run.query("traffic_conversion", _TRAFFIC, ("client",) + columns,
                    build(f"UPPER(client) IN ({lit_list(names)})"))
    if not rec["ok"] or rec["rows"] or names != [run.brand]:
        return rec, None
    b = lit(run.brand)
    for col, what, how in _TRAFFIC_MATCH:
        if not _has(run, _TRAFFIC, col):
            continue
        where = f"UPPER({col}) = {b}" if how == "=" else f"STRPOS(UPPER({col}), {b}) > 0"
        # one key per column: Run caches a query by its key
        alt = run.query(f"traffic_conversion_{col}", _TRAFFIC, (col,) + columns, build(where))
        if alt["ok"] and alt["rows"]:
            return alt, what if how == "=" else f"{what} containing {run.brand}"
    return rec, None


def sec_traffic(run):
    sec = section("traffic", "C", "Traffic and conversion")
    w = months_window(run)
    month = _first_known(run, _TRAFFIC, _TRAFFIC_MONTH) or _TRAFFIC_MONTH[0]
    orders = _first_known(run, _TRAFFIC, _TRAFFIC_ORDERS) or _TRAFFIC_ORDERS[0]
    nmv = _first_known(run, _TRAFFIC, _TRAFFIC_NMV)
    views = _has(run, _TRAFFIC, "views")
    rec, matched = _traffic_rows(
        run, (month, "visitors", orders),
        lambda where: lambda dl: _traffic_sql(dl, w, where, month, orders, nmv, views))
    if not use(sec, rec):
        sec["missing"].append(_funnel_gap())
        return degrade(sec)
    if matched:
        _traffic_matched(run, sec, matched, rec["key"])
    rows = [[L.month_label(r.get("month_key")), num(r.get("visitors")),
             num(r.get("views")) if views else _NOT_RECORDED, num(r.get("orders")),
             pct(r.get("orders"), r.get("visitors"))]
            + ([num(r.get("nmv"))] if nmv else []) for r in rec["rows"]]
    sec["tables"].append(table("traffic", "Store visitors, views and conversion by month",
                               ["Month", "Visitors", "Views", "Orders", _CONVERSION]
                               + (["NMV"] if nmv else []),
                               rows, rec["key"], formulas=[rat(4, 3, 1, 100)]))
    by = {r[0]: r for r in rows}
    c, p = by.get(run.month.label), by.get(run.month.shift(-1).label)
    if c and p:
        _traffic_headline(run, sec, c, p, rec["key"])
    elif not rows:
        sec["missing"].append(_traffic_gap(run))
    sec["missing"].append(_funnel_gap())
    return degrade(sec)


def _traffic_matched(run, sec, matched, key) -> None:
    # a client name pins only a client match: the brand column is already exact, and a
    # store match is not what client_names filters
    pin = (" Setting the client name in the brand's MBR settings pins it."
           if matched.startswith("client") else "")
    sec["facts"].append(fact(
        f"The traffic data names no client {run.brand}; its rows were matched by "
        f"{matched}.{pin}", key, "INTERPRETATION"))


def _traffic_headline(run, sec, c, p, key) -> None:
    sec["headline"] = (f"Visitors {updown(change(c[1], p[1]))} on {run.month.shift(-1).label}"
                       f" while conversion moved from {p[4] or 0:.2f}% to {c[4] or 0:.2f}%")
    sec["facts"].append(fact(
        f"Visitors {change_text(c[1], p[1], count)}; conversion moved from "
        f"{p[4] or 0:.2f}% to {c[4] or 0:.2f}%.", key))


def _traffic_gap(run):
    """No traffic rows for the brand. A client (the principal) can sell several brands:
    staging Sep 2026, Acmesweet was about a quarter of client Acmehealth's traffic NMV. With no
    brand or store column the client name is no answer, so the data team is asked for one."""
    names = run.config.brand.client_names
    split = [c for c, _what, _how in _TRAFFIC_MATCH if c != "client" and _has(run, _TRAFFIC, c)]
    if names != [run.brand]:
        needs = ("the client name as the traffic data stores it, in the brand's MBR settings "
                 "(the name set finds no rows)")
        detail = f"{_TRAFFIC}.client; mbr.brands.<brand>.client_names"
    elif not split:
        needs = ("a brand or store column in the traffic data: it is kept per client, and a "
                 "client can sell several brands, so the client name alone would mix other "
                 "brands in")
        detail = (f"{_TRAFFIC} has no brand or store column and no client value holds "
                  f"{run.brand}; set mbr.brands.<brand>.client_names only when that client "
                  "sells this brand alone")
    else:
        needs = (f"the brand's rows in the traffic data: no client, brand or store name in it "
                 f"holds {run.brand}")
        detail = f"{_TRAFFIC}: {', '.join(['client'] + split)} hold no {run.brand}"
    return missing(f"traffic rows for {', '.join(names)}",
                   "traffic times conversion is the first breakdown of NMV", needs,
                   detail=detail)


def _funnel_gap():
    return missing(
        "impressions, clicks and add to cart per traffic source",
        "the funnel by source shows which engine (LIVE, video, affiliate, ads) brings buyers",
        "the product performance export with the funnel per source",
        visual="funnel by source", fallback="store level visitors and conversion",
        updates="the funnel per source is added", key="funnel")


# the analyst's quadrant names (review standard v1.2): traffic x conversion.
STAR = "Star"
HIDDEN_GEM = "Hidden gem"
ROUGH_DIAMOND = "Rough diamond"
UNDERPERFORMER = "Underperformer"
QUADRANTS = (STAR, HIDDEN_GEM, ROUGH_DIAMOND, UNDERPERFORMER)
QUADRANT_RULE = {STAR: "high traffic, high conversion",
                 HIDDEN_GEM: "low traffic, high conversion",
                 ROUGH_DIAMOND: "high traffic, low conversion",
                 UNDERPERFORMER: "low traffic, low conversion"}
# The standard sets traffic at 10k to 25k IMPRESSIONS and conversion at 0.15% or 0.2%. The
# product report holds page views, not impressions, so the traffic line comes from the data;
# conversion keeps the standard line whenever the data is on the same (percent) scale, and the
# basis says where that line sits in this month's products.
_CVR_STANDARD = (0.15, 0.2)
_VIEWS_STANDARD = (10000, 15000, 20000, 25000)


def quadrant_of(high_views: bool, high_cvr: bool) -> str:
    return {(True, True): STAR, (False, True): HIDDEN_GEM, (True, False): ROUGH_DIAMOND,
            (False, False): UNDERPERFORMER}[(high_views, high_cvr)]


def _two_sig(x: float) -> float:
    return float(f"{x:.2g}") if x else 0.0


def quadrant_thresholds(rows: list, configured: dict) -> dict:
    """The traffic and conversion lines, each with the reason it is where it is. Without
    settings they are the medians of the products that made 80% of revenue (the pareto
    products the review is about; a median over every listing would put the line in the
    long tail of listings nobody sees)."""
    pareto = _revenue_pareto(rows)
    mv = statistics.median([num(r.get("views")) for r in pareto]) if pareto else 0.0
    mc = statistics.median([num(r.get("cvr")) for r in pareto]) if pareto else 0.0
    tv = sum(num(r.get("views")) for r in rows)
    wc = sum(num(r.get("cvr")) * num(r.get("views")) for r in rows) / tv if tv else 0.0
    n = len(pareto)
    out = {"pareto_products": n, "median_views": mv, "median_cvr": mc, "weighted_cvr": wc}
    _views_line(out, configured, mv, n)
    _cvr_line(out, configured, pareto, (wc, mc))
    return out


def _revenue_pareto(rows) -> list:
    """The products, largest revenue first, that make 80% of revenue (at least five)."""
    ranked = sorted(rows, key=lambda r: -num(r.get("revenue")))
    total = sum(num(r.get("revenue")) for r in ranked)
    pareto, cum = [], 0.0
    for r in ranked:
        pareto.append(r)
        cum += num(r.get("revenue"))
        if total and cum / total >= 0.8 and len(pareto) >= 5:
            break
    return pareto


def _views_line(out, configured, mv, n) -> None:
    if configured.get("views"):
        out["views"], out["views_basis"] = configured["views"], "set in the MBR settings"
        out["views_note"] = out["views_basis"]
    elif 7500 <= mv <= 30000:
        out["views"] = min(_VIEWS_STANDARD, key=lambda v: abs(v - mv))
        out["views_basis"] = f"standard value nearest the median of the top {n} products"
        out["views_note"] = (f"the standard value nearest the median of the {n} products that "
                             f"made 80% of revenue ({count(mv)} page views)")
    else:
        out["views"] = _two_sig(mv)
        out["views_basis"] = f"median of the {n} products that made 80% of revenue"
        out["views_note"] = (f"the median of the {n} products that made 80% of revenue; the "
                             "standard's 10,000 to 25,000 counts impressions, which the product "
                             "report does not hold, so the line comes from page views")


def _cvr_line(out, configured, pareto, rates) -> None:
    """``rates`` = (conversion weighted by page views over every product, median
    conversion of the pareto products)."""
    wc, mc = rates
    n = len(pareto)
    if configured.get("cvr"):
        out["cvr"], out["cvr_basis"] = configured["cvr"], "set in the MBR settings"
        out["cvr_note"] = out["cvr_basis"]
    elif 0.05 <= wc <= 5:
        out["cvr"] = _CVR_STANDARD[1] if wc >= _CVR_STANDARD[1] else _CVR_STANDARD[0]
        above = sum(1 for r in pareto if num(r.get("cvr")) >= out["cvr"])
        out["cvr_basis"] = f"standard line, {above} of those {n} products convert above it"
        out["cvr_note"] = (f"the standard line; {above} of the {n} products that made 80% of "
                           f"revenue convert above it, and all products together convert "
                           f"{wc:.2f}% of their page views")
    else:
        out["cvr"] = round(mc, 2) if mc >= 0.01 else float(f"{mc:.2g}")
        out["cvr_basis"] = f"median of the same {n} products"
        out["cvr_note"] = (f"the median conversion of the same {n} products; the conversion "
                           "figures are on a different scale from the standard's 0.15% to 0.2%")


def sec_product_funnel(run):
    sec = section("product_funnel", "C", "Products by traffic and conversion")
    cur = run.month
    units = _has(run, "acme_store_product_mp", "total_selling_product")
    rec = product_traffic(run)
    if not use(sec, rec):
        return degrade(sec)
    rows = rec["rows"]
    if not rows:
        return degrade(sec)
    th = quadrant_thresholds(rows, run.config.brand.quadrant)
    tv = sum(num(r.get("views")) for r in rows)
    tr = sum(num(r.get("revenue")) for r in rows)
    quad = _quadrant_groups(rows, th)
    sec["tables"].append(table(
        "quadrant", "Products by traffic and conversion",
        ["Group", "Rule", "Products", "Page views", "Revenue", "Revenue share %",
         "Page view share %"],
        [[q, QUADRANT_RULE[q], v["products"], v["views"], v["revenue"], pct(v["revenue"], tr) or 0,
          pct(v["views"], tv) or 0] for q, v in quad.items()], rec["key"],
        formulas=[shr(5, 4), shr(6, 3)]))
    top_cols = ["Group", "#", "SKU", "Product", "Page views", _CONVERSION, "Revenue"] + \
        (["Units"] if units else [])
    sec["tables"].append(table(
        "quadrant_top", "Top 5 products of each group by revenue", top_cols,
        _quadrant_top_rows(run, quad, units),
        rec["key"], deck_columns=[j for j in range(len(top_cols)) if top_cols[j] != "SKU"]))
    sec["tables"].append(table(
        "quadrant_thresholds", "Quadrant thresholds",
        ["Line", "Threshold", "Basis"],
        [["Traffic (page views in the month)", th["views"], th["views_note"]],
         [_CONVERSION, th["cvr"], th["cvr_note"]]], rec["key"]))
    sec["quadrants"] = {"thresholds": th, "groups": _quadrant_summary(run, quad, tr, tv)}
    star, gem, rough = quad[STAR], quad[HIDDEN_GEM], quad[ROUGH_DIAMOND]
    sec["headline"] = _quadrant_headline(star, gem, tr, tv)
    sec["facts"].append(fact(sec["headline"] + ".", rec["key"]))
    _quadrant_advice(sec, rec["key"], (rough, gem), th, tv)
    sec["facts"].append(fact(
        f"High traffic means at least {count(th['views'])} page views in {cur.label}, high "
        f"conversion at least {th['cvr']:.2f}%; the quadrant thresholds table gives the basis "
        "of each line.", rec["key"], "INTERPRETATION"))
    sec["facts"].append(fact(
        "Revenue here is the product report's sales revenue; NMV is not in that report per "
        "listing, so the NMV ranking of products is on the top products page.", rec["key"],
        "INTERPRETATION"))
    return degrade(sec)


def _quadrant_groups(rows, th) -> dict:
    """Products, page views, revenue and the top five by revenue of each quadrant."""
    quad = {q: {"products": 0, "views": 0.0, "revenue": 0.0, "top": []} for q in QUADRANTS}
    for r in sorted(rows, key=lambda r: -num(r.get("revenue"))):
        q = quadrant_of(num(r.get("views")) >= th["views"], num(r.get("cvr")) >= th["cvr"])
        slot = quad[q]
        slot["products"] += 1
        slot["views"] += num(r.get("views"))
        slot["revenue"] += num(r.get("revenue"))
        if len(slot["top"]) < 5 and num(r.get("revenue")):
            slot["top"].append(r)
    return quad


def _quadrant_top_rows(run, quad, units) -> list:
    top_rows = []
    for q, v in quad.items():
        for i, r in enumerate(v["top"], 1):
            top_rows.append([q, i, r.get("product_sku"), _name(run, r), num(r.get("views")),
                             num(r.get("cvr")), num(r.get("revenue"))]
                            + ([num(r.get("units"))] if units else []))
    return top_rows


def _quadrant_summary(run, quad, tr, tv) -> dict:
    """Per quadrant: products, revenue and page view shares, the top five by name."""
    return {
        q: {"products": v["products"], "revenue_share": pct(v["revenue"], tr) or 0,
            "views_share": pct(v["views"], tv) or 0,
            "top": [{"name": _name(run, r), "revenue": num(r.get("revenue")),
                     "views": num(r.get("views")), "cvr": num(r.get("cvr"))} for r in v["top"]]}
        for q, v in quad.items()}


def _quadrant_headline(star, gem, tr, tv) -> str:
    head = (f"{star['products']} Star product{'s' if star['products'] != 1 else ''}"
            f" made {share_text(pct(star['revenue'], tr), 0)} of revenue")
    if gem["products"]:
        many = gem["products"] > 1
        head += (f"; {gem['products']} Hidden gem{'s' if many else ''} convert"
                 f"{'' if many else 's'} well on only "
                 f"{share_text(pct(gem['views'], tv), 0)} of page views")
    return head


def _quadrant_advice(sec, key, groups, th, tv) -> None:
    """What to do with the Rough diamonds and the Hidden gems."""
    rough, gem = groups
    if rough["products"]:
        sec["facts"].append(fact(
            f"{rough['products']} Rough diamond "
            f"{'products take' if rough['products'] != 1 else 'product takes'} "
            f"{share_text(pct(rough['views'], tv))} of page views without converting well; "
            "fix their page, price or offer before buying them more traffic.", key,
            "INTERPRETATION"))
    if gem["products"]:
        sec["facts"].append(fact(
            f"{gem['products']} Hidden gem "
            f"{'products convert' if gem['products'] != 1 else 'product converts'} at or above "
            f"{th['cvr']:.2f}% on low traffic; "
            f"{'they are the candidates' if gem['products'] != 1 else 'it is the candidate'} for "
            "more traffic.", key,
            "INTERPRETATION"))


def sec_creators(run):
    sec = section("creators", "C", "Creators and affiliates")
    b, cur, prev = lit(run.brand), run.month, run.month.shift(-1)
    tot = run.query(
        "affiliate_monthly", "acme_store_affiliatetrx_mp",
        ("month_id", "content_type", "order_id", "total_harga_item_pesanan", "creator_username"),
        lambda dl: (
            f"SELECT month_id, content_type, COUNT(DISTINCT order_id) AS orders, "
            f"SUM(total_harga_item_pesanan) AS gmv, COUNT(DISTINCT creator_username) AS creators "
            f"FROM acme_store_affiliatetrx_mp WHERE UPPER(brand) = {b} "
            f"AND month_id IN ({cur.mid}, {prev.mid}) GROUP BY month_id, content_type"))
    top = run.query(
        "affiliate_top", "acme_store_affiliatetrx_mp",
        ("creator_username", "month_id", "order_id", "total_harga_item_pesanan"),
        lambda dl: (
            f"SELECT creator_username, COUNT(DISTINCT order_id) AS orders, "
            f"SUM(total_harga_item_pesanan) AS gmv FROM acme_store_affiliatetrx_mp "
            f"WHERE UPPER(brand) = {b} AND month_id = {cur.mid} "
            f"GROUP BY creator_username ORDER BY gmv DESC LIMIT 20"))
    if use(sec, tot):
        rows = [[L.month_label(int(num(r.get("month_id")))),
                 str(r.get("content_type") or _NOT_RECORDED).capitalize(),
                 num(r.get("orders")), num(r.get("gmv")), num(r.get("creators"))]
                for r in tot["rows"]]
        if not any(r[3] for r in rows):
            sec["missing"].append(_empty_feed("affiliate orders", "acme_store_affiliatetrx_mp",
                                              run, "creator GMV is how the creator programme is "
                                              "judged", tot["key"]))
        else:
            sec["tables"].append(table("affiliate_mix", "Affiliate orders and GMV by content type",
                                       ["Month", "Content type", "Orders", "GMV", "Creators"],
                                       rows, tot["key"]))
            g = lambda label: sum(r[3] for r in rows if r[0] == label)
            sec["headline"] = f"Affiliate GMV {updown(change(g(cur.label), g(prev.label)))} on {prev.label}"
            sec["facts"].append(fact(
                f"Affiliate GMV {change_text(g(cur.label), g(prev.label))}.", tot["key"]))
    if use(sec, top) and any(num(r.get("gmv")) for r in top["rows"]):
        sec["tables"].append(table(
            "top_creators", f"Top creators, {cur.label}", ["Creator", "Orders", "GMV"],
            [[r.get("creator_username"), num(r.get("orders")), num(r.get("gmv"))]
             for r in top["rows"]], top["key"]))
    sec["missing"].append(missing(
        "affiliate partner (mitra) data", "partner commissions are part of the creator cost",
        "the affiliate partner export", fallback="creator level only",
        updates="partner rows join the creator table"))
    return degrade(sec)


def _accounts(run, table) -> list:
    """The columns that tell LIVE/video accounts apart, when the table has them."""
    return [c for c in ("marketplace", "owner") if _has(run, table, c)]


def _account(r) -> str:
    who = {"official": "own account", "affiliate": "affiliates"}.get(
        str(r.get("owner") or "").strip().lower(), "")
    mp = str(r.get("marketplace") or "").strip() or L.NOT_RECORDED
    return f"{mp}, {who}" if who else mp


def _monthly_by_account(rec, split, measures) -> list:
    """[month, (account,) *measures] rows of a LIVE/video query."""
    return [[L.month_label(int(num(r.get("month_id"))))] + ([_account(r)] if split else [])
            + [num(r.get(m)) for m in measures] for r in rec["rows"]]


def _silent_accounts(rows, cur_label, gmv_col) -> list:
    """(account, last month with GMV, that GMV) for accounts with GMV in an earlier month
    and no row at all in the review month, which a monthly total reads as a collapse
    (ACMESWEET Sep 2026: the shop's own Shopee LIVE made IDR 70.0 m in Jul and nothing
    after; the page said LIVE fell to 1.2 m, which was TikTok affiliates alone)."""
    present = {r[1] for r in rows if r[0] == cur_label}
    last = {}
    for r in rows:
        if r[0] != cur_label and r[1] not in present and r[gmv_col]:
            last[r[1]] = (r[0], r[gmv_col])     # rows come in month order
    return [(name, month, gmv) for name, (month, gmv) in sorted(last.items())]


def _empty_exports(run, kind) -> dict:
    """{account: [month labels]} of the attached exports of this kind that hold their
    columns and no rows: the marketplace had nothing to export for that account and month."""
    from .uploads.channels import owner_of
    from .uploads.parse import NO_ROWS
    out = {}
    for f in (getattr(run, "uploads", None) or {}).get("files") or []:
        if f.get("kind") != kind or f.get("reason") != NO_ROWS or not f.get("month"):
            continue
        try:
            label = Month.parse(f["month"]).label
        except Exception:  # noqa: BLE001 - a month the name misread is just not used
            continue
        name = _account({"marketplace": f.get("marketplace"), "owner": owner_of(f.get("path") or "")})
        out.setdefault(name, []).append((f["month"], label))
    return {name: [label for _, label in sorted(set(months))] for name, months in out.items()}


def _flag_silent(sec, rows, cur_label, gmv_col, what, key, empty=None) -> str:
    """Name the silent accounts on the page; returns the headline clause ('' when none).
    An account whose review-month export was attached with no rows stopped (nothing ran);
    one with no export at all is missing data."""
    silent = _silent_accounts(rows, cur_label, gmv_col)
    unit = "sessions" if what == "LIVE" else "videos"
    stopped, gone = [], []
    for name, month, gmv in silent:
        months = (empty or {}).get(name) or []
        if cur_label in months:
            stopped.append(name)
            sec["facts"].append(fact(
                f"{name} {what} made {idr(gmv)} GMV in {month}, and the exports for "
                f"{' and '.join(months)} hold no {unit}: {what} on this account stopped after "
                f"{month}, so the drop is real and needs its reason (a change of plan, team or "
                "account), not more data.", key, "INTERPRETATION"))
            continue
        gone.append(name)
        sec["facts"].append(fact(
            f"{name} {what} has data up to {month} ({idr(gmv)} GMV) and none for {cur_label}; "
            f"the {cur_label} total leaves it out, so a drop against earlier months is missing "
            "data, not a fall.", key, "INTERPRETATION"))
        sec["missing"].append(missing(
            f"{name} {what} for {cur_label}", f"without it the {what} total reads as a collapse",
            f"the {name} {what} data for {cur_label}; none was loaded or attached",
            fallback="the accounts that have data", updates=f"the full {what} total",
            detail=f"{key}: no rows for {name} in {cur_label}", key="live"))
    clauses = []
    if stopped:
        clauses.append(f"{' and '.join(stopped)} {what} had no {unit} in {cur_label}")
    if gone:
        clauses.append(f"{' and '.join(gone)} {what} has no data for {cur_label}")
    return "".join(f"; {c}" for c in clauses)


def sec_live_video(run):
    sec = section("live_video", "C", "LIVE and video")
    b = lit(run.brand)
    start, end = run.month.shift(-2), run.month
    lsplit, vsplit = _accounts(run, "acme_store_live_mp"), _accounts(run, "acme_store_video_mp")
    lby, vby = "".join(f", {c}" for c in lsplit), "".join(f", {c}" for c in vsplit)
    live = run.query(
        "live_monthly", "acme_store_live_mp", ("live_date", "gmv", "orders", "brand", *lsplit),
        lambda dl: (
            f"SELECT {month_key(dl, 'live_date')} AS month_id{lby}, COUNT(*) AS sessions, "
            f"SUM(gmv) AS gmv, SUM(orders) AS orders FROM acme_store_live_mp "
            f"WHERE UPPER(brand) = {b} AND live_date BETWEEN {d(start.first)} AND {d(end.last)} "
            f"GROUP BY {month_key(dl, 'live_date')}{lby} ORDER BY 1{lby}"))
    video = run.query(
        "video_monthly", "acme_store_video_mp",
        ("video_date", "gmv", "views", "id_video", "brand", *vsplit),
        lambda dl: (
            f"SELECT {month_key(dl, 'video_date')} AS month_id{vby}, "
            f"COUNT(DISTINCT id_video) AS videos, "
            f"SUM(gmv) AS gmv, SUM(views) AS views FROM acme_store_video_mp "
            f"WHERE UPPER(brand) = {b} AND video_date BETWEEN {d(start.first)} AND {d(end.last)} "
            f"GROUP BY {month_key(dl, 'video_date')}{vby} ORDER BY 1{vby}"))
    if use(sec, live):
        _live_part(run, sec, live, lsplit)
    if use(sec, video):
        _video_part(run, sec, video, vsplit)
    return degrade(sec)


def _live_part(run, sec, live, split) -> None:
    """The LIVE table, its headline and the accounts that went silent."""
    cur_label = run.month.label
    no_orders = not any(num(r.get("orders")) for r in live["rows"])
    rows = _monthly_by_account(live, split, ("sessions", "gmv", "orders"))
    k = 2 if split else 1                              # where the measures start
    if no_orders:
        for r in rows:
            r[k + 2] = None
    sec["tables"].append(table(
        "live", "LIVE sessions, GMV and orders by month" + (" and account" if split else ""),
        ["Month"] + (["Account"] if split else []) + ["Sessions", "GMV", "Orders"],
        rows, live["key"]))
    if no_orders and rows:
        sec["missing"].append(_empty_feed("LIVE orders", "acme_store_live_mp", run,
                                          "orders per session show whether LIVE converts",
                                          live["key"], field="order count"))
    cur = [r for r in rows if r[0] == cur_label]
    if cur:
        sec["headline"] = (f"{count(sum(r[k] for r in cur))} LIVE sessions in {cur_label} "
                           f"brought {idr(sum(r[k + 1] for r in cur))} GMV")
        sec["facts"].append(fact(sec["headline"] + ".", live["key"]))
        if split:
            sec["headline"] += _flag_silent(sec, rows, cur_label, k + 1, "LIVE", live["key"],
                                            _empty_exports(run, "live"))
    from_files = getattr(run, "from_files", lambda _t: False)("acme_store_live_mp")
    if from_files and split and any(r[1].startswith("Shopee") for r in rows):
        sec["facts"].append(fact(
            "Shopee's LIVE export has one row per day, so its sessions are days with LIVE.",
            live["key"], "INTERPRETATION"))


def _video_part(run, sec, video, split) -> None:
    """The video table and its line, added to the LIVE headline when there is one."""
    cur_label, prev_label = run.month.label, run.month.shift(-1).label
    rows = _monthly_by_account(video, split, ("videos", "gmv", "views"))
    k = 2 if split else 1
    sec["tables"].append(table(
        "video", "Videos, GMV and views by month" + (" and account" if split else ""),
        ["Month"] + (["Account"] if split else []) + ["Videos", "GMV", "Views"],
        rows, video["key"]))

    def total(label, j):
        return sum(r[j] for r in rows if r[0] == label)

    if any(r[0] == cur_label for r in rows):
        line = (f"Video GMV {change_text(total(cur_label, k + 1), total(prev_label, k + 1))} "
                f"from {count(total(cur_label, k))} videos")
        sec["facts"].append(fact(line + ".", video["key"]))
        if split:
            line += _flag_silent(sec, rows, cur_label, k + 1, "video", video["key"],
                                 _empty_exports(run, "video"))
        sec["headline"] = f"{sec['headline']}; {line[:1].lower()}{line[1:]}" \
            if sec["headline"] else line


# The ads table's split column has been renamed in the warehouse before (source -> store);
# the first one the scan knows is used, none = totals only.
_ADS_SPLIT = (("source", "Source"), ("ads_source", "Source"), ("channel", "Channel"),
              ("store", "Store"))


_ADS = "acme_store_ads_performance"
_ADS_LEVER = "ad spend and its return are the main paid lever"


def _ads_sql(dl, run, split, clicks):
    """Monthly cost and ads GMV over three months, split by `split` when there is one."""
    day = day_of(dl, "date")                     # staging holds it as text
    pick = f"{split} AS source" if split else "'All ads' AS source"
    by = f", {split}" if split else ""
    # the money columns are text on staging too (Oct 2026): read as numbers, and count how
    # many filled values were not, so unreadable text is not reported as zero
    cost = num_of(dl, "ads_cost_idr")
    clicks_sql = f", SUM({num_of(dl, 'clicks')}) AS clicks" if clicks else ""
    return (f"SELECT {month_key(dl, day)} AS month_id, {pick}, SUM({cost}) AS cost, "
            f"SUM({num_of(dl, 'ads_gmv_idr')}) AS gmv{clicks_sql}, "
            f"COUNT(ads_cost_idr) AS cost_filled, COUNT({cost}) AS cost_read "
            f"FROM {_ADS} WHERE UPPER(brand) = {lit(run.brand)} "
            f"AND {day} BETWEEN {d(run.month.shift(-2).first)} AND {d(run.month.last)} "
            f"GROUP BY {month_key(dl, day)}{by} ORDER BY 1")


def _ads_gap(run, rec):
    """The open item when the ads rows hold no spend to show, else None."""
    filled = sum(num(r.get("cost_filled")) for r in rec["rows"])
    if filled and not sum(num(r.get("cost_read")) for r in rec["rows"]):
        return missing(
            f"ads cost and ads GMV for {run.brand} as numbers", _ADS_LEVER,
            "the ads feed holds its cost as text that is not a plain number; the data team "
            "stores it as a number", fallback=_NOT_SHOWN, updates=_ON_PAGE,
            detail=f"{_ADS}.ads_cost_idr: no value reads as a number")
    if not rec["rows"]:
        # no row at all is not "only zeros": the brand is absent from the feed
        return missing(
            f"ads cost and ads GMV for {run.brand}", _ADS_LEVER,
            f"the ads feed has no rows for {run.brand} in these months; the data team loads "
            "the brand's ads, or confirms it ran none", fallback=_NOT_SHOWN, updates=_ON_PAGE,
            detail=f"{_ADS} has no rows for brand {run.brand}")
    if not any(num(r.get("cost")) or num(r.get("gmv")) for r in rec["rows"]):
        return _empty_feed("ads cost and ads GMV", _ADS, run, _ADS_LEVER, rec["key"])
    return None


def _ads_headline(run, sec, rows, key):
    cur = [r for r in rows if r[0] == run.month.label]
    cost, gmv = sum(r[2] for r in cur), sum(r[3] for r in cur)
    if not cost:
        return
    nmv = num(_by_month(orders_monthly(run)).get(run.month.mid, {}).get("nmv"))
    line = f"Ads spend in {run.month.label} was {idr(cost)} with a return of {gmv / cost:.1f} times"
    if nmv:
        line += f"; spend equals {cost / nmv * 100:.1f}% of NMV"
    sec["headline"] = line
    sec["facts"].append(fact(line + ".", key))


def sec_ads(run):
    sec = section("ads", "C", "Ads")
    split, header = next(((c, h) for c, h in _ADS_SPLIT if _has(run, _ADS, c)), (None, "Source"))
    # clicks left the table on staging (Oct 2026 rescan); cost and return still answer
    clicks = _has(run, _ADS, "clicks")
    columns = ("date", "ads_cost_idr", "ads_gmv_idr", "brand")
    rec = run.query("ads_monthly", _ADS, columns, lambda dl: _ads_sql(dl, run, split, clicks))
    renamed = bool(split and not rec["ok"] and re.search(
        rf"Unrecognized name: {re.escape(split)}\b", str(rec.get("error") or ""), re.I))
    if renamed:
        # The warehouse dropped the split column after Poros last scanned the table (staging,
        # Oct 2026: source -> store). Totals still answer; the rescan brings the split back.
        split, header = None, "Source"
        rec = run.query("ads_monthly_total", _ADS, columns,
                        lambda dl: _ads_sql(dl, run, None, clicks))
    gmv_max = missing(
        "GMV Max results per product and upper funnel campaigns",
        "shows which products the automated campaigns push and at what return",
        "the GMV Max product report and the Ads Manager upper funnel export",
        updates="return per product and the upper funnel cap")
    if not use(sec, rec):
        return degrade(sec)
    gap = _ads_gap(run, rec)
    if gap:
        sec["missing"] += [gap, dict(gmv_max, fallback="none")]
        return degrade(sec)
    extra = ["Clicks"] if clicks else []
    rows = [[L.month_label(int(num(r.get("month_id")))), r.get("source") or _NOT_RECORDED,
             num(r.get("cost")), num(r.get("gmv")),
             (num(r.get("gmv")) / num(r.get("cost"))) if num(r.get("cost")) else None]
            + [num(r.get("clicks")) for _ in extra] for r in rec["rows"]]
    sec["tables"].append(table("ads", "Ads cost, ads GMV and return on ad spend by month",
                               ["Month", header, "Cost", "Ads GMV", "ROAS"] + extra,
                               rows, rec["key"], formulas=[rat(4, 3, 2)]))
    _ads_headline(run, sec, rows, rec["key"])
    if renamed:
        sec["missing"].append(missing(
            "ads split by source", "which source returns the spend is the budget question",
            "a rescan of the ads table in Poros: the warehouse renamed its source column",
            fallback="ads totals", updates="cost and return per source",
            detail="acme_store_ads_performance: the scanned split column is gone from the "
                   "warehouse; rescan the datasource"))
    sec["missing"].append(dict(gmv_max, fallback="ads totals by source"))
    return degrade(sec)


_FEE_KEYS = ("wallet_gmv", "revenue", "mp_fee", "affiliate_fee", "commission_fee", "os_fee",
             "dynamic_fee", "order_processing_fee", "promotion_fee", "platform_subsidy",
             "seller_voucher", "settlement")


def sec_economics(run):
    sec = section("mp_economics", "C", "Marketplace economics")
    b = lit(run.brand)
    start, end = run.month.shift(-5), run.month
    rec = run.query(
        "wallet_monthly", "acme_store_wallet_mp",
        ("month_id", "wallet_gmv", "revenue_wallet", "mp_fee", "affiliate_fee", "commission_fee",
         "os_fee", "dynamic_fee", "order_processing_fee", "promotion_fee", "platform_subsidi",
         "seller_voucher", "total_settlement_amount", "brand"),
        lambda dl: (
            f"SELECT month_id, SUM(wallet_gmv) AS wallet_gmv, SUM(revenue_wallet) AS revenue, "
            f"SUM(mp_fee) AS mp_fee, SUM(affiliate_fee) AS affiliate_fee, "
            f"SUM(commission_fee) AS commission_fee, SUM(os_fee) AS os_fee, "
            f"SUM(dynamic_fee) AS dynamic_fee, SUM(order_processing_fee) AS order_processing_fee, "
            f"SUM(promotion_fee) AS promotion_fee, SUM(platform_subsidi) AS platform_subsidy, "
            f"SUM(seller_voucher) AS seller_voucher, SUM(total_settlement_amount) AS settlement "
            f"FROM acme_store_wallet_mp WHERE UPPER(brand) = {b} "
            f"AND month_id BETWEEN {start.mid} AND {end.mid} GROUP BY month_id ORDER BY month_id"))
    if not use(sec, rec):
        return degrade(sec)
    months = sorted(rec["rows"], key=lambda r: num(r.get("month_id")))
    share_rows = []
    for r in months:
        g = num(r.get("wallet_gmv"))
        share_rows.append([L.month_label(int(num(r.get("month_id")))), g,
                           pct(abs(num(r.get("mp_fee"))), g), pct(abs(num(r.get("affiliate_fee"))), g),
                           pct(num(r.get("settlement")), g)])
    sec["tables"].append(table(
        "fee_share", "Fees as a share of settled GMV, by settlement month",
        ["Month", "Settled GMV", "Marketplace fees %", "Affiliate commission %", "Paid out %"],
        share_rows, rec["key"], chart={"type": "column", "x": 0, "y": [2, 3]}))
    sec["tables"].append(table(
        "fee_stack", "Settlement and fee lines by settlement month",
        ["Line"] + [r[0] for r in share_rows],
        [[L.FEE_LABELS[k]] + [num(r.get(k)) for r in months] for k in _FEE_KEYS], rec["key"]))
    cur = next((r for r in months if int(num(r.get("month_id"))) == run.month.mid), None)
    prev = next((r for r in months if int(num(r.get("month_id"))) == run.month.shift(-1).mid), None)
    fees = lambda r: abs(num(r.get("mp_fee"))) + abs(num(r.get("affiliate_fee")))
    if cur and num(cur.get("wallet_gmv")):
        x = pct(fees(cur), cur.get("wallet_gmv"))
        sec["headline"] = (f"Marketplace fees and affiliate commission took {x:.1f}% of "
                           f"settled GMV in {run.month.label}")
        if prev and num(prev.get("wallet_gmv")):
            sec["headline"] += (f", against {pct(fees(prev), prev.get('wallet_gmv')):.1f}% in "
                                f"{run.month.shift(-1).label}")
        sec["facts"].append(fact(sec["headline"] + " (by settlement month, not order month).",
                                 rec["key"]))
        if abs(num(cur.get("seller_voucher"))) > num(cur.get("wallet_gmv")):
            sec["missing"].append(missing(
                "a believable seller voucher figure",
                f"the stored seller voucher ({idr(cur.get('seller_voucher'))}) is larger than "
                f"settled GMV ({idr(cur.get('wallet_gmv'))}), so it cannot be the voucher cost "
                "of these orders",
                "the data team checks the seller voucher in the settlement data (sign, unit or "
                "duplicated rows)", fallback="shown as stored, not used in any figure",
                detail="acme_store_wallet_mp.seller_voucher", key="voucher"))
    sec["missing"].append(missing(
        "Mall service, PayLater, bonus cashback and GMV Max fees as separate lines",
        "the fee stack is incomplete without them", "the settlement export with those fee lines",
        fallback="fees as the settlement data provides them", updates="the fee stack gains rows"))
    return degrade(sec)


def sec_margin(run):
    sec = section("margin", "C", "Margin after cost of goods")
    b, cur, prev = lit(run.brand), run.month, run.month.shift(-1)
    rec = run.query(
        "pnl_monthly", "acme_finance_pnl_mp",
        ("order_date", "nmv", "net_revenue", "mp_fee", "affiliate_fee", "cogm", "brand"),
        lambda dl: (
            f"SELECT {month_key(dl, 'order_date')} AS month_id, SUM(nmv) AS nmv, "
            f"SUM(net_revenue) AS net_revenue, SUM(mp_fee) AS mp_fee, "
            f"SUM(affiliate_fee) AS affiliate_fee, SUM(cogm) AS cogm "
            f"FROM acme_finance_pnl_mp WHERE UPPER(brand) = {b} "
            f"AND order_date BETWEEN {d(prev.first)} AND {d(cur.last)} "
            f"GROUP BY {month_key(dl, 'order_date')} ORDER BY 1"))
    if not use(sec, rec):
        return degrade(sec)
    if not any(num(r.get("cogm")) for r in rec["rows"]):
        _margin_without_cogs(run, sec, rec)
        return degrade(sec)
    ours = _by_month(orders_monthly(run))
    rows = []
    for r in rec["rows"]:
        mid = int(num(r.get("month_id")))
        margin = num(r.get("net_revenue")) - num(r.get("cogm"))
        mine = num(ours.get(mid, {}).get("nmv")) or None
        rows.append([L.month_label(mid), num(r.get("nmv")),
                     num(r.get("net_revenue")), num(r.get("cogm")), margin,
                     pct(margin, r.get("net_revenue")), mine,
                     change(r.get("nmv"), mine) if mine else None])
    sec["tables"].append(table("pnl", "Net revenue and margin after cost of goods",
                               ["Month", "NMV, finance data", "Net revenue", "Cost of goods",
                                "Margin", "Margin %", "NMV, this review",
                                "Finance vs this review %"],
                               rows, f"{rec['key']}, orders_monthly",
                               formulas=[{"col": 4, "kind": "diff", "a": 2, "b": 3},
                                         rat(5, 4, 2, 100), chg(7, 1, 6)],
                               deck_columns=[0, 1, 2, 4, 5, 6, 7]))
    c = next((r for r in rows if r[0] == cur.label), None)
    p = next((r for r in rows if r[0] == prev.label), None)
    if c and c[5] is not None:
        _margin_headline(sec, c, p, cur, prev, ours, rec["key"])
    return degrade(sec)


def _margin_without_cogs(run, sec, rec) -> None:
    # Two different "NMV" figures for one month side by side read as an error unless
    # the page says which is which and how far apart they are.
    cur = run.month
    ours = _by_month(orders_monthly(run))
    rows = []
    for r in rec["rows"]:
        mid = int(num(r.get("month_id")))
        mine = num(ours.get(mid, {}).get("nmv")) or None
        rows.append([L.month_label(mid), num(r.get("nmv")), num(r.get("net_revenue")), mine,
                     change(r.get("nmv"), mine) if mine else None])
    sec["tables"].append(table(
        "pnl", "Finance data by month (cost of goods not loaded)",
        ["Month", "NMV, finance data", "Net revenue, finance data", "NMV, this review",
         "Finance vs this review %"], rows, f"{rec['key']}, orders_monthly",
        formulas=[chg(4, 1, 3)]))
    c = next((r for r in rows if r[0] == cur.label), None)
    sec["headline"] = f"No margin yet: cost of goods is not loaded for {run.brand}"
    if c and c[4] is not None:
        sec["facts"].append(fact(
            f"The finance data books {idr(c[1])} NMV for {cur.label}, {than(c[4])} than the "
            f"{idr(c[3])} in this review; it follows its own booking rules (statuses and "
            "dates), so its NMV is not this review's.", rec["key"], "INTERPRETATION"))
    sec["missing"].append(_empty_feed(
        "cost of goods", "acme_finance_pnl_mp", run,
        "without cost of goods the margin would read 100%, which is not a margin", rec["key"]))


def _margin_headline(sec, c, p, cur, prev, ours, key) -> None:
    partial = c[7] is not None and c[7] <= -_BOOKING_GAP
    sec["headline"] = (f"Margin after cost of goods was {c[5]:.1f}% of net revenue in "
                       f"{cur.label}")
    if partial:
        sec["headline"] += ", a month the finance data has only partly booked"
    sec["facts"].append(fact(sec["headline"] + f" ({idr(c[4])}).", key))
    if partial:
        sec["facts"].append(fact(_partly_booked(c, p, cur, prev, ours), key, "INTERPRETATION"))


# The finance data books a month as its orders settle. When its NMV for the review month
# trails this review's by more than this, the month is not fully booked yet (ACMESWEET
# Sep 2026: IDR 1.00 bn against 1.22 bn while Aug matched within 0.2%), and its lower net
# revenue and margin AMOUNTS are timing, not a decline; the margin rate still compares.
_BOOKING_GAP = 5.0


def _partly_booked(c, p, cur, prev, ours) -> str:
    text = (f"The finance data books {idr(c[1])} NMV for {cur.label}, {than(c[7])} than the "
            f"{idr(c[6])} in this review")
    if p and p[7] is not None and abs(p[7]) < _BOOKING_GAP:
        text += f", while {prev.label} matches within {abs(p[7]):.1f}%"
    text += f": {cur.label} is not fully booked in the finance data yet"
    flying = num(ours.get(cur.mid, {}).get("nmv_in_flight"))
    if flying:
        text += f" ({idr(flying)} of its NMV is still in a shipping status)"
    return (text + ". Its net revenue and margin amounts are part of a month, so the margin "
            "rate compares and the amounts do not.")


# ---------------------------------------------------------------- D. what is working
_BUCKETS = (_SAME_DAY, "Next day", "2 days", "3 days or more")


_RTS_COLUMNS = ("order_date", "paid_date", "rts_date", "payment_method", "order_status",
                "order_id", "month_id")


def _rts_query(run, key, months):
    """Orders per whole day from start to ready to ship, per month."""
    p, b = processed_list(run), lit(run.brand)
    cod = lit_list(_COD_VALUES)
    start = (f"CASE WHEN LOWER(payment_method) IN ({cod}) OR paid_date IS NULL "
             f"THEN order_date ELSE paid_date END")
    ids = ", ".join(str(m.mid) for m in months)
    return run.query(
        key, L2_ORDERS, _RTS_COLUMNS,
        lambda dl: (
            f"SELECT month_id, {days_between(dl, start, 'rts_date')} AS days_to_rts, "
            f"COUNT(DISTINCT order_id) AS orders FROM {L2_ORDERS} "
            f"WHERE UPPER(brand) = {b} AND order_status IN ({p}) "
            f"AND month_id IN ({ids}) AND rts_date IS NOT NULL "
            f"GROUP BY month_id, {days_between(dl, start, 'rts_date')} ORDER BY 1, 2"))


def _day_stats(hist: dict) -> dict | None:
    """Average, median, fastest and slowest from {whole days: orders}."""
    total = sum(hist.values())
    if not total:
        return None
    acc, med = 0.0, 0
    for k in sorted(hist):
        acc += hist[k]
        if acc >= total / 2:
            med = k
            break
    seen = [k for k, v in hist.items() if v]
    return {"orders": total, "average": sum(k * v for k, v in hist.items()) / total,
            "median": med, "fastest": min(seen), "slowest": max(seen)}


def _days(k) -> str:
    k = int(k)
    if k == 0:
        return "the same day"
    return "1 day" if k == 1 else f"{k} days"


def sec_fulfilment(run):
    """Time to ready to ship, the reference package's clock: from order creation for cash
    on delivery (payment comes at delivery), from payment for everything else. The
    standard: the average leads, with the median, fastest and slowest, before vs after."""
    sec = section("fulfilment", "D", "Fulfilment speed")
    cur, prev = run.month, run.month.shift(-1)
    rec = _rts_query(run, "fulfilment_days", (cur, prev))
    if not use(sec, rec):
        return degrade(sec)
    hist, odd = _rts_hist(rec["rows"], cur, prev)
    counts = _bucket_counts(hist)
    tc, tp = sum(counts[cur.mid].values()), sum(counts[prev.mid].values())
    rows = [[k, counts[cur.mid][k], pct(counts[cur.mid][k], tc), counts[prev.mid][k],
             pct(counts[prev.mid][k], tp)] for k in _BUCKETS]
    sec["tables"].append(table(
        "rts", "Orders by time to ready to ship",
        ["Time to ready to ship", f"Orders {cur.label}", f"Share % {cur.label}",
         f"Orders {prev.label}", f"Share % {prev.label}"], rows, rec["key"],
        formulas=[shr(2, 1), shr(4, 3)], chart={"type": "column", "x": 0, "y": [2]}))
    if not tc:
        return degrade(sec)
    sc, sp = _day_stats(hist[cur.mid]), _day_stats(hist[prev.mid])

    # before vs after: the two months before the takeover month, when the date is set
    base, base_label = None, ""
    takeover = run.config.brand.takeover_date
    if takeover:
        base, base_label = _takeover_base(run, sec, takeover, (cur, prev))
    sec["tables"].append(_spread_table(rec["key"], (sc, sp, base), base_label, (cur, prev)))

    same = pct(counts[cur.mid][_SAME_DAY], tc)
    within = pct(counts[cur.mid][_SAME_DAY] + counts[cur.mid]["Next day"], tc) or 0
    sec["headline"] = (_fulfilment_average(sc, sp, (cur, prev))
                       + f"; {share_text(same)} the same day")
    sec["facts"].append(fact(sec["headline"] + ".", rec["key"]))
    spread = _fulfilment_spread(sc, sp, (cur, prev))
    sec["facts"].append(fact(spread + ".", rec["key"]))
    if base:
        sec["facts"].append(fact(
            f"Before the takeover ({base_label}) orders took {base['average']:.2f} days on "
            f"average and the slowest {_days(base['slowest'])}; in {cur.label} "
            f"{sc['average']:.2f} days and {_days(sc['slowest'])}.", "fulfilment_days_before"))
        sec["tables"][-1]["takeaway"] = sec["facts"][-1]["text"]
    else:
        sec["tables"][-1]["takeaway"] = spread + "."
    sec["facts"].insert(1, _within_a_day(within, rec["key"]))
    sec["facts"].append(fact(
        "The clock starts at order creation for cash on delivery and at payment for other "
        "methods, and stops when the order is ready to ship; it counts whole days, as the "
        "order dates are recorded.", rec["key"], "INTERPRETATION"))
    if not takeover:
        sec["facts"].append(fact(
            f"Before and after the takeover needs the Acme takeover date in the brand's MBR "
            f"settings; until then the comparison is with {prev.label}.", "settings",
            "INTERPRETATION"))
    if odd:
        sec["facts"].append(fact(
            f"{count(odd)} orders show a ready to ship time before their start time and are "
            "left out.", rec["key"], "INTERPRETATION"))
    return degrade(sec)


def _rts_hist(rows, cur, prev) -> tuple[dict, float]:
    """({month id: {whole days: orders}} for the two months, review month orders whose
    ready to ship time comes before their start, which are left out)."""
    hist = {cur.mid: {}, prev.mid: {}}
    odd = 0.0
    for r in rows:
        mid, k = int(num(r.get("month_id"))), int(num(r.get("days_to_rts")))
        if mid not in hist:
            continue
        if k < 0:
            odd += num(r.get("orders")) if mid == cur.mid else 0
            continue
        hist[mid][k] = hist[mid].get(k, 0.0) + num(r.get("orders"))
    return hist, odd


def _bucket_counts(hist) -> dict:
    counts = {mid: dict.fromkeys(_BUCKETS, 0.0) for mid in hist}
    for mid, h in hist.items():
        for k, v in h.items():
            counts[mid][_BUCKETS[min(k, 3)]] += v
    return counts


def _takeover_base(run, sec, takeover, months) -> tuple:
    """(day stats of the two months before the takeover month, their label); no stats
    when the takeover comes after the review month or the orders cannot be read."""
    cur, prev = months
    tk = Month(takeover.year, takeover.month)
    if tk.mid > cur.mid:
        return None, ""
    before = (tk.shift(-2), tk.shift(-1))
    base_label = f"{before[0].label} to {before[1].label}"
    brec = _rts_query(run, "fulfilment_days_before", before)
    sec["queries"].append(brec["key"])
    if not brec["ok"]:
        sec["missing"].append(missing(
            "order handling times before the takeover",
            "the standard compares fulfilment before and after Acme",
            "the order data of the two months before the takeover",
            fallback=f"the comparison is with {prev.label}",
            detail=f"{brec.get('code')}: {brec.get('error')}"))
        return None, base_label
    bh = {}
    for r in brec["rows"]:
        k = int(num(r.get("days_to_rts")))
        if k >= 0:
            bh[k] = bh.get(k, 0.0) + num(r.get("orders"))
    return _day_stats(bh), base_label


def _spread_table(key, stats, base_label, months) -> dict:
    """Average, median, fastest and slowest per month; ``stats`` = (review month, month
    before, before the takeover or None)."""
    (sc, sp, base), (cur, prev) = stats, months
    measures = (("Average days", "average"), ("Median days", "median"),
                ("Fastest (days)", "fastest"), ("Slowest (days)", "slowest"))
    cols = ["Measure", cur.label, prev.label] + ([f"Before Acme, {base_label}"] if base else [])
    stats_rows = [[label] + [(st or {}).get(k) for st in (sc, sp) + ((base,) if base else ())]
                  for label, k in measures]
    return table(
        "rts_spread", "Time to ready to ship: average, median, fastest and slowest", cols,
        stats_rows, key + (",fulfilment_days_before" if base else ""),
        chart={"type": "column", "x": 0, "y": list(range(1, len(cols)))})


def _fulfilment_average(sc, sp, months) -> str:
    cur, prev = months
    head = f"{cur.label} orders were ready to ship in {sc['average']:.2f} days on average"
    if sp:
        head += f", against {sp['average']:.2f} in {prev.label}"
    return head


def _fulfilment_spread(sc, sp, months) -> str:
    cur, prev = months
    spread = (f"In {cur.label} the median order was ready {_days(sc['median'])}, the fastest "
              f"{_days(sc['fastest'])} and the slowest after {_days(sc['slowest'])}")
    if sp:
        spread += (f"; in {prev.label} the median was {_days(sp['median'])} and the slowest "
                   f"{_days(sp['slowest'])}")
    return spread


def _within_a_day(within, key) -> dict:
    if within >= 95:
        return fact(
            f"Fulfilment is not the constraint: {share_text(within)} of orders were ready "
            "within one day, so the growth levers sit elsewhere.", key, "INTERPRETATION")
    return fact(
        f"{share_text(100 - within)} of orders took two days or more to be ready; those are "
        "the orders to review with the warehouse.", key, "INTERPRETATION")


def sec_cancellation(run):
    sec = section("cancellation", "D", "Cancellation")
    c, b, cur, prev = cancelled_list(run), lit(run.brand), run.month, run.month.shift(-1)
    rec = run.query(
        "cancel_reasons", L2_ORDERS,
        ("cancel_reason_groupping", "cancel_by", "order_status", "order_id", "month_id"),
        lambda dl: (
            f"SELECT month_id, cancel_reason_groupping AS reason_group, cancel_by, "
            f"COUNT(DISTINCT order_id) AS orders FROM {L2_ORDERS} "
            f"WHERE UPPER(brand) = {b} AND month_id IN ({cur.mid}, {prev.mid}) "
            f"AND order_status IN ({c}) "
            f"GROUP BY month_id, cancel_reason_groupping, cancel_by ORDER BY orders DESC"))
    if not use(sec, rec):
        return degrade(sec)
    by = _by_month(orders_monthly(run))
    all_cur, all_prev = num(by.get(cur.mid, {}).get("all_orders")), num(by.get(prev.mid, {}).get("all_orders"))
    actors = {g: [0.0, 0.0] for g in L.ACTOR_GROUPS}
    reasons = {}
    for r in rec["rows"]:
        mid = int(num(r.get("month_id")))
        g = L.cancel_group(r.get("reason_group"), r.get("cancel_by"))
        slot = 0 if mid == cur.mid else 1
        actors[g][slot] += num(r.get("orders"))
        if mid == cur.mid:
            key = (L.reason_label(r.get("reason_group")), L.actor_label(r.get("cancel_by")))
            reasons[key] = reasons.get(key, 0) + num(r.get("orders"))
    rows = [[g, v[0], pct(v[0], all_cur), v[1], pct(v[1], all_prev)]
            for g, v in actors.items() if v[0] or v[1]]
    sec["tables"].append(table(
        "cancel_actor", "Cancellations by who caused them, as a share of all orders",
        ["Caused by", f"Orders {cur.label}", f"Rate % {cur.label}", f"Orders {prev.label}",
         f"Rate % {prev.label}"], rows, rec["key"]))
    total = sum(reasons.values())
    rrows = sorted(([k[0], k[1], v, pct(v, total) or 0] for k, v in reasons.items()),
                   key=lambda r: -r[2])
    sec["tables"].append(table("cancel", f"Cancelled orders by reason, {cur.label}",
                               ["Reason", "Cancelled by", "Orders", "Share of cancellations %"],
                               rrows, rec["key"], formulas=[shr(3, 2)]))
    m, pm = by.get(cur.mid, {}), by.get(prev.mid, {})
    rate = pct(m.get("cancelled_orders"), m.get("all_orders"))
    prev_rate = pct(pm.get("cancelled_orders"), pm.get("all_orders"))
    seller = actors["Seller or fulfilment"]
    unpaid = actors["Buyer did not pay"]
    if rate is not None:
        head = f"{rate:.1f}% of {cur.label} orders were cancelled"
        if prev_rate is not None:
            head += f" ({prev_rate:.1f}% in {prev.label})"
        head += (f"; the seller or fulfilment caused {share_text(pct(seller[0], all_cur), 2)} "
                 f"of orders and buyers who did not pay {share_text(pct(unpaid[0], all_cur))}")
        sec["headline"] = head
        sec["facts"].append(fact(head + ".", "orders_monthly, cancel_reasons"))
    if all_cur:
        sec["facts"].append(fact(
            f"Seller and fulfilment cancellations: {share_text(pct(seller[0], all_cur), 2)} of "
            f"{cur.label} orders ({count(seller[0])} orders) against "
            f"{share_text(pct(seller[1], all_prev), 2)} in {prev.label}.", rec["key"]))
        sec["facts"].append(fact(
            "A seller cancellation is one made by the seller or the fulfilment centre, or for a "
            "stock or fulfilment reason; an order closed because the buyer never paid is not a "
            "seller failure even when the platform closes it.", rec["key"], "INTERPRETATION"))
    if rrows:
        sec["facts"].append(fact(f"The largest cancellation reason was {rrows[0][0].lower()} "
                                 f"({share_text(rrows[0][3])} of cancellations).", rec["key"]))
    return degrade(sec)


def sec_payment(run):
    sec = section("payment", "D", "Payment mix and COD")
    p, c, b = processed_list(run), cancelled_list(run), lit(run.brand)
    cur, ly = run.month, run.month.shift(-12)
    rec = run.query(
        "payment_mix", L2_ORDERS, ("payment_method", "order_status", "order_id", "nmv", "month_id"),
        lambda dl: (
            f"SELECT month_id, payment_method, COUNT(DISTINCT order_id) AS all_orders, "
            f"COUNT(DISTINCT CASE WHEN order_status IN ({p}) THEN order_id END) AS orders, "
            f"COUNT(DISTINCT CASE WHEN order_status IN ({c}) THEN order_id END) AS cancelled, "
            f"SUM(CASE WHEN order_status IN ({p}) THEN nmv ELSE 0 END) AS nmv "
            f"FROM {L2_ORDERS} WHERE UPPER(brand) = {b} AND month_id IN ({cur.mid}, {ly.mid}) "
            f"GROUP BY month_id, payment_method ORDER BY nmv DESC"))
    if not use(sec, rec):
        return degrade(sec)
    agg = _payment_agg(rec["rows"], cur, ly)
    now, last_year = agg[cur.mid], agg[ly.mid]
    tc = sum(v[1] for v in now.values())
    tl = sum(v[1] for v in last_year.values())
    sec["tables"].append(table("payment", f"Payment methods, {cur.label}",
                               ["Payment method", "Orders", "NMV", "NMV share %", "Cancel rate %",
                                f"NMV share % {ly.label}"],
                               _payment_rows(now, last_year, tc, tl), rec["key"],
                               formulas=[shr(3, 2)]))
    head = _cod_headline(now, last_year, tc, tl)
    if head:
        sec["headline"] = head
        sec["facts"].append(fact(sec["headline"] + ".", rec["key"]))
    return degrade(sec)


def _payment_agg(rows, cur, ly) -> dict:
    """{month id: {payment method: [orders, NMV, cancelled, all orders]}}."""
    agg = {cur.mid: {}, ly.mid: {}}
    for r in rows:
        mid = int(num(r.get("month_id")))
        if mid not in agg:
            continue
        slot = agg[mid].setdefault(L.payment_label(r.get("payment_method")), [0.0, 0.0, 0.0, 0.0])
        for i, k in enumerate(("orders", "nmv", "cancelled", "all_orders")):
            slot[i] += num(r.get(k))
    return agg


def _payment_rows(now, last_year, tc, tl) -> list:
    """The eight largest methods by NMV and one row for the rest."""
    ranked = sorted(now.items(), key=lambda kv: -kv[1][1])
    head, tail = ranked[:8], ranked[8:]
    ly_nmv = {k: v[1] for k, v in last_year.items()}
    rows = [[k, v[0], v[1], pct(v[1], tc) or 0, pct(v[2], v[3]) or 0, pct(ly_nmv.get(k, 0), tl)]
            for k, v in head]
    if tail:
        other = [sum(v[i] for _, v in tail) for i in range(4)]
        shown = {k for k, _ in head}
        # last year's "other" is everything outside this year's top methods, including
        # methods that no longer appear at all
        other_ly = sum(n for k, n in ly_nmv.items() if k not in shown)
        rows.append([f"Other methods ({len(tail)})", other[0], other[1], pct(other[1], tc) or 0,
                     pct(other[2], other[3]) or 0, pct(other_ly, tl)])
    return rows


def _cod_headline(now, last_year, tc, tl) -> str | None:
    """Cash on delivery's share of NMV against a year ago, and its cancellation rate."""
    cod_now = [v for k, v in now.items() if L.is_cod(k)]
    if not (cod_now and tc):
        return None
    share = pct(sum(v[1] for v in cod_now), tc)
    canc = pct(sum(v[2] for v in cod_now), sum(v[3] for v in cod_now)) or 0
    cod_ly = [v for k, v in last_year.items() if L.is_cod(k)]
    head_txt = f"Cash on delivery carried {share_text(share)} of NMV"
    if tl:
        ly_share = pct(sum(v[1] for v in cod_ly), tl) or 0
        head_txt += (f" against {share_text(ly_share)} a year ago" if ly_share
                     else ", up from none a year ago")
    return head_txt + f", and {share_text(canc)} of COD orders were cancelled"


# a delivered date per order, under whichever name the order table carries it
_DELIVERED = ("delivered_date", "delivery_date", "received_date", "completed_date",
              "complete_date", "finish_date")


def _delivery_days(run, sec, col):
    """Average days from payment (order creation for cash on delivery) to delivered, per
    province, review month; one value per order so a many line order counts once."""
    p, b, cur = processed_list(run), lit(run.brand), run.month
    cod = lit_list(_COD_VALUES)
    start = (f"CASE WHEN LOWER(payment_method) IN ({cod}) OR paid_date IS NULL "
             f"THEN order_date ELSE paid_date END")
    rec = run.query(
        "geography_delivery", L2_ORDERS,
        ("provinsi_match_clean", "order_date", "paid_date", "payment_method", "order_status",
         "order_id", "month_id", col),
        lambda dl: (
            f"SELECT province, AVG(days) AS days, COUNT(*) AS orders FROM ("
            f"SELECT provinsi_match_clean AS province, order_id, "
            f"MAX({days_between(dl, start, col)}) AS days FROM {L2_ORDERS} "
            f"WHERE UPPER(brand) = {b} AND month_id = {cur.mid} AND order_status IN ({p}) "
            f"AND {col} IS NOT NULL GROUP BY provinsi_match_clean, order_id) AS o "
            f"WHERE days >= 0 GROUP BY province"))
    if not rec["ok"]:
        sec["queries"].append(rec["key"])
        sec["missing"].append(missing(
            "delivery time by area", "the standard pairs each area with its delivery time",
            "the delivered date per order, readable for this brand",
            visual="NMV share and paid to delivered days by province",
            fallback="NMV and its share by area", updates="a delivery time column joins the "
            "province table", key="delivery", detail=f"{rec.get('code')}: {rec.get('error')}"))
        return {}
    sec["queries"].append(rec["key"])
    out = {}
    for r in rec["rows"]:
        name = L.province_label(r.get("province"))
        o = num(r.get("orders"))
        acc = out.setdefault(name, [0.0, 0.0, L.island(r.get("province"))])
        acc[0] += num(r.get("days")) * o
        acc[1] += o
    return out


def sec_geography(run):
    sec = section("geography", "D", "Geography")
    p, b, cur, prev = processed_list(run), lit(run.brand), run.month, run.month.shift(-1)
    rec = run.query(
        "geography", L2_ORDERS, ("provinsi_match_clean", "order_status", "nmv", "order_id", "month_id"),
        lambda dl: (
            f"SELECT month_id, provinsi_match_clean AS province, "
            f"SUM(CASE WHEN order_status IN ({p}) THEN nmv ELSE 0 END) AS nmv, "
            f"COUNT(DISTINCT CASE WHEN order_status IN ({p}) THEN order_id END) AS orders "
            f"FROM {L2_ORDERS} WHERE UPPER(brand) = {b} AND month_id IN ({cur.mid}, {prev.mid}) "
            f"GROUP BY month_id, provinsi_match_clean"))
    if not use(sec, rec):
        return degrade(sec)
    prov, isl = _geo_totals(rec["rows"], cur)
    tc = sum(v[0] for v in isl.values())
    irows = sorted(([k, v[0], pct(v[0], tc) or 0, v[1], change(v[0], v[1])] for k, v in isl.items()),
                   key=lambda r: -r[1])
    sec["tables"].append(table("island", "NMV by island group",
                               ["Island group", cur.label, _SHARE, prev.label, _MOM], irows,
                               rec["key"], formulas=[shr(2, 1), chg(4, 1, 3)],
                               chart={"type": "bar", "x": 0, "y": [1, 3]}))
    prows = _province_rows(prov, tc)
    col = next((c for c in _DELIVERED if _has(run, L2_ORDERS, c)), None)
    deliv = _delivery_days(run, sec, col) if col else {}
    pcols = ["Province", "NMV", _SHARE, "Orders", "Island group"]
    if deliv:
        pcols.append("Paid to delivered, days")
        _add_delivery_days(prows, deliv)
    sec["tables"].append(table("province", f"NMV by province, {cur.label}", pcols,
                               prows[:20], rec["key"] + (",geography_delivery" if deliv else "")))
    if not col:
        sec["missing"].append(missing(
            "delivery time by area",
            "the standard pairs each area with its delivery time, paid to delivered",
            "the delivered date per order in the order data",
            visual="NMV share and paid to delivered days by province",
            fallback="NMV and its share by area",
            updates="a delivery time column joins the province table", key="delivery",
            detail=f"no delivered date column ({', '.join(_DELIVERED)}) in {L2_ORDERS}"))
    head = _java_headline(isl, tc, (cur, prev))
    if head:
        sec["headline"] = head
        sec["facts"].append(fact(head + ".", rec["key"]))
    if len(prows) >= 3:
        top3 = sum(r[2] for r in prows[:3])
        sec["facts"].append(fact(
            f"The top three provinces ({', '.join(r[0] for r in prows[:3])}) made "
            f"{share_text(top3)} of NMV.", rec["key"]))
    if deliv:
        _delivery_contrast(sec, deliv)
    return degrade(sec)


def _geo_totals(rows, cur) -> tuple[dict, dict]:
    """({province: [NMV now, NMV month before, orders now]}, {island: [now, before]})."""
    prov, isl = {}, {}
    for r in rows:
        slot = 0 if int(num(r.get("month_id"))) == cur.mid else 1
        name = L.province_label(r.get("province"))
        prov.setdefault(name, [0.0, 0.0, 0.0])
        prov[name][slot] += num(r.get("nmv"))
        if slot == 0:
            prov[name][2] += num(r.get("orders"))
        group = L.island(r.get("province"))
        isl.setdefault(group, [0.0, 0.0])
        isl[group][slot] += num(r.get("nmv"))
    return prov, isl


def _province_rows(prov, tc) -> list:
    """Provinces with NMV this month, largest first."""
    return sorted(([k, v[0], pct(v[0], tc) or 0, v[2],
                    _NOT_RECORDED if k == _NOT_RECORDED else L.island(k)]
                   for k, v in prov.items() if v[0]), key=lambda r: -r[1])


def _add_delivery_days(prows, deliv) -> None:
    for r in prows:
        d_ = deliv.get(r[0])
        r.append(d_[0] / d_[1] if d_ and d_[1] else None)


def _java_headline(isl, tc, months) -> str | None:
    """Java's share of NMV, and how Java and the rest moved on the month before."""
    if not tc:
        return None
    cur, prev = months
    java = isl.get("Java", [0.0, 0.0])
    out = [sum(v[0] for k, v in isl.items() if k != "Java"),
           sum(v[1] for k, v in isl.items() if k != "Java")]
    head = f"Java made {pct(java[0], tc):.0f}% of {cur.label} NMV"
    if java[1] and out[1]:
        head += (f"; outside Java NMV was {updown(change(out[0], out[1]))} on {prev.label} "
                 f"against {updown(change(java[0], java[1]))} for Java")
    return head


def _avg_days(vs):
    return sum(v[0] for v in vs) / sum(v[1] for v in vs) if sum(v[1] for v in vs) else None


def _delivery_contrast(sec, deliv) -> None:
    """Paid to delivered days in Java against outside Java."""
    inside = [v for v in deliv.values() if v[2] == "Java"]
    outside = [v for v in deliv.values() if v[2] not in ("Java", _NOT_RECORDED)]
    di, do = _avg_days(inside), _avg_days(outside)
    if di is not None and do is not None:
        sec["facts"].append(fact(
            f"Orders in Java took {di:.1f} days from payment to delivery, against "
            f"{do:.1f} days outside Java.", "geography_delivery"))


def sec_customers(run):
    sec = section("customers", "D", "Customers and retention")
    p, b = processed_list(run), lit(run.brand)
    w = months_window(run)
    # the standard states where buyer history starts; a brand setting fixes it (ACS: Jan
    # 2026), otherwise it is the first month of the 13 month window
    since = run.config.brand.customer_since
    since = since if since and since.mid < run.month.mid else None
    start = since or w[0]
    span = (run.month.year - start.year) * 12 + run.month.month - start.month
    shown = [run.month.shift(-i) for i in range(min(span, 7) - 1, -1, -1)]
    first = run.query(
        "new_buyers", L2_ORDERS, ("customer_id", "month_id", "order_status", "brand"),
        lambda dl: (
            f"SELECT first_month AS month_id, COUNT(*) AS new_buyers FROM ("
            f"SELECT customer_id, MIN(month_id) AS first_month FROM {L2_ORDERS} "
            f"WHERE UPPER(brand) = {b} AND order_status IN ({p}) AND customer_id IS NOT NULL "
            f"AND month_id BETWEEN {start.mid} AND {w[-1].mid} GROUP BY customer_id) AS firsts "
            f"GROUP BY first_month ORDER BY first_month"))
    if use(sec, first) and first["rows"]:
        _new_buyers(run, sec, first, (start, shown), since)
    new = run.query(
        "new_customers", "acme_new_customer_monthly",
        ("brand", "order_month", "new_customer", "total_orders", "total_nmv"),
        lambda dl: (
            f"SELECT order_month, SUM(new_customer) AS new_customers, SUM(total_orders) AS orders, "
            f"SUM(total_nmv) AS nmv FROM acme_new_customer_monthly WHERE UPPER(brand) = {b} "
            f"AND order_month BETWEEN {d(w[0].first)} AND {d(w[-1].last)} "
            f"GROUP BY order_month ORDER BY order_month"))
    cohort = run.query(
        "cohort_retention", "acme_cohort_monthly",
        ("brand", "cohort_month", "cohort_index", "customer_count", "cohort_size"),
        lambda dl: (
            f"SELECT cohort_index, SUM(customer_count) AS customers, SUM(cohort_size) AS cohort_size "
            f"FROM acme_cohort_monthly WHERE UPPER(brand) = {b} "
            f"AND cohort_month BETWEEN {d(w[0].first)} AND {d(w[-1].last)} "
            f"AND cohort_index BETWEEN 0 AND 6 GROUP BY cohort_index ORDER BY cohort_index"))
    if use(sec, new):
        _new_customers(run, sec, new)
    if use(sec, cohort):
        rows = [[int(num(r.get("cohort_index"))), num(r.get("customers")), num(r.get("cohort_size")),
                 pct(r.get("customers"), r.get("cohort_size"))] for r in cohort["rows"]]
        sec["tables"].append(table("cohort", "Customers still buying N months after their first order",
                                   ["Months after first order", "Customers", "Cohort size",
                                    "Retention %"], rows, cohort["key"],
                                   formulas=[rat(3, 1, 2, 100)]))
    return degrade(sec)


def _new_buyers(run, sec, first, window, since) -> None:
    """New and returning buyers by month; ``window`` = (history start, months shown)."""
    start, shown = window
    buyers = _by_month(orders_monthly(run))
    news = {int(num(r.get("month_id"))): num(r.get("new_buyers")) for r in first["rows"]}
    rows = []
    for mo in shown:
        total = num(buyers.get(mo.mid, {}).get("buyers"))
        new = news.get(mo.mid, 0.0)
        rows.append([mo.label, total, new, max(total - new, 0), pct(new, total)])
    sec["tables"].append(table(
        "new_buyers", f"New and returning buyers by month, history since {start.label}",
        ["Month", "Buyers", "New buyers", "Returning buyers", "New buyer share %"], rows,
        first["key"], formulas=[rat(4, 2, 1, 100)],
        chart={"type": "column", "x": 0, "y": [2, 3]}))
    c, pv = rows[-1], rows[-2]
    if c[1] and c[2]:
        sec["headline"] = (f"{count(c[2])} new buyers in {c[0]} ({share_text(c[4], 0)} of "
                           "buyers)" + (f", {updown(change(c[2], pv[2]))} on {pv[0]}"
                                        if pv[2] else ""))
        sec["facts"].append(fact(sec["headline"] + ".", first["key"]))
    elif c[1]:
        sec["headline"] = f"Every buyer in {c[0]} had ordered before since {start.label}"
    sec["facts"].append(fact(_history_note(start, since), first["key"], "INTERPRETATION"))


def _history_note(start, since) -> str:
    if since:
        return (f"Buyer history starts in {start.label}, the start set for this brand: new "
                "means a first order since then.")
    return (f"Buyer history starts in {start.label}, 12 months back: new means a first "
            "order since then; a fixed start can be set in the brand's MBR settings.")


def _new_customers(run, sec, new) -> None:
    """New customers by month as the customer data counts them."""
    rows = [[L.month_label(str(r.get("order_month"))[:7]), num(r.get("new_customers")),
             num(r.get("orders")), num(r.get("nmv"))] for r in new["rows"]]
    sec["tables"].append(table("new_customers", "New customers by month (customer data)",
                               ["Month", "New customers", "Orders", "NMV"], rows, new["key"]))
    by = {r[0]: r for r in rows}
    c, pv = by.get(run.month.label), by.get(run.month.shift(-1).label)
    if c and pv:
        sec["facts"].append(fact(f"New customers in the customer data "
                                 f"{change_text(c[1], pv[1], count)}.", new["key"]))


def _csat(row):
    """CSAT as a percent. A platform whose scores are all zero or empty for the month is
    not rated in the feed (staging DSOL Sep 2026: Shopee, 188 chats, read as "0.0% CSAT")."""
    top = num(row.get("csat_max"))
    if top <= 0:
        return _NOT_RECORDED
    avg = num(row.get("csat"))
    return avg * 100 if top <= 1 else avg


def _cs_rows(sec, rec, cur):
    rows = [[L.platform_label(r.get("platform")), num(r.get("chats")),
             num(r.get("avg_response_seconds")) / 60, _csat(r)] for r in rec["rows"]]
    sec["tables"].append(table("cs", f"Customer service, {cur.label}",
                               ["Platform", "Chats", "Response (minutes)",
                                "Satisfaction (CSAT) %"], rows, rec["key"]))
    chats = sum(r[1] for r in rows)
    if not chats:
        return
    minutes = sum(r[1] * r[2] for r in rows) / chats
    sec["headline"] = (f"{count(chats)} customer chats in {cur.label}, answered in "
                       f"{minutes:.1f} minutes on average")
    rated = [r for r in rows if r[3] != _NOT_RECORDED]
    rated_chats = sum(r[1] for r in rated)
    if rated_chats:
        score = sum(r[1] * r[3] for r in rated) / rated_chats
        where = "" if len(rated) == len(rows) else " where rated"
        sec["headline"] += f" with {score:.0f}% satisfaction{where}"
    sec["facts"].append(fact(sec["headline"] + (" (CSAT)." if rated_chats else "."), rec["key"]))
    unrated = [r[0] for r in rows if r[3] == _NOT_RECORDED]
    if unrated:
        sec["facts"].append(fact(
            f"{', '.join(unrated)} chats carry no satisfaction score in the customer service "
            f"data, so their CSAT is not recorded, not 0%.", rec["key"]))


def sec_cs(run):
    sec = section("customer_service", "D", "Inbound and customer service")
    names, cur = lit_list(run.config.brand.client_names), run.month
    rec = run.query(
        "customer_service", "acme_store_customer_service",
        ("client", "platform", "created_date", "chat_count", "avg_response_time_seconds", "csat_rate"),
        lambda dl: (
            f"SELECT platform, SUM(chat_count) AS chats, "
            f"AVG(avg_response_time_seconds) AS avg_response_seconds, AVG(csat_rate) AS csat, "
            f"MAX(csat_rate) AS csat_max "
            f"FROM acme_store_customer_service WHERE UPPER(client) IN ({names}) "
            f"AND {as_date(dl, 'created_date')} BETWEEN {d(cur.first)} AND {d(cur.last)} "
            f"GROUP BY platform"))
    if use(sec, rec):
        _cs_rows(sec, rec, cur)
    codes = run.config.brand.client_codes
    if codes:
        inb = run.query(
            "inbound", "datamart_acme_ims_inbound",
            ("client_code", "created_date", "received_date", "received_quantity_fix", "code"),
            lambda dl: (
                f"SELECT COUNT(DISTINCT code) AS documents, SUM(received_quantity_fix) AS units, "
                f"AVG({days_between(dl, 'created_date', as_date(dl, 'received_date'))}) AS avg_days "
                f"FROM datamart_acme_ims_inbound WHERE Client_Code IN ({lit_list(codes)}) "
                f"AND created_date BETWEEN {d(cur.first)} AND {d(cur.last)}"))
        if use(sec, inb) and inb["rows"]:
            r = inb["rows"][0]
            sec["tables"].append(table("inbound", f"Warehouse inbound, {cur.label}",
                                       ["Inbound documents", "Units received",
                                        "Average days from created to received"],
                                       [[num(r.get("documents")), num(r.get("units")),
                                         num(r.get("avg_days"))]], inb["key"]))
    else:
        sec["missing"].append(missing(
            "warehouse client codes for this brand",
            "inbound speed shows whether stock reaches the shelf in time",
            "the brand's warehouse client codes in the MBR settings",
            fallback="customer service only", updates="inbound documents, units and lead time appear",
            detail="tenant config mbr.brands.<brand>.client_codes"))
    return degrade(sec)


def _stock_value(run, sec):
    """Client level stock value and days of cover from L3 (small table)."""
    codes, cur = run.config.brand.client_codes, run.month
    if not codes:
        sec["missing"].append(missing(
            "warehouse client codes for this brand",
            "stock value and days of cover are reported per warehouse client code",
            "the brand's warehouse client codes in the MBR settings",
            fallback="stock by item only, when it fits the reading limit",
            updates="stock value and days of cover by day appear",
            detail="tenant config mbr.brands.<brand>.client_codes"))
        return
    rec = run.query(
        "stock_value", "acme_store_stock_value",
        ("client_code_detail", "report_date", "stock_value", "sum_avail_qt_per_client",
         "doi30_client"),
        lambda dl: (
            f"SELECT report_date, SUM(stock_value) AS stock_value, "
            f"SUM(sum_avail_qt_per_client) AS units, AVG(doi30_client) AS doi30 "
            f"FROM acme_store_stock_value WHERE client_code_detail IN ({lit_list(codes)}) "
            f"AND report_date BETWEEN {d(cur.first)} AND {d(cur.last)} "
            f"GROUP BY report_date ORDER BY report_date"))
    if not use(sec, rec) or not rec["rows"]:
        return
    last = rec["rows"][-1]
    sec["tables"].append(table("stock_value", f"Stock value and days of cover, {cur.label}",
                               ["Date", "Stock value", "Units", "Days of cover"],
                               [[r.get("report_date"), num(r.get("stock_value")), num(r.get("units")),
                                 num(r.get("doi30"))] for r in rec["rows"]], rec["key"]))
    sec["headline"] = (f"Stock on {L.day_label(last.get('report_date'))} was worth "
                       f"{idr(last.get('stock_value'))}, {num(last.get('doi30')):.0f} days of cover")
    sec["facts"].append(fact(sec["headline"] + ".", rec["key"]))


def sec_inventory(run):
    sec = section("inventory", "D", "Inventory readiness")
    _stock_value(run, sec)
    cfg = run.config.brand
    col, vals, cur = cfg.inventory_column, lit_list(cfg.inventory_values), run.month
    t = "vw_inventory_order_wh_daily_snapshot"
    # an attached stock file is the snapshot the person chose, usually taken after the month
    cap = "" if run.from_files(t) else f" AND report_date <= {d(cur.last)}"
    snap = run.query(
        "inventory_snapshot_date", t, (col, "report_date"),
        lambda dl: (f"SELECT MAX(report_date) AS snapshot FROM {t} WHERE UPPER({col}) IN ({vals})"
                    + cap))
    if not use(sec, snap):
        return degrade(sec)
    day = (snap["rows"][0].get("snapshot") if snap["rows"] else None)
    if not day:
        sec["missing"].append(missing(
            f"warehouse stock rows for {', '.join(cfg.inventory_values)}",
            "stock cover against the plan decides what can be sold next month",
            "the brand name as the warehouse stock data stores it, in the brand's MBR settings",
            detail=f"{t}.{col}; mbr.brands.<brand>.inventory"))
        return degrade(sec)
    snap_day = date.fromisoformat(str(day)[:10])
    rec = run.query(
        "inventory_skus", t,
        (col, "report_date", "sto_item_code", "sto_item_name", "sto_available_stock", "max_hpp",
         "orderqty30", "po_ship_date", "po_quantity"),
        lambda dl: (
            f"SELECT sto_item_code, MAX(sto_item_name) AS item_name, "
            f"SUM(sto_available_stock) AS stock, MAX(max_hpp) AS hpp, SUM(orderqty30) AS sold_30d, "
            f"MIN(po_ship_date) AS next_po_date, SUM(po_quantity) AS po_qty FROM {t} "
            f"WHERE UPPER({col}) IN ({vals}) AND report_date = {d(snap_day)} "
            f"GROUP BY sto_item_code LIMIT 500"))
    if not use(sec, rec):
        return degrade(sec)
    rows = []
    for r in rec["rows"]:
        stock, hpp, sold = num(r.get("stock")), num(r.get("hpp")), num(r.get("sold_30d"))
        doi = (stock / (sold / 30)) if sold else None
        rows.append([r.get("sto_item_code"), L.short_product(r.get("item_name"), run.brand),
                     stock, stock * hpp, doi, r.get("next_po_date"), num(r.get("po_qty"))])
    rows.sort(key=lambda x: -x[3])
    value = sum(x[3] for x in rows)
    when = f"{snap_day.day} {snap_day.strftime('%b')} {snap_day.year}"
    sec["tables"].append(table(
        "inventory", f"Stock by item at {when} (top by value)",
        ["Item code", "Item", "Units", "Value at cost (HPP)", "Days of stock", "Next delivery",
         "Units on order"], rows[:35], rec["key"], deck_columns=[1, 2, 3, 4, 5, 6]))
    low = [x for x in rows if x[4] is not None and x[4] < 14]
    line = (f"Stock at cost on {when} was {idr(value)} across {len(rows)} items; "
            f"{len(low)} {'item has' if len(low) == 1 else 'items have'} under 14 days of stock")
    if not sec["headline"]:
        sec["headline"] = line
    sec["facts"].append(fact(line + ".", rec["key"]))
    return degrade(sec)


# ---------------------------------------------------------------- E, F and appendix
def sec_synthesis(run, built):
    sec = section("synthesis", "E", "Synthesis, asks and strategy")
    lead = [s["headline"] for s in built if s.get("headline")]
    for text in lead[:6]:
        sec["facts"].append(fact(text + ".", "sections"))
    sec["missing"].append(missing(
        "written synthesis, asks of the brand and strategy",
        "the review ends with what it means and what we ask",
        "the narration step (written from the evidence, every number traced)",
        fallback="the finding of each page, listed above",
        updates="synthesis, asks and strategy pages are written"))
    return degrade(sec)


def _so_far(run) -> dict | None:
    """The month after the review month, through yesterday (the warehouse is live)."""
    nxt = run.month.shift(1)
    upto = min(nxt.last, _today(run) - timedelta(days=1))
    if upto < nxt.first:
        return None
    rec = orders_daily(run, "orders_next_month", nxt.first, upto)
    out = {"rec": rec, "month": nxt, "upto": upto, "asked": upto, "complete": upto == nxt.last}
    if rec["ok"]:
        out["rows"] = [[r.get("order_date"), num(r.get("gmv")), num(r.get("nmv")),
                        num(r.get("orders"))] for r in rec["rows"]]
        out["nmv"] = sum(r[2] for r in out["rows"])
        # The warehouse loads with a lag: the last day or two with no orders at all are
        # days not loaded yet, not days that sold nothing. Counting them would understate
        # the pace (live 13 Oct: 12 days counted, 11 loaded, pace 18.7 m instead of 20.5 m
        # a day). A longer silence is a quiet brand, and stays counted.
        seen = [date.fromisoformat(str(r[0])[:10]) for r in out["rows"] if r[3] or r[2]]
        if seen and 0 < (upto - max(seen)).days <= LOAD_LAG_DAYS:
            out["upto"] = max(seen)
            out["complete"] = False
    out["days"] = (out["upto"] - nxt.first).days + 1
    return out


def sec_month_so_far(run):
    so = _so_far(run)
    if so is None:
        return None
    nxt, cur = so["month"], run.month
    sec = section("month_so_far", "F", f"{nxt.label} so far")
    if not use(sec, so["rec"]):
        return degrade(sec)
    rows = so["rows"]
    if not rows or not so["nmv"]:
        return None                        # nothing booked yet: the month ahead says so
    sec["tables"].append(table("so_far", f"Daily GMV, NMV and orders, {nxt.label} so far",
                               ["Date", "GMV", "NMV", "Orders"], rows, so["rec"]["key"],
                               chart={"type": "column", "x": 0, "y": [2],
                                      "title": f"Daily NMV, {nxt.label} so far"}))
    per_day = so["nmv"] / so["days"]
    cur_nmv = num(_by_month(orders_monthly(run)).get(cur.mid, {}).get("nmv"))
    cur_pd = cur_nmv / cur.days if cur_nmv else 0
    daily = run.queries.get("orders_daily") or {}
    same = sum(num(r.get("nmv")) for r in daily.get("rows") or []
               if str(r.get("order_date"))[:10] <= (cur.first + timedelta(days=so["days"] - 1)).isoformat())
    if so["complete"]:
        head = f"{nxt.label} closed at {idr(so['nmv'])} NMV, {idr(per_day)} a day"
    else:
        head = (f"{nxt.label} so far: {idr(so['nmv'])} NMV in {so['days']} days, "
                f"{idr(per_day)} a day")
    if cur_pd:
        head += f" against {idr(cur_pd)} a day in {cur.label}"
    sec["headline"] = head
    sec["facts"].append(fact(head + ".", so["rec"]["key"]))
    if same and not so["complete"]:
        sec["facts"].append(fact(
            f"Day 1 to {so['days']} of {nxt.label} made {idr(so['nmv'])} NMV against "
            f"{idr(same)} over the same days of {cur.label} ({updown(change(so['nmv'], same))}).",
            so["rec"]["key"]))
    sec["facts"].append(fact(_so_far_note(so), so["rec"]["key"], "INTERPRETATION"))
    return degrade(sec)


def _so_far_note(so) -> str:
    """Which days the month so far counts, naming every day not loaded yet."""
    if so["upto"] >= so["asked"]:
        return (f"Counted through {L.day_label(so['upto'].isoformat())}, the day before the "
                "data was pulled; recent orders can still cancel.")
    first = so["upto"] + timedelta(days=1)
    gap = (f"{L.day_label(first.isoformat())} is" if first == so["asked"] else
           f"{L.day_label(first.isoformat())} to {L.day_label(so['asked'].isoformat())} are")
    return (f"Counted through {L.day_label(so['upto'].isoformat())}, the latest day in the "
            f"data; {gap} not loaded yet. Recent orders can still cancel.")


def sec_month_ahead(run):
    nxt = run.month.shift(1)
    sec = section("month_ahead", "F", f"Plan and building blocks for {nxt.label}")
    target = (run.config.brand.targets.get(nxt.key) or {}).get("nmv")
    pace = _next_month_pace(run)
    if target:
        _pace_to_target(sec, run, target, pace)
    else:
        sec["missing"].append(missing(
            f"NMV target for {nxt.label}",
            "the pace needed per day is measured against the next month's target",
            "the NMV target for next month, entered when the report is generated",
            fallback="pace so far only", updates="the needed pace per day is computed",
            detail=f"no targets[{nxt.key}].nmv in the request or tenant config"))
    sec["missing"].append(missing(
        f"plan for {nxt.label} (building blocks per product and lever)",
        "the review should end with how the next target is reached",
        "the plan model (later phase)",
        visual="waterfall from current run rate to target", fallback="the pace to target only",
        updates="building blocks per product and a supply check appear", key="plan"))
    return degrade(sec)


def _review_month_pace(run):
    """The review month's NMV a day; 0 without sold statuses or NMV."""
    cur = run.month
    cur_nmv = num(_by_month(orders_monthly(run)).get(cur.mid, {}).get("nmv")) \
        if run.statuses.get("processed") else 0
    return cur_nmv / cur.days if cur_nmv else 0


def _next_month_pace(run) -> dict:
    """NMV booked so far in the month after the review, the days counted and left, and
    the pace a day: so far when anything is booked, else the review month's."""
    nxt, cur = run.month.shift(1), run.month
    so = _so_far(run) if run.statuses.get("processed") else None
    if so and not so["rec"]["ok"]:
        so = None                      # booked so far unknown: measure across the month
    booked = (so or {}).get("nmv") or 0.0
    counted = (so or {}).get("days", 0) if booked else 0
    cur_pd = _review_month_pace(run)
    pace, pace_label = ((booked / counted, "so far") if counted else (cur_pd, f"{cur.label}"))
    return {"booked": booked, "counted": counted, "left": nxt.days - counted,
            "pace": pace, "label": pace_label}


def _pace_to_target(sec, run, target, pace) -> None:
    """The pace table and headline against the next month's NMV target."""
    nxt = run.month.shift(1)
    booked, left = pace["booked"], pace["left"]
    rows = [[f"NMV target {nxt.label}", target], ["Booked so far", booked],
            ["Still needed", max(target - booked, 0)]]
    if left > 0:
        need = max(target - booked, 0) / left
        rows += [["Needed per remaining day", need],
                 [f"Pace per day, {pace['label']}", pace["pace"]]]
        head = _pace_headline(target, nxt, need, pace)
    else:
        head = f"{nxt.label} closed at {pct(booked, target) or 0:.0f}% of its {idr(target)} target"
    sec["tables"].append(table("pace", f"Pace to the {nxt.label} target", ["Measure", "NMV"],
                               rows, "orders_next_month, orders_monthly"))
    sec["headline"] = head
    sec["facts"].append(fact(head + ".", "orders_next_month, orders_monthly, settings"))
    sec["queries"] += [k for k in ("orders_next_month", "orders_monthly") if k in run.queries]


def _pace_headline(target, nxt, need, pace) -> str:
    left, rate, label = pace["left"], pace["pace"], pace["label"]
    if pace["booked"] >= target:
        return f"The {idr(target)} {nxt.label} target is already reached"
    if rate:
        return (f"Reaching the {idr(target)} {nxt.label} target needs {idr(need)} NMV a "
                f"day for the remaining {left} days, {need / rate:.1f} times the "
                f"{label} pace of {idr(rate)} a day")
    return (f"Reaching the {idr(target)} {nxt.label} target needs {idr(need)} NMV a "
            f"day for {left} days")


def sec_price(run):
    sec = section("price_vs_competitor", "C", "Price versus Shopee")
    sec["missing"].append(missing(
        "daily competitor price check per product (published price, seller discount, voucher)",
        "price gaps against Shopee explain conversion moves on TikTok and Tokopedia",
        "a daily price check table or upload (later phase)",
        visual="price index by product and day", fallback="own price per unit history only",
        updates="the price review pages are filled"))
    return degrade(sec)


def sec_gmv_definition(run):
    sec = section("gmv_definition", "Appendix", "Definitions: GMV and NMV")
    rec = orders_monthly(run)
    if not use(sec, rec):
        return degrade(sec)
    m = _by_month(rec)
    rows = []
    for mo in months_window(run)[-6:]:
        r = m.get(mo.mid, {})
        rows.append([mo.label, num(r.get("gmv")), num(r.get("listed_gmv")), num(r.get("nmv")),
                     change(r.get("listed_gmv"), r.get("gmv"))])
    sec["tables"].append(table(
        "gmv_check", "GMV in this review against the GMV column of the order data",
        ["Month", "GMV, this review", "GMV column, order data", "NMV",
         "Order data vs this review %"], rows, rec["key"], formulas=[chg(4, 2, 1)]))
    cur = m.get(run.month.mid, {})
    sold = L.status_list(run.statuses.get("processed"))
    sec["facts"].append(fact(
        "GMV is the value of order lines after seller discount over every order status "
        "(cancelled and unpaid included); NMV is the value after seller discount with the "
        f"platform discount added back, over orders counted as sold ({sold}).", "definitions"))
    sec["facts"].append(fact(
        "The reference definition also adds net shipping (shipping fee less seller and "
        "platform shipping discounts). The order data has no shipping fields, so GMV here is "
        "lower by that amount.", "definitions", "INTERPRETATION"))
    gap = change(cur.get("listed_gmv"), cur.get("gmv"))
    if gap is not None:
        sec["headline"] = (f"GMV here is order value after seller discount over every status; "
                           f"the order data's own GMV column is {than(gap)} for "
                           f"{run.month.label}")
    same = [r[0] for r in rows if r[2] and abs(r[2] - r[3]) < 1]
    if same:
        sec["facts"].append(fact(
            f"In {', '.join(same)} the order data's GMV column equals NMV exactly, so it is not "
            "a gross figure there; this review does not use it.", rec["key"], "INTERPRETATION"))
    unvalued = num(cur.get("cancelled_unvalued"))
    if unvalued:
        sec["facts"].append(fact(
            f"{idr(unvalued)} of cancelled lines in {run.month.label} carry no item value in the "
            "order data, so GMV here leaves them out and reads low by up to that amount.",
            rec["key"], "INTERPRETATION"))
    return degrade(sec)


def sec_reconciliation(run):
    sec = section("reconciliation", "Appendix", "Acme curated orders check")
    b = lit(run.brand)
    w = months_window(run)
    rec = run.query(
        "l3_orders_monthly", L3_ORDERS, ("brand", "order_date", "gmv", "nmv", "order_id"),
        lambda dl: (
            f"SELECT {month_key(dl, 'order_date')} AS month_id, SUM(gmv) AS gmv, SUM(nmv) AS nmv, "
            f"COUNT(DISTINCT order_id) AS orders FROM {L3_ORDERS} WHERE UPPER(brand) = {b} "
            f"AND order_date BETWEEN {d(w[0].first)} AND {d(w[-1].last)} "
            f"GROUP BY {month_key(dl, 'order_date')} ORDER BY 1"))
    if not use(sec, rec):
        return degrade(sec)
    # Like for like: the curated table is a copy of the order history, so both sides are
    # summed over EVERY status from their own GMV and NMV columns. Comparing curated
    # all-status NMV with this review's sold-only NMV reported a 25% "difference" that
    # was only the cancelled and unpaid orders.
    l2 = _by_month(orders_monthly(run))
    rows = []
    for r in rec["rows"]:
        mid = int(num(r.get("month_id")))
        a = l2.get(mid, {})
        rows.append([L.month_label(mid), num(a.get("listed_gmv")), num(r.get("gmv")),
                     change(r.get("gmv"), a.get("listed_gmv")), num(a.get("nmv_all")),
                     num(r.get("nmv")), change(r.get("nmv"), a.get("nmv_all"))])
    sec["tables"].append(table(
        "recon", "GMV and NMV columns over every order status, order history against Acme "
        "curated orders",
        ["Month", "GMV, order history", "GMV, Acme curated", "GMV difference %",
         "NMV, order history", "NMV, Acme curated", "NMV difference %"],
        rows, rec["key"], formulas=[chg(3, 2, 1), chg(6, 5, 4)]))
    if rows:
        sec["facts"].append(fact(
            f"Acme curated orders hold {len(rows)} month(s) for {run.brand} in the window. Both "
            "sides are the tables' own GMV and NMV columns over every order status, so the "
            "check shows whether the curated copy holds the same orders, not this review's "
            "definitions.", rec["key"]))
        _recon_verdict(sec, rec["key"], rows)
    return degrade(sec)


def _recon_verdict(sec, key, rows) -> None:
    """The months where the curated copy is more than 1% off, or how closely it matches."""
    diffs = [abs(x) for r in rows for x in (r[3], r[6]) if x is not None]
    off = [r for r in rows if any(x is not None and abs(x) > 1 for x in (r[3], r[6]))]
    if off:
        worst, gap = _worst_gap(off)
        sec["headline"] = (f"Acme curated orders differ from the order history by more than "
                           f"1% in {len(off)} of {len(rows)} month(s), most in {worst[0]} "
                           f"({than(gap)})")
        sec["facts"].append(fact(
            sec["headline"] + "; the curated copy is missing or adding orders there.",
            key, "INTERPRETATION"))
    elif diffs:
        span = f"all {len(rows)} months" if len(rows) > 1 else "the one month"
        close = "exactly" if max(diffs) < 0.05 else f"within {max(diffs):.1f}%"
        sec["headline"] = (f"Acme curated orders match the order history {close} on GMV "
                           f"and NMV in {span} they share")


def _gap_size(r):
    return max(abs(r[3] or 0), abs(r[6] or 0))


def _worst_gap(off) -> tuple:
    """The month furthest off and its larger difference (NMV on a tie)."""
    worst = max(off, key=_gap_size)
    gap = worst[6] if abs(worst[6] or 0) >= abs(worst[3] or 0) else worst[3]
    return worst, gap


def sec_status_map(run):
    sec = section("status_map", "Appendix", "Definitions: order status groups")
    rows = [[r["value"], L.status_label(r["value"]), L.GROUP_LABELS.get(r["group"], r["group"]),
             "MBR settings" if r["configured"] else "keyword guess, to confirm",
             num(r["lines"]), num(r["gmv"])] for r in run.status_rows]
    sec["tables"].append(table("status_map", "Order status values and how they are counted",
                               ["Status as stored", "Meaning", "Counted as", "Decided by",
                                "Order lines (13 months)", "GMV"],
                               rows, "order_status_values", deck_columns=[1, 2, 3, 4, 5]))
    sec["queries"].append("order_status_values")
    guessed = [r for r in run.status_rows if not r["configured"]]
    if guessed:
        sec["missing"].append(missing(
            f"confirmation of {len(guessed)} order status value(s) grouped by keyword",
            "NMV, orders and cancellation all depend on which statuses count as sold",
            "the owner confirms the status groups; Poros stores them in the MBR settings",
            detail="tenant config mbr.status_map"))
    sec["facts"].append(fact(
        "Orders counted as sold: " + L.status_list(run.statuses.get("processed")) +
        ". Cancelled: " + L.status_list(run.statuses.get("cancelled")) +
        ". Not paid: " + L.status_list(run.statuses.get("unpaid")) + ".", "definitions"))
    return degrade(sec)


# Deck storyline groups (reference deck: A..F + appendix), in reading order.
GROUPS = (
    ("Summary", "Summary"),
    ("A", "Where are we now"),
    ("B", "What changed after Acme"),
    ("C", "Why did it change"),
    ("D", "What is working"),
    ("E", "Synthesis"),
    ("F", "The month ahead"),
    ("Appendix", "Appendix"),
)


def ordered(sections: list) -> list:
    """Sections grouped A..F in deck order, each numbered within its group (C1, C2...)."""
    rank = {g: i for i, (g, _) in enumerate(GROUPS)}
    out = sorted(sections, key=lambda s: rank.get(s["group"], len(GROUPS)))
    counters = {}
    for s in out:
        counters[s["group"]] = counters.get(s["group"], 0) + 1
        n = counters[s["group"]]
        s["ref"] = f"{s['group']} {n}" if s["group"] in ("Summary", "Appendix") else f"{s['group']}{n}"
    return out


SECTIONS = (
    sec_scorecard, sec_trend, sec_daily, sec_after_acme, sec_marketplace, sec_traffic,
    sec_price, sec_products, sec_nmv_bridge, sec_price_pack, sec_product_type,
    sec_product_funnel, sec_margin, sec_creators, sec_live_video, sec_ads, sec_economics,
    sec_fulfilment, sec_cancellation, sec_payment, sec_geography, sec_customers, sec_cs,
    sec_inventory,
)


# The fixed brand baseline the chat's brand analysis stands on: what happened (scorecard,
# trend), where (marketplaces, provinces), on what (products, price and pack), why NMV moved
# (units, price, new and stopped listings) and the quality signals (cancellations, buyers,
# fulfilment). Same template queries as the review, so the numbers match the deck.
CORE = (
    sec_scorecard, sec_trend, sec_marketplace, sec_products, sec_nmv_bridge, sec_price_pack,
    sec_cancellation, sec_customers, sec_fulfilment, sec_geography,
)


def build_core(run, between=None) -> list:
    """The CORE sections in deck order; [] when the brand has no order rows in reach.
    ``between`` runs before each section (the chat passes its cancellation check)."""
    if not discover_statuses(run):
        return []
    out = []
    for fn in CORE:
        if between:
            between()
        out.append(_safe(fn, run))
    return ordered(out)


def _safe(fn, run, *args):
    try:
        return fn(run, *args)
    except Exception as exc:                          # a section bug never sinks the report
        sec = section(fn.__name__.replace("sec_", ""), "Appendix", fn.__name__)
        sec["status"] = "failed"
        sec["missing"].append(missing("section computation", "this page is empty",
                                      "a fix in Poros; the technical detail is in the open items",
                                      detail=f"{type(exc).__name__}: {str(exc)[:300]}"))
        return sec


def build_all(run) -> list:
    out = _build_all(run)
    if getattr(run, "uploads", None):
        from . import file_sections           # file pages import this module's helpers
        out = ordered(out + [_safe(fn, run) for fn in file_sections.pages(run)])
        file_sections.mark_file_pages(out, run)
        file_sections.link_delivery(out)
    drafts.attach(out, run.brand, run.month, run.month.shift(1))
    return out


def _build_all(run) -> list:
    if not discover_statuses(run):
        rec = run.queries.get("order_status_values") or {}
        fail = section("orders", "Summary", "Orders")
        errored = not rec.get("ok") and rec.get("code") not in ("TABLE_NOT_IN_SCOPE",
                                                                "COLUMN_MISSING")
        fail["status"] = "failed" if errored else "placeholder"
        fail["queries"].append("order_status_values")
        fail["missing"].append(missing(
            f"order rows for {run.brand}",
            "every order based figure of the review needs them",
            f"orders for brand {run.brand} in the order history for the last 13 months",
            detail=rec.get("error") or f"no rows for brand {run.brand} in {L2_ORDERS}"))
        others = [_safe(s, run) for s in (sec_cs, sec_inventory)]
        return ordered([fail] + others + [sec_price(run), _safe(sec_month_ahead, run)])
    built = [_safe(fn, run) for fn in SECTIONS]
    built.append(_safe(sec_synthesis, run, list(built)))
    so_far = _safe(sec_month_so_far, run)
    if so_far:
        built.append(so_far)
    built.append(_safe(sec_month_ahead, run))
    built.append(_safe(sec_gmv_definition, run))
    built.append(_safe(sec_reconciliation, run))
    built.append(_safe(sec_status_map, run))
    return ordered(built)
