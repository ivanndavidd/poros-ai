"""The files a person attaches -> the upload tables, with one FileResult per file.

People attach what they download: loose csv/xlsx files or a zip of folders. A zip is
opened in memory, never extracted; each entry is read only after its size, its
compression ratio and the archive's running total pass the guards below, because a zip
can claim small and expand to gigabytes. A zip inside a zip is not opened.

Reading one file never raises: whatever goes wrong becomes that file's plain-English
``reason`` and the other files still load. Files that repeat data already read (the same
bytes, or rows another file already gave) are kept out, so a month attached twice is not
counted twice.
"""
from __future__ import annotations

import hashlib
import io
import logging
import posixpath
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Iterator

from . import channels, detect, ops, orders, wallet, xlsx
from .schema import TABLES, Bundle, FileResult
from .sheets import SourceError, find, sources

log = logging.getLogger(__name__)

MAX_ENTRY_BYTES = 300 * 1024 * 1024     # one file; the biggest real export is ~40 MB
MAX_TOTAL_ROWS = 3_000_000              # every table together, for one report
MAX_ARCHIVE_BYTES = 2 * 1024 ** 3       # everything one zip may expand to
MAX_ZIP_RATIO = 200                     # exports compress 5-30x; a bomb compresses 1000x+
RATIO_FLOOR = 1024 * 1024               # tiny entries may compress absurdly well, harmlessly
DUPLICATE_SHARE = 0.9                   # a file whose rows are 90% known is a copy
_READ_CHUNK = 1024 * 1024
_SYSTEM_FILES = ("thumbs.db", "desktop.ini")
_WALLET = "acme_store_wallet_mp"
_ORDERS = "upload_orders"
_MONEY = tuple(c for c, t in TABLES[_WALLET] if t == "DOUBLE")

NESTED_ZIP = "a zip inside a zip is not opened; attach it separately"
UNKNOWN = "Poros does not recognise this file as a marketplace or Acme export"
EMPTY = "the file is empty"
NO_ROWS = "the file has the expected columns but no data rows"


@dataclass(frozen=True)
class Kind:
    table: str
    specs: tuple
    build: Callable


KINDS = {
    "orders": Kind(_ORDERS, tuple(orders.SPECS), orders.build),
    "product": Kind("acme_store_product_mp", tuple(channels.PRODUCT_SPECS), channels.build_product),
    "affiliate": Kind("acme_store_affiliatetrx_mp", tuple(channels.AFFILIATE_SPECS),
                      channels.build_affiliate),
    "live": Kind("acme_store_live_mp", tuple(channels.LIVE_SPECS), channels.build_live),
    "video": Kind("acme_store_video_mp", tuple(channels.VIDEO_SPECS), channels.build_video),
    "wallet": Kind(_WALLET, tuple(wallet.SPECS), wallet.build),
    "inventory": Kind("upload_inventory", (ops.INVENTORY,), ops.build_inventory),
    "po": Kind("upload_po", (ops.PO,), ops.build_po),
    "srp": Kind("upload_srp", (ops.SRP,), ops.build_srp),
}
ALL_SPECS = [spec for kind in KINDS.values() for spec in kind.specs]


@dataclass
class Context:
    """What a builder knows about the file besides its rows."""
    path: str
    month_id: int | None
    marketplace: str
    owner: str | None
    sheets: list = field(default_factory=list)


class _TooBig(Exception):
    """An entry kept expanding past its cap while it was read."""


# ---- zip handling ----
def _is_archive(name: str, data: bytes) -> bool:
    low = name.lower()
    if low.endswith((".xlsx", ".xlsm")):
        return False
    return low.endswith(".zip") or (data[:2] == b"PK" and not xlsx.is_xlsx(data))


def _skipped(info: zipfile.ZipInfo) -> bool:
    parts = [p for p in info.filename.replace("\\", "/").split("/") if p]
    if info.is_dir() or not parts or parts[0] == "__MACOSX":
        return True
    return any(p.startswith(".") for p in parts) or parts[-1].lower() in _SYSTEM_FILES


def _refusal(info: zipfile.ZipInfo, max_bytes: int) -> str:
    if info.flag_bits & 0x1:
        return "the file is password-protected"
    if info.file_size > max_bytes:
        return f"the file is larger than {max_bytes // 2 ** 20} MB"
    if info.file_size > RATIO_FLOOR and info.file_size > MAX_ZIP_RATIO * max(info.compress_size, 1):
        return "the file expands far more than a real export does, so it was not opened"
    return ""


def _read_capped(z: zipfile.ZipFile, info: zipfile.ZipInfo, max_bytes: int) -> bytes:
    """The entry's bytes, stopping at the cap even when its header under-states its size."""
    out = bytearray()
    with z.open(info) as f:
        while True:
            chunk = f.read(_READ_CHUNK)
            if not chunk:
                return bytes(out)
            out += chunk
            if len(out) > max_bytes:
                raise _TooBig


def _entries(name: str, data: bytes,
             max_bytes: int) -> Iterator[tuple[str, str, bytes | None, str]]:
    """(path used to classify, path shown, bytes or None, reason) per file to read."""
    if not _is_archive(name, data):
        yield name, name, data, ""
        return
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError):
        yield name, name, None, "the zip cannot be opened (damaged, or not a zip file)"
        return
    budget = MAX_ARCHIVE_BYTES
    with z:
        for info in z.infolist():
            if _skipped(info):
                continue
            blob, reason, size = _zip_entry(z, info, max_bytes, budget)
            budget -= size
            yield info.filename, f"{name}/{info.filename}", blob, reason


def _zip_entry(z, info, max_bytes: int, budget: int) -> tuple[bytes | None, str, int]:
    """(bytes or None, reason, bytes expanded) for one entry, read only once the guards pass.

    A nested zip still counts against the archive's budget: it was expanded to find out.
    """
    reason = _refusal(info, max_bytes)
    if not reason and info.file_size > budget:
        reason = "the zip expands to more data than one report reads"
    if reason:
        return None, reason, 0
    blob, reason = _read_entry(z, info, max_bytes)
    if blob is None:
        return None, reason, 0
    if _is_archive(info.filename, blob):
        return None, NESTED_ZIP, len(blob)
    return blob, reason, len(blob)


def _read_entry(z, info, max_bytes) -> tuple[bytes | None, str]:
    try:
        return _read_capped(z, info, max_bytes), ""
    except _TooBig:
        return None, f"the file is larger than {max_bytes // 2 ** 20} MB"
    except (zipfile.BadZipFile, RuntimeError, OSError, EOFError):
        # A damaged entry, an unsupported compression method or encryption the header
        # did not declare: the zip library says so with one of these.
        return None, "the file inside the zip is damaged or packed in a way Poros cannot read"


# ---- the rows every table has seen ----
def _order_key(row: dict) -> tuple:
    return row["marketplace"], row["order_id"], row["line_id"]


def _row_key(row: dict) -> tuple:
    return tuple(str(v) for v in row.values())


class _Tables:
    """Rows per table, keeping out rows an earlier file already gave."""

    def __init__(self):
        self.rows: dict[str, list] = {}
        self.seen: dict[str, dict] = {}
        self.wallet: dict[tuple, tuple[dict, str]] = {}
        self.by_file: dict[str, list] = {}
        self.total = 0

    def add(self, table: str, rows: list, path: str) -> tuple[int, str, list[str]]:
        """(rows kept, the file these rows duplicate or '', warnings)."""
        if table == _WALLET:
            return self._add_wallet(rows, path)
        seen = self.seen.setdefault(table, {})
        keys = [(_order_key if table == _ORDERS else _row_key)(r) for r in rows]
        earlier = [seen[k] for k in keys if k in seen]
        if rows and len(earlier) >= DUPLICATE_SHARE * len(rows):
            return 0, Counter(earlier).most_common(1)[0][0], []
        fresh = [r for k, r in zip(keys, rows) if k not in seen]
        seen.update(dict.fromkeys(keys, path))
        self.rows.setdefault(table, []).extend(fresh)
        self.by_file[path] = fresh
        self.total += len(fresh)
        notes = []
        if earlier:
            first = Counter(earlier).most_common(1)[0][0]
            notes.append(f"{len(earlier)} rows were already read from {first} and were skipped")
        return len(fresh), "", notes

    def _add_wallet(self, rows: list, path: str) -> tuple[int, str, list[str]]:
        """Wallet rows are monthly sums: an identical month is a copy, a different one for
        the same month (a settlement split over two files) is added to it."""
        same, merged = [], []
        for row in rows:
            outcome, told = self._wallet_row(row, path)
            if outcome == "same":
                same.append(told)
            elif outcome == "merged":
                merged.append(told)
        if rows and len(same) == len(rows):
            return 0, same[0], []
        notes = [f"added to the settlement already read for {', '.join(merged)}"] if merged else []
        if same:
            notes.append(f"{len(same)} months were the same as in {same[0]} and were skipped")
        kept = len(rows) - len(same)
        self.total += kept
        return kept, "", notes

    def _wallet_row(self, row: dict, path: str) -> tuple[str, str]:
        """('new' | 'same' | 'merged', the file or month to name) for one monthly row."""
        key = (row["marketplace"], row["month_id"])
        have = self.wallet.get(key)
        if have is None:
            self.wallet[key] = (row, path)
            return "new", ""
        kept, where = have
        if all(abs((kept[c] or 0) - (row[c] or 0)) < 0.5 for c in _MONEY):
            return "same", where
        for c in _MONEY:
            kept[c] = (kept[c] or 0) + (row[c] or 0)
        return "merged", f"{key[0]} {key[1]} (also in {where})"

    def into(self, bundle: Bundle) -> None:
        for table, rows in self.rows.items():
            bundle.add(table, rows)
        if self.wallet:
            bundle.add(_WALLET, [row for row, _ in self.wallet.values()])


# ---- files named for both TikTok and Tokopedia ----
_OTHER_SHOP = {"TikTok": "Tokopedia", "Tokopedia": "TikTok"}


def settle_shared(files: list[FileResult], tables: _Tables) -> None:
    """A report named for both shops was read as the shop its layout usually comes from.
    The folders hold one report per shop per month, so when the other shop's report for the
    same month is attached on its own, this file is the missing shop's: relabel its rows."""
    have = {(f.kind, f.marketplace, f.month) for f in files if f.used}
    for res in files:
        if res.used and any(w.startswith(channels.SHARED_NOTE) for w in res.warnings):
            _settle_one(res, tables.by_file.get(res.path, []), have)


def _settle_one(res: FileResult, rows: list, have: set) -> None:
    present = [shop for shop in _OTHER_SHOP if (res.kind, shop, res.month) in have]
    if len(present) != 1:
        return      # neither shop's report (or both) is attached: the layout's guess stays
    shop = _OTHER_SHOP[present[0]]
    for row in rows:
        row["marketplace"] = shop
    res.marketplace = shop
    res.warnings = [w for w in res.warnings if not w.startswith(channels.SHARED_NOTE)]
    res.warnings.append(f"{channels.SHARED_NOTE}; it was read as {shop} because the "
                        f"{present[0]} report for {res.month} is attached as its own file")


# ---- one file ----
def _month_id(month: str) -> int | None:
    return int(month.replace("-", "")) if month else None


def _peek(sheets) -> list:
    it = sheets[0].open()
    try:
        return [row for _, row in zip(range(12), it)]
    finally:
        close = getattr(it, "close", None)
        if close:
            close()


def _matched(path: str, sheets, res: FileResult):
    """The matching spec for the file, its kind told by the name first, the columns last."""
    kind = res.kind or detect.classify(path, _peek(sheets))["kind"]
    match = find(sheets, list(KINDS[kind].specs)) if kind in KINDS else None
    if match is None:
        match = find(sheets, ALL_SPECS)
        if match is not None and kind and match.spec.kind != kind:
            res.warnings.append(f"the file name reads as {kind} but its columns are a "
                                f"{match.spec.kind} export, so it was read as {match.spec.kind}")
    return match


def _context(path: str, match, res: FileResult, sheets) -> Context:
    by_rows = detect.month_from_rows(match.title_rows)
    if by_rows and res.month and by_rows != res.month:
        res.warnings.append(f"the file name says {res.month} but the file covers {by_rows}")
    res.month = by_rows or res.month
    res.kind = match.spec.kind
    if not res.marketplace:
        res.marketplace = match.spec.marketplace
    return Context(path, _month_id(res.month), res.marketplace, channels.owner_of(path), sheets)


def _build(path: str, data: bytes, res: FileResult):
    """(table, rows) for one file, or None with ``res.reason`` set."""
    sheets = sources(path, data, xlsx.MAX_ROWS)
    match = _matched(path, sheets, res)
    if match is None:
        res.reason = (f"the columns are not those of a {res.kind} export Poros knows"
                      if res.kind else UNKNOWN)
        return None
    ctx = _context(path, match, res, sheets)
    if match.spec in channels.SUMMARY_SPECS:
        res.reason = ("a per-creator affiliate summary; the affiliate transactions export "
                      "carries the same sales order by order")
        return None
    kind = KINDS[match.spec.kind]
    rows, notes = kind.build(match, ctx)
    res.warnings.extend(notes)
    if any(sheet.truncated() for sheet in sheets):
        res.warnings.append(f"a sheet has more than {xlsx.MAX_ROWS:,} rows; only the first "
                            f"{xlsx.MAX_ROWS:,} were read")
    if not rows:
        res.reason = NO_ROWS
        return None
    return kind.table, rows


def _classified(path: str, shown: str) -> FileResult:
    info = detect.classify(path)
    return FileResult(path=shown, kind=info["kind"], marketplace=info["marketplace"],
                      month=info["month"])


def _load_one(path: str, shown: str, data: bytes, tables: _Tables, max_total_rows: int,
              res: FileResult) -> None:
    room = max_total_rows - tables.total
    if room <= 0:
        res.reason = f"the row limit for one report ({max_total_rows:,} rows) was reached"
        return
    try:
        built = _build(path, data, res)
    except SourceError as exc:
        res.reason = str(exc)
        return
    except xlsx.XlsxError as exc:
        res.reason = f"the workbook could not be read: {exc}"
        return
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the others
        log.warning("upload %s could not be read: %s", shown, type(exc).__name__)
        res.reason = f"the file could not be read ({type(exc).__name__})"
        return
    if built is None:
        return
    table, rows = built
    if len(rows) > room:
        res.warnings.append(f"only the first {room:,} rows were read: the report's row limit")
        rows = rows[:room]
    kept, copy_of, notes = tables.add(table, rows, shown)
    res.table = table
    res.warnings.extend(notes)
    if copy_of:
        res.reason = f"its data is already in {copy_of}"
        return
    res.rows, res.used = kept, kept > 0


@dataclass
class _Run:
    """One load: the result, the rows so far and the files already seen byte for byte."""
    max_total_rows: int
    bundle: Bundle = field(default_factory=Bundle)
    tables: _Tables = field(default_factory=_Tables)
    hashes: dict = field(default_factory=dict)

    def entry(self, path: str, shown: str, blob: bytes | None, reason: str) -> None:
        res = _classified(path, shown)
        self.bundle.files.append(res)
        res.reason = reason or self._skip_reason(blob, res)
        if not res.reason:
            _load_one(path, shown, blob, self.tables, self.max_total_rows, res)

    def _skip_reason(self, blob: bytes | None, res: FileResult) -> str:
        """Why the file is not read at all: nothing in it, or the same bytes as another.
        Two exports with headers and no rows are byte for byte the same whatever month
        they are for (ACMESWEET's Aug and Sep Shopee LIVE), so a copy of one is still
        read: it says that month had nothing, which is not what a dropped copy says."""
        if blob is None:
            return EMPTY
        digest = hashlib.sha256(blob).hexdigest()
        first = self.hashes.get(digest)
        if first is not None and first.reason != NO_ROWS:
            return f"the same file as {first.path}"
        self.hashes.setdefault(digest, res)
        return EMPTY if not blob or blob.isspace() else ""


def load_files(files: list[tuple[str, bytes]], max_entry_bytes: int = MAX_ENTRY_BYTES,
               max_total_rows: int = MAX_TOTAL_ROWS) -> Bundle:
    """Every attached file (zips opened) read into the upload tables."""
    run = _Run(max_total_rows)
    for name, data in files:
        name = posixpath.basename(str(name).replace("\\", "/")) or "file"
        for entry in _entries(name, bytes(data or b""), max_entry_bytes):
            run.entry(*entry)
    settle_shared(run.bundle.files, run.tables)
    run.tables.into(run.bundle)
    return run.bundle
