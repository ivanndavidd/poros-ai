"""Attached exports as a governed, throwaway datasource for one MBR run.

The person's files are normalized (``uploads.load_files``, buyer identity dropped), loaded
into a DuckDB file in a temporary folder and registered in the run's table index:

  * a table named like a warehouse table (``acme_store_live_mp`` ...) REPLACES that table
    for this report: the person attached the export because they want it read, and the
    page says the figures come from their files;
  * ``upload_*`` tables are what only the files have (shipping per order, delivery
    times, purchase orders, SRP); the file pages read them.

Every query still goes through ``registry.execute_governed`` (read-only, allowlist, role
tables, PII columns of the warehouse table it replaces, row filters). The folder is
removed when the run ends; the stored files are removed by the job handler.
"""
from __future__ import annotations

import csv
import logging
import os
import shutil
import tempfile
from dataclasses import asdict
from datetime import date, datetime

import duckdb

from .. import store
from .uploads.schema import BRANDED, TABLES, Bundle

log = logging.getLogger(__name__)

SOURCE_NAME = "Attached files"
INVENTORY_VIEW = "vw_inventory_order_wh_daily_snapshot"
_NULL = "\\N"


def _load(tenant_id: str, input_id: str) -> Bundle:
    """One file at a time, so a 100 MB zip is the most held in memory at once."""
    from .uploads import load_files
    row = store.get_report_input(tenant_id, input_id)
    if not row:
        return Bundle()
    files: dict = {}
    for p in row["parts"]:
        files.setdefault(p["file_no"], (p["name"], []))[1].append(p["chunk_no"])
    out = Bundle()
    for file_no, (name, chunks) in sorted(files.items()):
        data = b"".join(store.read_report_input_chunk(tenant_id, input_id, file_no, n)
                        for n in sorted(chunks))
        got = load_files([(name, data)])
        for table, rows in got.tables.items():
            out.add(table, rows)
        out.files.extend(got.files)
    return out


def _cell(value):
    if value is None:
        return _NULL
    if isinstance(value, (datetime, date)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    return value


def _write(path: str, bundle: Bundle, brand: str) -> dict:
    """Create every table that has rows; returns table -> column names. Rows go through
    a CSV beside the database and DuckDB's COPY: row-by-row inserts of a few hundred
    thousand export lines take minutes, COPY takes seconds."""
    made = {}
    folder = os.path.dirname(path)
    con = duckdb.connect(path)
    try:
        for table, rows in bundle.tables.items():
            if not rows:
                continue
            cols = [c for c, _ in TABLES[table]]
            con.execute(f"CREATE TABLE {table} ("
                        + ", ".join(f"{c} {t}" for c, t in TABLES[table]) + ")")
            stamp = table in BRANDED
            staged = os.path.join(folder, f"{table}.csv")
            with open(staged, "w", newline="", encoding="utf-8") as fh:
                out = csv.writer(fh)
                for r in rows:
                    out.writerow([_cell(brand if (stamp and c == "brand") else r.get(c))
                                  for c in cols])
            where = staged.replace("\\", "/").replace("'", "''")
            con.execute(f"COPY {table} FROM '{where}' "
                        f"(FORMAT csv, HEADER false, NULLSTR '{_NULL}', QUOTE '\"', ESCAPE '\"')")
            os.remove(staged)
            made[table] = cols
    finally:
        con.close()
    return made


def _inventory_view(path: str, made: dict, column: str, value: str) -> None:
    """The inventory page reads the warehouse stock snapshot by the brand's configured
    column; the attached stock file becomes a table of that shape."""
    if "upload_inventory" not in made:
        return
    col = column if column.isidentifier() else "brand_ref_dpim"
    cols = [col, "report_date", "sto_item_code", "sto_item_name", "sto_available_stock",
            "max_hpp", "orderqty30", "po_ship_date", "po_quantity"]
    con = duckdb.connect(path)
    try:
        con.execute(
            f"CREATE TABLE {INVENTORY_VIEW} AS SELECT ?::VARCHAR AS {col}, report_date, "
            "COALESCE(item_code, sku) AS sto_item_code, item_name AS sto_item_name, "
            "available_qty AS sto_available_stock, hpp AS max_hpp, order_qty_30 AS orderqty30, "
            "CAST(NULL AS DATE) AS po_ship_date, CAST(NULL AS DOUBLE) AS po_quantity "
            "FROM upload_inventory", [value.upper()])
    finally:
        con.close()
    made[INVENTORY_VIEW] = cols


def _pii(run, replaced: list) -> list:
    """The PII list of every warehouse source whose table a file replaces: one PII list
    per table must not drift because the rows came from a file (conservative union)."""
    out = set()
    for table in replaced:
        ref = run.tables.get(table)
        if ref:
            out.update((ref.datasource.get("mdl") or {}).get("pii_columns") or [])
    return sorted(out)


def attach(run, tenant_id: str, input_id: str) -> None:
    """Load the attached files into the run; never fails the report (a broken file set
    shows as an open item on the files page)."""
    from .mbr_engine import TableRef
    run.uploads = {"input_id": input_id, "files": [], "tables": {}, "replaced": [], "error": None}
    folder = tempfile.mkdtemp(prefix="poros-mbr-")
    run.cleanup.append(lambda: shutil.rmtree(folder, ignore_errors=True))
    try:
        bundle = _load(tenant_id, input_id)
        path = os.path.join(folder, "inputs.duckdb")
        made = _write(path, bundle, run.brand)
        cfg = run.config.brand
        _inventory_view(path, made, cfg.inventory_column or "brand_ref_dpim",
                        (cfg.inventory_values or [run.brand])[0])
    except Exception as exc:                      # the warehouse report still ships
        log.exception("attached files could not be loaded input=%s", input_id)
        run.uploads["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return
    run.uploads["files"] = [asdict(f) for f in bundle.files]
    run.uploads["tables"] = {t: len(bundle.tables.get(t) or []) for t in made}
    replaced = [t for t in made if not t.startswith("upload_")]
    run.uploads["replaced"] = replaced
    ds = {"id": f"upload_{input_id}", "name": SOURCE_NAME, "type": "duckdb",
          "conn": {"path": path},
          "mdl": {"tenant_id": tenant_id, "tables": {t: list(c) for t, c in made.items()},
                  "allowed_tables": sorted(made), "pii_columns": _pii(run, replaced)}}
    for table, cols in made.items():
        run.tables[table] = TableRef(table, ds, {c.lower() for c in cols}, "duckdb")
