"""Product performance, affiliate, LIVE and video exports -> their warehouse-shaped tables.

Product reports are one month per file (the month comes from the title row or the name).
TikTok's report repeats GMV/orders/... once per traffic source ("Semua", "LIVE penjual",
"Video penjual", ...): the first block, "Semua" (all sources), is the total that is mapped.
Tokopedia's report has only per-source traffic, so its page views, impressions and unique
visitors are the SUM of the four sources, and its conversion is unique buyers over unique
page views (the same ratio the report shows per source).
"""
from __future__ import annotations

import datetime as dt

from . import values as V
from .detect import SHARED
from .schema import MARKETPLACES, TABLES
from .sheets import Spec, col, records

_DIGIT = r"\d"
SHARED_NOTE = "the file name names both TikTok and Tokopedia"


def _blank(table: str) -> dict:
    return dict.fromkeys(c for c, _ in TABLES[table])


def _marketplace(ctx, default: str) -> str:
    return ctx.marketplace if ctx.marketplace in MARKETPLACES else default


# ---- product performance ----
SHOPEE_PRODUCT = Spec("shopee_product", "product", "Shopee", {
    "product_sku": col("Kode Produk"),
    "product_name": col("Produk"),
    "variation_code": col("Kode Variasi"),       # variation rows repeat their product's row
    "sales_revenue": col("Total Penjualan (Pesanan Dibuat) (IDR)", kind="num"),
    "orders": col("Pesanan Dibuat", kind="num"),
    "total_selling_product": col("Produk (Pesanan Dibuat)", kind="num"),
    "impressions": col("Jumlah Produk Dilihat", kind="num"),
    "clicks": col("Produk Diklik", kind="num"),
    "visitors": col("Pengunjung Produk (Kunjungan)", kind="num"),
    "total_views_page": col("Halaman Produk Dilihat", kind="num"),
    "add_to_cart": col("Dimasukkan ke Keranjang (Produk)", kind="num"),
    "cvr": col("Tingkat Konversi (Pesanan yang Dibuat)", kind="pct"),   # buyers / visitors
}, required=("product_sku", "product_name", "sales_revenue", "total_views_page"),
    key="product_sku", key_pattern=_DIGIT, sheets=(V.norm("Produk dengan Performa Terbaik"),))

TIKTOK_PRODUCT = Spec("tiktok_product", "product", "TikTok", {
    "product_name": col("Nama"),
    "product_sku": col("ID Produk"),
    "sales_revenue": col("GMV", kind="num"),
    "orders": col("Pesanan", kind="num"),
    "total_selling_product": col("Produk terjual", kind="num"),
    "impressions": col("Impresi produk", kind="num"),
    "clicks": col("Klik produk", kind="num"),
    "total_views_page": col("Klik produk", kind="num"),   # a product click opens its page
    "visitors": col("Klik unik", kind="num"),
    "add_to_cart": col("Jumlah tambahkan ke keranjang", kind="num"),
    "cvr": col("CTOR (pesanan SKU)", kind="pct"),          # orders per click
}, required=("product_sku", "product_name", "sales_revenue", "impressions"),
    key="product_sku", key_pattern=_DIGIT)

_SOURCES = ("tab Toko", "dari LIVE", "dari video", "dari kartu produk")
TOKOPEDIA_PRODUCT = Spec("tokopedia_product", "product", "Tokopedia", {
    "product_sku": col("ID"),
    "product_name": col("Produk"),
    "sales_revenue": col("GMV", kind="num"),
    "total_selling_product": col("Produk terjual", kind="num"),
    "orders": col("Pesanan", kind="num"),
    "total_views_page": col(*(f"Tayangan halaman {s}" for s in _SOURCES), kind="num", total=True),
    "impressions": col("Impresi daftar produk tab Toko", "Impresi dari LIVE", "Impresi dari video",
                       "Impresi dari kartu produk", kind="num", total=True),
    "visitors": col(*(f"Tayangan halaman unik {s}" for s in _SOURCES), kind="num", total=True),
    "buyers": col("Pembeli produk unik tab Toko", "Pembeli produk unik dari LIVE",
                  "Pembeli produk unik dari video", "Pembeli unik dari kartu produk",
                  kind="num", total=True),
}, required=("product_sku", "product_name", "sales_revenue", "total_views_page"),
    key="product_sku", key_pattern=_DIGIT)

BLIBLI_PRODUCT = Spec("blibli_product", "product", "Blibli", {
    "product_sku": col("Kode SKU Blibli"),
    "product_name": col("Nama produk"),
    "sales_revenue": col("Penjualan (Rp)", kind="num"),
    "impressions": col("Tampil di pencarian", kind="num"),
    "clicks": col("Klik pencarian", kind="num"),
    "total_views_page": col("Halaman dikunjungi", kind="num"),
    "add_to_cart": col("Tambah ke Bag", kind="num"),
    "total_selling_product": col("Terjual", kind="num"),
    "cvr": col("CVR", kind="pct"),
    "orders": col("Pesanan", kind="num"),
}, required=("product_sku", "product_name", "sales_revenue", "total_views_page"),
    key="product_sku", key_pattern=_DIGIT)

# Lazada (an .xls): the first header cell names the report but its column holds the
# product id; SKU rows sit under their product row and repeat its figures.
LAZADA_PRODUCT = Spec("lazada_product", "product", "Lazada", {
    "product_sku": col("Kinerja Produk"),
    "product_name": col("Nama Produk"),
    "variation_code": col("SKU ID"),
    "visitors": col("Pengunjung", kind="num"),
    "total_views_page": col("Tayangan Halaman Produk", kind="num"),
    "add_to_cart": col("Unit yang ditambahkan ke troli", kind="num"),
    "orders": col("Pesanan", kind="num"),
    "total_selling_product": col("Unit Terjual", kind="num"),
    "sales_revenue": col("Pendapatan", kind="num"),
    "cvr": col("Tingkat Konversi", kind="pct"),               # buyers / visitors
}, required=("product_sku", "product_name", "sales_revenue", "total_views_page"),
    key="product_sku", key_pattern=_DIGIT)

PRODUCT_SPECS = [SHOPEE_PRODUCT, TIKTOK_PRODUCT, TOKOPEDIA_PRODUCT, BLIBLI_PRODUCT,
                 LAZADA_PRODUCT]
_PRODUCT_COPIED = ("product_sku", "product_name", "total_views_page", "cvr", "sales_revenue",
                   "total_selling_product", "orders", "impressions", "clicks", "add_to_cart",
                   "visitors")


def build_product(match, ctx) -> tuple[list[dict], list[str]]:
    spec = match.spec
    rows = []
    for rec in records(match):
        if rec.get("variation_code"):
            continue
        out = _blank("acme_store_product_mp")
        out.update({k: rec.get(k) for k in _PRODUCT_COPIED if k in rec})
        if spec is TOKOPEDIA_PRODUCT and rec.get("visitors"):
            out["cvr"] = 100.0 * (rec.get("buyers") or 0.0) / rec["visitors"]
        # TikTok Shop and Tokopedia share one seller centre and its report layouts, so the
        # layout does not tell which shop a file is: the file name does.
        out["marketplace"] = _marketplace(ctx, spec.marketplace)
        out["month_id"] = ctx.month_id
        rows.append(out)
    warnings = [] if ctx.month_id else ["the month of this report could not be read"]
    if ctx.marketplace == SHARED:
        warnings.append(f"{SHARED_NOTE}; it was read as {spec.marketplace}, the shop this "
                        f"layout usually comes from")
    return rows, warnings


# ---- affiliate ----
SHOPEE_AFFILIATE = Spec("shopee_affiliate", "affiliate", "Shopee", {
    "order_id": col("Kode Pesanan"),
    "order_status": col("Status Pesanan"),
    "ordered_at": col("Waktu Pesanan", kind="ts"),
    "product_name": col("Nama Produk"),
    "creator_username": col("Username Affiliate"),
    "total_harga_item_pesanan": col("Nilai Pembelian(Rp)", kind="num"),
    "commission": col("Estimasi Komisi per Produk(Rp)", kind="num"),
    "content": col("Platform"),                 # Shopeelive-Shopee, Shopeevideo-Shopee, ...
}, required=("order_id", "creator_username", "total_harga_item_pesanan"),
    key="order_id", key_pattern=_DIGIT)

TIKTOK_AFFILIATE = Spec("tiktok_affiliate", "affiliate", "TikTok", {
    "order_id": col("ID Pesanan"),
    "order_status": col("Status Pesanan"),
    "ordered_at": col("Waktu Dibuat", kind="ts"),
    "product_name": col("Produk"),
    "creator_username": col("Nama pengguna kreator"),
    "content": col("Jenis Konten"),
    "total_harga_item_pesanan": col("Payment Amount", kind="num"),
    "commission": col("Perkiraan pembayaran komisi standar",
                      "Perkiraan pembayaran komisi Iklan Toko", kind="num", total=True),
    "platform": col("Platform"),                # TTS | TT_LITE | TOKOPEDIA
}, required=("order_id", "creator_username", "content"), key="order_id", key_pattern=_DIGIT)

# Per-creator totals: no table takes them, the transaction export carries the same month.
SHOPEE_AFFILIATE_SUMMARY = Spec("shopee_affiliate_summary", "affiliate", "Shopee", {
    "creator": col("Username Affiliate", "export_affiliate_username"),
    "gmv": col("Omzet Penjualan(Rp)", "export_sales_affiliate_performance(Rp)", kind="num"),
}, required=("creator", "gmv"), key="creator")
# TikTok has written this summary per creator and for the whole shop, in Indonesian and
# in English; any of them is recognised so the file is reported as a summary.
TIKTOK_AFFILIATE_SUMMARY = Spec("tiktok_affiliate_summary", "affiliate", "TikTok", {
    "creator": col("Creator name", "Creator username"),
    "gmv": col("GMV dari kreator", "GMV dari afiliasi", "Affiliate GMV", kind="num"),
}, required=("gmv",), key="gmv")

AFFILIATE_SPECS = [SHOPEE_AFFILIATE, TIKTOK_AFFILIATE, SHOPEE_AFFILIATE_SUMMARY,
                   TIKTOK_AFFILIATE_SUMMARY]
SUMMARY_SPECS = (SHOPEE_AFFILIATE_SUMMARY, TIKTOK_AFFILIATE_SUMMARY)


def content_type(text) -> str:
    """live | video | showcase | other."""
    t = str(text or "").lower()
    if "live" in t:
        return "live"
    if "video" in t:
        return "video"
    if "showcase" in t or "etalase" in t:
        return "showcase"
    return "other"


def build_affiliate(match, ctx) -> tuple[list[dict], list[str]]:
    spec = match.spec
    rows = []
    for rec in records(match):
        out = _blank("acme_store_affiliatetrx_mp")
        for k in ("order_id", "order_status", "product_name", "creator_username",
                  "total_harga_item_pesanan", "commission"):
            out[k] = rec.get(k)
        out["content_type"] = content_type(rec.get("content"))
        if spec is TIKTOK_AFFILIATE:
            tokopedia = "tokopedia" in str(rec.get("platform") or "").lower()
            out["marketplace"] = "Tokopedia" if tokopedia else "TikTok"
        else:
            out["marketplace"] = spec.marketplace
        out["month_id"] = V.month_id(rec.get("ordered_at")) or ctx.month_id
        rows.append(out)
    return rows, []


# ---- LIVE ----
SHOPEE_LIVE = Spec("shopee_live", "live", "Shopee", {
    "live_date": col("Periode Data", kind="date"),
    "creator": col("User Id"),                  # the shop's own streaming account
    "gmv": col("Penjualan(Pesanan Dibuat)", kind="num"),
    "orders": col("Pesanan(Pesanan Dibuat)", kind="num"),
    "units": col("Produk Terjual(Pesanan Dibuat)", kind="num"),
    "viewers": col("Penonton", kind="num"),
    "active": col("Penonton Aktif", kind="num"),   # only the LIVE export has it
}, required=("live_date", "gmv", "viewers", "active"), key="live_date")

TIKTOK_LIVE = Spec("tiktok_live", "live", "TikTok", {
    "live_date": col("Waktu Live", kind="date"),
    # TikTok's LIVE export labels the @handle "Nama panggilan" (lowercase, no spaces, the
    # same value as "Nama pengguna kreator" in the affiliate export); "Kreator" is the
    # display name and is the fallback.
    "creator": col("Nama panggilan", "Kreator"),
    # Older exports name the same figure "Nilai bruto barang dagangan dari LIVE".
    "gmv": col("GMV dari LIVE (Rp)", "Nilai bruto barang dagangan dari LIVE (Rp)", kind="num"),
    "orders": col("Pesanan Dibayar", "Pesanan SKU dari LIVE", kind="num"),
    "units": col("Produk Terjual", kind="num"),
    "viewers": col("Penonton", kind="num"),
}, required=("live_date", "gmv"), key="live_date")

LIVE_SPECS = [SHOPEE_LIVE, TIKTOK_LIVE]


def owner_of(path: str) -> str | None:
    words = V.norm(path).split()
    if "affiliate" in words or "afiliasi" in words:
        return "affiliate"
    if "official" in words or "resmi" in words:
        return "official"
    return None


def build_live(match, ctx) -> tuple[list[dict], list[str]]:
    rows = []
    for rec in records(match):
        out = _blank("acme_store_live_mp")
        for k in ("live_date", "gmv", "orders", "viewers", "units", "creator"):
            out[k] = rec.get(k)
        out["marketplace"] = _marketplace(ctx, match.spec.marketplace)
        out["owner"] = ctx.owner
        rows.append(out)
    return rows, _owner_warning(ctx)


def _owner_warning(ctx) -> list[str]:
    if ctx.owner:
        return []
    return ["the file name does not say Official or Affiliate, so the owner is left empty"]


# ---- video ----
SHOPEE_VIDEO = Spec("shopee_video", "video", "Shopee", {
    "video_date": col("Periode Data", kind="date"),
    "creator": col("User Id"),
    "gmv": col("Penjualan(Pesanan Dibuat)", kind="num"),
    "orders": col("Pesanan(Pesanan Dibuat)", kind="num"),
    # Exports before Jun 2026 have no plays column; their viewers are the closest figure.
    "views": col("Ditonton", "Penonton", kind="num"),
}, required=("video_date", "gmv", "views"), key="video_date")

TIKTOK_VIDEO = Spec("tiktok_video", "video", "TikTok", {
    "creator": col("Nama Kreator"),
    "id_video": col("ID Video"),
    "video_date": col("Waktu", kind="date"),
    "views": col("VV", kind="num"),
    "gmv": col("GMV dari video (Rp)", "Nilai barang dagangan bruto (Video) (Rp)", kind="num"),
    "orders": col("Pesanan SKU teratribusi", "Pesanan Video", kind="num"),
}, required=("id_video", "gmv", "views"), key="id_video", key_pattern=_DIGIT)

VIDEO_SPECS = [SHOPEE_VIDEO, TIKTOK_VIDEO]


def _period_start(ctx) -> dt.date | None:
    return dt.date(ctx.month_id // 100, ctx.month_id % 100, 1) if ctx.month_id else None


def build_video(match, ctx) -> tuple[list[dict], list[str]]:
    """TikTok's video report has one row per video with that video's sales IN THE REPORT'S
    PERIOD; its date is when the video was posted (often months earlier). The pages sum GMV
    by the month of ``video_date``, so a TikTok row is dated on the first day of the period
    the file covers. Shopee's report is one row per day and keeps its day."""
    rows, warnings = [], _owner_warning(ctx)
    period = _period_start(ctx) if match.spec is TIKTOK_VIDEO else None
    if match.spec is TIKTOK_VIDEO and period is None:
        warnings.append("the period of this report could not be read, so videos are dated "
                        "on the day they were posted")
    for rec in records(match):
        out = _blank("acme_store_video_mp")
        for k in ("video_date", "id_video", "gmv", "views", "orders", "creator"):
            out[k] = rec.get(k)
        out["video_date"] = period or out["video_date"]
        out["marketplace"] = _marketplace(ctx, match.spec.marketplace)
        out["owner"] = ctx.owner
        rows.append(out)
    return rows, warnings
