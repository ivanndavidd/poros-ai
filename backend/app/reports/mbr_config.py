"""Per-tenant MBR settings: the inputs BigQuery does not hold.

Stored in tenant_config section ``mbr`` (editable through the generic config endpoint;
a console form comes in a later phase). Every field is optional — a missing input turns
its section into a documented placeholder, it never fails the report.

    {
      "brands": {
        "ACMESMILE": {
          "takeover_date": "2026-09-01",
          "targets": {"2026-10": {"nmv": 1000000000}},
          "campaigns": [{"name": "10.10", "start": "2026-10-10", "end": "2026-10-10",
                         "type": "double date"}],
          "focus_skus": {"<sku_id>": {"flag": "Premium", "pack": 2}},
          "client_names": ["ACMESMILE"],
          "inventory": {"column": "brand_ref_dpim", "values": ["ACMESMILE"]},
          "client_codes": ["..."],         # warehouse client codes (stock value, inbound)
          "focus": "Premium range first; no price cut on the x4",   # the brand's direction
          "quadrant": {"views": 15000, "cvr": 0.2},  # product quadrant thresholds (optional)
          "customer_since": "2026-01"     # history start for new vs returning buyers
        }
      },
      "status_map": {"<order_status value>": "processed|cancelled|unpaid|returned|other"},
      "max_report_bytes": 200000000000,
      "max_query_bytes": 10737418240
    }
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date

from .. import store
from .sqlkit import Month, ReportInputError, clean_brand

SECTION = "mbr"
STATUS_GROUPS = ("processed", "cancelled", "unpaid", "returned", "other")
DEFAULT_MAX_REPORT_BYTES = 200 * 1024 ** 3
# Per-query ceiling for REPORT queries only (chat keeps the datasource's own limit). Acme
# order tables are unpartitioned (acme_store_order_mp ~29.5 GB), so a 13-month monthly
# rollup reads ~5 GB whatever the date filter; 10 GiB covers it at on-demand cost of a
# few cents, while the 696 GB inventory view still stops at the gate.
DEFAULT_MAX_QUERY_BYTES = 10 * 1024 ** 3
# Inventory/client columns that may carry the brand; the default is a guess the
# preflight row count either confirms or turns into an open item.
DEFAULT_INVENTORY_COLUMN = "brand_ref_dpim"
_IDENT = re.compile(r"^[A-Za-z_]\w{0,63}$", re.ASCII)


@dataclass
class BrandConfig:
    brand: str
    takeover_date: date | None = None
    targets: dict = field(default_factory=dict)          # "YYYY-MM" -> {"nmv": float, ...}
    campaigns: list = field(default_factory=list)        # [{name,start,end,type}]
    focus_skus: dict = field(default_factory=dict)       # sku_id -> {flag, pack}
    client_names: list = field(default_factory=list)
    inventory_column: str = DEFAULT_INVENTORY_COLUMN
    inventory_values: list = field(default_factory=list)
    client_codes: list = field(default_factory=list)      # warehouse client codes
    focus: str = ""                                       # the brand's stated product focus
    quadrant: dict = field(default_factory=dict)          # {"views": n, "cvr": pct}
    customer_since: Month | None = None                   # new buyer history start
    configured: bool = False                              # an entry exists for this brand


@dataclass
class MbrConfig:
    brand: BrandConfig
    status_map: dict
    max_report_bytes: int
    max_query_bytes: int = DEFAULT_MAX_QUERY_BYTES


def _date(value, what: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ReportInputError("MBR_CONFIG_INVALID", f"{what} must be YYYY-MM-DD") from exc


def _month(value, what: str) -> Month | None:
    if value in (None, ""):
        return None
    try:
        return Month.parse(str(value)[:7])
    except ReportInputError as exc:
        raise ReportInputError("MBR_CONFIG_INVALID", f"{what} must be YYYY-MM") from exc


def _strings(value, what: str) -> list:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ReportInputError("MBR_CONFIG_INVALID", f"{what} must be a list")
    return [str(v).strip() for v in value if str(v).strip()]


def _campaigns(value) -> list:
    out = []
    for item in value or []:
        if not isinstance(item, dict):
            raise ReportInputError("MBR_CONFIG_INVALID", "each campaign must be an object")
        start = _date(item.get("start"), "campaign start")
        end = _date(item.get("end") or item.get("start"), "campaign end")
        if not start or not end or end < start:
            raise ReportInputError("MBR_CONFIG_INVALID", "campaign needs start <= end")
        out.append({"name": str(item.get("name") or "").strip() or start.isoformat(),
                    "type": str(item.get("type") or "campaign").strip(),
                    "start": start, "end": end})
    return out


def _quadrant(value) -> dict:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise ReportInputError("MBR_CONFIG_INVALID", "quadrant must be an object")
    out = {}
    for key in ("views", "cvr"):
        if value.get(key) in (None, ""):
            continue
        try:
            n = float(value[key])
        except (TypeError, ValueError) as exc:
            raise ReportInputError("MBR_CONFIG_INVALID", f"quadrant.{key} must be a number") from exc
        if n <= 0 or math.isnan(n):
            raise ReportInputError("MBR_CONFIG_INVALID", f"quadrant.{key} must be above zero")
        out[key] = n
    return out


def _targets(value) -> dict:
    out = {}
    for month, spec in (value or {}).items():
        if not re.match(r"^\d{4}-\d{2}$", str(month)):
            raise ReportInputError("MBR_CONFIG_INVALID", "target keys must be YYYY-MM")
        if isinstance(spec, (int, float)):
            spec = {"nmv": spec}
        if not isinstance(spec, dict):
            raise ReportInputError("MBR_CONFIG_INVALID", "target must be a number or object")
        clean = {}
        for metric, amount in spec.items():
            try:
                clean[str(metric).lower()] = float(amount)
            except (TypeError, ValueError) as exc:
                raise ReportInputError("MBR_CONFIG_INVALID", "target amounts must be numbers") from exc
        out[str(month)] = clean
    return out


MAX_TARGET = 1e15
BAD_TARGET_AMOUNT = "the target must be an amount in IDR"


def request_targets(value, month) -> dict:
    """NMV targets typed with the report request: {"YYYY-MM": amount}, only for the review
    month and the month after it (the two the review measures), each a positive amount
    in IDR. Raises REPORT_BAD_TARGET with a reason a person can fix."""
    if not value:
        return {}
    if not isinstance(value, dict):
        raise ReportInputError("REPORT_BAD_TARGET", "targets must map YYYY-MM to an amount")
    allowed = {month.key, month.shift(1).key}
    out = {}
    for key, amount in value.items():
        key = str(key).strip()
        if key not in allowed:
            raise ReportInputError(
                "REPORT_BAD_TARGET",
                f"a target can only be set for {month.key} or {month.shift(1).key}, not {key}")
        amount = _target_amount(amount)
        if amount is not None:
            out[key] = {"nmv": amount}
    return out


def _target_amount(amount) -> float | None:
    """One typed target as IDR, None when left empty; REPORT_BAD_TARGET when unusable."""
    if isinstance(amount, dict):
        amount = amount.get("nmv")
    if amount in (None, ""):
        return None
    if isinstance(amount, bool):
        raise ReportInputError("REPORT_BAD_TARGET", BAD_TARGET_AMOUNT)
    try:
        amount = float(amount)
    except (TypeError, ValueError) as exc:
        raise ReportInputError("REPORT_BAD_TARGET", BAD_TARGET_AMOUNT) from exc
    if not (0 < amount < MAX_TARGET):          # NaN fails every comparison, so lands here
        raise ReportInputError("REPORT_BAD_TARGET",
                               "the target must be more than zero and a believable amount")
    return amount


def _brand_entry(raw: dict, wanted: str) -> tuple[dict, bool]:
    """(the stored entry for this brand, whether one exists), matched case-blind."""
    for key, value in (raw.get("brands") or {}).items():
        if clean_brand(key) == wanted and isinstance(value, dict):
            return value, True
    return {}, False


def _status_map(value) -> dict:
    status_map = {}
    for status, group in (value or {}).items():
        group = str(group).strip().lower()
        if group not in STATUS_GROUPS:
            raise ReportInputError("MBR_CONFIG_INVALID",
                                   f"status_map groups must be one of {', '.join(STATUS_GROUPS)}")
        status_map[status_key(status)] = group
    return status_map


def _byte_limits(raw: dict) -> tuple[int, int]:
    try:
        return (int(raw.get("max_report_bytes") or DEFAULT_MAX_REPORT_BYTES),
                int(raw.get("max_query_bytes") or DEFAULT_MAX_QUERY_BYTES))
    except (TypeError, ValueError) as exc:
        raise ReportInputError("MBR_CONFIG_INVALID",
                               "max_report_bytes / max_query_bytes must be numbers") from exc


def parse(raw: dict | None, brand: str) -> MbrConfig:
    """Validate the stored section and pick the entry for ``brand`` (case-blind)."""
    raw = raw or {}
    wanted = clean_brand(brand)
    entry, found = _brand_entry(raw, wanted)
    inv = entry.get("inventory") or {}
    column = str(inv.get("column") or DEFAULT_INVENTORY_COLUMN)
    if not _IDENT.match(column):
        raise ReportInputError("MBR_CONFIG_INVALID", "inventory.column must be a column name")
    status_map = _status_map(raw.get("status_map"))
    max_bytes, max_query = _byte_limits(raw)
    focus = entry.get("focus_skus") or {}
    if not isinstance(focus, dict):
        raise ReportInputError("MBR_CONFIG_INVALID", "focus_skus must be an object")
    return MbrConfig(
        brand=BrandConfig(
            brand=wanted,
            takeover_date=_date(entry.get("takeover_date"), "takeover_date"),
            targets=_targets(entry.get("targets")),
            campaigns=_campaigns(entry.get("campaigns")),
            focus_skus={str(k): (v if isinstance(v, dict) else {"flag": str(v)})
                        for k, v in focus.items()},
            client_names=[n.upper() for n in _strings(entry.get("client_names"), "client_names")]
            or [wanted],
            inventory_column=column,
            inventory_values=[v.upper() for v in _strings(inv.get("values"), "inventory.values")]
            or [wanted],
            client_codes=_strings(entry.get("client_codes"), "client_codes")
            or _strings(entry.get("inbound_client_codes"), "inbound_client_codes"),
            focus=str(entry.get("focus") or "").strip()[:400],
            quadrant=_quadrant(entry.get("quadrant")),
            customer_since=_month(entry.get("customer_since"), "customer_since"),
            configured=found,
        ),
        status_map=status_map,
        max_report_bytes=max_bytes,
        max_query_bytes=max_query,
    )


def load(tenant_id: str, brand: str) -> MbrConfig:
    return parse(store.get_tenant_config(tenant_id, SECTION), brand)


# Order status values differ per table (L2 vs L3 already disagree), so the groups are
# derived from the values actually present. A configured mapping always wins; the
# keyword guess is reported as an INFERENCE in OPEN-ITEMS for the owner to confirm.
# Shopee writes "Pesanan diterima, namun Pembeli masih dapat mengajukan pengembalian hingga
# <date>." on a delivered order whose return window is still open: received, so sold. The
# word "pengembalian" in it must not make it a return (it cost Sep 2026 IDR 337 m of NMV).
_RECEIVED = ("pesanan diterima", "masih dapat mengajukan pengembalian",
             "can still request a return", "return window")
_GUESS = (
    ("cancelled", ("cancel", "batal", "dibatalkan", "failed", "gagal")),
    ("returned", ("return", "retur", "refund", "pengembalian")),
    ("unpaid", ("unpaid", "belum bayar", "belum dibayar", "menunggu pembayaran", "pending payment")),
)


DATE_IN_STATUS = re.compile(r"\d{4}-\d{2}-\d{2}")


def status_key(value) -> str:
    """Lower-cased status with any date written as <date>, so the dated variants of one
    status ("... hingga 2026-10-13.") are one value to group and to configure."""
    return DATE_IN_STATUS.sub("<date>", str(value if value is not None else "").strip().lower())


def classify_status(value, status_map: dict) -> tuple[str, bool]:
    """(group, from_config)."""
    key = status_key(value)
    raw = str(value if value is not None else "").strip().lower()
    for k in (key, raw):
        if k in status_map:
            return status_map[k], True
    if any(w in key for w in _RECEIVED):
        return "processed", False
    for group, words in _GUESS:
        if any(w in key for w in words):
            return group, False
    if not key:
        return "other", False
    return "processed", False
