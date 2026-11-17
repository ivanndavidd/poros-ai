"""The tables marketplace exports are normalized into, for one MBR run only.

The person attaches the exports they already download (orders, product performance,
affiliate, LIVE, video, settlement, inventory, purchase orders, SRP master). The parsers
turn each file into rows of the tables below; the report engine loads them into a
throwaway DuckDB file and reads them through the same governed path as the warehouse.

Tables named like a warehouse table (``acme_store_*``) carry the columns the MBR pages
read from that table, with the same meaning, so a page reads the files when the
warehouse has nothing for the brand. ``upload_*`` tables hold what the warehouse does not
have at all (shipping per order, delivery times, warehouse, purchase orders, SRP).

Rules every parser keeps:
  * no buyer identity: names, usernames, phone numbers, addresses, emails, tracking
    numbers and notes are never copied into a row (city and province are kept, they are
    what the geography page groups by);
  * money in rupiah as a float, counts as a float, percentages on the PERCENT scale
    (4.16 means 4.16%), dates as ``datetime.date`` and times as ``datetime.datetime``;
  * ``marketplace`` is one of MARKETPLACES; ``month_id`` is YYYYMM as an int;
  * the brand is not read from the files: the loader stamps the report's brand on every
    table that has a ``brand`` column (the person attached the files for that brand).
"""
from __future__ import annotations

from dataclasses import dataclass, field

MARKETPLACES = ("Shopee", "TikTok", "Tokopedia", "Lazada", "Blibli")

# Order status groups, the same names as mbr_config.STATUS_GROUPS where they overlap.
STATUS_GROUPS = ("processed", "cancelled", "unpaid", "returned", "in_flight", "other")

T, F, I, D, TS = "VARCHAR", "DOUBLE", "BIGINT", "DATE", "TIMESTAMP"

TABLES: dict[str, tuple[tuple[str, str], ...]] = {
    # ---- same meaning as the warehouse table of the same name ----
    "acme_store_product_mp": (
        ("brand", T), ("month_id", I), ("marketplace", T), ("product_sku", T),
        ("product_name", T),
        ("total_views_page", F),       # product page views
        ("cvr", F),                    # conversion, percent scale
        ("sales_revenue", F),          # product GMV / sales in rupiah
        ("total_selling_product", F),  # units sold
        ("orders", F), ("impressions", F), ("clicks", F), ("add_to_cart", F),
        ("visitors", F),
    ),
    "acme_store_affiliatetrx_mp": (
        ("brand", T), ("month_id", I), ("marketplace", T), ("order_id", T),
        ("content_type", T),           # lowercase: live | video | showcase | other
        ("creator_username", T),       # the creator's public handle (a business partner)
        ("total_harga_item_pesanan", F),   # item value of the line in rupiah
        ("commission", F), ("order_status", T), ("product_name", T),
    ),
    "acme_store_live_mp": (
        ("brand", T), ("marketplace", T), ("live_date", D), ("gmv", F), ("orders", F),
        ("viewers", F), ("units", F), ("owner", T),   # owner: official | affiliate
        ("creator", T),
    ),
    "acme_store_video_mp": (
        ("brand", T), ("marketplace", T), ("video_date", D), ("id_video", T), ("gmv", F),
        ("views", F), ("orders", F), ("owner", T), ("creator", T),
    ),
    "acme_store_wallet_mp": (
        ("brand", T), ("month_id", I), ("marketplace", T),
        ("wallet_gmv", F),              # item value after seller discount that settled
        ("revenue_wallet", F),          # revenue the settlement counts
        ("mp_fee", F), ("affiliate_fee", F), ("commission_fee", F), ("os_fee", F),
        ("dynamic_fee", F), ("order_processing_fee", F), ("promotion_fee", F),
        ("platform_subsidi", F), ("seller_voucher", F), ("shipping_fee", F),
        ("other_fee", F),
        ("total_settlement_amount", F),  # paid out to the seller
    ),
    # ---- only the files have these ----
    "upload_orders": (
        ("marketplace", T), ("month_id", I), ("order_id", T), ("line_id", T),
        ("order_status", T), ("status_group", T), ("created_at", TS), ("paid_at", TS),
        ("shipped_at", TS), ("delivered_at", TS), ("completed_at", TS), ("cancelled_at", TS),
        ("cancel_by", T), ("cancel_reason", T), ("seller_sku", T), ("platform_sku", T),
        ("product_name", T), ("variation", T), ("quantity", F),
        ("unit_price_original", F),
        ("item_value_before_discount", F),     # price x quantity before any discount
        ("seller_discount", F), ("platform_discount", F),
        ("item_value_after_seller_discount", F),
        ("shipping_fee_buyer", F),             # shipping the buyer paid
        ("shipping_fee_original", F),          # shipping before discounts
        ("shipping_discount_seller", F), ("shipping_discount_platform", F),
        ("order_amount", F),                   # what the buyer paid for the order line/order
        ("payment_method", T), ("warehouse", T), ("province", T), ("city", T),
        ("order_channel", T), ("creator_handle", T), ("fulfilment_type", T),
    ),
    "upload_inventory": (
        ("report_date", D), ("sku", T), ("item_code", T), ("item_name", T),
        ("available_qty", F), ("hpp", F), ("order_qty_14", F), ("order_qty_30", F),
        ("doi_30", F), ("recommended_action", T), ("sales_contribution", F),
    ),
    "upload_po": (
        ("created_date", D), ("gr_date", D), ("supplier_code", T), ("reference_number", T),
        ("sku", T), ("sku_name", T), ("warehouse", T), ("ordered_quantity", F),
        ("delivered_quantity", F), ("open_quantity", F),
    ),
    "upload_srp": (
        ("sku", T), ("product_name", T), ("brand_name", T), ("product_type", T),
        ("composition", T), ("price_date", D), ("srp", F),
    ),
}

BRANDED = tuple(t for t, cols in TABLES.items() if any(c == "brand" for c, _ in cols))

# Header words that mark a column as buyer identity: a parser never maps them, and the
# loader refuses a row dict that carries any column outside its table.
PII_HEADER_WORDS = (
    "nama penerima", "recipient", "buyer username", "username (pembeli)", "nama pemesan",
    "no. telepon", "phone", "alamat", "address", "email", "no. resi", "tracking",
    "catatan", "note", "buyer message", "customername", "customer name", "zipcode",
    "kode pos", "detail address",
)


@dataclass
class FileResult:
    """What one file (or one entry of a zip) became."""
    path: str
    kind: str = ""              # orders | product | affiliate | live | video | wallet |
    #                             inventory | po | srp | "" when not recognized
    marketplace: str = ""
    month: str = ""             # "YYYY-MM" when the file covers one month
    table: str = ""             # the table its rows went to
    rows: int = 0
    used: bool = False
    reason: str = ""            # why a file was not used, in plain words
    warnings: list = field(default_factory=list)


@dataclass
class Bundle:
    """Every table's rows plus what happened to each file."""
    tables: dict = field(default_factory=dict)      # table -> [row dict]
    files: list = field(default_factory=list)       # [FileResult]

    def add(self, table: str, rows: list) -> None:
        if table not in TABLES:
            raise ValueError(f"unknown upload table {table}")
        allowed = {c for c, _ in TABLES[table]}
        for row in rows:
            extra = set(row) - allowed
            if extra:
                raise ValueError(f"{table} rows carry columns outside the table: {sorted(extra)}")
        self.tables.setdefault(table, []).extend(rows)
