"""Backend-owned live discovery, profiling, and semantic verification.

The LLM never receives raw access to INFORMATION_SCHEMA/system catalogs.  Each
connector normalizes its metadata here; this service then intersects it with the
datasource MDL and role-visible table set before returning anything to the research
agent.  Profiling uses an exact metadata-resolved relation and a bounded sample, never
arbitrary model-authored profiling SQL.
"""
from __future__ import annotations

import datetime as dt
import decimal
import json
import math
import os
import re
import threading
import time
from collections import Counter

from .connectors.factory import make_connector


_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
_CACHE_LOCK = threading.Lock()
_WORD = re.compile(r"[a-z0-9_]+", re.I)
_NUMERIC = (
    "int", "decimal", "numeric", "number", "float", "double", "real",
    "money", "bigdecimal", "bignumeric",
)
_TEMPORAL = ("date", "time", "timestamp", "datetime", "interval")
_BOOLEAN = ("bool", "bit")


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(low, min(value, high))


def clear_cache(datasource_id: str | None = None) -> None:
    """Test/admin helper; normal request flow relies on the short TTL."""
    with _CACHE_LOCK:
        if datasource_id is None:
            _CACHE.clear()
        else:
            for key in list(_CACHE):
                if key[0] == datasource_id:
                    _CACHE.pop(key, None)


def _live(ds: dict, refresh: bool = False) -> tuple[dict, object]:
    key = (str(ds.get("id") or ""), str(ds.get("type") or ""))
    ttl = _env_int("DISCOVERY_CACHE_TTL_SEC", 180, 0, 3600)
    now = time.monotonic()
    if not refresh and ttl:
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached and now - cached[0] <= ttl:
                return cached[1], make_connector(ds["type"], ds["conn"])
    connector = make_connector(ds["type"], ds["conn"])
    catalog = connector.discover()
    catalog = catalog if isinstance(catalog, dict) else {}
    catalog.setdefault("dialect", connector.dialect)
    catalog.setdefault("relations", [])
    catalog["discovered_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    if ttl:
        with _CACHE_LOCK:
            _CACHE[key] = (now, catalog)
    return catalog, connector


def _tokens(value) -> set[str]:
    return {token.lower() for token in _WORD.findall(str(value or "")) if len(token) > 1}


def _visible_names(visible_mdl: dict) -> dict[str, str]:
    tables = (visible_mdl or {}).get("tables") or {}
    return {str(name).lower(): str(name) for name in tables}


def _relation_aliases(relation: dict) -> set[str]:
    name = str(relation.get("name") or "")
    namespace = str(relation.get("namespace") or "")
    aliases = {name.lower()}
    if namespace:
        aliases.add(f"{namespace}.{name}".lower())
    return aliases


def _authorized_relations(catalog: dict, visible_mdl: dict) -> tuple[list[dict], int]:
    visible = _visible_names(visible_mdl)
    relations, hidden = [], 0
    for relation in catalog.get("relations") or []:
        matched = next((visible[alias] for alias in _relation_aliases(relation) if alias in visible), None)
        if not matched:
            hidden += 1
            continue
        item = dict(relation)
        item["mdl_name"] = matched
        relations.append(item)
    return relations, hidden


def _column_names(relation: dict) -> list[str]:
    return [str(column.get("name") or "") for column in relation.get("columns") or []]


def _semantic_overlay(visible_mdl: dict, relation: dict) -> dict:
    table = relation["mdl_name"]
    descriptions = (visible_mdl or {}).get("descriptions") or {}
    grain = (visible_mdl or {}).get("grain") or {}
    metrics = []
    for metric in (visible_mdl or {}).get("metrics") or []:
        blob = json.dumps(metric, ensure_ascii=False, default=str).lower()
        if table.lower() in blob:
            metrics.append(metric)
    return {
        "description": str(descriptions.get(table) or relation.get("description") or "")[:1000],
        "grain": str(grain.get(table) or "")[:1000],
        "metrics": metrics[:12],
    }


def _relation_view(visible_mdl: dict, relation: dict) -> dict:
    table = relation["mdl_name"]
    mdl_columns = list(((visible_mdl or {}).get("tables") or {}).get(table) or [])
    live_columns = _column_names(relation)
    mdl_lower = {str(name).lower() for name in mdl_columns}
    live_lower = {str(name).lower() for name in live_columns}
    visible_tables = _visible_names(visible_mdl)
    primary_key = [
        name for name in relation.get("primary_key") or []
        if str(name).lower() in mdl_lower
    ]
    foreign_keys = []
    for item in relation.get("foreign_keys") or []:
        target = str(item.get("references_table") or "")
        target_namespace = str(item.get("references_namespace") or "")
        target_aliases = {target.lower()}
        if target_namespace:
            target_aliases.add(f"{target_namespace}.{target}".lower())
        if (
            str(item.get("column") or "").lower() in mdl_lower
            and target_aliases & set(visible_tables)
        ):
            foreign_keys.append(item)
    partitioning = relation.get("partitioning")
    if isinstance(partitioning, dict) and partitioning.get("field"):
        if str(partitioning["field"]).lower() not in mdl_lower:
            partitioning = {"type": partitioning.get("type"), "field": None}
    return {
        "table": table,
        "namespace": relation.get("namespace") or "",
        "kind": relation.get("kind") or "table",
        "estimated_rows": relation.get("estimated_rows"),
        "estimated_bytes": relation.get("estimated_bytes"),
        "partitioning": partitioning,
        "clustering": [
            name for name in relation.get("clustering") or []
            if str(name).lower() in mdl_lower
        ],
        "primary_key": primary_key,
        "foreign_keys": foreign_keys,
        "columns": [{
            "name": column.get("name"),
            "data_type": column.get("data_type") or "",
            "nullable": column.get("nullable"),
            "description": str(column.get("description") or "")[:500],
        } for column in (relation.get("columns") or [])
            if str(column.get("name") or "").lower() in mdl_lower],
        "semantic": _semantic_overlay(visible_mdl, relation),
        "schema_drift": {
            # New live column names remain hidden until an admin approves them into
            # the MDL allowlist. The agent only needs to know that drift exists.
            "live_not_registered_count": sum(
                name.lower() not in mdl_lower for name in live_columns
            ),
            "registered_not_live": [
                name for name in mdl_columns if name.lower() not in live_lower
            ][:50],
        },
    }


def discover_datasource(ds: dict, visible_mdl: dict, query: str = "", limit: int = 30,
                        refresh: bool = False) -> dict:
    """Discover current authorized relations and compare them with the MDL snapshot."""
    catalog, _connector = _live(ds, refresh=refresh)
    relations, hidden = _authorized_relations(catalog, visible_mdl)
    query_tokens = _tokens(query)
    views = []
    for relation in relations:
        view = _relation_view(visible_mdl, relation)
        blob = " ".join([
            view["table"],
            view["semantic"]["description"],
            view["semantic"]["grain"],
            " ".join(str(column.get("name") or "") for column in view["columns"]),
        ])
        overlap = len(query_tokens & _tokens(blob))
        name_overlap = len(query_tokens & _tokens(view["table"]))
        view["relevance_score"] = name_overlap * 4 + overlap
        views.append(view)
    views.sort(key=lambda item: (
        -int(item.get("relevance_score") or 0),
        str(item.get("table") or "").lower(),
    ))
    cap = max(1, min(int(limit or 30), 100))
    return {
        "ok": True,
        "source": "live_connector_metadata",
        "datasource_id": ds.get("id"),
        "datasource_name": ds.get("name"),
        "datasource_type": ds.get("type"),
        "dialect": catalog.get("dialect"),
        "discovered_at": catalog.get("discovered_at"),
        "authorized_relation_count": len(relations),
        "hidden_relation_count": hidden,
        # Detailed relation metadata is relevance-capped, but the complete authorized
        # name index stays visible so a broad research question can discover a less
        # obvious evidence family instead of repeatedly querying the first ranked table.
        "relation_index": [
            str(item.get("table") or "") for item in views[:500]
            if item.get("table")
        ],
        "relations": views[:cap],
        "truncated": len(views) > cap,
    }


def _resolve_relation(ds: dict, visible_mdl: dict, table: str,
                      refresh: bool = False) -> tuple[dict, object]:
    catalog, connector = _live(ds, refresh=refresh)
    relations, _hidden = _authorized_relations(catalog, visible_mdl)
    target = str(table or "").strip().lower()
    matches = [
        relation for relation in relations
        if target in _relation_aliases(relation)
        or target == str(relation.get("mdl_name") or "").lower()
    ]
    if len(matches) != 1:
        raise ValueError(
            "table is not authorized or is ambiguous; use the exact table returned by live_discovery"
        )
    return matches[0], connector


def _json_value(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return str(value)
        return value
    if isinstance(value, (dt.date, dt.time, dt.datetime, decimal.Decimal)):
        return str(value)
    return str(value)[:500]


def _semantic_role(name: str, data_type: str) -> str:
    lower_type = str(data_type or "").lower()
    lower_name = str(name or "").lower()
    if any(token in lower_type for token in _TEMPORAL):
        return "time"
    if any(token in lower_type for token in _BOOLEAN):
        return "boolean"
    if any(token in lower_type for token in _NUMERIC):
        if lower_name.endswith("_id") or lower_name in {"id", "year", "month_id", "date_id"}:
            return "identifier"
        return "measure"
    return "dimension"


def _profile_column(name: str, data_type: str, values: list, sample_rows: int) -> dict:
    non_null = [value for value in values if value is not None]
    serialized = [_json_value(value) for value in non_null]
    distinct = len({json.dumps(value, ensure_ascii=False, default=str) for value in serialized})
    role = _semantic_role(name, data_type)
    out = {
        "name": name,
        "data_type": data_type,
        "semantic_role": role,
        "null_count": sample_rows - len(non_null),
        "null_pct": round(100 * (sample_rows - len(non_null)) / max(sample_rows, 1), 2),
        "distinct_in_sample": distinct,
        "distinct_pct": round(100 * distinct / max(len(non_null), 1), 2),
    }
    if non_null and role in {"measure", "time"}:
        try:
            out["min_in_sample"] = _json_value(min(non_null))
            out["max_in_sample"] = _json_value(max(non_null))
        except TypeError:
            pass
    if non_null and role in {"dimension", "boolean"} and distinct <= 20:
        counts = Counter(json.dumps(value, ensure_ascii=False, default=str) for value in serialized)
        out["top_values"] = [{
            "value": json.loads(value),
            "count": count,
        } for value, count in counts.most_common(10)]
    return out


def profile_relation(ds: dict, visible_mdl: dict, table: str, columns: list | None = None,
                     sample_size: int = 200, refresh: bool = False) -> dict:
    """Produce a bounded, sample-labelled profile for one authorized live relation."""
    relation, connector = _resolve_relation(ds, visible_mdl, table, refresh=refresh)
    mdl_name = relation["mdl_name"]
    mdl_columns = list(((visible_mdl or {}).get("tables") or {}).get(mdl_name) or [])
    live_by_lower = {
        str(column.get("name") or "").lower(): column
        for column in relation.get("columns") or []
    }
    allowed = [
        name for name in mdl_columns if str(name).lower() in live_by_lower
    ]
    pii = {str(name).lower() for name in (visible_mdl or {}).get("pii_columns") or []}
    requested = [str(name) for name in (columns or [])]
    if requested:
        unknown = [name for name in requested if name.lower() not in {a.lower() for a in allowed}]
        if unknown:
            raise ValueError(f"columns are not authorized/live: {', '.join(unknown[:10])}")
        selected = [
            next(name for name in allowed if name.lower() == requested_name.lower())
            for requested_name in requested
            if requested_name.lower() not in pii
        ]
    else:
        non_pii = [name for name in allowed if name.lower() not in pii]
        grouped = {"time": [], "measure": [], "dimension": [], "identifier": [], "boolean": []}
        for name in non_pii:
            metadata = live_by_lower.get(name.lower()) or {}
            grouped.setdefault(
                _semantic_role(name, str(metadata.get("data_type") or "")), []
            ).append(name)
        # Wide operational tables often place identifiers first and business metrics
        # much later. A representative default must not blindly take the first N columns.
        selected = (
            grouped["time"][:6]
            + grouped["measure"][:12]
            + grouped["dimension"][:12]
            + grouped["identifier"][:6]
            + grouped["boolean"][:4]
        )
        selected += [name for name in non_pii if name not in set(selected)]
    max_columns = _env_int("PROFILE_MAX_COLUMNS", 24, 4, 60)
    selected = selected[:max_columns]
    if not selected:
        raise ValueError("no non-PII authorized columns are available for profiling")
    relation_view = _relation_view(visible_mdl, relation)
    size = max(10, min(int(sample_size or 200), _env_int(
        "PROFILE_MAX_SAMPLE_ROWS", 300, 20, 1000,
    )))
    returned_columns, rows = connector.sample_rows(
        str(relation.get("namespace") or ""),
        str(relation.get("name") or ""),
        selected,
        size,
    )
    rows = [list(row) for row in rows]
    profiles = []
    for index, name in enumerate(returned_columns):
        metadata = live_by_lower.get(str(name).lower()) or {}
        profiles.append(_profile_column(
            str(name),
            str(metadata.get("data_type") or ""),
            [row[index] if index < len(row) else None for row in rows],
            len(rows),
        ))
    keys = [
        profile["name"] for profile in profiles
        if rows and profile["null_count"] == 0
        and profile["distinct_in_sample"] == len(rows)
    ]
    warnings = []
    if len(rows) < size:
        warnings.append("relation returned fewer rows than the requested sample")
    for profile in profiles:
        if profile["null_pct"] >= 50:
            warnings.append(f"{profile['name']} is at least 50% null in the sample")
        if rows and profile["distinct_in_sample"] <= 1:
            warnings.append(f"{profile['name']} is constant in the sample")
    semantic = relation_view["semantic"]
    return {
        "ok": True,
        "source": "bounded_live_sample",
        "sample_based": True,
        "datasource_id": ds.get("id"),
        "datasource_name": ds.get("name"),
        "datasource_type": ds.get("type"),
        "table": mdl_name,
        "namespace": relation.get("namespace") or "",
        "kind": relation.get("kind") or "table",
        "estimated_rows": relation.get("estimated_rows"),
        "estimated_bytes": relation.get("estimated_bytes"),
        "sample_rows": len(rows),
        "sample_limit": size,
        "excluded_pii_columns": [
            name for name in allowed if name.lower() in pii
        ],
        "primary_key": relation_view["primary_key"],
        "candidate_keys_from_sample": keys[:12],
        "foreign_keys": relation_view["foreign_keys"],
        "columns": profiles,
        "semantic": semantic,
        "warnings": warnings[:20],
        "limitations": [
            "statistics are computed from a bounded sample, not an exact full-table scan",
            "candidate keys and grain must be confirmed with governed SQL before final conclusions",
        ],
    }


def verify_semantics(visible_mdl: dict, assertions: list) -> dict:
    """Check that proposed business interpretations are backed by curated semantics.

    This is intentionally deterministic: existence can be verified, while business
    meaning remains ``partial`` unless a description, grain, or curated metric supports it.
    """
    tables = (visible_mdl or {}).get("tables") or {}
    descriptions = (visible_mdl or {}).get("descriptions") or {}
    grains = (visible_mdl or {}).get("grain") or {}
    metrics = (visible_mdl or {}).get("metrics") or []
    checks = []
    for raw in (assertions or [])[:12]:
        assertion = raw if isinstance(raw, dict) else {}
        table = str(assertion.get("table") or "").strip()
        concept = str(assertion.get("concept") or assertion.get("interpretation") or "").strip()
        columns = [str(column) for column in (assertion.get("columns") or [])[:20]]
        actual_table = next((name for name in tables if name.lower() == table.lower()), None)
        if not actual_table:
            checks.append({
                "concept": concept,
                "table": table,
                "status": "invalid",
                "reason": "table is not present in the authorized semantic catalog",
            })
            continue
        known = {str(column).lower(): str(column) for column in tables[actual_table]}
        missing = [column for column in columns if column.lower() not in known]
        metric_matches = []
        needles = _tokens(" ".join([concept, actual_table, *columns]))
        for metric in metrics:
            blob = json.dumps(metric, ensure_ascii=False, default=str)
            if needles and len(needles & _tokens(blob)) >= min(2, len(needles)):
                metric_matches.append(metric)
        support = {
            "table_description": str(descriptions.get(actual_table) or "")[:1000],
            "grain": str(grains.get(actual_table) or "")[:1000],
            "metrics": metric_matches[:8],
        }
        curated = bool(support["table_description"] or support["grain"] or support["metrics"])
        status = "invalid" if missing else ("verified" if curated else "partial")
        reason = (
            f"unknown columns: {', '.join(missing)}" if missing else
            "table/columns exist and curated semantic evidence is available" if curated else
            "table/columns exist, but business meaning is not curated; treat the interpretation as an assumption"
        )
        checks.append({
            "concept": concept,
            "table": actual_table,
            "columns": [known.get(column.lower(), column) for column in columns],
            "status": status,
            "reason": reason,
            "support": support,
        })
    return {
        "ok": all(check.get("status") != "invalid" for check in checks),
        "checks": checks,
        "summary": {
            status: sum(check.get("status") == status for check in checks)
            for status in ("verified", "partial", "invalid")
        },
    }
