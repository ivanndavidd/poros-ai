"""Cell values as the exports write them, turned into plain Python values.

Marketplace exports mix Indonesian and English conventions in one download folder:
"212.493.871", "4,16%", "\\xa0257,00", "Rp 1.234.567", "-Rp99.999", "1,234.50", "999999.99",
Excel-escaped CSV cells like ``="12345"``, and dates in a dozen shapes ("2026-09-01 10:22",
"01/09/2026 10:22:33", "Oct 21, 2026", "21 Sep 2026 10:22", "22-Jan-2026", "2026/09/30/ 21:46",
Excel serial numbers). The rules here read each of them without guessing where a reading is
provable, and fall back to the file's own habit (``decimal``) only for the two truly
ambiguous shapes ("1.234" and "1,234").
"""
from __future__ import annotations

import datetime as dt
import math
import re

BLANKS = ("", "-", "--", "\u2014", "null", "none", "nan")
_EXCEL_ESCAPE = re.compile(r'^="(.*)"$', re.S)
_NUM_JUNK = re.compile(r"(?i)rp\.?|idr|[\s%]")
_NUMBER = re.compile(r"\d[\d.,]*|\.\d+")
_SCIENTIFIC = re.compile(r"\d+(?:\.\d+)?[eE][+-]?\d+")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_EXCEL_EPOCH = dt.datetime(1899, 12, 30)
# Excel day numbers for 1954..2119: these exports are recent, so a number in this range
# in a date column is a day count, not a year-month-day.
SERIAL_MIN, SERIAL_MAX = 20000, 80000

MONTHS = {
    "jan": 1, "januari": 1, "january": 1, "feb": 2, "februari": 2, "february": 2,
    "mar": 3, "maret": 3, "march": 3, "apr": 4, "april": 4, "may": 5, "mei": 5,
    "jun": 6, "juni": 6, "june": 6, "jul": 7, "juli": 7, "july": 7,
    "aug": 8, "agu": 8, "ags": 8, "agt": 8, "agustus": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9, "oct": 10, "okt": 10, "oktober": 10, "october": 10,
    "nov": 11, "november": 11, "dec": 12, "des": 12, "desember": 12, "december": 12,
}

_TIME = r"(?:[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s*([AaPp][Mm])?)?"
_ZONE = r"\s*(?:Z|[+-]\d{2}:?\d{2})?"
_YMD = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})" + _TIME + _ZONE + r"$")
_DMY = re.compile(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})" + _TIME + r"$")
_D_MON_Y = re.compile(r"(\d{1,2})[ -]+([A-Za-z]{3,9})\.?[ ,-]+(\d{4})" + _TIME + r"$")
_MON_D_Y = re.compile(r"([A-Za-z]{3,9})\.? +(\d{1,2}),? +(\d{4})" + _TIME + r"$")
_COMPACT = re.compile(r"(\d{4})(\d{2})(\d{2})$")


def norm(text) -> str:
    """Header key: lowercase, punctuation and underscores to spaces, spaces collapsed."""
    if text is None:
        return ""
    s = str(text).lower().replace("\xa0", " ")
    return " ".join(_NON_ALNUM.sub(" ", s).split())


def unwrap(text: str) -> str:
    """``="0123"`` is how a CSV keeps Excel from eating leading zeros; the value is inside."""
    m = _EXCEL_ESCAPE.match(text.strip())
    return m.group(1) if m else text


def is_blank(v) -> bool:
    if v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    return isinstance(v, str) and unwrap(v).strip().lower() in BLANKS


# ---- text ----
def text(v) -> str | None:
    """A trimmed string, or None for an empty cell. Integral floats lose their '.0'."""
    if is_blank(v):
        return None
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    if isinstance(v, int):
        return str(v)
    s = " ".join(unwrap(str(v)).replace("\xa0", " ").split())
    return s or None


# ---- numbers ----
def _single_separator(s: str, sep: str, hint: str) -> str | None:
    """One '.' or one ',' in the number: is it the decimal mark?"""
    head, tail = s.split(sep)
    if len(tail) != 3 or len(head) > 3 or head in ("", "0"):
        return sep              # a thousands group always has three digits after it
    return sep if hint == sep else None


def _decimal_mark(s: str, hint: str) -> str | None:
    dots, commas = s.count("."), s.count(",")
    if dots and commas:
        return "." if s.rfind(".") > s.rfind(",") else ","
    if dots > 1 or commas > 1:
        return None             # several marks of one kind are thousands groups
    if dots == 1:
        return _single_separator(s, ".", hint)
    if commas == 1:
        return _single_separator(s, ",", hint)
    return None


def _strip_number(v) -> tuple[str, bool] | None:
    s = unwrap(str(v)).strip()
    if s.lower() in BLANKS:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    s = _NUM_JUNK.sub("", s)
    if s.startswith(("-", "\u2212")):
        neg, s = not neg, s[1:]
    s = s.lstrip("+")
    return s, neg


def number(v, decimal: str = ",") -> float | None:
    """A number from a cell; ``decimal`` is the file's decimal mark for ambiguous shapes."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return None if math.isnan(f) or math.isinf(f) else f
    stripped = _strip_number(v)
    if stripped is None:
        return None
    s, neg = stripped
    value = _unsigned(s, decimal)
    if value is None:
        return None
    return -value if neg else value


def _unsigned(s: str, decimal: str) -> float | None:
    """The value of a sign-free number string, its separators read with ``decimal``."""
    if _SCIENTIFIC.fullmatch(s):
        return float(s)
    if not _NUMBER.fullmatch(s):
        return None
    mark = _decimal_mark(s, decimal)
    for sep in {".", ","} - {mark}:
        s = s.replace(sep, "")
    if mark:
        s = s.replace(mark, ".")
    try:
        return float(s)
    except ValueError:
        return None


def percent(v, decimal: str = ",") -> float | None:
    """Percent scale (4.16 means 4.16%): the exports already write percents that way."""
    return number(v, decimal)


def detect_decimal(samples) -> str:
    """The file's decimal mark, from the number strings it writes ('.' or ',')."""
    dot = comma = 0
    for v in samples:
        if not isinstance(v, str):
            continue
        stripped = _strip_number(v)
        if stripped is None or not _NUMBER.fullmatch(stripped[0]):
            continue
        vote = _mark_vote(stripped[0])
        if vote == ".":
            dot += 1
        elif vote == ",":
            comma += 1
    return "." if dot > comma else ","


def _mark_vote(s: str) -> str | None:
    """The decimal mark one number string speaks for, or None when it says nothing."""
    mark = _decimal_mark(s, "")
    if mark:
        return mark
    if s.count(".") > 1:
        return ","              # 1.234.567 is the Indonesian way
    if s.count(",") > 1:
        return "."              # 1,234,567 is the English way
    return None


# ---- dates ----
def _hms(hour, minute, second, ampm) -> tuple[int, int, int]:
    h = int(hour or 0)
    if ampm:
        h = h % 12 + (12 if ampm.lower() == "pm" else 0)
    return h, int(minute or 0), int(second or 0)


def _build(y, m, d, time_parts) -> dt.datetime | None:
    try:
        return dt.datetime(int(y), int(m), int(d), *_hms(*time_parts))
    except ValueError:
        return None


def _from_number(v: float) -> dt.datetime | None:
    if float(v).is_integer() and 19000101 <= v <= 21001231:
        m = _COMPACT.match(str(int(v)))
        return _build(m.group(1), m.group(2), m.group(3), (None,) * 4) if m else None
    if SERIAL_MIN <= v < SERIAL_MAX:
        return _EXCEL_EPOCH + dt.timedelta(days=float(v))
    return None


def _dmy(m) -> dt.datetime | None:
    day, month = int(m.group(1)), int(m.group(2))
    if month > 12 >= day:
        day, month = month, day          # a month-first export (12/25/2026)
    return _build(m.group(3), month, day, m.groups()[3:])


def _named_month(name: str) -> int | None:
    return MONTHS.get(name.lower().rstrip("."))


def _from_text(s: str) -> dt.datetime | None:
    s = re.sub(r"/\s+", " ", s.strip())          # "2026/09/30/ 21:46"
    m = _YMD.match(s)
    if m:
        return _build(m.group(1), m.group(2), m.group(3), m.groups()[3:])
    m = _DMY.match(s)
    if m:
        return _dmy(m)
    m = _D_MON_Y.match(s)
    if m and _named_month(m.group(2)):
        return _build(m.group(3), _named_month(m.group(2)), m.group(1), m.groups()[3:])
    m = _MON_D_Y.match(s)
    if m and _named_month(m.group(1)):
        return _build(m.group(3), _named_month(m.group(1)), m.group(2), m.groups()[3:])
    m = _COMPACT.match(s)
    if m:
        return _build(m.group(1), m.group(2), m.group(3), (None,) * 4)
    return None


def timestamp(v) -> dt.datetime | None:
    """A datetime from a cell (text in many shapes, or an Excel serial number)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, dt.datetime):
        return v
    if isinstance(v, dt.date):
        return dt.datetime(v.year, v.month, v.day)
    if isinstance(v, (int, float)):
        return _from_number(v)
    s = unwrap(str(v)).strip()
    if s.lower() in BLANKS:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", s) and len(s) != 8:
        return _from_number(float(s))
    return _from_text(s)


def date(v) -> dt.date | None:
    ts = timestamp(v)
    return ts.date() if ts else None


def month_id(d) -> int | None:
    return d.year * 100 + d.month if d else None


def is_serial(v) -> bool:
    """A cell Excel stored as a date number (styles are not read, so this is the tell)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and SERIAL_MIN <= v < SERIAL_MAX
