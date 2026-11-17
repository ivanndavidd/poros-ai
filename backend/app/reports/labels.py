"""Words a business reader sees in the MBR package.

The warehouse speaks in column names (wallet_gmv, os_fee), month ids (202509), Indonesian
status values (Dibatalkan) and listing titles stuffed with promo tags. The deck, the
workbook and the evidence file are read by brand owners and account managers, so every
visible label goes through this module. Query keys and table names stay in the trace
(speaker notes, the Queries sheet, the open items' technical detail) where the data team
looks for them.
"""
from __future__ import annotations

import calendar
import re

NOT_RECORDED = "Not recorded"
COD = "Cash on delivery (COD)"
BALI_NUSA = "Bali and Nusa Tenggara"
MALUKU_PAPUA = "Maluku and Papua"
STORE_TRAFFIC = "Store traffic"


# ---------------------------------------------------------------- data sources
SOURCE_LABELS = {
    "narration": "written by AI from the figures on this page",
    "order_status_values": "Order statuses",
    "orders_monthly": "Orders by month",
    "orders_daily": "Orders by day",
    "orders_next_month": "Orders so far next month",
    "orders_marketplace": "Orders by marketplace",
    "sku_performance": "Sales by product",
    "product_line_mix": "Sales by product line",
    "traffic_conversion": STORE_TRAFFIC,
    "traffic_conversion_brand": STORE_TRAFFIC,
    "traffic_conversion_client": STORE_TRAFFIC,
    "traffic_conversion_store": STORE_TRAFFIC,
    "traffic_conversion_store_name": STORE_TRAFFIC,
    "product_funnel": "Product traffic and conversion",
    "affiliate_monthly": "Affiliate orders",
    "affiliate_top": "Affiliate orders by creator",
    "live_monthly": "LIVE sessions",
    "video_monthly": "Video performance",
    "ads_monthly": "Ads performance",
    "ads_monthly_total": "Ads performance",
    "wallet_monthly": "Marketplace settlements",
    "pnl_monthly": "Profit and loss",
    "fulfilment_days": "Order handling times",
    "fulfilment_days_before": "Order handling times before the takeover",
    "sku_price_range": "Price per unit by product",
    "cancel_reasons": "Cancelled orders",
    "payment_mix": "Orders by payment method",
    "geography": "Orders by province",
    "new_customers": "New customers",
    "new_buyers": "First orders per buyer",
    "cohort_retention": "Customer retention",
    "customer_service": "Customer service chats",
    "inbound": "Warehouse inbound",
    "stock_value": "Stock value",
    "inventory_snapshot_date": "Warehouse stock",
    "inventory_skus": "Warehouse stock by item",
    "l3_orders_monthly": "Acme curated orders",
    "files_orders": "Orders in the attached exports",
    "files_orders_prev": "Orders in the attached exports, last month",
    "files_delivery": "Delivery times in the attached exports",
    "files_province": "Delivery times by province in the attached exports",
    "files_warehouse": "Orders by warehouse in the attached exports",
    "files_cancel": "Cancellations in the attached exports",
    "files_po": "Purchase orders in the attached files",
    "files_po_skus": "Purchase orders by item in the attached files",
    "files_srp": "SRP master and order prices in the attached files",
    "files_manifest": "Attached files",
    "settings": "MBR settings",
    "sections": "the section headlines",
    "definitions": "report definitions",
}


def source_label(key) -> str:
    """'orders_monthly' -> 'Orders by month'; a comma list maps each key."""
    keys = [k.strip() for k in str(key or "").split(",") if k.strip()]
    if not keys:
        return "MBR settings"
    seen = []
    for k in keys:
        label = SOURCE_LABELS.get(k, k.replace("_", " ").capitalize())
        if label not in seen:
            seen.append(label)
    return "; ".join(seen)


def source_line(keys) -> str:
    return source_label(", ".join(keys) if isinstance(keys, (list, tuple)) else keys)


# ---------------------------------------------------------------- months and dates
def month_label(value) -> str:
    """202509 / '202509' / '2025-09' / '2025-09-30' -> 'Sep 2025'; anything else as is."""
    text = str(value if value is not None else "").strip()
    if text.endswith(".0"):
        text = text[:-2]
    m = re.fullmatch(r"(\d{4})-?(\d{2})(?:-\d{2}.*)?", text)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{calendar.month_abbr[int(m.group(2))]} {m.group(1)}"
    return text


def day_label(value) -> str:
    """'2026-09-09' -> '9 Sep'."""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2}).*", str(value or ""))
    if not m:
        return str(value or "")
    return f"{int(m.group(3))} {calendar.month_abbr[int(m.group(2))]}"


def stamp_label(value) -> str:
    """'2026-10-13T07:20:33Z' -> '13 Oct 2026 07:20 UTC'."""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}).*", str(value or ""))
    if not m:
        return str(value or "")
    return (f"{int(m.group(3))} {calendar.month_abbr[int(m.group(2))]} {m.group(1)} "
            f"{m.group(4)}:{m.group(5)} UTC")


# ---------------------------------------------------------------- order statuses
_STATUS = {
    "selesai": "Completed", "dibatalkan": "Cancelled", "batal": "Cancelled",
    "dikirim": "Shipped", "perlu dikirim": "To ship", "belum dibayar": "Unpaid",
    "diproses": "Processing", "dalam pengiriman": "In delivery", "dikemas": "Packed",
    "dikembalikan": "Returned", "pengembalian": "Return or refund", "terkirim": "Delivered",
    "telah dikirim": "Shipped", "sedang dikirim": "In transit", "diterima": "Received",
    "belum bayar": "Unpaid", "order batal": "Cancelled", "canceled": "Cancelled",
    "package returned": "Returned",
}
GROUP_LABELS = {
    "processed": "Counted as sold", "cancelled": "Cancelled", "unpaid": "Not paid",
    "returned": "Returned", "other": "Not counted",
}


_RECEIVED_OPEN = ("pesanan diterima", "masih dapat mengajukan pengembalian")


def status_label(value) -> str:
    key = str(value if value is not None else "").strip().lower()
    if not key:
        return NOT_RECORDED
    if any(w in key for w in _RECEIVED_OPEN):        # Shopee, one spelling per date
        # no comma: status_list joins with commas, and "Received, return window open"
        # read as two statuses next to the plain "Received"
        return "Received (return window open)"
    return _STATUS.get(key, " ".join(key.replace("_", " ").split()).capitalize())


def status_list(values) -> str:
    labels = dict.fromkeys(status_label(v) for v in values or [])
    return ", ".join(labels) or "none"


# ---------------------------------------------------------------- payment, actors, reasons
_PAYMENT = {
    "bayar di tempat": COD, "cod": COD,
    "cash on delivery": COD, "transfer bank": "Bank transfer",
    "bank transfer": "Bank transfer", "cicilan kartu kredit": "Credit card instalment",
    "kartu kredit/debit": "Credit or debit card", "kartu kredit": "Credit card",
    "tiktok shop balance": "TikTok Shop balance", "saldo": "Wallet balance",
    "jago / jago syariah": "Bank Jago", "cash": "Cash", "paylater": "PayLater",
}


def payment_label(value) -> str:
    text = str(value if value is not None else "").strip()
    if not text:
        return NOT_RECORDED
    parts = [p.strip() for p in text.split("+")]
    return " plus ".join(_PAYMENT.get(p.lower(), p) for p in parts if p)


def is_cod(value) -> bool:
    return bool(re.search(r"\bcod\b|bayar di tempat|cash on delivery", str(value or ""), re.I))


_ACTOR = {"user": "Buyer", "buyer": "Buyer", "pembeli": "Buyer", "system": "System",
          "sistem": "System", "seller": "Seller", "penjual": "Seller",
          "seller/fulfillment": "Seller", "fulfillment": "Seller"}


def actor_label(value) -> str:
    key = str(value if value is not None else "").strip().lower()
    return _ACTOR.get(key, str(value).strip().capitalize() if key else NOT_RECORDED)


_REASON = {"change of mind": "Change of mind", "payment overdue": "Payment overdue",
           "others": "Other reasons", "other": "Other reasons",
           "logistics issues": "Logistics issue", "fulfillment issues": "Fulfilment issue",
           "stock unavailable": "Stock unavailable",
           "seller fulfillment issue": "Fulfilment issue"}


def reason_label(value) -> str:
    key = str(value if value is not None else "").strip().lower()
    return _REASON.get(key, str(value).strip().capitalize() if key else NOT_RECORDED)


# Who caused a cancellation, in the reference package's terms. Seller cancellations are
# the ones the store answers for (cancelled by the seller or the fulfilment centre, or
# for a stock or fulfilment reason); a buyer who never paid is not a seller failure even
# when the platform's system closes the order.
ACTOR_GROUPS = ("Seller or fulfilment", "Buyer did not pay", "Buyer", "System, other reasons",
                NOT_RECORDED)


def cancel_group(reason, actor) -> str:
    r = str(reason or "").strip().lower()
    a = str(actor or "").strip().lower()
    if "seller" in a or "fulfil" in a or "penjual" in a or re.search(
            r"fulfil|stock|stok|out of stock|habis", r):
        return "Seller or fulfilment"
    if re.search(r"payment overdue|not paid|unpaid|belum (di)?bayar|pembayaran", r):
        return "Buyer did not pay"
    if a in ("user", "buyer", "pembeli"):
        return "Buyer"
    if a in ("system", "sistem"):
        return "System, other reasons"
    return NOT_RECORDED


# ---------------------------------------------------------------- geography
_UPPER_WORDS = {"dki": "DKI", "di": "DI", "ntb": "NTB", "ntt": "NTT"}
ISLAND = {
    "DKI Jakarta": "Java", "Jawa Barat": "Java", "Banten": "Java", "Jawa Tengah": "Java",
    "DI Yogyakarta": "Java", "Jawa Timur": "Java",
    "Sumatera Utara": "Sumatra", "Riau": "Sumatra", "Sumatera Selatan": "Sumatra",
    "Kepulauan Riau": "Sumatra", "Lampung": "Sumatra", "Sumatera Barat": "Sumatra",
    "Jambi": "Sumatra", "Bangka Belitung": "Sumatra", "Kepulauan Bangka Belitung": "Sumatra",
    "Bengkulu": "Sumatra", "Aceh": "Sumatra",
    "Bali": BALI_NUSA, "Nusa Tenggara Barat": BALI_NUSA,
    "Nusa Tenggara Timur": BALI_NUSA,
    "Kalimantan Barat": "Kalimantan", "Kalimantan Timur": "Kalimantan",
    "Kalimantan Selatan": "Kalimantan", "Kalimantan Tengah": "Kalimantan",
    "Kalimantan Utara": "Kalimantan",
    "Sulawesi Selatan": "Sulawesi", "Sulawesi Tengah": "Sulawesi", "Sulawesi Utara": "Sulawesi",
    "Sulawesi Tenggara": "Sulawesi", "Sulawesi Barat": "Sulawesi", "Gorontalo": "Sulawesi",
    "Maluku": MALUKU_PAPUA, "Maluku Utara": MALUKU_PAPUA, "Papua": MALUKU_PAPUA,
    "Papua Barat": MALUKU_PAPUA, "Papua Tengah": MALUKU_PAPUA,
    "Papua Selatan": MALUKU_PAPUA, "Papua Pegunungan": MALUKU_PAPUA,
    "Papua Barat Daya": MALUKU_PAPUA,
}


# Spellings the marketplaces use for one province: TikTok writes English names, sometimes
# with "Province"; Shopee writes capitals with the short form in brackets; Tokopedia "D.I.".
_PROVINCE_SPELLINGS = {
    "DKI Jakarta": ("jakarta", "jakarta raya", "dki jakarta", "daerah khusus ibukota jakarta"),
    "DI Yogyakarta": ("yogyakarta", "di yogyakarta", "daerah istimewa yogyakarta",
                      "special region of yogyakarta"),
    "Aceh": ("di aceh", "nanggroe aceh darussalam", "nad"),
    "Jawa Barat": ("west java",), "Jawa Tengah": ("central java",), "Jawa Timur": ("east java",),
    "Sumatera Utara": ("north sumatra", "north sumatera"),
    "Sumatera Selatan": ("south sumatra", "south sumatera"),
    "Sumatera Barat": ("west sumatra", "west sumatera"),
    "Kepulauan Riau": ("riau islands",),
    "Kepulauan Bangka Belitung": ("bangka belitung", "bangka belitung islands"),
    "Kalimantan Barat": ("west kalimantan",), "Kalimantan Timur": ("east kalimantan",),
    "Kalimantan Selatan": ("south kalimantan",), "Kalimantan Tengah": ("central kalimantan",),
    "Kalimantan Utara": ("north kalimantan",),
    "Sulawesi Utara": ("north sulawesi",), "Sulawesi Selatan": ("south sulawesi",),
    "Sulawesi Tengah": ("central sulawesi",), "Sulawesi Tenggara": ("southeast sulawesi",),
    "Sulawesi Barat": ("west sulawesi",),
    "Nusa Tenggara Barat": ("west nusa tenggara", "ntb"),
    "Nusa Tenggara Timur": ("east nusa tenggara", "ntt"),
    "Maluku Utara": ("north maluku",), "Papua Barat": ("west papua",),
    "Papua Barat Daya": ("southwest papua",), "Papua Tengah": ("central papua",),
    "Papua Selatan": ("south papua",), "Papua Pegunungan": ("highland papua",),
}
_PROVINCE_ALIASES = {alias: name for name, aliases in _PROVINCE_SPELLINGS.items()
                     for alias in aliases}
_PROVINCE_HEAD, _PROVINCE_TAIL = "provinsi ", " province"


def _province_text(value) -> str:
    """The name without brackets ("ACEH (NAD)") or a Provinsi/Province word; plain string
    work, no backtracking regex on a value that comes from an export."""
    text = " ".join(re.sub(r"\([^)]*\)", " ", str(value if value is not None else "")).split())
    if text.lower().startswith(_PROVINCE_HEAD):
        text = text[len(_PROVINCE_HEAD):]
    if text.lower().endswith(_PROVINCE_TAIL):
        text = text[:-len(_PROVINCE_TAIL)]
    return text.strip()


def province_label(value) -> str:
    """One name per province whichever marketplace spelled it ('WEST JAVA Province',
    'JAWA BARAT' and 'Jawa Barat' are one row), so totals by province do not split."""
    text = _province_text(value)
    if not text:
        return NOT_RECORDED
    key = text.lower().replace("d.i.", "di").replace(".", "")
    key = " ".join(key.split())
    if key in _PROVINCE_ALIASES:
        return _PROVINCE_ALIASES[key]
    return " ".join(_UPPER_WORDS.get(w.lower(), w[:1].upper() + w[1:].lower())
                    for w in text.split(" "))


def island(province: str) -> str:
    name = province_label(province)
    if name.lower() == "not recorded":
        return NOT_RECORDED
    return ISLAND.get(name, "Other")


_PLATFORM = {"tiktok": "TikTok", "tiktok shop": "TikTok Shop", "shopee": "Shopee",
             "tokopedia": "Tokopedia", "lazada": "Lazada", "blibli": "Blibli",
             "whatsapp": "WhatsApp"}


def platform_label(value) -> str:
    key = " ".join(str(value if value is not None else "").split()).lower()
    return _PLATFORM.get(key, key.title() if key else NOT_RECORDED)


# ---------------------------------------------------------------- products
_TAG = re.compile(r"^\s*(\[[^\]]*\]|\([^)]*\))\s*(?:[-‒–—]\s*)?")
_DASH = re.compile(r"\s{1,10}[-‒–—]{1,10}(?:\s{1,10}|$)|^[-‒–—]{1,10}\s{1,10}|[‒–—]{1,10}")
_BUY = re.compile(r"\b(?:buy|beli)\s*(\d{1,2})(?:\s*(?:get|gratis|free)\s*(\d{1,2}))?\b", re.I)
_PACK_WORDS = (
    (_BUY, ""),
    (re.compile(r"\b(?:value|twin|triple|tri|mega)\s*pack\b", re.I), ""),
    (re.compile(r"[\[(]\s*(?:x\s*)?\d{1,2}\s*pcs?\s*[\])]", re.I), ""),
    (re.compile(r"\bisi\s*\d{1,2}(?:\s*pcs?)?\b", re.I), ""),
    (re.compile(r"\b\d{1,2}\s*pcs?\b", re.I), ""),
    (re.compile(r"\b\d{1,2}\s*x\s*(\d+(?:[.,]\d+)?\s*(?:g|gr|gram|ml)\b)", re.I), r"\1"),
    (re.compile(r"(?<=[a-z])\s*x\s*\d{1,2}\b", re.I), ""),
)


def short_product(name, brand="", limit=58) -> str:
    """Listing title -> a product name a reader can scan: promo tags and pack wording
    out, the pack size as one short suffix, the brand word out (every row is the
    brand), creator listings marked, one line long."""
    text = " ".join(str(name if name is not None else "").split())
    if not text:
        return "Unnamed product"
    pack = pack_of(text)
    text, creator = _strip_tags(text)
    if brand:
        text = re.sub(rf"\b{re.escape(str(brand))}\b", "", text, flags=re.I)
    for pat, repl in _PACK_WORDS:
        text = pat.sub(repl, text)
    text = _DASH.sub(" ", text)
    text = re.sub(r"\(\s*\)|\[\s*\]", "", " ".join(text.split())).strip(" ,")
    text = text[:1].upper() + text[1:] if text else "Unnamed product"
    suffix = _pack_suffix(pack) + (" (creator)" if creator else "")
    room = max(limit - len(suffix), 20)
    if len(text) > room:
        text = text[: room - 3].rsplit(" ", 1)[0].rstrip(" ,") + "..."
    return text + suffix


def _strip_tags(text: str) -> tuple[str, bool]:
    """Leading promo tags removed; True when one of them marked a creator listing."""
    creator = False
    while True:
        m = _TAG.match(text)
        if not m:
            return text, creator
        if re.search(r"exclusive|creator|by\s", m.group(1), re.I):
            creator = True
        text = text[m.end():]


def _pack_suffix(pack) -> str:
    if pack is None:
        return ", value pack"
    return f", {pack} pack" if pack > 1 else ""


_PACK_PATTERNS = (
    re.compile(r"\bisi\s*(\d{1,2})\b", re.I),
    re.compile(r"\b(\d{1,2})\s*x\s*\d+(?:[.,]\d+)?\s*(?:g|gr|gram|ml|pcs)\b", re.I),
    re.compile(r"(?:^|[\s\[(])x\s*(\d{1,2})\b", re.I),
    re.compile(r"\d\s*(?:g|gr|ml)\s*x\s*(\d{1,2})\b", re.I),
    re.compile(r"\b(\d{1,2})\s*pcs?\b", re.I),
)


def _title_count(text) -> int | None:
    """A piece count the title writes as a number (1 to 48), Buy N first."""
    buy = _BUY.search(text)                  # "[Buy 3] ...", "Beli 2 Gratis 1"
    counts = [int(buy.group(1)) + int(buy.group(2) or 0)] if buy else []
    counts += [int(m.group(1)) for m in (p.search(text) for p in _PACK_PATTERNS) if m]
    return next((n for n in counts if 1 <= n <= 48), None)


def pack_of(name, configured=None) -> int | None:
    """Pieces per listing, read from the title ('Isi 4 Pcs', '4 x 225 g', 'x3', 'Twin
    Pack'); a configured pack wins. None = a pack whose size the title does not say."""
    if configured:
        try:
            return max(int(configured), 1)
        except (TypeError, ValueError):
            pass
    text = str(name or "")
    n = _title_count(text)
    if n:
        return n
    if re.search(r"\btwin\s*pack\b", text, re.I):
        return 2
    if re.search(r"\btri(?:ple)?\s*pack\b", text, re.I):
        return 3
    if re.search(r"value\s*pack|bundle|paket|hemat", text, re.I):
        return None
    return 1


def pack_type(name, configured=None) -> str:
    n = pack_of(name, configured)
    return "Single" if n == 1 else "Bundle"


ONE_PIECE = "1 piece"


def pack_label(n) -> str:
    # "1 piece", not "Single": the product mix page's "Single" is a product type (not a
    # bundle), a different count, and the two pages read as disagreeing (package 6)
    if n is None:
        return "Pack, size not in the title"
    return ONE_PIECE if n == 1 else f"Pack of {n}"


def product_base(name, brand="") -> str:
    """The product without its pack size: 'Jumbo Vanilla 950g, 2 pack' and the 1 piece
    listing share one base, so their prices per piece compare like for like."""
    short = short_product(name, brand, limit=200)
    return re.sub(r", (?:\d{1,2}|value) pack(?= \(creator\)$|$)", "", short).lower()


# ---------------------------------------------------------------- settlement lines
FEE_LABELS = {
    "wallet_gmv": "Settled GMV", "revenue": "Revenue settled",
    "mp_fee": "Marketplace fees, total", "affiliate_fee": "Affiliate commission",
    "commission_fee": "Platform commission", "os_fee": "Official store fee",
    "dynamic_fee": "Dynamic commission", "order_processing_fee": "Order processing fee",
    "promotion_fee": "Promotion fee", "platform_subsidy": "Platform subsidy",
    "seller_voucher": "Seller voucher (as stored)", "settlement": "Paid out to the store",
}


def claim_label(f: dict) -> str:
    """'Inference' / 'Inference, AI' for a claim the narration wrote."""
    label = str(f.get("label") or "FACT").capitalize()
    return f"{label}, AI" if f.get("by") == "ai" else label


def claim_text(f: dict) -> str:
    """The claim with what a reader needs to weigh it: an AI claim carries its confidence,
    the pages it rests on and, for an inference, the strongest alternative reading."""
    text = str(f.get("text") or "")
    if f.get("by") != "ai":
        return text
    extra = [f"confidence {f.get('confidence') or 'medium'}"]
    if f.get("pages"):
        extra.append(f"pages {f['pages']}")
    out = f"{text} ({'; '.join(extra)})"
    if f.get("alternative"):
        out += f" Alternative reading: {f['alternative']}"
    return out
