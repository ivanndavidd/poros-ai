"""Small, strict helpers for building report SQL.

Report SQL is built from TEMPLATES, never written by a model, but a few values still come
from the caller (the brand) or from the data (order status values). Connectors take one
SQL string with no bind parameters, so every value goes through ``lit``: a quoted literal
with quotes doubled and control characters refused. The result still passes govern()
like any other query — this module only keeps the template honest.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

SUPPORTED_DIALECTS = ("bigquery", "duckdb")

_PERIOD = re.compile(r"^(\d{4})-(\d{2})$")
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class ReportInputError(ValueError):
    """A caller-supplied report parameter that cannot be used."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def lit(value) -> str:
    """A single-quoted SQL string literal. Refuses control characters outright: no brand
    or status value legitimately contains them, and they are how literals get broken."""
    text = str(value)
    if _BAD_CHARS.search(text):
        raise ReportInputError("REPORT_BAD_VALUE", "value contains control characters")
    return "'" + text.replace("'", "''") + "'"


def lit_list(values) -> str:
    vals = [lit(v) for v in values]
    return ", ".join(vals) if vals else "NULL"


def clean_brand(brand: str) -> str:
    text = " ".join(str(brand or "").split()).upper()
    if not text:
        raise ReportInputError("REPORT_BAD_BRAND", "brand is required")
    if len(text) > 100 or _BAD_CHARS.search(text):
        raise ReportInputError("REPORT_BAD_BRAND", "brand must be a short plain name")
    return text


@dataclass(frozen=True)
class Month:
    year: int
    # Month.month is the calendar month number, the plain name every caller reads
    month: int  # NOSONAR

    @classmethod
    def parse(cls, text: str) -> "Month":
        m = _PERIOD.match(str(text or "").strip())
        if not m:
            raise ReportInputError("REPORT_BAD_PERIOD", "period must look like 2026-09")
        year, month = int(m.group(1)), int(m.group(2))
        if not (1 <= month <= 12) or not (2000 <= year <= 2100):
            raise ReportInputError("REPORT_BAD_PERIOD", "period is out of range")
        return cls(year, month)

    def shift(self, months: int) -> "Month":
        idx = self.year * 12 + (self.month - 1) + months  # NOSONAR (see the field)
        return Month(idx // 12, idx % 12 + 1)

    @property
    def first(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def last(self) -> date:
        return date(self.year, self.month, calendar.monthrange(self.year, self.month)[1])

    @property
    def mid(self) -> int:
        """The YYYYMM integer Acme tables use as month_id."""
        return self.year * 100 + self.month

    @property
    def key(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def label(self) -> str:
        return f"{calendar.month_abbr[self.month]} {self.year}"

    @property
    def days(self) -> int:
        return self.last.day


def d(value: date) -> str:
    """DATE literal; identical in BigQuery and DuckDB."""
    return f"DATE '{value.isoformat()}'"


def days_between(dialect: str, start: str, end: str) -> str:
    """Whole days from start to end (end - start)."""
    if dialect == "bigquery":
        return f"DATE_DIFF({end}, {start}, DAY)"
    return f"date_diff('day', {start}, {end})"


def month_key(dialect: str, expr: str) -> str:  # NOSONAR: every SQL helper takes the dialect first
    """YYYYMM integer from a DATE/DATETIME expression (the same SQL in every dialect)."""
    return f"(EXTRACT(YEAR FROM {expr}) * 100 + EXTRACT(MONTH FROM {expr}))"


def as_date(dialect: str, expr: str) -> str:
    if dialect == "bigquery":
        return f"DATE({expr})"
    return f"CAST({expr} AS DATE)"


def day_of(dialect: str, expr: str) -> str:
    """A DATE from a column whatever it holds (DATE, DATETIME, TIMESTAMP or text): the
    first ten characters of its text read as YYYY-MM-DD; unreadable = NULL. The scan's
    type is not trusted for this: the ads table's ``date`` was scanned as DATE and is
    STRING in the warehouse now, and a native comparison then fails the dry run."""
    if dialect == "bigquery":
        return f"SAFE_CAST(SUBSTR(CAST({expr} AS STRING), 1, 10) AS DATE)"
    return f"TRY_CAST(SUBSTR(CAST({expr} AS VARCHAR), 1, 10) AS DATE)"


def num_of(dialect: str, expr: str) -> str:
    """A number from a column whatever it holds; text that is not a plain number = NULL.
    The ads table's money columns turned to text in the warehouse (staging, Oct 2026) and
    SUM over text fails the dry run."""
    if dialect == "bigquery":
        return f"SAFE_CAST({expr} AS FLOAT64)"
    return f"TRY_CAST({expr} AS DOUBLE)"


def month_text(dialect: str, expr: str) -> str:
    """'YYYY-MM' from a month column whatever it holds (DATE, TIMESTAMP, 'YYYY-MM' or
    'YYYY-MM-DD' text); compare it with Month.key-style literals."""
    kind = "STRING" if dialect == "bigquery" else "VARCHAR"
    return f"SUBSTR(CAST({expr} AS {kind}), 1, 7)"
