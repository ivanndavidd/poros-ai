"""What a file is, from its folder, its name and (when given) its first rows.

People keep their downloads in folders like ``Monthly Transaction/Shopee`` and name files
by hand, so names carry typos ("Diabteasol", "Aug_20026"), copy suffixes ("(1)", "_ (2)"),
mixed case ("SEP", "TikTok") and different words for the same thing (KNOS calls the order
folder "Monthly Performance" and its order files "RAW OBI"). The file NAME decides first
(a LIVE file filed under "Product Performance" is still a LIVE file), then the folders,
then the header words. A date range written inside the file beats the month in its name.
"""
from __future__ import annotations

import datetime as dt
import posixpath
import re

from . import values as V

KINDS = ("orders", "product", "affiliate", "live", "video", "wallet", "inventory", "po", "srp")
# A file name that names both shops. TikTok Shop and Tokopedia write the same layouts, so
# such a file is read as one of them and parse.settle_shared() decides which.
SHARED = "TikTok/Tokopedia"

# The first word of a file name names its export in every folder seen so far.
_FIRST_WORD = {
    "live": "live", "video": "video", "affiliateperf": "affiliate", "affiliatetrx": "affiliate",
    "affiliate": "affiliate", "order": "orders", "orders": "orders", "product": "product",
    "produk": "product", "wallet": "wallet", "settlement": "wallet", "income": "wallet",
    "srpmaster": "srp",
}
# Phrases anywhere in a file name.
_NAME_PHRASES = (
    ("inventory", "inventory"), ("po service level", "po"), ("purchase order", "po"),
    ("srp master", "srp"), ("raw obi", "orders"), ("settlement", "wallet"),
)
# Folder names, nearest folder first.
_FOLDERS = (
    ("affiliate", "affiliate"), ("live", "live"), ("video", "video"),
    ("monthly transaction", "orders"), ("monthly performance", "orders"), ("order", "orders"),
    ("product performance", "product"), ("wallet", "wallet"), ("settlement", "wallet"),
    ("inventory", "inventory"),
)
# Header words that only one kind of export writes (fallback when names say nothing).
_HEADER_WORDS = {
    "orders": ("no pesanan", "order id", "ordernumber", "nomor pesanan", "no order",
               "status pesanan", "order status", "sku unit original price", "harga awal"),
    "product": ("kode produk", "id produk", "halaman produk dilihat", "impresi produk",
                "kode sku blibli", "halaman dikunjungi", "rentang gmv"),
    "affiliate": ("username affiliate", "nama pengguna kreator", "estimasi komisi per produk rp",
                  "jenis konten"),
    "live": ("waktu live", "gmv dari live rp", "penonton aktif"),
    "video": ("id video", "gmv dari video rp", "vv"),
    "wallet": ("total settlement amount", "order settled time", "nama biaya", "fee name",
               "total margin", "laporan penghasilan"),
    "inventory": ("available qty", "orderqty14", "doi 30", "recommended action"),
    "po": ("ordered quantity", "delivered quantity", "open quantity", "gr date"),
    "srp": ("acme sku", "srp per date"),
}
_MARKETPLACE_WORDS = (("shopee", "Shopee"), ("lazada", "Lazada"), ("blibli", "Blibli"))
_MONTH_YEAR = re.compile(r"(?<![a-z])([a-z]{3,9})[\s_.,-]*(\d{4,5})(?!\d)")
# one pattern per way a date is written (2026-09-01, 01/09/2026, 1 Sep 2026)
_DATE_SHAPES = (r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", r"\d{1,2}[-/]\d{1,2}[-/]\d{4}",
                r"\d{1,2}[ -][A-Za-z]{3,9}[ -]\d{4}")
_DATE_IN_TEXT = re.compile("|".join(_DATE_SHAPES))
_YEARS = range(2010, 2041)


def _parts(path: str) -> tuple[str, list[str]]:
    clean = path.replace("\\", "/").strip("/")
    folders = [V.norm(p) for p in clean.split("/")[:-1]]
    stem = posixpath.splitext(posixpath.basename(clean))[0]
    return stem, folders[::-1]


def _kind_from_name(stem: str) -> str:
    words = V.norm(stem).split()
    if words and words[0] in _FIRST_WORD:
        return _FIRST_WORD[words[0]]
    joined = " ".join(words)
    for phrase, kind in _NAME_PHRASES:
        if phrase in joined:
            return kind
    return ""


def _kind_from_folders(folders: list[str]) -> str:
    for folder in folders:
        for word, kind in _FOLDERS:
            if word in folder:
                return kind
    return ""


def _kind_from_header(head_rows) -> str:
    cells = {V.norm(c) for row in head_rows or () for c in row if c not in (None, "")}
    best, score = "", 1
    for kind, words in _HEADER_WORDS.items():
        hits = sum(1 for w in words if w in cells)
        if hits > score:
            best, score = kind, hits
    return best


def marketplace_of(text: str) -> str:
    low = V.norm(text).replace(" ", "")
    tiktok, tokopedia = "tiktok" in low, "tokopedia" in low
    if tiktok and tokopedia:
        return SHARED
    if tiktok:
        return "TikTok"
    if tokopedia:
        return "Tokopedia"
    for word, name in _MARKETPLACE_WORDS:
        if word in low:
            return name
    return ""


def _marketplace(stem: str, folders: list[str]) -> str:
    found = marketplace_of(stem)
    if found:
        return found
    for folder in folders:
        found = marketplace_of(folder)
        if found:
            return found
    return ""


def _year(digits: str) -> int | None:
    """'2026' -> 2026; a typo'd '20026' -> 2026 only when one reading is a plausible year."""
    if len(digits) == 4:
        year = int(digits)
        return year if year in _YEARS else None
    readings = {int(digits[:i] + digits[i + 1:]) for i in range(len(digits))}
    plausible = [y for y in readings if y in _YEARS]
    return plausible[0] if len(plausible) == 1 else None


def month_from_name(stem: str) -> str:
    for m in _MONTH_YEAR.finditer(stem.lower()):
        month = V.MONTHS.get(m.group(1))
        year = _year(m.group(2)) if month else None
        if month and year:
            return f"{year:04d}-{month:02d}"
    return ""


def _range_in(cell) -> tuple[dt.date, dt.date] | None:
    """A 'from ~ to' range written in one cell ("2026-09-01 ~ 2026-09-30")."""
    if not isinstance(cell, str):
        return None
    dates = [d for d in (V.date(t) for t in _DATE_IN_TEXT.findall(cell)) if d]
    return (dates[0], dates[1]) if len(dates) >= 2 else None


def month_from_rows(head_rows) -> str:
    """The month a date range written in the title rows covers (both ends in one month).

    Only a range inside ONE cell counts: data rows carry single dates, and a title such as
    "01 Sep 2026 - 30 Sep 2026 Created Date: 02 Oct 2026" is read by its first two dates.
    """
    for row in (head_rows or ())[:12]:
        for cell in row:
            found = _range_in(cell)
            if not found:
                continue
            first, last = found
            if (first.year, first.month) == (last.year, last.month):
                return f"{first.year:04d}-{first.month:02d}"
            return ""
    return ""


def classify(path: str, head_rows: list | None = None) -> dict:
    """{kind, marketplace, month} for one file; empty strings for what cannot be told.

    ``note`` is added when the file's own date range disagrees with its name.
    """
    stem, folders = _parts(path)
    kind = _kind_from_name(stem) or _kind_from_folders(folders) or _kind_from_header(head_rows)
    by_name = month_from_name(stem)
    by_rows = month_from_rows(head_rows)
    out = {"kind": kind, "marketplace": _marketplace(stem, folders), "month": by_rows or by_name}
    if by_rows and by_name and by_rows != by_name:
        out["note"] = f"the file name says {by_name} but the file covers {by_rows}"
    return out
