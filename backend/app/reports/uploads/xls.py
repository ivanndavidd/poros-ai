"""Old Excel (.xls, BIFF in an OLE2 container) workbooks, through xlrd.

Lazada still exports its product report as a real .xls. xlrd 2.x reads only this format
(no .xlsx, formulas are never evaluated, no macros run), is pure Python, and parses from
bytes, so nothing is written to disk. Cells arrive the way the .xlsx reader gives them:
text as ``str``, numbers as ``int``/``float``, booleans as ``bool``, error and empty cells
as ``None``, and dates as their Excel serial number (the parser that knows the column is a
date converts it).

The format itself caps a sheet at 65,536 rows x 256 columns and the file arrives under the
per-entry byte cap, so xlrd holding one sheet in memory stays small. A password-protected
.xlsx is also an OLE2 container; xlrd cannot read it and the reason says so.
"""
from __future__ import annotations

import io
from typing import Iterator

import xlrd

from .xlsx import MAX_COLS, MAX_ROWS

UNREADABLE = ("the .xls workbook could not be read (it may be password-protected or "
              "damaged); save it as .xlsx")


class XlsError(ValueError):
    """The file is not a readable .xls workbook."""


def _cell(kind: int, value):
    if kind in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK, xlrd.XL_CELL_ERROR):
        return None
    if kind == xlrd.XL_CELL_BOOLEAN:
        return bool(value)
    if kind in (xlrd.XL_CELL_NUMBER, xlrd.XL_CELL_DATE):
        return int(value) if float(value).is_integer() and abs(value) < 1e15 else value
    return value if value != "" else None


class Workbook:
    """One .xls held in memory, a sheet loaded only when its rows are asked for."""

    def __init__(self, data: bytes, max_rows: int = MAX_ROWS, max_cols: int = MAX_COLS):
        try:
            self._book = xlrd.open_workbook(file_contents=data, on_demand=True,
                                            logfile=io.StringIO(), verbosity=0)
        except Exception as exc:  # noqa: BLE001 - xlrd raises many kinds on a bad file
            raise XlsError(UNREADABLE) from exc
        self.max_rows, self.max_cols = max_rows, max_cols
        self.sheet_names = list(self._book.sheet_names())
        self.truncated: dict = {}

    def rows(self, sheet: str | None = None) -> Iterator[list]:
        label = sheet or self.sheet_names[0]
        try:
            sh = self._book.sheet_by_name(label)
        except Exception as exc:  # noqa: BLE001
            raise XlsError(UNREADABLE) from exc
        self.truncated[label] = sh.nrows > self.max_rows
        width = min(sh.ncols, self.max_cols)
        for r in range(min(sh.nrows, self.max_rows)):
            kinds, values = sh.row_types(r, 0, width), sh.row_values(r, 0, width)
            yield [_cell(k, v) for k, v in zip(kinds, values)]
