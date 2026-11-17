"""Helpers for the per-(business, datasource) curation layer.

Shared by the admin knowledge endpoint and the auto-enrich job so the NARROWING rule -- a
business may only restrict what its datasource already permits, never widen it -- has
exactly one implementation. Two callers with two copies of that rule is how a business
eventually reaches a table its connection was never allowed to serve.
"""
from . import semantic_contract, store


def narrow_tables(mdl: dict, wanted) -> tuple[list, list]:
    """``(kept, dropped)``: ``wanted`` intersected with the datasource's own allowlist.

    Matching is case-insensitive and the DATASOURCE's spelling wins, so a business cannot
    introduce a second casing of a table that govern() would then see as a different name.
    """
    ceiling = {str(t).lower(): str(t) for t in (mdl.get("allowed_tables") or [])}
    kept, dropped = [], []
    for table in wanted or []:
        match = ceiling.get(str(table).lower())
        if match:
            kept.append(match)
        else:
            dropped.append(str(table))
    return list(dict.fromkeys(kept)), dropped


# Every curation key a business INHERITS from its datasource instead of replacing.
# `allowed_tables` is the ONE exception and is handled separately: it is the narrowing
# mechanism itself, so inheriting it would undo the whole layer.
#
# Blanking them was a real bug, not a nuance. A business nobody had curated yet dropped Acme
# L2's 652 scanned descriptions out of every prompt, left `reference_values` empty -- which
# silently disables the catalog pruner's value signal AND phase 5's entity confirmation,
# whose index IS reference_values -- and reported "0 metrics" for a tenant whose only
# curation IS metrics. Nothing announced any of it, because an empty curation layer looks
# exactly like a new one, so a business answered WORSE than no business at all.
#
# Inheritance is a DEFAULT, not a lock. "What GMV means for 1P can differ from 3P" stays
# true and is still the reason this layer exists -- the business overrides by name, and an
# override always wins. What changed is only what happens when it has said nothing.
INHERITED_KEYS = ("descriptions", "reference_values", "metrics", "business_concepts",
                  "routing_aliases", "examples")


def _slice_descriptions(scanned, own, allowed) -> dict:
    """The datasource's descriptions for tables IN this slice, the business's own on top.

    Filtering is not politeness: keys are ``table`` or ``table.column``, so inheriting the
    whole set would put 3P table names and their meanings into a 1P prompt -- leaking the
    shape of a slice the role was never granted, through the very layer meant to hide it.
    Applied to the merged result so a business's own stale key (saved before its allowlist
    narrowed) cannot smuggle one back either.
    """
    keep = {str(t).lower() for t in allowed or []}
    merged = dict(scanned or {})
    merged.update(own or {})
    return {k: v for k, v in merged.items()
            if str(k).split(".", 1)[0].lower() in keep}


def _slice_reference_values(mdl: dict, own, allowed) -> dict:
    """Dimension values whose COLUMN exists in this slice's tables.

    ``reference_values`` is keyed by bare column name (mdl_enrich samples one entry per
    column, not per table), so "is this dimension part of the slice" can only be asked of
    the columns the allowed tables actually have. A column no table in the slice carries is
    another slice's dimension.
    """
    tables = mdl.get("tables") or {}
    columns = {str(c).lower() for t in (allowed or []) for c in (tables.get(t) or [])}
    merged = dict(mdl.get("reference_values") or {})
    merged.update(own or {})
    return {k: v for k, v in merged.items() if str(k).lower() in columns}


def _outside_slice(mdl: dict, allowed) -> list:
    """Scanned tables this slice does NOT include."""
    keep = {str(t).lower() for t in allowed or []}
    return [str(t) for t in (mdl.get("tables") or {}) if str(t).lower() not in keep]


def _names_outside(text: str, outside: list) -> bool:
    """Whether `text` refers to a table outside this slice.

    Only the OUTSIDE names are scanned, which makes the rule "no out-of-slice mention"
    rather than the pruner's "at least one in-slice mention". Deliberately stricter: an
    example joining offline_sales to mp_orders is useful to the pruner and a leak here,
    because it would put a 3P table name in front of a 1P reader.
    """
    from .nl2sql import mentioned_tables      # REUSED: one whole-identifier rule, not two
    return bool(outside) and bool(mentioned_tables(text, outside))


def _merge_named(scanned, own, keep):
    """The business's own entries, then inherited ones that pass `keep`, matched on `name`
    case-blind so an override REPLACES a definition instead of sitting next to it."""
    out, taken = [], set()
    for item in own or []:
        if isinstance(item, dict):
            out.append(item)
            taken.add(str(item.get("name") or "").strip().lower())
    for item in scanned or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().lower()
        if name and name not in taken and keep(item):
            out.append(item)
            taken.add(name)
    return out


def _slice_metrics(mdl: dict, own, allowed) -> list:
    """A metric naming no table (``SUM(gmv)``) is generic and inherited; one naming a table
    outside the slice is not."""
    outside = _outside_slice(mdl, allowed)

    def keep(metric):
        text = " ".join(str(metric.get(f) or "") for f in ("name", "definition", "note"))
        return not _names_outside(text, outside)

    return _merge_named(mdl.get("metrics"), own, keep)


def _slice_concepts(mdl: dict, own, allowed) -> list:
    """A concept carries its table EXPLICITLY, so the slice test is exact -- and it has to
    be, because concepts are ENFORCED (semantic_contract): inheriting one that points
    outside the slice would make govern() refuse every question it matched."""
    keep_tables = {str(t).lower() for t in allowed or []}
    outside = _outside_slice(mdl, allowed)

    def keep(concept):
        if str(concept.get("table") or "").strip().lower() not in keep_tables:
            return False
        text = " ".join(str(concept.get(f) or "") for f in ("formula", "metric"))
        return not _names_outside(text, outside)

    return _merge_named(mdl.get("business_concepts"), own, keep)


def _inherited_example_ok(sql: str, inside, outside) -> bool:
    """An inherited example must name at least one table IN the slice (one that names none
    cannot be checked against this slice at all, and an unverifiable example is the opposite
    of what these are for) and must not mention an out-of-slice table."""
    from .nl2sql import mentioned_tables
    return bool(mentioned_tables(sql, inside)) and not _names_outside(sql, outside)


def _slice_examples(mdl: dict, own, allowed, cap: int = 20) -> list:
    """Validated question/SQL pairs whose SQL stays entirely inside the slice.

    The business's own are kept as-is -- it authored them.
    """
    outside = _outside_slice(mdl, allowed)
    inside = list(allowed or [])
    own_list = list(own or [])
    out, seen = [], set()
    for index, example in enumerate(own_list + list(mdl.get("examples") or [])):
        if not isinstance(example, dict):
            continue
        sql = str(example.get("sql") or "").strip()
        key = " ".join(sql.lower().split())
        if not sql or key in seen:
            continue
        if index >= len(own_list) and not _inherited_example_ok(sql, inside, outside):
            continue
        out.append(example)
        seen.add(key)
        if len(out) >= cap:
            break
    return out


def _slice_aliases(mdl: dict, own) -> list:
    """Routing vocabulary names no table -- it says "this datasource answers that term", and
    the datasource is already part of the business. So it is a union, business terms first.
    """
    return semantic_contract.normalize_routing_aliases(
        list(own or []) + list(mdl.get("routing_aliases") or []))


def inherited_for(mdl: dict, knowledge: dict, allowed) -> dict:
    """What a slice inherits, for BOTH the answering path and the console editor.

    One function so the panel cannot report "0 described" about a prompt that carries 652:
    the editor showing something different from what the model reads is how an admin ends up
    curating a problem that does not exist.
    """
    own = knowledge or {}
    return {
        "descriptions": _slice_descriptions(
            mdl.get("descriptions"), own.get("descriptions"), allowed),
        "reference_values": _slice_reference_values(
            mdl, own.get("reference_values"), allowed),
        "metrics": _slice_metrics(mdl, own.get("metrics"), allowed),
        "business_concepts": _slice_concepts(mdl, own.get("business_concepts"), allowed),
        "routing_aliases": _slice_aliases(mdl, own.get("routing_aliases")),
        "examples": _slice_examples(mdl, own.get("examples"), allowed),
    }


def validate(mdl: dict, knowledge: dict) -> dict:
    """Normalize the contract layer against {scanned schema, the BUSINESS's allowlist}.

    Run on every save, not only when concepts were edited: narrowing the allowlist can
    orphan a concept that was valid a moment ago, and an orphaned contract is exactly what
    govern() would refuse at answer time. Raises ValueError, which the caller reports.
    """
    scope = {"tables": mdl.get("tables") or {},
             "allowed_tables": knowledge.get("allowed_tables") or []}
    out = dict(knowledge)
    out["business_concepts"] = semantic_contract.validate_definitions(
        scope, knowledge.get("business_concepts"))
    out["routing_aliases"] = semantic_contract.normalize_routing_aliases(
        knowledge.get("routing_aliases"))
    return out


def scoped_view(ds: dict, knowledge: dict, business: dict | None = None) -> dict:
    """A datasource whose MDL shows one BUSINESS's slice.

    The connection's technical layer (scanned schema, pii_columns, dialect, function lists,
    and the warehouse's own descriptions/reference values -- see ``INHERITED_KEYS``) with
    the business's curation and allowlist laid over it. Everything downstream —
    routing, catalog pruning, the semantic contract, govern() — reads only ``mdl``, so one
    substitution here scopes the whole answering path instead of each of them learning
    about businesses separately.

    ``allowed_tables`` is re-narrowed rather than trusted: the save path already intersects
    it, but this view is what governance enforces, and a stored row is not a reason to skip
    the check that makes widening impossible.
    """
    mdl = dict(ds.get("mdl") or {})
    allowed, _ = narrow_tables(mdl, knowledge.get("allowed_tables"))
    # Computed BEFORE the overwrite below, which is what used to discard them.
    inherited = inherited_for(mdl, knowledge, allowed)
    mdl.update({key: knowledge.get(key) for key in store.KNOWLEDGE_KEYS})
    mdl.update(inherited)
    mdl["allowed_tables"] = allowed
    if business:
        # Carried so govern() can refuse an out-of-slice table BY NAME. Without it the
        # narrowed allowlist still blocks the query, but as a generic "table not in the
        # model", which tells the user nothing about which business they are in.
        mdl["business"] = {"id": business["id"], "name": business["name"],
                           "tables": list(allowed)}
    return {**ds, "mdl": mdl}


def enrich_view(ds: dict, knowledge: dict) -> dict:
    """The same view, for mdl_enrich: it reads nothing but ``mdl``, so this makes it draft
    FOR one business while ``datasources.mdl`` is neither read as curation nor written."""
    return scoped_view(ds, knowledge)


def curation_of(mdl: dict) -> dict:
    """The curated half of an MDL-shaped dict, in the business-knowledge shape."""
    return store.clean_knowledge({key: mdl.get(key) for key in store.KNOWLEDGE_KEYS})


# ---- runtime: which slice is this question being answered in ----
CONFIG_SECTION = "business"
# `confirm_entities` belongs to entity_confirm but lives in the SAME section: both are
# "how is what the person named resolved", one admin panel edits them, and one section
# means one GET/POST instead of two that can disagree.
CONFIG_DEFAULTS = {"required": False, "confirm_entities": False}


class BusinessError(Exception):
    """Resolution refused. Carries a code and the choices, so a caller can turn it into a
    403, or (phase 4) into a question the end user can answer."""

    def __init__(self, code: str, message: str, choices: list | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.choices = choices or []


def config(tenant_id: str) -> dict:
    return {**CONFIG_DEFAULTS, **(store.get_tenant_config(tenant_id, CONFIG_SECTION) or {})}


def is_required(tenant_id: str) -> bool:
    """Whether this tenant must answer inside a business. Default OFF: turning it on
    changes what every consumer app has to send, so it is the tenant's decision."""
    return bool(config(tenant_id).get("required"))


def choices(tenant_id: str, role_name: str) -> list[dict]:
    """Active businesses this role may use — the menu a caller can pick from. ``in_chat``
    says whether the chat's "which business?" question offers it (a business kept for the
    MBR report is used when named, never offered)."""
    return [{"id": b["id"], "name": b["name"], "descr": b.get("descr") or "",
             "in_chat": b.get("in_chat", True)}
            for b in store.list_role_businesses(tenant_id, role_name)
            if b.get("status", "active") == "active"]


def resolve(tenant_id: str, role_name: str, ref: str | None) -> dict | None:
    """The business this turn runs in, or None when none was asked for and none is required.

    `ref` may be an id or a name — a consumer app knows its business as "3P", not as
    `b_1a2b3c4d`. An unresolvable ref is an ERROR, never a silent fallback to "everything":
    answering a 3P question from the whole catalog is the exact failure this layer exists
    to stop.
    """
    allowed = choices(tenant_id, role_name)
    wanted = (ref or "").strip()
    if not wanted:
        if is_required(tenant_id):
            if not allowed:
                raise BusinessError(
                    "BUSINESS_NOT_GRANTED",
                    f"role '{role_name}' has no business assigned, and this tenant requires "
                    "one — grant it in the console's Knowledge Base")
            raise BusinessError(
                "BUSINESS_REQUIRED",
                "this tenant requires a business for every question; pick one of "
                f"{[c['name'] for c in allowed]}", allowed)
        return None
    by_key = {}
    for c in allowed:
        by_key[c["id"].lower()] = c
        by_key.setdefault(c["name"].lower(), c)
    hit = by_key.get(wanted.lower())
    if not hit:
        raise BusinessError(
            "BUSINESS_NOT_FOUND",
            f"'{wanted}' is not a business available to role '{role_name}'; available: "
            f"{[c['name'] for c in allowed]}", allowed)
    return store.get_business(hit["id"])


def visible_datasources(tenant_id: str, business: dict | None,
                        include_disabled: bool = False) -> list[dict]:
    """The tenant's datasources as this turn may see them.

    With no business this is exactly what every answering path got before businesses
    existed. With one, it is only the datasources that business spans, each already scoped
    — so a caller that forgets businesses exist still cannot reach outside the slice.
    """
    sources = store.list_datasources(tenant_id, include_disabled=include_disabled)
    if not business:
        return sources
    out = []
    for ds in sources:
        knowledge = store.get_business_knowledge(business["id"], ds["id"])
        if knowledge is None:
            continue                     # not part of this business: not reachable at all
        out.append(scoped_view(ds, knowledge, business))
    return out
