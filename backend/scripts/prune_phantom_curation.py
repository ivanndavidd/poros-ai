"""Repair curation left behind by rescans that ran before mdl_merge learned to prune it.

An older rescan dropped vanished tables from ``tables``/``allowed_tables`` but kept their
descriptions, grain, reference values, exemplar SQL and business concepts. That curation
still reached the NL->SQL prompt, so the model was taught to query a table govern() then
rejected as not-allowlisted.

This re-runs the SAME merge (app.mdl_merge) offline: the MDL's own schema stands in for
the fresh scan, so nothing is dropped that the catalog itself still claims exists — and no
database connection (nor Vault-held credential) is needed, because only the plain ``mdl``
column is read and written. Removed curation is parked under ``curation_quarantine`` and
comes back by itself on the next rescan that sees the table again.

Dry run by default. Re-runnable: a second pass reports nothing to do.

  cd backend
  python scripts/prune_phantom_curation.py --datasource ds_6ebf561d --env-file .env \
      --only-tables sample_inventory_daily_snapshot
  # ...same command with --apply once the report looks right

Prefer --only-tables when a catalog lost MANY tables at once: that usually means a partial
or narrowed scan rather than a real schema change, and each missing table deserves its own
verdict. Without it, every table absent from the catalog has its curation parked.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)

# Only the schema/governance half of an MDL says what EXISTS; the curated half is what
# gets validated against it.
_SCAN_KEYS = ("tables", "allowed_tables", "pii_columns", "denied_functions", "allowed_functions")


def _load_env(path: str) -> None:
    if not path:
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        sys.exit("python-dotenv is not installed; export ADMIN_DB_URL instead of --env-file")
    if not load_dotenv(path, override=False):
        sys.exit(f"could not read env file: {path}")


def _as_scan(mdl: dict) -> dict:
    """The catalog's own live schema, shaped like a connector scan result."""
    return {key: mdl[key] for key in _SCAN_KEYS if key in mdl}


def _count(bundle: dict) -> int:
    return sum(len(value) for value in (bundle or {}).values())


def _labels(values) -> list:
    if isinstance(values, dict):
        return sorted(values)
    return [str((item or {}).get("q") or (item or {}).get("name") or item)[:110] for item in values]


def _print_layer(title: str, values) -> None:
    if values:
        print(f"    {title} ({len(values)}):")
        for value in _labels(values):
            print(f"      - {value}")


def _print_full_report(pruned: dict) -> None:
    print(f"  vanished tables    : {len(pruned['vanished_tables'])} "
          f"{', '.join(pruned['vanished_tables'][:6])}{' ...' if len(pruned['vanished_tables']) > 6 else ''}")
    print(f"  curation to park   : {pruned['dropped_total']}")
    print(f"  already parked     : {pruned['still_parked_total']}")
    print(f"  restored (revived) : {pruned['restored_total']}")
    for layer, values in pruned["dropped"].items():
        _print_layer(layer, values)
    for note in pruned["notes"]:
        print(f"    note: {note}")


def _other_changes(stored: dict, repaired: dict, quarantine_key: str) -> list:
    """Differences a targeted repair did not explicitly ask for — printed, never hidden."""
    out = []
    for key in sorted(set(stored) | set(repaired)):
        before, after = stored.get(key), repaired.get(key)
        if key == quarantine_key or before == after:
            continue
        size = (f"{len(before or ())} -> {len(after or ())}"
                if hasattr(before or (), "__len__") else "changed")
        out.append(f"{key}: {size}")
    return out


def _repair(mdl_merge, stored: dict, only: list) -> tuple[dict, dict, dict]:
    """Return ``(repaired_mdl, parked_bundle, full_report)``."""
    merged, pruned = mdl_merge.merge_scan(stored, _as_scan(stored))
    if not only:
        return merged, merged.get(mdl_merge.QUARANTINE_KEY) or {}, pruned
    mine, rest = mdl_merge.select_curation(
        merged.get(mdl_merge.QUARANTINE_KEY) or {}, only,
    )
    repaired = mdl_merge.reinstate({**merged, mdl_merge.QUARANTINE_KEY: rest})
    if mine:
        repaired[mdl_merge.QUARANTINE_KEY] = mine
    return repaired, mine, pruned


def _process(mdl_merge, store, row: dict, args) -> int:
    stored = json.loads(row["mdl"] or "{}")
    print(f"\n{row['id']}  {row['name']}  (tenant {row['tenant_id']})")
    if not stored.get("tables"):
        print("  no SQL catalog (documentation source) — skipped")
        return 0
    repaired, parked, pruned = _repair(mdl_merge, stored, args.only_tables)
    if args.only_tables:
        print(f"  targeting          : {', '.join(args.only_tables)}")
        print(f"  curation to park   : {_count(parked)}")
        print(f"  (whole-catalog scan would have parked {pruned['dropped_total']} "
              f"across {len(pruned['vanished_tables'])} missing tables)")
        for layer, values in parked.items():
            _print_layer(layer, values)
    else:
        _print_full_report(pruned)
    for line in _other_changes(stored, repaired, mdl_merge.QUARANTINE_KEY):
        print(f"  also changes       : {line}")
    if repaired == stored:
        print("  nothing to do")
        return 0
    if not args.apply:
        print("  DRY RUN — re-run with --apply to write this back")
        return 0
    store.update_mdl(row["id"], repaired)
    print(f"  APPLIED — {_count(parked)} entries parked under mdl['{mdl_merge.QUARANTINE_KEY}']")
    return 1


def _fetch(store, wanted: str, take_all: bool) -> list:
    c = store._c()
    try:
        rows = c.execute("SELECT id, tenant_id, name, mdl FROM datasources").fetchall()
    finally:
        c.close()
    return [row for row in rows if take_all or row["id"] == wanted]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--datasource", help="datasource id to repair")
    target.add_argument("--all", action="store_true", help="inspect every datasource")
    parser.add_argument("--env-file", default="", help="path to a .env holding ADMIN_DB_URL")
    parser.add_argument("--only-tables", nargs="+", default=[], metavar="TABLE",
                        help="park curation for these missing tables only")
    parser.add_argument("--apply", action="store_true", help="write the repaired MDL back")
    args = parser.parse_args()

    _load_env(args.env_file)
    from app import mdl_merge, store           # noqa: E402  — after the env is loaded

    rows = _fetch(store, args.datasource, args.all)
    if not rows:
        print(f"no datasource matched {args.datasource!r}")
        return 1
    changed = sum(_process(mdl_merge, store, row, args) for row in rows)
    print(f"\n{len(rows)} datasource(s) inspected, {changed} updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
