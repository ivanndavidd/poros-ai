"""Order exports (Shopee, TikTok/Tokopedia, Lazada, Blibli) -> upload_orders.

One row per order LINE. Money is in rupiah; discounts are stored as positive amounts
(Lazada and Blibli write them negative, Shopee and TikTok positive), and
``item_value_after_seller_discount`` is always ``item_value_before_discount - seller_discount``
so the four marketplaces mean the same thing by it.

Order-level money: Shopee and TikTok repeat the order's shipping and total payment on every
line of a multi-line order. Those amounts are kept on the FIRST line of each order only
(the other lines get None), so a SUM over lines counts each order once. Lazada splits its
shipping per item and Blibli exports per item, so their rows are kept as they are.
"""
from __future__ import annotations

import datetime as dt
import re

from .. import labels as L
from . import values as V
from .schema import TABLES
from .sheets import Spec, col, records

ORDER_LEVEL = ("shipping_fee_buyer", "shipping_fee_original", "shipping_discount_seller",
               "shipping_discount_platform", "order_amount")
TIMES = ("created_at", "paid_at", "shipped_at", "delivered_at", "completed_at", "cancelled_at")
_DIGIT = r"\d"
_DMY_TEXT = re.compile(r"^\s*(\d{1,2})[/-](\d{1,2})[/-]\d{4}")

SHOPEE = Spec("shopee_orders", "orders", "Shopee", {
    "order_id": col("No. Pesanan"),
    "order_status": col("Status Pesanan"),
    "returned_qty": col("Returned quantity", kind="num"),
    "cancel_text": col("Alasan Pembatalan"),
    "created_at": col("Waktu Pesanan Dibuat", kind="raw"),
    # KNOS "RAW OBI" files split the order time into a date and a time column.
    "created_date": col("Date", "Tanggal", kind="raw"),
    "created_time": col("Time", kind="raw"),
    "paid_at": col("Waktu Pembayaran Dilakukan", kind="raw"),
    "shipped_at": col("Waktu Pengiriman Diatur", kind="raw"),
    "completed_at": col("Waktu Pesanan Selesai", kind="raw"),
    "seller_sku": col("Nomor Referensi SKU", "SKU Induk"),
    "product_name": col("Nama Produk"),
    "variation": col("Nama Variasi"),
    "unit_price_original": col("Harga Awal", kind="num"),
    "quantity": col("Jumlah", kind="num"),
    "seller_discount": col("Diskon Dari Penjual", kind="num"),
    "platform_discount": col("Diskon Dari Shopee", kind="num"),
    "shipping_fee_buyer": col("Ongkos Kirim Dibayar oleh Pembeli", kind="num"),
    "shipping_fee_original": col("Perkiraan Ongkos Kirim", kind="num"),
    "shipping_discount_platform": col("Estimasi Potongan Biaya Pengiriman", kind="num"),
    "order_amount": col("Total Pembayaran", kind="num"),
    "warehouse": col("Nama Gudang"),
    "payment_method": col("Metode Pembayaran"),
    "city": col("Kota/Kabupaten"),
    "province": col("Provinsi"),
    "fulfilment_type": col("Fulfillment Service"),
}, required=("order_id", "order_status", "quantity", "unit_price_original"),
    key="order_id", key_pattern=_DIGIT)

TIKTOK = Spec("tiktok_orders", "orders", "TikTok", {
    "order_id": col("Order ID"),
    "order_status": col("Order Status"),
    "substatus": col("Order Substatus"),
    "returned_qty": col("Sku Quantity of return", kind="num"),
    "platform_sku": col("SKU ID"),
    "seller_sku": col("Seller SKU"),
    "product_name": col("Product Name"),
    "variation": col("Variation"),
    "quantity": col("Quantity", kind="num"),
    "unit_price_original": col("SKU Unit Original Price", kind="num"),
    "item_value_before_discount": col("SKU Subtotal Before Discount", kind="num"),
    "platform_discount": col("SKU Platform Discount", kind="num"),
    "seller_discount": col("SKU Seller Discount", kind="num"),
    "shipping_fee_buyer": col("Shipping Fee After Discount", kind="num"),
    "shipping_fee_original": col("Original Shipping Fee", kind="num"),
    "shipping_discount_seller": col("Shipping Fee Seller Discount", kind="num"),
    "shipping_discount_platform": col("Shipping Fee Platform Discount", kind="num"),
    "order_amount": col("Order Amount", kind="num"),
    "created_at": col("Created Time", kind="raw"),
    "created_date": col("Date", "Tanggal", kind="raw"),
    "created_time": col("Time", kind="raw"),
    "paid_at": col("Paid Time", kind="raw"),
    "shipped_at": col("Shipped Time", kind="raw"),
    "delivered_at": col("Delivered Time", kind="raw"),
    "cancelled_at": col("Cancelled Time", kind="raw"),
    "cancel_by": col("Cancel By"),
    "cancel_reason": col("Cancel Reason"),
    "fulfilment_type": col("Fulfillment Type"),
    "warehouse": col("Warehouse Name"),
    "province": col("Province"),
    "city": col("Regency and City"),
    "payment_method": col("Payment Method"),
    "channel": col("Purchase Channel"),                 # TikTok | Tokopedia, per order
    "order_channel": col("Order Channel"),
    "creator_handle": col("Creator Handle"),
}, required=("order_id", "order_status", "quantity", "unit_price_original"),
    key="order_id", key_pattern=_DIGIT)

LAZADA = Spec("lazada_orders", "orders", "Lazada", {
    "order_id": col("orderNumber"),
    "line_id": col("orderItemId"),
    "order_status": col("status"),
    "seller_sku": col("sellerSku"),
    "platform_sku": col("lazadaSku"),
    "product_name": col("itemName"),
    "variation": col("variation"),
    "created_at": col("createTime", kind="raw"),
    "delivered_at": col("deliveredDate", kind="raw"),
    "unit_price_original": col("unitPrice", kind="num"),
    "seller_discount": col("sellerDiscountTotal", kind="num"),
    "platform_discount": col("platformDiscountTotal", kind="num"),
    "shipping_fee_buyer": col("shippingFee", kind="num"),
    "order_amount": col("paidPrice", kind="num"),
    "payment_method": col("payMethod"),
    "warehouse": col("wareHouse"),
    "city": col("shippingCity"),
    "city_billing": col("billingCity"),                 # used when shippingCity is masked
    "province": col("shippingRegion"),
    "cancel_by": col("buyerFailedDeliveryReturnInitiator"),
    "cancel_reason": col("buyerFailedDeliveryReason"),
}, required=("order_id", "line_id", "order_status", "unit_price_original"),
    key="order_id", key_pattern=_DIGIT)

BLIBLI = Spec("blibli_orders", "orders", "Blibli", {
    "order_id": col("Nomor pesanan", "No. Order"),
    "line_id": col("Nomor Item pesanan", "No. Order Item"),
    "created_at": col("Tanggal pesanan", "Tanggal Order", kind="raw"),
    "shipped_at": col("Tanggal pengiriman", kind="raw"),
    "seller_sku": col("SKU seller", "Merchant SKU"),
    "platform_sku": col("SKU Blibli", "Blibli SKU"),
    "product_name": col("Nama produk"),
    "quantity": col("Total produk", "Total Barang", kind="num"),
    "unit_price_original": col("Harga produk", "Harga item pesanan", kind="num"),
    "item_value_before_discount": col("Total harga produk", "Total harga item pesanan",
                                      kind="num"),
    "order_amount": col("Total", kind="num"),
    "platform_discount": col("Diskon produk ditanggung Blibli", kind="num"),
    "seller_discount": col("Diskon produk ditanggung Seller", "Diskon", kind="num"),
    "shipping_discount_platform": col("Diskon ongkir ditanggung Blibli", kind="num"),
    "shipping_discount_seller": col("Diskon ongkir ditanggung Seller", kind="num"),
    "order_status": col("Status pesanan", "Order Status"),
    "cancel_by": col("Dibatalkan oleh"),
    "cancel_reason": col("Alasan pembatalan"),
    "return_status": col("Status retur", "Status pengembalian"),
    "city": col("Kota"),
    "province": col("Provinsi"),
    "warehouse": col("Lokasi pengambilan"),
    "fulfilment_type": col("Metode pengiriman"),
}, required=("order_id", "line_id", "order_status", "unit_price_original"),
    key="order_id", key_pattern=_DIGIT)

SPECS = [SHOPEE, TIKTOK, LAZADA, BLIBLI]
_REPEATS_ORDER_MONEY = ("Shopee", "TikTok")

# ---- status groups ----
# Checked in this order: the first phrase found wins. Shopee's "Pesanan diterima, namun
# Pembeli masih dapat mengajukan pengembalian ..." (received, return window still open) is
# a delivered order, so it is tested before the return words it contains.
_STATUS_WORDS = (
    ("processed", ("pesanan diterima",)),
    ("cancelled", ("batal", "cancel", "failed", "gagal")),
    ("unpaid", ("belum bayar", "belum dibayar", "unpaid", "menunggu pembayaran",
                "pending payment", "awaiting payment")),
    ("returned", ("pengembalian", "retur", "return", "refund", "dikembalikan")),
    ("processed", ("selesai", "completed", "complete", "delivered", "terkirim", "diterima",
                   "confirmed", "finished")),
    ("in_flight", ("dikirim", "to ship", "ready to ship", "ready_to_ship", "shipped",
                   "shipping", "transit", "awaiting", "menunggu", "dalam pengiriman",
                   "diproses", "processing", "packed", "pending")),
)


def status_group(status) -> str:
    """processed | cancelled | unpaid | returned | in_flight | other, from the raw status."""
    s = str(status or "").strip().lower()
    if not s:
        return "other"
    for group, words in _STATUS_WORDS:
        if any(w in s for w in words):
            return group
    return "other"


def _returned_after(rec: dict, group: str) -> bool:
    """A delivered or shipping LINE that went back: every unit returned (Shopee, TikTok
    'Returned quantity'), or a finished return (Blibli 'Status retur' FINISHED)."""
    if group not in ("processed", "in_flight"):
        return False
    qty, back = rec.get("quantity"), rec.get("returned_qty")
    if qty and back and back >= qty:
        return True
    return str(rec.get("return_status") or "").strip().lower() == "finished"


def _group(rec: dict) -> str:
    group = status_group(rec.get("order_status"))
    sub = status_group(rec.get("substatus")) if rec.get("substatus") else ""
    if group == "in_flight" and sub == "processed":
        group = "processed"         # TikTok "Dikirim" with substatus "Terkirim" (delivered)
    return "returned" if _returned_after(rec, group) else group


# ---- cancellation ----
_ACTORS = (("buyer", ("pembeli", "buyer", "user", "customer", "pelanggan")),
           ("seller", ("penjual", "seller", "merchant")),
           ("system", ("sistem", "system", "otomatis", "platform")))


def _actor(text) -> str | None:
    t = str(text or "").strip().lower()
    if not t:
        return None
    for name, words in _ACTORS:
        if any(w in t for w in words):
            return name
    return V.text(text)


def _shopee_cancel(text) -> tuple[str | None, str | None]:
    """'Dibatalkan oleh Pembeli. Alasan: X' -> ('buyer', 'X')."""
    t = V.text(text)
    if not t:
        return None, None
    by, _, reason = t.partition("Alasan:")
    return _actor(by), (reason.strip() or t)


# ---- dates ----
class _Dates:
    """Times per column, with one repair: a workbook re-saved by Excel in a month-first
    locale turns '05/09/2026' (5 Sep) into a 9 May date number while '25/09/2026' stays
    text. When one column holds both, the date numbers have day and month swapped back."""

    def __init__(self):
        self.serial_rows: dict[str, list] = {}
        self.dmy_proof: set[str] = set()

    def read(self, name: str, raw, row: dict, extra_time=None):
        ts = V.timestamp(raw)
        if ts is None:
            return None
        if V.is_serial(raw):
            self.serial_rows.setdefault(name, []).append(row)
        elif isinstance(raw, str):
            m = _DMY_TEXT.match(raw)
            if m and int(m.group(1)) > 12:
                self.dmy_proof.add(name)
        if extra_time is not None and ts.time() == dt.time(0) and isinstance(extra_time, float) \
                and 0 <= extra_time < 1:
            ts = ts + dt.timedelta(days=extra_time)       # the separate "Time" column
        return ts

    def repair(self) -> int:
        fixed = 0
        for name in self.dmy_proof:
            for row in self.serial_rows.get(name, ()):
                ts = row.get(name)
                if ts and ts.day <= 12:
                    row[name] = ts.replace(month=ts.day, day=ts.month)
                    fixed += 1
        return fixed


def _set_times(rec: dict, out: dict, dates: _Dates) -> None:
    created = rec.get("created_at")
    if created is None:
        created = rec.get("created_date")
    out["created_at"] = dates.read("created_at", created, out, rec.get("created_time"))
    for name in TIMES[1:]:
        if name in rec:
            out[name] = dates.read(name, rec.get(name), out)


# ---- one line ----
def _positive(v):
    return abs(v) if v is not None else None


def _money(rec: dict, out: dict, marketplace: str) -> None:
    qty = rec.get("quantity")
    if marketplace == "Lazada" and qty is None:
        qty = 1.0                                   # Lazada writes one row per unit
    unit = rec.get("unit_price_original")
    before = rec.get("item_value_before_discount")
    if before is None and unit is not None and qty is not None:
        before = unit * qty
    seller = _positive(rec.get("seller_discount"))
    platform = _positive(rec.get("platform_discount"))
    out.update(quantity=qty, unit_price_original=unit, item_value_before_discount=before,
               seller_discount=seller, platform_discount=platform)
    if before is not None:
        out["item_value_after_seller_discount"] = before - (seller or 0.0)
    for name in ORDER_LEVEL:
        if name in rec:
            out[name] = _positive(rec[name]) if name.startswith("shipping_discount") else rec[name]


def _marketplace(rec: dict, spec: Spec) -> str:
    if spec is TIKTOK:
        return "Tokopedia" if "tokopedia" in str(rec.get("channel") or "").lower() else "TikTok"
    return spec.marketplace


def _city(rec: dict) -> str | None:
    city = rec.get("city")
    if city and "*" in city:                        # Lazada masks the shipping city
        city = rec.get("city_billing")
    return city


_COPIED = ("order_id", "line_id", "order_status", "seller_sku", "platform_sku", "product_name",
           "variation", "payment_method", "warehouse", "province", "order_channel",
           "creator_handle", "fulfilment_type", "cancel_reason")


def _line(rec: dict, spec: Spec, columns: tuple, dates: _Dates) -> dict:
    out = dict.fromkeys(columns)
    out.update({k: rec.get(k) for k in _COPIED if k in rec})
    out["marketplace"] = _marketplace(rec, spec)
    out["status_group"] = _group(rec)
    out["city"] = _city(rec)
    if V.text(out.get("province")):         # one spelling, so a GROUP BY province adds up
        out["province"] = L.province_label(out["province"])
    if spec is SHOPEE:
        out["cancel_by"], out["cancel_reason"] = _shopee_cancel(rec.get("cancel_text"))
    else:
        out["cancel_by"] = _actor(rec.get("cancel_by"))
    _set_times(rec, out, dates)
    _money(rec, out, out["marketplace"])
    return out


def build(match, ctx) -> tuple[list[dict], list[str]]:
    """Rows of upload_orders from one matched order sheet, plus warnings."""
    columns = tuple(c for c, _ in TABLES["upload_orders"])
    spec = match.spec
    dates = _Dates()
    rows: list[dict] = []
    lines_per_order: dict[str, int] = {}
    for rec in records(match):
        out = _line(rec, spec, columns, dates)
        n = lines_per_order.get(out["order_id"], 0) + 1
        lines_per_order[out["order_id"]] = n
        if not out.get("line_id"):
            out["line_id"] = f"{out['order_id']}-{n}"
        if n > 1 and spec.marketplace in _REPEATS_ORDER_MONEY:
            for name in ORDER_LEVEL:
                out[name] = None       # the order's money already sits on its first line
        rows.append(out)
    warnings = []
    fixed = dates.repair()
    if fixed:
        warnings.append(f"{fixed} dates were stored month-first by Excel and were read back "
                        "day-first")
    for out in rows:
        created = out.get("created_at")
        out["month_id"] = V.month_id(created) or ctx.month_id
    return rows, warnings
