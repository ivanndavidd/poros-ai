"""MBR pages only the attached exports can fill.

The warehouse order table has no shipping per order, no shipped or delivered time and no
warehouse; the person's marketplace exports do. These pages read the normalized files
(``upload_*`` tables, see uploads/schema.py) through the same governed path, and they are
added only when files were attached. Every figure is a query cell or arithmetic on one.

GMV with shipping = item value after the seller's discount plus the shipping the buyer
paid, every order status: the reference package's GMV, which the warehouse cannot give.
"""
from __future__ import annotations

from . import labels as L
from .file_source import SOURCE_NAME
from .mbr_sections import (
    change, change_text, count, degrade, fact, idr, missing, num, pct, section, share_text,
    table, updown, use,
)

ORDERS = "upload_orders"
_PROCESSED = "status_group = 'processed'"
# What the warehouse NMV counts as sold: delivered AND still shipping (flagged not final).
_SOLD = "status_group IN ('processed', 'in_flight')"
_DAYS = "DATE_DIFF('second', {a}, {b}) / 86400.0"
_FILE_NOTE = "These figures come from the files attached to this report, not from the warehouse."
_FILES_VS_WH = "files_orders, orders_marketplace"


def _days(a: str, b: str) -> str:
    return _DAYS.format(a=a, b=b)


def _orders_sql(mid: int) -> str:
    return (
        "SELECT marketplace, COUNT(DISTINCT order_id) AS orders, "
        f"COUNT(DISTINCT CASE WHEN {_PROCESSED} THEN order_id END) AS processed_orders, "
        f"COUNT(DISTINCT CASE WHEN {_SOLD} THEN order_id END) AS sold_orders, "
        "SUM(COALESCE(quantity, 0)) AS units, "
        "SUM(COALESCE(item_value_after_seller_discount, 0)) AS item_value, "
        f"SUM(CASE WHEN {_SOLD} THEN COALESCE(item_value_after_seller_discount, 0) "
        "ELSE 0 END) AS item_value_sold, "
        # no COALESCE: a column the marketplace's export lacks sums to NULL, not to 0
        "SUM(shipping_fee_buyer) AS shipping_buyer, "
        "SUM(shipping_discount_seller) AS ship_seller, "
        "SUM(shipping_discount_platform) AS ship_platform "
        f"FROM {ORDERS} WHERE month_id = {mid} GROUP BY marketplace ORDER BY item_value DESC")


def _opt(v):
    """A figure, or 'Not recorded' when the export has no such field (never a 0)."""
    return L.NOT_RECORDED if v is None else num(v)


def _orders(run, key: str, mid: int) -> dict:
    return run.query(key, ORDERS, ("marketplace", "order_id", "month_id", "status_group"),
                     lambda dl: _orders_sql(mid))


def _gmv_ship(r) -> float:
    return num(r.get("item_value")) + num(r.get("shipping_buyer"))


def _warehouse_nmv(run) -> dict:
    """Warehouse NMV by marketplace for the review month (the marketplace page's query)."""
    rec = run.queries.get("orders_marketplace") or {}
    out = {}
    for r in rec.get("rows") or []:
        if int(num(r.get("month_id"))) == run.month.mid:
            key = L.platform_label(r.get("marketplace"))
            out[key] = out.get(key, 0.0) + num(r.get("nmv"))
    return out


def _reconcile(sec, run, rows) -> None:
    """Files against the warehouse, per marketplace, for the same month and statuses: the
    warehouse NMV counts orders still shipping as sold, so the export side does too (a
    'completed only' export side read 17% short on Shopee that was all in transit)."""
    wh = _warehouse_nmv(run)
    if not wh:
        return
    lines = []
    for r in rows:
        mp = L.platform_label(r.get("marketplace"))
        files, house = num(r.get("item_value_sold")), wh.get(mp)
        lines.append([mp, files, house, change(files, house) if house else None])
    sec["tables"].append(table(
        "files_vs_warehouse", f"Order value counted as sold: attached exports against the "
        f"warehouse, {run.month.label}", ["Marketplace", "Attached exports", "Warehouse NMV",
                                          "Difference %"],
        lines, _FILES_VS_WH))
    keyed = [x for x in lines if x[3] is not None]
    worst = max(keyed, key=lambda x: abs(x[2] - x[1]), default=None)   # largest in rupiah
    if worst and abs(worst[3]) >= 5:
        sec["facts"].append(fact(
            f"{worst[0]} differs most: the export shows {idr(worst[1])} of order value counted "
            f"as sold against {idr(worst[2])} in the warehouse ({_than_text(worst[3])}).",
            _FILES_VS_WH))
    elif keyed:
        far = max(abs(x[3]) for x in keyed)
        sec["facts"].append(fact(
            f"The attached exports match the warehouse NMV within {far:.1f}% on every "
            f"marketplace, counting orders still shipping as sold on both sides.",
            _FILES_VS_WH))


def _than_text(c) -> str:
    return f"{abs(c):.1f}% {'higher' if c > 0 else 'lower'} in the export"


def sec_files_orders(run):
    sec = section("files_orders", "C", "GMV with shipping, from the marketplace exports")
    cur, prev = run.month, run.month.shift(-1)
    rec = _orders(run, "files_orders", cur.mid)
    if not use(sec, rec) or not rec["rows"]:
        if not sec["missing"]:
            sec["missing"].append(missing(
                f"order exports for {cur.label}", "shipping and GMV with shipping come only from "
                "the marketplace order exports", f"the order exports of {cur.label} attached to "
                "the report", detail=f"no {ORDERS} rows for month {cur.mid}"))
        return degrade(sec)
    old = {L.platform_label(r.get("marketplace")): _gmv_ship(r)
           for r in _orders(run, "files_orders_prev", prev.mid)["rows"]}
    rows = [[L.platform_label(r.get("marketplace")), num(r.get("orders")), num(r.get("units")),
             num(r.get("item_value")), _opt(r.get("shipping_buyer")), _gmv_ship(r),
             _opt(r.get("ship_seller")), _opt(r.get("ship_platform"))] for r in rec["rows"]]
    sec["tables"].append(table(
        "gmv_shipping", f"Orders and GMV with shipping by marketplace, {cur.label} (every status)",
        ["Marketplace", "Orders", "Units", "Item value after seller discount",
         "Shipping paid by buyers", "GMV with shipping", "Shipping discount by seller",
         "Shipping discount by marketplace"], rows, rec["key"],
        chart={"type": "column", "x": 0, "y": [5]}))
    total = sum(r[5] for r in rows)
    ship = sum(num(r.get("shipping_buyer")) for r in rec["rows"])
    before = sum(old.values())
    sec["headline"] = (f"GMV with shipping was {idr(total)} in {cur.label}, "
                       f"{updown(change(total, before)) if before else 'with no export for last month'}; "
                       f"shipping paid by buyers was {share_text(pct(ship, total))} of it")
    sec["facts"].append(fact(sec["headline"] + ".", rec["key"]))
    if before:
        sec["facts"].append(fact(f"GMV with shipping {change_text(total, before)}.",
                                 "files_orders, files_orders_prev"))
    seller = sum(num(r.get("ship_seller")) for r in rec["rows"])
    if seller:
        sec["facts"].append(fact(f"The seller funded {idr(seller)} of shipping discounts in "
                                 f"{cur.label}.", rec["key"]))
    _reconcile(sec, run, rec["rows"])
    _warehouses(sec, run)
    return degrade(sec)


def _warehouses(sec, run) -> None:
    mid = run.month.mid
    rec = run.query(
        "files_warehouse", ORDERS, ("warehouse", "order_id", "month_id", "status_group"),
        lambda dl: (f"SELECT COALESCE(NULLIF(TRIM(warehouse), ''), 'Not recorded') AS warehouse, "
                    f"COUNT(DISTINCT order_id) AS orders FROM {ORDERS} WHERE month_id = {mid} "
                    f"AND {_PROCESSED} GROUP BY 1 ORDER BY orders DESC LIMIT 12"))
    if not use(sec, rec) or len(rec["rows"]) < 2:
        return
    total = sum(num(r.get("orders")) for r in rec["rows"])
    sec["tables"].append(table(
        "files_warehouse", f"Completed orders by warehouse, {run.month.label}",
        ["Warehouse", "Orders", "Share %"],
        [[r.get("warehouse"), num(r.get("orders")), pct(r.get("orders"), total)]
         for r in rec["rows"]], rec["key"]))


def _delivery_sql(mid: int, group: str, limit: int) -> str:
    return (
        f"SELECT {group} AS grp, COUNT(DISTINCT order_id) AS orders, "
        f"AVG({_days('COALESCE(paid_at, created_at)', 'shipped_at')}) AS to_ship, "
        f"AVG({_days('shipped_at', 'delivered_at')}) AS in_transit, "
        f"AVG({_days('COALESCE(paid_at, created_at)', 'delivered_at')}) AS to_door, "
        f"MEDIAN({_days('COALESCE(paid_at, created_at)', 'delivered_at')}) AS to_door_median "
        f"FROM {ORDERS} WHERE month_id = {mid} AND delivered_at IS NOT NULL "
        f"AND delivered_at >= COALESCE(paid_at, created_at) GROUP BY 1 "
        f"ORDER BY orders DESC LIMIT {limit}")


def sec_files_delivery(run):
    sec = section("files_delivery", "D", "Delivery times, from the marketplace exports")
    cur = run.month
    cols = ("marketplace", "order_id", "month_id", "created_at", "delivered_at")
    rec = run.query("files_delivery", ORDERS, cols,
                    lambda dl: _delivery_sql(cur.mid, "marketplace", 10))
    if not use(sec, rec) or not rec["rows"]:
        sec["missing"].append(missing(
            f"delivered dates in the order exports of {cur.label}",
            "the time to the buyer's door shows whether the warehouse and couriers keep the "
            "promise", "order exports that carry the delivered time (TikTok, Lazada, Shopee "
            "completion time)", detail=f"no delivered rows in {ORDERS} for {cur.mid}"))
        return degrade(sec)
    rows = [[L.platform_label(r.get("grp")), num(r.get("orders")), _opt(r.get("to_ship")),
             _opt(r.get("in_transit")), num(r.get("to_door")), num(r.get("to_door_median"))]
            for r in rec["rows"]]
    without = _no_delivered_time(run, {r[0] for r in rows})
    note = f" (no delivered time in the {_names(without)} export)" if without else ""
    sec["tables"].append(table(
        "files_delivery", f"Days from payment to delivery by marketplace, {cur.label}{note}",
        ["Marketplace", "Delivered orders", "Days to ship", "Days in transit",
         "Days to the door", "Median days to the door"], rows, rec["key"]))
    orders = sum(r[1] for r in rows)
    door = sum(r[1] * r[4] for r in rows) / orders if orders else 0
    sec["headline"] = (f"Delivered orders reached the buyer {door:.1f} days after payment on "
                       f"average in {cur.label}")
    if without:
        sec["headline"] += f", {_names(sorted(r[0] for r in rows))} orders only"
    sec["facts"].append(fact(sec["headline"] + f" ({count(orders)} delivered orders).", rec["key"]))
    if without:
        sold = _sold_orders(run)
        left = sum(sold.get(mp, 0) for mp in without)
        exports = "export carries" if len(without) == 1 else "exports carry"
        sec["facts"].append(fact(
            f"The {_names(without)} order {exports} no delivered time, so their "
            f"{count(left)} orders counted as sold ({share_text(pct(left, sum(sold.values())))} "
            f"of the month's) are not in these delivery times.", "files_delivery, files_orders"))
    _provinces(sec, run, note)
    _cancellations(sec, run)
    return degrade(sec)


def _sold_orders(run) -> dict:
    """{marketplace: orders counted as sold} for the review month, from the export."""
    rec = _orders(run, "files_orders", run.month.mid)
    return {L.platform_label(r.get("marketplace")): num(r.get("sold_orders"))
            for r in rec.get("rows") or []}


def _no_delivered_time(run, covered: set) -> list:
    """Marketplaces with orders counted as sold this month but no delivered time at all."""
    return sorted(mp for mp, n in _sold_orders(run).items() if n and mp not in covered)


def _names(items) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _provinces(sec, run, note: str = "") -> None:
    group = "COALESCE(NULLIF(TRIM(province), ''), 'Not recorded')"
    rec = run.query("files_province", ORDERS,
                    ("province", "order_id", "month_id", "created_at", "delivered_at"),
                    lambda dl: _delivery_sql(run.month.mid, group, 12))
    if not use(sec, rec) or not rec["rows"]:
        return
    rows = [[r.get("grp"), num(r.get("orders")), num(r.get("to_door")),
             num(r.get("to_door_median"))] for r in rec["rows"]]
    sec["tables"].append(table(
        "files_province", f"Days to the door in the busiest provinces, {run.month.label}{note}",
        ["Province", "Delivered orders", "Days to the door", "Median days"], rows, rec["key"]))
    named = [r for r in rows if r[0] != L.NOT_RECORDED]
    if named:
        slow = max(named, key=lambda r: r[2])
        sec["facts"].append(fact(f"Slowest of the busiest provinces: {slow[0]}, {slow[2]:.1f} "
                                 "days to the door on average.", rec["key"]))


def link_delivery(sections: list) -> None:
    """The geography page asks for delivery time by area; when the attached exports gave
    it, the open item points to that page instead of asking for data already in the pack."""
    files = next((s for s in sections if s.get("id") == "files_delivery"), None)
    geo = next((s for s in sections if s.get("id") == "geography"), None)
    if not files or not geo or not any(t.get("id") == "files_province"
                                       for t in files.get("tables") or []):
        return
    kept = [m for m in geo["missing"] if m.get("key") != "delivery"]
    if len(kept) == len(geo["missing"]):
        return
    geo["missing"] = kept
    geo["facts"].append(fact(f"Delivery days by province, from the attached order exports, are "
                             f"on the page '{files['title']}'.", "files_province"))
    if not kept and geo["status"] == "partial":
        geo["status"] = "complete"


def _cancellations(sec, run) -> None:
    mid = run.month.mid
    rec = run.query(
        "files_cancel", ORDERS, ("cancel_by", "order_id", "month_id", "status_group"),
        lambda dl: (f"SELECT marketplace, COALESCE(NULLIF(TRIM(cancel_by), ''), 'Not recorded') "
                    f"AS cancel_by, COUNT(DISTINCT order_id) AS orders FROM {ORDERS} "
                    f"WHERE month_id = {mid} AND status_group = 'cancelled' GROUP BY 1, 2 "
                    f"ORDER BY orders DESC LIMIT 20"))
    if not use(sec, rec) or not rec["rows"]:
        return
    sec["tables"].append(table(
        "files_cancel", f"Cancelled orders by who cancelled, {run.month.label}",
        ["Marketplace", "Cancelled by", "Orders"],
        [[L.platform_label(r.get("marketplace")), str(r.get("cancel_by")).capitalize(),
          num(r.get("orders"))] for r in rec["rows"]], rec["key"]))


def sec_files_supply(run):
    sec = section("files_supply", "D", "Purchase orders and fill rate, from the attached file")
    start, end = run.month.shift(-2), run.month
    rec = run.query(
        "files_po", "upload_po", ("created_date", "gr_date", "ordered_quantity", "delivered_quantity"),
        lambda dl: (
            "SELECT CAST(strftime(created_date, '%Y%m') AS INTEGER) AS month_id, "
            "COUNT(DISTINCT reference_number) AS pos, SUM(COALESCE(ordered_quantity, 0)) AS ordered, "
            "SUM(COALESCE(delivered_quantity, 0)) AS delivered, "
            "SUM(COALESCE(open_quantity, 0)) AS open_qty, "
            f"AVG(CASE WHEN gr_date IS NOT NULL THEN {_days('created_date', 'gr_date')} END) AS lead_days "
            f"FROM upload_po WHERE created_date BETWEEN DATE '{start.first.isoformat()}' "
            f"AND DATE '{end.last.isoformat()}' GROUP BY 1 ORDER BY 1"))
    if not use(sec, rec) or not rec["rows"]:
        return degrade(sec)
    rows = [[L.month_label(int(num(r.get("month_id")))), num(r.get("pos")), num(r.get("ordered")),
             num(r.get("delivered")), pct(r.get("delivered"), r.get("ordered")),
             num(r.get("lead_days")), num(r.get("open_qty"))] for r in rec["rows"]]
    sec["tables"].append(table(
        "files_po", "Purchase orders by month created",
        ["Month", "Purchase orders", "Units ordered", "Units delivered", "Fill rate %",
         "Days to goods receipt", "Units still open"], rows, rec["key"]))
    last = next((r for r in rows if r[0] == end.label), rows[-1])
    sec["headline"] = (f"Suppliers delivered {share_text(last[4])} of the units ordered in "
                       f"{last[0]}, received {last[5]:.1f} days after the order on average")
    sec["facts"].append(fact(sec["headline"] + ".", rec["key"]))
    return degrade(sec)


def sec_files_srp(run):
    sec = section("files_srp", "C", "Selling price against SRP, from the attached files")
    cur = run.month
    if not run.ref(ORDERS):
        sec["missing"].append(missing(
            "order exports for the selling price", "the price paid per unit is compared with the "
            "SRP", f"the order exports of {cur.label} attached to the report"))
        return degrade(sec)
    rec = run.query(
        "files_srp", "upload_srp", ("sku", "price_date", "srp"),
        lambda dl: (
            "WITH srp AS (SELECT UPPER(TRIM(sku)) AS sku, MAX(product_name) AS product_name, "
            "AVG(srp) AS srp FROM upload_srp WHERE srp > 0 AND price_date BETWEEN "
            f"DATE '{cur.first.isoformat()}' AND DATE '{cur.last.isoformat()}' GROUP BY 1), "
            "sold AS (SELECT UPPER(TRIM(seller_sku)) AS sku, SUM(quantity) AS units, "
            "SUM(item_value_after_seller_discount) AS value FROM upload_orders "
            f"WHERE month_id = {cur.mid} AND {_PROCESSED} AND quantity > 0 GROUP BY 1) "
            "SELECT srp.sku, srp.product_name, srp.srp, sold.units, sold.value / sold.units AS price "
            "FROM srp JOIN sold ON sold.sku = srp.sku ORDER BY sold.value DESC LIMIT 30"))
    if not use(sec, rec):
        return degrade(sec)
    if not rec["rows"]:
        sec["missing"].append(missing(
            "order SKUs that match the SRP master", "without a match no price can be compared",
            "seller SKUs in the order exports written as the SRP master's Acme SKU",
            detail="upload_orders.seller_sku does not join upload_srp.sku for this month"))
        return degrade(sec)
    rows = [[r.get("sku"), L.short_product(r.get("product_name"), run.brand), num(r.get("srp")),
             num(r.get("price")), change(r.get("price"), r.get("srp")), num(r.get("units"))]
            for r in rec["rows"]]
    sec["tables"].append(table(
        "files_srp", f"Average selling price per unit against SRP, {cur.label}",
        ["SKU", "Product", "SRP", "Selling price", "vs SRP %", "Units"], rows, rec["key"],
        deck_columns=[1, 2, 3, 4, 5]))
    units = sum(r[5] for r in rows)
    gap = sum(r[5] * (r[4] or 0) for r in rows) / units if units else 0
    sec["headline"] = (f"Products sold {abs(gap):.1f}% {'below' if gap < 0 else 'above'} SRP on "
                       f"average in {cur.label}, weighted by units")
    sec["facts"].append(fact(sec["headline"] + ".", rec["key"]))
    return degrade(sec)


_KIND = {"orders": "Orders", "product": "Product performance", "affiliate": "Affiliate",
         "live": "LIVE", "video": "Video", "wallet": "Settlement", "inventory": "Inventory",
         "po": "Purchase orders", "srp": "SRP master"}


def sec_files_used(run):
    sec = section("files_used", "Appendix", "Attached files read for this review")
    up = run.uploads or {}
    if up.get("error"):
        sec["status"] = "failed"
        sec["missing"].append(missing(
            "the attached files", "the pages that read them show the warehouse only",
            "attach the exports again; the technical detail is in the open items",
            detail=up["error"]))
        return sec
    files = up.get("files") or []
    rows = [[f.get("path"), _KIND.get(f.get("kind"), "Not recognized"), f.get("marketplace") or "",
             L.month_label(f.get("month")) if f.get("month") else "", num(f.get("rows")),
             "Yes" if f.get("used") else "No", f.get("reason") or ""] for f in files]
    sec["tables"].append(table("files_used", "Files and what was read from them",
                               ["File", "Export", "Marketplace", "Month", "Rows", "Used", "Note"],
                               rows, "files_manifest", deck_columns=[1, 2, 3, 4, 5]))
    used = sum(1 for f in files if f.get("used"))
    replaced = [L.source_label(t) for t in up.get("replaced") or []]
    sec["headline"] = f"{used} of {len(files)} attached files were read for this review"
    sec["facts"].append(fact(sec["headline"] + ". The files are deleted when the review is done.",
                             "files_manifest"))
    if replaced:
        sec["facts"].append(fact("Pages that read the files instead of the warehouse: "
                                 + ", ".join(sorted(set(replaced))) + ".", "files_manifest"))
    return sec


def file_query_keys(run) -> set:
    return {k for k, q in run.queries.items() if q.get("ok") and q.get("datasource") == SOURCE_NAME}


def mark_file_pages(sections: list, run) -> None:
    """Say on every page which figures come from the attached files."""
    keys = file_query_keys(run)
    if not keys:
        return
    for sec in sections:
        hit = False
        for t in sec.get("tables") or []:
            if set(str(t.get("source") or "").replace(" ", "").split(",")) & keys:
                t["title"] = f"{t['title']} (attached files)"
                hit = True
        if hit and sec["id"] != "files_used":
            sec["facts"].append(fact(_FILE_NOTE, "files_manifest"))
            sec["from_files"] = True


def pages(run) -> list:
    """The file page builders that have something to read; [] when no file was attached."""
    if not run.uploads:
        return []
    out = [sec_files_used]
    if run.ref(ORDERS):
        out += [sec_files_orders, sec_files_delivery]
    if run.ref("upload_srp"):
        out.append(sec_files_srp)
    if run.ref("upload_po"):
        out.append(sec_files_supply)
    return out
