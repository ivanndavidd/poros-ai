"""Inventory, purchase-order and SRP master files -> upload_inventory, upload_po, upload_srp.

These come from Acme's own systems, not from a marketplace, so their headers are stable
column names (``reporting_date``, ``orderqty14``). The SRP master is the odd one: one
sheet per month, the SRP block spread over one column per DAY, so one product row becomes
one row per day that carries a price.
"""
from __future__ import annotations

import datetime as dt
import itertools
import re

from . import values as V
from .schema import TABLES
from .sheets import Spec, col, match_columns, records

_TOP_SHARE = re.compile(r"(\d{1,6}(?:[.,]\d{1,6})?) {0,3}%")
_YEAR = re.compile(r"(?<!\d)(20\d\d)(?!\d)")


def _cell(row, i):
    return row[i] if i < len(row) else None


def _blank(table: str) -> dict:
    return dict.fromkeys(c for c, _ in TABLES[table])


# ---- inventory ----
INVENTORY = Spec("inventory", "inventory", "", {
    "report_date": col("reporting_date", "report_date", kind="date"),
    "sku": col("SKU"),
    "item_code": col("item_code"),
    "item_name": col("item_name"),
    "recommended_action": col("recommended action", "recommended_action"),
    "hpp_accurate": col("hpp_accurate", kind="num"),
    "hpp_after_disc": col("hpp_after_disc num", "hpp_after_disc", kind="num"),
    "sales_contribution": col("Sales Contribution (value)", "sales_contribution", kind="raw"),
    "available_qty": col("available_qty", kind="num"),
    "order_qty_14": col("orderqty14", "order_qty_14", kind="num"),
    "order_qty_30": col("orderqty30", "order_qty_30", kind="num"),
    "doi_30": col("doi_30", kind="num"),
}, required=("sku", "available_qty"), key="sku")


def sales_contribution(value) -> float | None:
    """'Top 080%' -> 80.0: the SKU sits inside the products making the top 80% of sales
    (a Pareto class on the percent scale, not the SKU's own share)."""
    if isinstance(value, (int, float)):
        return float(value)
    m = _TOP_SHARE.search(str(value or ""))
    return float(m.group(1).replace(",", ".")) if m else None


def build_inventory(match, ctx) -> tuple[list[dict], list[str]]:  # NOSONAR: every builder takes (match, ctx)
    rows = []
    for rec in records(match):
        out = _blank("upload_inventory")
        for k in ("report_date", "sku", "item_code", "item_name", "recommended_action",
                  "available_qty", "order_qty_14", "order_qty_30", "doi_30"):
            out[k] = rec.get(k)
        # The accurate HPP when finance filled it, else the after-discount purchase price.
        hpp = rec.get("hpp_accurate")
        out["hpp"] = hpp if hpp else rec.get("hpp_after_disc")
        out["sales_contribution"] = sales_contribution(rec.get("sales_contribution"))
        rows.append(out)
    return rows, []


# ---- purchase orders ----
PO = Spec("po", "po", "", {
    "created_date": col("created_date", kind="date"),
    "gr_date": col("gr_date", kind="date"),
    "supplier_code": col("supplier_code"),
    "reference_number": col("reference_number"),
    "sku": col("warehouse_sku_code", "sku"),
    "sku_name": col("warehouse_sku_name", "sku_name"),
    "warehouse": col("warehouse"),
    "ordered_quantity": col("ordered_quantity", kind="num"),
    "delivered_quantity": col("delivered_quantity", kind="num"),
    "open_quantity": col("open_quantity", kind="num"),
}, required=("reference_number", "sku", "ordered_quantity"), key="reference_number")


def build_po(match, ctx) -> tuple[list[dict], list[str]]:  # NOSONAR: every builder takes (match, ctx)
    rows = []
    for rec in records(match):
        out = _blank("upload_po")
        out.update({k: rec.get(k) for k in out})
        rows.append(out)
    return rows, []


# ---- SRP master ----
SRP = Spec("srp", "srp", "", {
    "sku": col("Acme SKU"),
    "product_name": col("Product Name"),
    "brand_name": col("Brand Name"),
    "product_type": col("Product Type"),
    "composition": col("Composition"),
    "srp": col("SRP Per Date"),
}, required=("sku", "srp"), key="sku")

_SRP_TEXT = ("sku", "product_name", "brand_name", "product_type", "composition")


def year_of(path: str) -> int | None:
    found = _YEAR.findall(path)
    return int(found[-1]) if found else None


def _day_columns(day_row: list, start: int) -> dict[int, int]:
    """{column index: day of month} from the row of day numbers under the SRP block."""
    days = {}
    for i, cell in enumerate(day_row):
        n = V.number(cell) if i >= start else None
        if n is not None and float(n).is_integer() and 1 <= n <= 31:
            days[i] = int(n)
    return days


def _srp_rows(rows, columns: dict, days: dict, year: int, month: int) -> list[dict]:
    out = []
    for row in rows:
        base = {k: V.text(_cell(row, columns[k][0])) for k in _SRP_TEXT if k in columns}
        if not base.get("sku"):
            continue
        for i, day in days.items():
            price = V.number(_cell(row, i))
            when = _safe_date(year, month, day)
            if not price or price <= 0 or when is None:
                continue                  # no SRP that day, or a day the month does not have
            item = _blank("upload_srp")
            item.update(base, price_date=when, srp=price)
            out.append(item)
    return out


def _safe_date(year: int, month: int, day: int) -> dt.date | None:
    try:
        return dt.date(year, month, day)
    except ValueError:
        return None                       # 31 Sep and the like: an empty column of the grid


def build_srp(match, ctx) -> tuple[list[dict], list[str]]:  # NOSONAR: every builder takes (match, ctx)
    """Every month sheet of the workbook, not only the sheet the header was found on."""
    year = year_of(ctx.path)
    if not year:
        return [], ["the year is not in the file name, so the SRP dates cannot be placed"]
    rows, skipped = [], []
    for sheet in ctx.sheets:
        month = V.MONTHS.get(V.norm(sheet.name)[:3]) or V.MONTHS.get(V.norm(sheet.name))
        if not month:
            skipped.append(sheet.name)
            continue
        rows.extend(_srp_sheet(sheet, year, month))
    warnings = ([f"sheets that are not a month were skipped: {', '.join(skipped)}"]
                if skipped else [])
    return rows, warnings


def _srp_sheet(sheet, year: int, month: int) -> list[dict]:
    it = sheet.open()
    head = list(itertools.islice(it, 6))
    for i, row in enumerate(head):
        columns = match_columns(row, SRP)
        if "sku" in columns and "srp" in columns and i + 1 < len(head):
            days = _day_columns(head[i + 1], columns["srp"][0])
            return _srp_rows(itertools.chain(head[i + 2:], it), columns, days, year, month)
    return []
