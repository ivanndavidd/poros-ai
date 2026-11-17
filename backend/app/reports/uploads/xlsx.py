"""A small, streaming .xlsx reader on the standard library.

Marketplace exports are .xlsx files, some of them tens of megabytes of sheet XML, and the
backend has no spreadsheet library (openpyxl is not a dependency and must not become one
for this). An .xlsx is a zip of XML parts, so ``zipfile`` plus ``iterparse`` reads it one
row at a time without building the sheet in memory.

What it gives: the sheet names, and per sheet the rows as lists of cell values (shared
and inline strings as ``str``, numbers as ``int``/``float``, booleans as ``bool``, error
cells as ``None``). Styles are ignored on purpose, so a date cell arrives as its Excel
serial number and the parser that knows the column is a date converts it.

Safety, because the file comes from a person's download folder: a part that declares a
DOCTYPE or an ENTITY is refused (no entity expansion, ever), every part has a byte cap and
a compression-ratio cap (zip bomb), rows and columns per sheet are capped, and the sheet's
own ``dimension`` element is never trusted for anything.
"""
from __future__ import annotations

import functools
import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Iterator

MAX_ROWS = 1_000_000
MAX_COLS = 400
MAX_PART_BYTES = 1_500_000_000      # sheet XML of a big order export is a few hundred MB
MAX_RATIO = 400                     # sheet XML compresses well, a bomb compresses absurdly
_CHUNK = 1 << 16
_WORKBOOK = "xl/workbook.xml"
_FORBIDDEN = (b"<!DOCTYPE", b"<!ENTITY")
_CELL_REF = re.compile(r"([A-Za-z]{1,3})(\d*)")
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"  # NOSONAR: XML namespace name, never fetched


class XlsxError(ValueError):
    """The file is not a readable .xlsx (or tripped a safety limit)."""


@functools.lru_cache(maxsize=512)
def _local(tag: str) -> str:
    """Tag without its namespace: transitional and strict OOXML use different ones.
    Cached: a sheet has a handful of distinct tags and millions of elements."""
    return tag.rsplit("}", 1)[-1]


@functools.lru_cache(maxsize=4096)
def _letters_index(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def col_index(ref: str) -> int:
    """'A1' -> 0, 'AB7' -> 27; -1 when the reference has no column letters."""
    m = _CELL_REF.match(ref or "")
    return _letters_index(m.group(1)) if m else -1


class _Guarded(io.RawIOBase):
    """A read-only stream over a zip part that refuses DTDs and caps the bytes read."""

    def __init__(self, raw, name: str, limit: int):
        super().__init__()
        self._raw = raw
        self._name = name
        self._limit = limit
        self._seen = 0
        self._tail = b""

    def readable(self) -> bool:
        return True

    def readinto(self, buf) -> int:
        chunk = self._raw.read(min(len(buf), _CHUNK))
        if not chunk:
            return 0
        self._seen += len(chunk)
        if self._seen > self._limit:
            raise XlsxError(f"{self._name} is larger than the reader allows")
        window = self._tail + chunk
        if any(word in window for word in _FORBIDDEN):
            raise XlsxError(f"{self._name} declares a DOCTYPE or ENTITY; refused")
        self._tail = window[-16:]
        buf[:len(chunk)] = chunk
        return len(chunk)

    def close(self) -> None:
        try:
            self._raw.close()
        finally:
            super().close()


class Workbook:
    """One .xlsx held in memory (the bytes), read part by part."""

    def __init__(self, data: bytes, max_rows: int = MAX_ROWS, max_cols: int = MAX_COLS,
                 max_part_bytes: int = MAX_PART_BYTES, max_ratio: int = MAX_RATIO):
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise XlsxError("not an .xlsx file (it is not a zip package)") from exc
        self.max_rows = max_rows
        self.max_cols = max_cols
        self.max_part_bytes = max_part_bytes
        self.max_ratio = max_ratio
        self.truncated: dict[str, bool] = {}
        self._names = {i.filename: i for i in self._zip.infolist()}
        self._sheets = self._read_sheets()
        self._strings: list[str] | None = None

    # ---- package parts ----
    def _open(self, name: str):
        info = self._names.get(name)
        if info is None:
            raise XlsxError(f"the package has no part {name}")
        if info.file_size > self.max_part_bytes:
            raise XlsxError(f"{name} is larger than the reader allows")
        if info.compress_size and info.file_size / info.compress_size > self.max_ratio:
            raise XlsxError(f"{name} is compressed suspiciously well (zip bomb guard)")
        return io.BufferedReader(_Guarded(self._zip.open(info), name, self.max_part_bytes),
                                 buffer_size=_CHUNK)

    def _events(self, name: str, events=("end",)):
        with self._open(name) as stream:
            try:
                yield from ET.iterparse(stream, events=events)
            except ET.ParseError as exc:
                raise XlsxError(f"{name} is not well-formed XML") from exc

    def _rels(self) -> dict[str, str]:
        out: dict[str, str] = {}
        name = "xl/_rels/workbook.xml.rels"
        if name not in self._names:
            return out
        for _, el in self._events(name):
            if _local(el.tag) == "Relationship":
                target = el.get("Target") or ""
                if target.startswith("/"):
                    target = target.lstrip("/")
                else:
                    target = posixpath.normpath(posixpath.join("xl", target))
                out[el.get("Id") or ""] = target
        return out

    def _read_sheets(self) -> list[tuple[str, str]]:
        if _WORKBOOK not in self._names:
            raise XlsxError("not an .xlsx workbook (no xl/workbook.xml)")
        rels = self._rels()
        sheets = []
        for _, el in self._events(_WORKBOOK):
            if _local(el.tag) != "sheet":
                continue
            rid = el.get(f"{{{_REL_NS}}}id") or el.get("id") or ""
            target = rels.get(rid) or f"xl/worksheets/sheet{el.get('sheetId')}.xml"
            if target in self._names:
                sheets.append((el.get("name") or "", target))
        return sheets

    @property
    def sheet_names(self) -> list[str]:
        return [n for n, _ in self._sheets]

    def _shared_strings(self) -> list[str]:
        if self._strings is not None:
            return self._strings
        strings: list[str] = []
        name = next((n for n in self._names if n.lower() == "xl/sharedstrings.xml"), None)
        if name:
            for _, el in self._events(name):
                if _local(el.tag) == "si":
                    strings.append(_text_of(el))
                    el.clear()
        self._strings = strings
        return strings

    # ---- rows ----
    def rows(self, sheet: str | None = None) -> Iterator[list]:
        """Yield each row of the sheet (the first one when not named) as a list.

        Some exporters (the TikTok order export) write every CELL in its own ``<row>``
        element carrying the same ``r`` number, so consecutive row elements with one row
        number are merged into one row.
        """
        part = self._part_for(sheet)
        strings = self._shared_strings()
        label = sheet or self._sheets[0][0]
        self.truncated[label] = False
        count = 0
        pending: list | None = None
        pending_r = None
        for el in self._row_elements(part):
            r = el.get("r")
            if pending is not None and (r is None or r != pending_r):
                yield pending
                count += 1
                pending = None
            if pending is None:
                if count >= self.max_rows:
                    self.truncated[label] = True
                    return
                pending, pending_r = [], r
            self._fill(el, strings, pending)
        if pending is not None:
            yield pending

    def _row_elements(self, part: str) -> Iterator:
        """Yield each finished ``<row>`` element, then drop it so memory stays flat."""
        container = None
        for event, el in self._events(part, events=("start", "end")):
            tag = _local(el.tag)
            if event == "start":
                if tag == "sheetData":
                    container = el
            elif tag == "row":
                yield el
                el.clear()
                if container is not None:
                    container.clear()

    def _part_for(self, sheet: str | None) -> str:
        if not self._sheets:
            raise XlsxError("the workbook has no sheets")
        if sheet is None:
            return self._sheets[0][1]
        for name, part in self._sheets:
            if name == sheet:
                return part
        raise XlsxError(f"the workbook has no sheet named {sheet!r}")

    def _fill(self, row_el, strings: list[str], values: list) -> None:
        """Put the row element's cells into ``values`` at their column positions."""
        nxt = len(values)
        for cell in row_el:
            if _local(cell.tag) != "c":
                continue
            idx = col_index(cell.get("r") or "")
            if idx < 0:
                idx = nxt
            nxt = idx + 1
            if idx >= self.max_cols:
                continue
            value = _cell_value(cell, strings)
            if value is None:
                continue
            if idx >= len(values):
                values.extend([None] * (idx + 1 - len(values)))
            values[idx] = value


def _text_of(el) -> str:
    """Text of a shared/inline string: plain <t> or rich-text runs, phonetics skipped."""
    parts = []
    for child in el:
        tag = _local(child.tag)
        if tag == "t":
            parts.append(child.text or "")
        elif tag == "r":
            parts.extend(t.text or "" for t in child if _local(t.tag) == "t")
    return "".join(parts)


def _number(text: str):
    text = text.strip()
    if not text:
        return None
    digits = text[1:] if text[0] == "-" else text
    if digits.isdigit() and len(digits) <= 15 and digits.isascii():
        return int(text)
    try:
        return float(text)
    except ValueError:
        return text


def _child(el, name: str):
    for child in el:
        if _local(child.tag) == name:
            return child
    return None


def _cell_value(cell, strings: list[str]):
    kind = cell.get("t") or "n"
    if kind == "inlineStr":
        inline = _child(cell, "is")
        return _text_of(inline) if inline is not None else None
    v = _child(cell, "v")
    text = v.text if v is not None else None
    if text is None:
        return None
    if kind == "s":
        try:
            return strings[int(text)]
        except (ValueError, IndexError):
            return None
    if kind == "b":
        return text.strip() == "1"
    if kind == "e":
        return None
    if kind in ("str", "d"):
        return text
    return _number(text)


def is_xlsx(data: bytes) -> bool:
    """A zip whose parts include the workbook (an .xlsx renamed or not)."""
    if not data.startswith(b"PK"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return _WORKBOOK in z.namelist()
    except zipfile.BadZipFile:
        return False
