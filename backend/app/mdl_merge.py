"""Rescan merge: keep admin curation, park what the fresh scan no longer contains.

A rescan must preserve hand-written curation (that is the whole point of merging instead
of overwriting), but curation pointing at a table or column the database no longer
exposes is worse than no curation at all: it is still rendered into the NL->SQL prompt,
so the model is taught to query a table that is absent from ``allowed_tables``, and
``govern()`` then rejects the generated SQL as not-allowlisted. The user sees a blocked
answer with no explanation.

Vanished curation is therefore PARKED under ``curation_quarantine`` instead of deleted.
A table can disappear for reasons that have nothing to do with the catalog being wrong —
a partially failed scan, a revoked grant, a view rebuilt at the wrong moment. When it
comes back, the next rescan restores its curation verbatim. An infrastructure hiccup
must never destroy what an admin typed by hand.
"""
from __future__ import annotations

import json
import re

import sqlglot
from sqlglot import exp

from . import semantic_contract

QUARANTINE_KEY = "curation_quarantine"

# Curated layers carried over wholesale from the previous MDL.
_CARRIED_KEYS = (
    "metrics", "reference_values", "grain", "examples", "business_concepts",
    "routing_aliases",
)
# Layers that can reference a table/column and are therefore prunable + parkable.
_PARKED_DICTS = ("descriptions", "grain", "reference_values")
_PARKED_LISTS = ("examples", "business_concepts")
# Discovery output: re-derived by every scan, so filtered rather than parked.
_DISCOVERED_KEYS = ("table_metadata", "column_metadata", "relationships")
# Governance knobs an admin can edit; a rescan must not reset them to scan defaults.
_POLICY_KEYS = ("denied_functions", "allowed_functions")
# Everything this merge decides about explicitly. Any OTHER key found in the stored MDL is
# carried over untouched instead of being dropped for being unrecognized.
_MANAGED_KEYS = frozenset({
    "tables", "allowed_tables", "pii_columns", "descriptions", QUARANTINE_KEY,
    *_CARRIED_KEYS, *_DISCOVERED_KEYS, *_POLICY_KEYS,
})
# The quarantine is deliberately UNCAPPED: it can only ever hold curation the MDL already
# stored (parking moves entries, it never generates them), so the row grows by at most the
# size of the curated layers. Capping it would silently delete the very curation this key
# exists to protect — the failure this whole module is here to prevent.
#
# The REPORT is capped instead: counts stay exact, but the label lists stay toast-sized.
_MAX_REPORTED = 50


class ScanLostAllTables(ValueError):
    """A scan returning zero tables is treated as a failed scan, not as an empty database.

    Merging it would empty the allowlist and quarantine the entire catalog, leaving the
    datasource unusable until someone noticed. Refusing keeps the last good MDL live.
    """


# ---- liveness of the fresh scan ----
class _Live:
    """Case-insensitive index of what the fresh scan actually contains."""

    def __init__(self, tables: dict):
        self.tables, self.columns = {}, {}
        for table, columns in (tables or {}).items():
            key = str(table).lower()
            self.tables[key] = str(table)
            self.columns[key] = {str(column).lower() for column in (columns or [])}
        self.any_column = {column for columns in self.columns.values() for column in columns}

    def has_table(self, name) -> bool:
        return str(name).lower() in self.tables

    def has_key(self, key) -> bool:
        """Whether a ``table`` / ``table.column`` curation key still resolves.

        An unqualified key is accepted when it names a live table OR a live column: the
        catalog uses bare column names for reference values, and mislabelling curation as
        dead is more damaging than keeping one stale entry.
        """
        text = str(key)
        if self.has_table(text):
            return True
        table, _, column = text.partition(".")
        if column:
            return column.lower() in self.columns.get(table.lower(), set())
        return text.lower() in self.any_column

    def dead_tables(self, names) -> list[str]:
        return sorted({str(name) for name in names if not self.has_table(name)})


def _table_of(key) -> str:
    return str(key).partition(".")[0]


# ---- example SQL ----
_FROM_RE = re.compile(r"\b(?:from|join)\s+`?([a-z_][\w.-]*)`?", re.I | re.ASCII)


def _sql_tables(sql) -> set[str]:
    """Physical tables an exemplar query reads, lowercased and unqualified.

    CTE aliases are excluded: they are defined by the query itself, so counting them
    would make a `WITH`-based example look like it referenced a missing table.
    """
    text = str(sql or "")
    try:
        trees = [tree for tree in sqlglot.parse(text) if tree is not None]
    except Exception:                       # governance owns syntax diagnostics
        trees = []
    if not trees:
        return {name.split(".")[-1].lower() for name in _FROM_RE.findall(text)}
    names, ctes = set(), set()
    for tree in trees:
        ctes |= {str(cte.alias_or_name).lower() for cte in tree.find_all(exp.CTE)}
        names |= {str(table.name).lower() for table in tree.find_all(exp.Table) if table.name}
    return names - ctes


def _example_label(example: dict) -> str:
    return str((example or {}).get("q") or (example or {}).get("sql") or "")[:120]


# ---- per-layer split into (kept, dropped) ----
def _split_dict(source, alive) -> tuple[dict, dict]:
    kept, dropped = {}, {}
    for key, value in (source or {}).items():
        (kept if alive(key) else dropped)[key] = value
    return kept, dropped


def _split_examples(examples, live: _Live) -> tuple[list, list, list]:
    """Drop only examples whose every referenced table vanished.

    A mixed example (one live table joined to a vanished one) is kept — deleting a
    mostly-good query is the more destructive mistake — but it is reported so an admin
    can repair it instead of waiting for a blocked answer.
    """
    kept, dropped, stale = [], [], []
    for example in examples or []:
        referenced = _sql_tables((example or {}).get("sql"))
        gone = live.dead_tables(referenced)
        if gone and len(gone) == len(referenced):
            dropped.append(example)
            continue
        kept.append(example)
        if gone:
            stale.append(
                f"example '{_example_label(example)}' still references missing "
                f"table(s): {', '.join(gone)}"
            )
    return kept, dropped, stale


def _split_concepts(mdl: dict) -> tuple[list, list, list]:
    """Split business concepts with the SAME checker the admin editor uses.

    ``partition_definitions`` triages per definition instead of failing the whole list, so
    one dead contract cannot decide the fate of the others. Concepts rejected for a reason
    unrelated to this scan (bad formula, duplicate name) are parked too: they are already
    invisible to ``resolve_mdl``, and parking them reports why instead of leaving them
    silently dead inside the catalog.
    """
    kept, rejected = semantic_contract.partition_definitions(
        mdl, mdl.get("business_concepts") or [],
    )
    return kept, [item["definition"] for item in rejected], [item["reason"] for item in rejected]


# ---- quarantine ----
def _signature(item) -> str:
    try:
        return json.dumps(item, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(item)


def _identity(key: str, item) -> str:
    """Stable identity for a parked list entry.

    A concept is identified by name, not by content: ``partition_definitions`` returns a
    NORMALIZED definition, so comparing whole objects would never match the raw copy in
    quarantine and a revived contract would go unreported.
    """
    if key == "business_concepts" and isinstance(item, dict):
        return str(item.get("name") or "").strip().lower() or _signature(item)
    return _signature(item)


def _dedupe(key: str, items) -> list:
    """Live curation wins over a restored copy of the same entry."""
    seen, out = set(), []
    for item in items or []:
        marker = _identity(key, item)
        if marker not in seen:
            seen.add(marker)
            out.append(item)
    return out


def _label(key: str, item) -> str:
    if key == "business_concepts":
        return str((item or {}).get("name") or "")
    return _example_label(item)


def _restored_source(old: dict) -> dict:
    """Fold parked curation back in before merging, so a returning table revives it.

    Live curation always wins a key collision: whatever an admin edited after the table
    vanished is newer than the parked copy.
    """
    parked = old.get(QUARANTINE_KEY)
    source = {key: value for key, value in (old or {}).items() if key != QUARANTINE_KEY}
    if not isinstance(parked, dict):
        return source
    for key in _PARKED_DICTS:
        values = parked.get(key)
        if isinstance(values, dict) and values:
            source[key] = {**values, **(source.get(key) or {})}
    for key in _PARKED_LISTS:
        items = parked.get(key)
        if isinstance(items, list) and items:
            source[key] = _dedupe(key, list(source.get(key) or []) + list(items))
    return source


def reinstate(mdl: dict) -> dict:
    """Fold parked curation back into the live layers and clear the quarantine.

    The inverse of parking, for a targeted repair or an admin "give me my curation back"
    action. Live curation still wins a key collision.
    """
    return _restored_source(mdl or {})


def _route_dict_entries(bundle: dict, wanted: set, mine: dict, rest: dict) -> None:
    for key in _PARKED_DICTS:
        for name, value in (bundle.get(key) or {}).items():
            side = mine if _table_of(name).lower() in wanted else rest
            side.setdefault(key, {})[name] = value


def _route_list_entries(bundle: dict, wanted: set, mine: dict, rest: dict) -> None:
    for item in bundle.get("examples") or []:
        side = mine if _sql_tables((item or {}).get("sql")) & wanted else rest
        side.setdefault("examples", []).append(item)
    for item in bundle.get("business_concepts") or []:
        side = mine if str((item or {}).get("table") or "").lower() in wanted else rest
        side.setdefault("business_concepts", []).append(item)


def select_curation(bundle: dict, tables) -> tuple[dict, dict]:
    """Split a parked bundle into ``(belonging to `tables`, everything else)``.

    A catalog can lose many tables at once — a limited scan, a revoked dataset grant. This
    lets an operator act on the one table they have actually diagnosed instead of deciding
    the fate of every missing table in the same stroke.
    """
    bundle = bundle or {}
    wanted = {str(table).lower() for table in tables or ()}
    mine, rest = {}, {}
    _route_dict_entries(bundle, wanted, mine, rest)
    _route_list_entries(bundle, wanted, mine, rest)
    return mine, rest


def _parked_layer(parked: dict, key: str):
    value = (parked or {}).get(key)
    if key in _PARKED_DICTS:
        return value if isinstance(value, dict) else {}
    return value if isinstance(value, list) else []


def _restored_report(parked: dict, merged: dict) -> dict:
    """Which previously parked entries are live again after this merge."""
    report = {}
    for key in _PARKED_DICTS:
        live_keys = set(merged.get(key) or {})
        report[key] = sorted(k for k in _parked_layer(parked, key) if k in live_keys)
    for key in _PARKED_LISTS:
        live = {_identity(key, item) for item in (merged.get(key) or [])}
        report[key] = [
            _label(key, item) for item in _parked_layer(parked, key)
            if _identity(key, item) in live
        ]
    return report


def _quarantine(dropped: dict) -> dict:
    return {key: value for key, value in dropped.items() if value}


# ---- discovery metadata carried over from a poorer scan ----
def _filtered_metadata(key: str, value, live: _Live):
    """Machine-discovered metadata is filtered, not parked — a rescan re-derives it."""
    if key == "relationships":
        return [
            item for item in (value or [])
            if live.has_table(item.get("from_table")) and live.has_table(item.get("to_table"))
        ]
    alive = live.has_table if key == "table_metadata" else live.has_key
    return {name: item for name, item in (value or {}).items() if alive(name)}


# ---- merge ----
def _carry_discovered(old: dict, merged: dict, live: _Live) -> None:
    """A temporary loss of permission to rich catalog views must not erase metadata
    captured by a previous successful discovery."""
    for key in _DISCOVERED_KEYS:
        if not merged.get(key) and old.get(key):
            carried = _filtered_metadata(key, old[key], live)
            if carried:
                merged[key] = carried


def _carry_admin_keys(old: dict, merged: dict) -> None:
    # Function policy is admin-edited (PATCH /mdl), not discovered — keep it instead of
    # resetting it to the scanner's defaults on every rescan.
    for key in _POLICY_KEYS:
        if old.get(key):
            merged[key] = old[key]
    # Anything else an admin or a migration script put in this MDL (enrichment notes, a
    # `kind` marker) is preserved as-is. A scan discovers schema; it does not get to decide
    # that a key it has never heard of is worthless.
    for key, value in old.items():
        if key not in _MANAGED_KEYS and key not in merged:
            merged[key] = value


def _carry_curation(old: dict, fresh: dict) -> dict:
    """The original merge: admin enrichment survives a rescan."""
    merged = dict(fresh)
    tables = merged.get("tables") or {}
    merged["allowed_tables"] = sorted(
        set(tables) & set(old.get("allowed_tables", merged.get("allowed_tables") or []))
    ) or (merged.get("allowed_tables") or [])
    merged["pii_columns"] = sorted(
        set(merged.get("pii_columns") or []) | set(old.get("pii_columns") or [])
    )
    # Curated values win, while newly discovered descriptions fill prior gaps.
    merged["descriptions"] = {
        **(merged.get("descriptions") or {}),
        **(old.get("descriptions") or {}),
    }
    for key in _CARRIED_KEYS:
        if old.get(key):
            merged[key] = old[key]
    _carry_discovered(old, merged, _Live(tables))
    _carry_admin_keys(old, merged)
    return merged


def _prune(merged: dict) -> tuple[dict, dict, list]:
    """Strip curation the fresh scan no longer supports; return (mdl, dropped, notes)."""
    live = _Live(merged.get("tables") or {})
    dropped, notes = {}, []
    merged["descriptions"], dropped["descriptions"] = _split_dict(
        merged.get("descriptions"), live.has_key,
    )
    merged["grain"], dropped["grain"] = _split_dict(merged.get("grain"), live.has_table)
    merged["reference_values"], dropped["reference_values"] = _split_dict(
        merged.get("reference_values"), live.has_key,
    )
    merged["examples"], dropped["examples"], stale = _split_examples(
        merged.get("examples"), live,
    )
    notes.extend(stale)
    merged["business_concepts"], dropped["business_concepts"], reasons = _split_concepts(merged)
    notes.extend(reasons)
    for key in (*_PARKED_DICTS, *_PARKED_LISTS):
        if not merged.get(key):
            merged.pop(key, None)
    return merged, dropped, notes


def _qualifiers(keys) -> set[str]:
    """Table names implied by ``table.column`` keys.

    An unqualified key names a bare column (reference values and enricher output use
    them); it says nothing about which table is missing, so it contributes no name.
    """
    return {_table_of(key) for key in keys or () if "." in str(key)}


def _vanished_tables(dropped: dict, live: _Live) -> list[str]:
    names = _qualifiers(dropped.get("descriptions") or {})
    names |= set(dropped.get("grain") or {})            # grain keys ARE table names
    names |= _qualifiers(dropped.get("reference_values") or {})
    for example in dropped.get("examples") or []:
        names |= set(_sql_tables((example or {}).get("sql")))
    names |= {
        str((concept or {}).get("table") or "")
        for concept in dropped.get("business_concepts") or []
    }
    return live.dead_tables(name for name in names if name)


def _newly_dropped(dropped: dict, parked: dict) -> dict:
    """Entries lost by THIS rescan — anything already parked is not news to report."""
    fresh_loss = {}
    for key in _PARKED_DICTS:
        known = set(_parked_layer(parked, key))
        fresh_loss[key] = sorted(k for k in (dropped.get(key) or {}) if k not in known)
    for key in _PARKED_LISTS:
        known = {_identity(key, item) for item in _parked_layer(parked, key)}
        fresh_loss[key] = [
            _label(key, item) for item in (dropped.get(key) or [])
            if _identity(key, item) not in known
        ]
    return fresh_loss


def _parked_total(quarantine: dict) -> int:
    return sum(len(value) for value in (quarantine or {}).values())


def _summary(dropped: dict, parked: dict, merged: dict, notes: list) -> dict:
    """What the rescan response tells the admin, so curation is never lost silently.

    Counts are exact; the label/name lists are capped because a badly broken scan can
    strand hundreds of keys and this payload only has to be readable in a toast.
    """
    labels = _newly_dropped(dropped, parked)
    restored = _restored_report(parked, merged)
    dropped_total = sum(len(value) for value in labels.values())
    return {
        "vanished_tables": _vanished_tables(dropped, _Live(merged.get("tables") or {}))[:_MAX_REPORTED],
        "dropped": {key: value[:_MAX_REPORTED] for key, value in labels.items()},
        "dropped_total": dropped_total,
        "restored": {key: value[:_MAX_REPORTED] for key, value in restored.items()},
        "restored_total": sum(len(value) for value in restored.values()),
        "still_parked_total": max(_parked_total(merged.get(QUARANTINE_KEY)) - dropped_total, 0),
        "quarantined": bool(merged.get(QUARANTINE_KEY)),
        "notes": notes[:_MAX_REPORTED],
    }


def merge_scan(old: dict, fresh: dict) -> tuple[dict, dict]:
    """Merge a fresh scan into the stored MDL.

    Returns ``(mdl, pruned)``. ``mdl`` is what gets persisted; ``pruned`` is a report for
    the admin — never stored — describing curation that was parked or revived.
    """
    previous = old or {}
    parked = previous.get(QUARANTINE_KEY) if isinstance(previous.get(QUARANTINE_KEY), dict) else {}
    source = _restored_source(previous)
    if not (fresh or {}).get("tables") and source.get("tables"):
        raise ScanLostAllTables(
            "scan returned no tables; keeping the previous catalog instead of emptying it"
        )
    merged, dropped, notes = _prune(_carry_curation(source, dict(fresh or {})))
    quarantine = _quarantine(dropped)
    if quarantine:
        merged[QUARANTINE_KEY] = quarantine
    else:
        merged.pop(QUARANTINE_KEY, None)
    return merged, _summary(dropped, parked, merged, notes)
