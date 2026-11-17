"""Correct `pii_columns` on the live Acme datasources (security review, finding 16).

The old name scan in `schema_scan` matched raw substrings, so business dimensions were
stored as personal data. That is not a harmless over-block: `mask_pii_for_llm` rewrites
those VALUES to "[PII:<col>]" before every LLM prompt, so the model was answering
"which warehouse should we expand" while seeing "[PII:warehouse_name]" on every row.

`schema_scan.is_pii_column` no longer produces these, but a rescan cannot repair the
stored list on its own: `admin.patch/rescan` UNIONS the old and fresh lists
(routers/admin.py) so a marking is never silently lost. The stored rows therefore have
to be corrected explicitly, which is what this script does.

Removals are an EXPLICIT list agreed with the data owner rather than a re-derivation,
so a column an administrator added by hand is never dropped by a heuristic. The script
asserts the current classifier agrees with every removal, so the code and the data
cannot drift apart.

Additions come from the classifier and are limited to columns that still exist in the
catalogue — `latitude`/`longitude` on the order tables are buyer delivery coordinates
that the old substring list missed entirely.

Usage (dry run prints the diff and changes nothing):
    python -m scripts.fix_pii_classification
    python -m scripts.fix_pii_classification --apply
"""
import argparse
import json
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from app import store                      # noqa: E402
from app.schema_scan import is_pii_column  # noqa: E402

# Business dimensions wrongly stored as PII. Reviewed column by column with the data
# owner on 2026-08-11; content-creator and company names were an explicit decision.
REMOVE = {
    "IMS Acme": [
        "Material_Name", "material_name", "Organization_Name",
        "Storage_Area_Name", "Storage_Site_Name", "business_partner_name",
        "logistics_service_name", "warehouse_name",
    ],
    "Acme L2": [
        "brand_name", "product_name", "product_name_pair", "store_address",
        "sheet_name", "table_name", "pembeli_unik",
        # owner decision: creator accounts are treated as business identities
        "creator_name", "creator_username", "nama_kreator",
    ],
}


def _mdl(row):
    return row["mdl"] if isinstance(row["mdl"], dict) else json.loads(row["mdl"] or "{}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the change (default: dry run)")
    args = ap.parse_args()

    conn = store._c()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, name, mdl FROM datasources WHERE type NOT IN ('docs')"
    ).fetchall()]

    exit_code, changed = 0, 0
    for row in rows:
        mdl = _mdl(row)
        stored = list(mdl.get("pii_columns") or [])
        if not stored:
            continue
        present = {c for cols in (mdl.get("tables") or {}).values() for c in cols}
        remove = [c for c in REMOVE.get(row["name"], []) if c in stored]
        # Guard: the classifier must agree, so the data fix cannot outlive the code fix.
        disputed = [c for c in remove if is_pii_column(c)]
        if disputed:
            print(f"ABORT {row['name']}: classifier still calls these PII: {disputed}")
            exit_code = 1
            continue
        add = sorted(c for c in present if is_pii_column(c) and c not in stored)
        if not remove and not add:
            continue
        new = sorted((set(stored) - set(remove)) | set(add))

        print(f"\n=== {row['name']} ({row['id']})  {len(stored)} -> {len(new)} pii_columns")
        print(f"  unmask ({len(remove)}): {remove}")
        print(f"  mask   ({len(add)}): {add}")
        # Printed so the previous state is recoverable from this run's output alone.
        print(f"  previous value: {json.dumps(stored)}")
        if args.apply:
            mdl["pii_columns"] = new
            store.update_mdl(row["id"], mdl)
            changed += 1

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply to save.")
    else:
        print(f"\nUpdated {changed} datasource(s).")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
