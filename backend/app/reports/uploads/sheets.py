"""Finding the header row of an export and reading its rows by column meaning.

Exports put title rows above the header ("Tanggal analisis: 01/09/2026~30/09/2026"), a
group row above the real header (Shopee LIVE/Video: "Periode Data | Data Utama | Konversi"),
or a row of column DESCRIPTIONS under it (TikTok orders). So the header is the row, within
the first rows of a sheet, whose cells name the most columns a known export carries; the
rows after it are data only when their key cell looks like a key.

A ``Spec`` lists, per table column, the export header names that carry it (normalized,
Indonesian and English). The first alias that appears wins; a ``total`` field adds every
column it names (exports that split one figure by traffic source). A header that names
buyer identity is never mapped, whatever an alias says.
"""
from __future__ import annotations

import codecs
import csv
import io
import itertools
import re
from dataclasses import dataclass, field
from typing import Callable, Iterator

from . import values as V
from . import xls, xlsx
from .schema import PII_HEADER_WORDS

HEAD_SCAN = 25            # rows searched for the header
SAMPLE_ROWS = 200         # data rows sampled for the file's decimal mark
MAX_CSV_FIELD = 1_000_000
_EXTRA_PII = (
    "nama pelanggan", "nama customer", "shippingname", "billingname", "billingaddr",
    "customeremail", "nationalregistrationnumber", "awb", "districts", "villages",
    "kecamatan", "kelurahan", "username penjual", "nama affiliate", "checked marked by",
)
_PII = tuple({V.norm(w).replace(" ", "") for w in PII_HEADER_WORDS + _EXTRA_PII})
OLE_MAGIC = b"\xd0\xcf\x11\xe0"
# Some sites save a web page under an .xls name; it is neither format.
NOT_EXCEL = "named .xls but not an Excel workbook; open it in Excel and save it as .xlsx"


class SourceError(ValueError):
    """The file cannot be read as a table; the message is the plain reason."""


def is_pii_header(header) -> bool:
    compact = V.norm(header).replace(" ", "")
    return bool(compact) and any(word in compact for word in _PII)


@dataclass(frozen=True)
class Field:
    aliases: tuple
    kind: str = "text"          # text | num | pct | date | ts | raw
    total: bool = False


def col(*aliases: str, kind: str = "text", total: bool = False) -> Field:
    return Field(tuple(V.norm(a) for a in aliases), kind, total)


@dataclass
class Spec:
    name: str
    kind: str
    marketplace: str
    fields: dict
    required: tuple
    key: str
    key_pattern: str = ""       # a data row's key must match this (skips description rows)
    sheets: tuple = ()          # sheet names to prefer, normalized


@dataclass
class Sheet:
    name: str
    open: Callable[[], Iterator[list]]
    truncated: Callable[[], bool] = lambda: False     # the reader stopped at its row cap


@dataclass
class Match:
    spec: Spec
    sheet: str
    header_row: int
    columns: dict
    title_rows: list
    rows: Iterator[list]
    header: list = field(default_factory=list)

    @property
    def score(self) -> int:
        return len(self.columns)


# ---- sources ----
def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _delimiter(text: str) -> str:
    """The delimiter that splits the first lines into the most (and most even) columns."""
    lines = text.splitlines()[:12]
    best, best_score = ",", 0
    for delim in (",", ";", "\t"):
        counts = [len(r) for r in csv.reader(lines, delimiter=delim)]
        if not counts:
            continue
        common = max(set(counts), key=counts.count)
        score = common * counts.count(common) if common > 1 else 0
        if score > best_score:
            best, best_score = delim, score
    return best


def csv_sheet(data: bytes, max_rows: int) -> Sheet:
    text = _decode(data)
    if "\x00" in text[:4096]:
        raise SourceError("not a text file (binary content)")
    delim = _delimiter(text)
    csv.field_size_limit(MAX_CSV_FIELD)

    capped = []

    def rows() -> Iterator[list]:
        try:
            for i, row in enumerate(csv.reader(io.StringIO(text), delimiter=delim)):
                if i >= max_rows:
                    capped.append(True)
                    return
                yield row
        except csv.Error as exc:
            raise SourceError(f"the CSV is malformed ({exc})") from exc
    return Sheet("csv", rows, lambda: bool(capped))


def sources(name: str, data: bytes, max_rows: int) -> list[Sheet]:
    """The readable sheets of one file, or SourceError with the reason it is not read."""
    low = name.lower()
    if data.startswith(OLE_MAGIC):
        try:
            book = xls.Workbook(data, max_rows=max_rows)
        except xls.XlsError as exc:
            raise SourceError(str(exc)) from exc
        return [Sheet(s, lambda s=s: book.rows(s), lambda s=s: book.truncated.get(s, False))
                for s in book.sheet_names]
    if low.endswith(".xls") and not data.startswith(b"PK"):
        raise SourceError(NOT_EXCEL)
    if data.startswith(b"PK"):
        try:
            wb = xlsx.Workbook(data, max_rows=max_rows)
        except xlsx.XlsxError as exc:
            raise SourceError(str(exc)) from exc
        return [Sheet(s, lambda s=s: wb.rows(s), lambda s=s: wb.truncated.get(s, False))
                for s in wb.sheet_names]
    if low.endswith((".csv", ".txt", ".tsv")):
        return [csv_sheet(data, max_rows)]
    if data[:3] == codecs.BOM_UTF8 or _looks_textual(data):
        return [csv_sheet(data, max_rows)]
    raise SourceError("not a csv, xlsx or zip file")


def _looks_textual(data: bytes) -> bool:
    head = data[:2048]
    return bool(head) and b"\x00" not in head and (b"," in head or b";" in head or b"\t" in head)


# ---- header ----
def _cells(row) -> list[str]:
    return [V.norm(c) if c is not None else "" for c in row]


def match_columns(row, spec: Spec) -> dict:
    """{field: [column index, ...]} for one candidate header row."""
    cells = _cells(row)
    usable = [c and not is_pii_header(raw) for c, raw in zip(cells, row)]
    out = {}
    for name, f in spec.fields.items():
        found: list[int] = []
        for alias in f.aliases:
            hits = [i for i, c in enumerate(cells) if usable[i] and c == alias]
            if not hits:
                continue
            if not f.total:
                found = [hits[0]]
                break
            found.extend(i for i in hits if i not in found)
        if found:
            out[name] = found
    return out


def _best_in(head: list, spec: Spec):
    best = None
    for i, row in enumerate(head):
        cols = match_columns(row, spec)
        if not all(r in cols for r in spec.required):
            continue
        if best is None or len(cols) > len(best[1]):
            best = (i, cols)
    return best


def find(sheets: list[Sheet], specs: list[Spec]) -> Match | None:
    """The (sheet, spec, header row) that names the most columns; None when nothing fits."""
    opened = []
    best = None
    for sheet in sheets:
        it = sheet.open()
        head = list(itertools.islice(it, HEAD_SCAN))
        opened.append((sheet, it, head))
        for spec in specs:
            got = _best_in(head, spec)
            if got is None:
                continue
            preferred = V.norm(sheet.name) in spec.sheets
            rank = (preferred, len(got[1]), len(got[1]) / max(len(spec.fields), 1))
            if best is None or rank > best[0]:
                best = (rank, sheet, spec, got)
    if best is None:
        _close(it for _, it, _ in opened)
        return None
    _, sheet, spec, (i, cols) = best
    chosen = next(o for o in opened if o[0] is sheet)
    _close(it for s, it, _ in opened if s is not sheet)
    _, it, head = chosen
    return Match(spec, sheet.name, i, cols, head[:i], itertools.chain(head[i + 1:], it), head[i])


def _close(iterators) -> None:
    for it in iterators:
        close = getattr(it, "close", None)
        if close:
            close()


# ---- data rows ----
def _cell(row, i):
    return row[i] if i < len(row) else None


def convert(value, kind: str, decimal: str):
    if kind == "num":
        return V.number(value, decimal)
    if kind == "pct":
        return V.percent(value, decimal)
    if kind == "date":
        return V.date(value)
    if kind == "ts":
        return V.timestamp(value)
    if kind == "raw":
        return None if V.is_blank(value) else value
    return V.text(value)


def _sample_decimal(match: Match, rows: list) -> str:
    numeric = [idx for name, idx in match.columns.items()
               if match.spec.fields[name].kind in ("num", "pct")]
    samples = (_cell(r, i) for r in rows for idxs in numeric for i in idxs)
    return V.detect_decimal(samples)


def _value(row, f: Field, idxs: list, decimal: str):
    if not f.total:
        return convert(_cell(row, idxs[0]), f.kind, decimal)
    nums = [v for v in (convert(_cell(row, i), f.kind, decimal) for i in idxs) if v is not None]
    return sum(nums) if nums else None


def records(match: Match, decimal: str | None = None) -> Iterator[dict]:
    """Each data row as {field: typed value}; rows without a usable key are skipped."""
    sample = list(itertools.islice(match.rows, SAMPLE_ROWS))
    mark = decimal or _sample_decimal(match, sample)
    spec = match.spec
    key_re = re.compile(spec.key_pattern) if spec.key_pattern else None
    for row in itertools.chain(sample, match.rows):
        rec = {name: _value(row, spec.fields[name], idxs, mark)
               for name, idxs in match.columns.items()}
        key = rec.get(spec.key)
        if key is None or (key_re and not key_re.search(str(key))):
            continue
        yield rec


def decimal_of(match: Match) -> str:
    """Peek the decimal mark without consuming rows (the sample is put back)."""
    sample = list(itertools.islice(match.rows, SAMPLE_ROWS))
    match.rows = itertools.chain(sample, match.rows)
    return _sample_decimal(match, sample)
