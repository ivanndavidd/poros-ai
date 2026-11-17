"""Seed a tenant's businesses (Acme: 1P / 3P / GDV) and move its curation onto them.

Knowledge used to live in one place per datasource: ``datasources.mdl``. It now lives per
(business, datasource) pair, so 1P and 3P can describe the SAME physical table differently
and a business can span datasources -- 3P reads L2+L3 while 1P reads L2 only.

This script creates the businesses and COPIES the datasource's existing curation into each
one. Copying, not moving: ``datasources.mdl`` is left exactly as it is, so nothing depends
on this script having run and a rollback is simply ignoring the new tables. Two things stay
on the datasource by design and are NOT copied -- ``pii_columns`` (a hard rule; one shared
list per table must not drift between businesses) and ``grain`` (a property of the table,
not of who reads it).

By default every SQL datasource is attached to every business with the FULL allowlist,
which reproduces today's behaviour exactly. Narrowing is then a review job in the console's
Knowledge Base tree -- or do it here with --assign when the split is already known:

  cd backend
  python scripts/init_businesses.py --tenant "Acme Assistant" --env-file .env
  python scripts/init_businesses.py --tenant "Acme Assistant" --env-file .env \
      --assign "3P=ds_l2,ds_l3" --assign "1P=ds_l2" --assign "GDV=ds_datamart"
  # ...same command with --apply once the report reads right

Dry run by default. Re-runnable: an existing business is reused, and a scope that already
holds curation is left alone unless --overwrite says otherwise.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_NAMES = ["1P", "3P", "GDV"]

# Curation that becomes per-business. pii_columns and grain are deliberately absent —
# see the module docstring.
COPIED = ("allowed_tables", "descriptions", "metrics", "business_concepts",
          "routing_aliases", "reference_values", "examples")

# Documents are not scoped by business yet: retrieval is governed by per-document ACL
# roles, a separate mechanism. Only SQL sources take part.
NON_SQL_TYPES = {"docs"}


def _load_env(path: str) -> None:
    if not path:
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        sys.exit("python-dotenv is not installed; export ADMIN_DB_URL instead of --env-file")
    if not load_dotenv(path, override=False):
        sys.exit(f"could not read env file: {path}")


def _require_schema(store, init_schema: bool) -> None:
    """The business tables are created by store.init(), which only runs when the backend
    starts. Say that, rather than letting a raw "relation does not exist" surface."""
    if init_schema:
        store.init()
        print("schema: store.init() ran (idempotent)")
        return
    import psycopg2
    try:
        store.list_businesses("__probe__")
    except psycopg2.errors.UndefinedTable:
        sys.exit("the business tables do not exist in this database yet. Restart the backend "
                 "once so store.init() creates them, or re-run this with --init-schema.")
    except Exception as exc:
        sys.exit(f"cannot reach the metadata store ({type(exc).__name__}: {exc})")


def _find_tenant(store, wanted: str) -> dict:
    tenants = store.list_tenants()
    for t in tenants:
        if t["id"] == wanted:
            return t
    matches = [t for t in tenants if (t.get("name") or "").lower() == wanted.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        sys.exit(f"{len(matches)} tenants are named {wanted!r}; pass the id instead")
    known = ", ".join(f"{t['id']} ({t.get('name')})" for t in tenants) or "(none)"
    sys.exit(f"no tenant with id or name {wanted!r}. Known: {known}")


def _parse_assign(values, names):
    """--assign "3P=ds_a,ds_b" -> {"3P": ["ds_a", "ds_b"]}; absent business = all sources."""
    out = {}
    for raw in values or []:
        if "=" not in raw:
            sys.exit(f"--assign expects BUSINESS=ds_id[,ds_id]; got {raw!r}")
        name, ids = raw.split("=", 1)
        name = name.strip()
        if name not in names:
            sys.exit(f"--assign names business {name!r}, which is not in --names ({names})")
        out[name] = [x.strip() for x in ids.split(",") if x.strip()]
    return out


def _curation_of(mdl: dict) -> dict:
    """The curated half of an MDL, in the business-knowledge shape."""
    from app import store
    return store.clean_knowledge({key: mdl.get(key) for key in COPIED})


def _describe(knowledge: dict) -> str:
    return (f"{len(knowledge['allowed_tables'])} tables · "
            f"{len(knowledge['descriptions'])} descriptions · "
            f"{len(knowledge['metrics'])} metrics · "
            f"{len(knowledge['business_concepts'])} concepts · "
            f"{len(knowledge['reference_values'])} dimensions · "
            f"{len(knowledge['examples'])} examples")


def _copy_scope(store, bid, name, ds, existing, explicit, overwrite, apply_mode):
    did = ds["id"]
    current = store.get_business_knowledge(bid, did) if bid else None
    if current is None and existing and not explicit:
        # A plain re-run must never widen a business that was deliberately narrowed:
        # the default "every datasource" would otherwise put back exactly what an
        # earlier --assign run left out. Attaching to an EXISTING business is only
        # ever an explicit act.
        print(f"    {ds['name']} ({did}): skipped — not in this business; "
              f'pass --assign "{name}={did}" to add it')
        return
    if current is not None and any(current[k] for k in store.KNOWLEDGE_KEYS) \
            and not overwrite:
        print(f"    {ds['name']} ({did}): kept — already curated "
              f"({_describe(current)}); --overwrite to replace")
        return
    knowledge = _curation_of(ds["mdl"])
    verb = "copy" if apply_mode else "would copy"
    print(f"    {ds['name']} ({did}): {verb} {_describe(knowledge)}")
    if apply_mode:
        store.set_business_knowledge(bid, did, knowledge)


def _init_business(store, tid, name, assign, sources, by_id, args):
    existing = store.get_business_by_name(tid, name)
    explicit = name in assign
    if existing:
        bid = existing["id"]
        print(f"\n  business {name}: reusing {bid}")
    elif args.apply:
        bid = store.create_business(tid, name)
        print(f"\n  business {name}: created {bid}")
    else:
        bid = None
        print(f"\n  business {name}: would be created")

    wanted = assign.get(name, [d["id"] for d in sources])
    for did in wanted:
        _copy_scope(store, bid, name, by_id[did], existing, explicit, args.overwrite, args.apply)

    for other in (d["id"] for d in sources if d["id"] not in wanted):
        attached = bool(bid) and store.get_business_knowledge(bid, other) is not None
        # detaching is never done here: it would silently discard curation
        note = ("STILL attached — detach it in the console if that is wrong"
                if attached else "not in this business")
        print(f"    {by_id[other]['name']} ({other}): {note}")


def _grant_businesses(store, tid, names, roles, apply_mode):
    found = [store.get_business_by_name(tid, n) for n in names]
    ids = [b["id"] for b in found if b]
    for role in roles:
        have = [x["id"] for x in store.list_role_businesses(tid, role)]
        # union, not replace: a run naming a subset of --names must not revoke grants an
        # earlier run made. Dry run reports intent — the businesses do not exist yet.
        merged = list(dict.fromkeys(have + ids))
        total = len(merged) if apply_mode else len(set(have) | set(names))
        if apply_mode:
            store.set_role_businesses(tid, role, merged)
        print(f"\n  role {role}: {'now has' if apply_mode else 'would have'} "
              f"{total} business(es)")


def _parse_args():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tenant", required=True, help="tenant id or exact name")
    ap.add_argument("--names", default=",".join(DEFAULT_NAMES),
                    help=f"comma-separated business names (default: {','.join(DEFAULT_NAMES)})")
    ap.add_argument("--assign", action="append", default=[],
                    help='limit one business to some datasources: "3P=ds_a,ds_b" (repeatable)')
    ap.add_argument("--roles", default="",
                    help="comma-separated roles to grant every business to "
                         "(default: every role the tenant has)")
    ap.add_argument("--overwrite", action="store_true",
                    help="replace curation on scopes that already have some")
    ap.add_argument("--init-schema", action="store_true",
                    help="create the business tables if the backend has not started since "
                         "this feature landed (runs store.init(), which is idempotent)")
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    ap.add_argument("--env-file", default="", help="dotenv file to read ADMIN_DB_URL from")
    return ap.parse_args()


def _business_names(raw: str) -> list:
    names = [x.strip() for x in raw.split(",") if x.strip()]
    if not names:
        sys.exit("--names left no business names")
    if len({n.lower() for n in names}) != len(names):
        sys.exit(f"--names repeats a name: {names}")
    return names


def _sql_sources(store, tenant: dict) -> list:
    tid = tenant["id"]
    sources = [d for d in store.list_datasources(tid, include_disabled=True)
               if d["type"] not in NON_SQL_TYPES]
    if not sources:
        sys.exit(f"tenant {tid} ({tenant.get('name')}) has no SQL datasource to scope")
    return sources


def _check_assign(assign: dict, by_id: dict) -> None:
    for name, ids in assign.items():
        unknown = [x for x in ids if x not in by_id]
        if unknown:
            sys.exit(f"--assign {name}: not a SQL datasource of this tenant: {unknown}")


def _roles(store, tid: str, raw: str) -> list:
    roles = ([x.strip() for x in raw.split(",") if x.strip()]
             if raw else [r["name"] for r in store.list_roles(tid)])
    missing = [r for r in roles if not store.get_role(tid, r)]
    if missing:
        sys.exit(f"--roles names roles this tenant does not have: {missing}")
    return roles


def main() -> None:
    args = _parse_args()

    _load_env(args.env_file)
    if not os.environ.get("ADMIN_DB_URL"):
        print("note: ADMIN_DB_URL is unset — falling back to the built-in default DSN")

    from app import store

    _require_schema(store, args.init_schema)

    names = _business_names(args.names)
    assign = _parse_assign(args.assign, names)

    tenant = _find_tenant(store, args.tenant)
    tid = tenant["id"]
    sources = _sql_sources(store, tenant)
    by_id = {d["id"]: d for d in sources}
    _check_assign(assign, by_id)
    roles = _roles(store, tid, args.roles)

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"[{mode}] tenant {tid} ({tenant.get('name')})")
    print("  datasources: " + ", ".join(f"{d['id']} ({d['name']}, {d['type']}, "
                                         f"{len(d['mdl'].get('tables') or {})} tables)"
                                         for d in sources))
    print(f"  roles to grant: {roles or '(none)'}")

    for name in names:
        _init_business(store, tid, name, assign, sources, by_id, args)

    if roles:
        _grant_businesses(store, tid, names, roles, args.apply)

    if not args.apply:
        print("\nDry run — nothing was written. Re-run with --apply.")
    else:
        print("\nDone. datasources.mdl was NOT modified; narrow each business in the "
              "console's Knowledge Base tree.")


if __name__ == "__main__":
    main()
