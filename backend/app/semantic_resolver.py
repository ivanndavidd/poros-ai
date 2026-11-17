"""Cross-datasource semantic resolution: which curated meaning OWNS a KPI phrase.

``semantic_contract.py`` answers two single-MDL questions — "which concepts match this
question" and "does this SQL satisfy them". What it cannot see is the tenant-level
collision this module exists for: the same alias ("GMV") curated in more than one
datasource, or under two names in one. Before this module that collision was settled by
DATASOURCE ORDER (the orchestrator took ``resolve_datasources(...)[0]``) — the exact bug
class where reversing two connections changes the answer.

Rules (docs/ANSWER-QUALITY-LATENCY-IMPLEMENTATION.md §6, phase 1):
- The most specific phrase wins its span of the question, across ALL visible datasources.
- One meaning left -> resolved deterministically; input order appears in no sort key.
- The same ``concept_id`` on several sources counts as REPLICAS of one meaning only with
  an explicit shared ``equivalence_contract_id`` and exactly one ``primary``. Missing or
  duplicated authority is a configuration CONFLICT, never a coin toss.
- Concepts without ``concept_id`` are UNCLASSIFIED: equal display names are never merged,
  because two rows both named "GMV" may be two different numbers.
- More than one surviving meaning -> AMBIGUOUS, carrying user-facing options (concept
  name + meaning only — physical table/datasource names stay in the admin `detail`).

Enforcement is PER-TENANT and default OFF (``config/semantic {strict_metrics}``), the
same adoption pattern as the business layer: an un-flagged tenant answers byte-identically
and gets a ``semantic_collision`` audit event instead (observe mode), so collisions can be
found and curated before refusals are turned on.
"""
from dataclasses import dataclass, field

from . import semantic_contract, store

CONFIG_SECTION = "semantic"
CONFIG_DEFAULTS = {"strict_metrics": False}


class SemanticError(Exception):
    """Resolution refused: one phrase, several curated meanings (or broken replica
    authority). Carries the end-user options so a caller can present them — the same
    contract shape a future metric confirmation (design doc phase 2) will consume."""

    def __init__(self, code: str, message: str, options: list | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.options = options or []


def config(tenant_id: str) -> dict:
    return {**CONFIG_DEFAULTS,
            **(store.get_tenant_config(tenant_id, CONFIG_SECTION) or {})}


def is_strict(tenant_id: str) -> bool:
    """Whether ambiguity/conflict REFUSES instead of being observed. Default OFF: turning
    it on changes which questions this tenant's consumers can ask, so it is the tenant's
    decision — and it is only useful once concepts are curated."""
    return bool(config(tenant_id).get("strict_metrics"))


@dataclass
class Analysis:
    """What one question's KPI phrases resolve to across the visible datasources."""
    resolved: list = field(default_factory=list)     # contract dicts (+ datasource_id/name)
    ambiguities: list = field(default_factory=list)  # {"term","options","detail"}
    conflicts: list = field(default_factory=list)    # {"concept_id","reason","detail"}

    @property
    def blocking(self) -> bool:
        return bool(self.ambiguities or self.conflicts)

    @property
    def winner_datasource_id(self):
        """The one datasource every resolved concept lives in, else None. Only a clean
        analysis routes: a blocked one has no business picking anything."""
        if self.blocking or not self.resolved:
            return None
        ids = {c.get("datasource_id") for c in self.resolved}
        return ids.pop() if len(ids) == 1 else None

    def route_datasource_id(self):
        """Where routing should land: the single owner when one exists, else the first
        resolved concept by a STABLE sort (name, concept_id, datasource_id) — a
        comparison spanning two datasources still needs one primary source, and that
        pick must not depend on connection order either."""
        if self.blocking or not self.resolved:
            return None
        winner = self.winner_datasource_id
        if winner:
            return winner
        ordered = sorted(self.resolved, key=lambda c: (
            str(c.get("name") or "").lower(), str(c.get("concept_id") or ""),
            str(c.get("datasource_id") or "")))
        return ordered[0].get("datasource_id")


def _meaning_key(entry: dict) -> tuple:
    """What counts as ONE meaning. An explicit concept_id is an identity; without one,
    every row is its own meaning — never merged by display name."""
    definition = entry["definition"]
    concept_id = str(definition.get("concept_id") or "")
    if concept_id:
        return ("id", concept_id)
    return ("row", str(entry.get("datasource_id") or ""), entry["index"],
            definition["name"].lower())


def _descr(definition: dict) -> str:
    """User-facing meaning of one option: the curated description, else the formula —
    never the table or datasource (those stay admin-only, per the design doc)."""
    return (str(definition.get("description") or "").strip()
            or str(definition.get("formula") or "").strip()
            or f"{definition.get('aggregation') or 'SUM'}({definition.get('metric') or ''})")


def _option(entry: dict) -> dict:
    definition = entry["definition"]
    return {"id": definition.get("concept_id") or None,
            "name": definition["name"], "descr": _descr(definition)}


def _admin_detail(entry: dict) -> dict:
    definition = entry["definition"]
    return {"concept_id": definition.get("concept_id") or None,
            "name": definition["name"],
            "table": definition.get("table") or "",
            "datasource_id": entry.get("datasource_id"),
            "datasource_name": entry.get("datasource_name"),
            "source_binding": definition.get("source_binding") or {}}


def _as_contract(entry: dict) -> dict:
    """Same shape ``semantic_contract.resolve_datasources`` emits, so the registry's
    per-datasource filter and every prompt builder keep working unchanged."""
    return {**entry["definition"], "matched_alias": entry["alias"],
            "datasource_id": entry.get("datasource_id"),
            "datasource_name": entry.get("datasource_name")}


def _sorted_entries(entries: list) -> list:
    return sorted(entries, key=lambda e: (
        e["definition"]["name"].lower(),
        str(e["definition"].get("concept_id") or ""),
        str(e.get("datasource_id") or "")))


def _replica_authority(members: list):
    """``(primary_entry, None)`` when same-concept_id copies form a valid replica set,
    else ``(None, reason)``. Valid = every copy names the SAME equivalence contract and
    exactly one is 'primary' — a missing binding never implies equivalence."""
    bindings = [m["definition"].get("source_binding") or {} for m in members]
    if not all(b.get("equivalence_contract_id") for b in bindings):
        return None, ("replicated without an equivalence contract — the same concept_id "
                      "on several datasources must declare source_binding on every copy")
    contracts = {b["equivalence_contract_id"] for b in bindings}
    if len(contracts) > 1:
        return None, "copies name different equivalence contracts"
    primaries = [m for m, b in zip(members, bindings) if b.get("role") == "primary"]
    if len(primaries) != 1:
        return None, (f"{len(primaries)} primary sources — a replicated concept needs "
                      "exactly one")
    return primaries[0], None


def _question_entries(datasources, question):
    entries = []
    for ds in datasources or []:
        for (tokens, chars, _neg, start, end, alias, index,
             definition) in semantic_contract.match_candidates(
                 ds.get("mdl") or {}, question):
            entries.append({"specificity": (tokens, chars), "start": start, "end": end,
                            "alias": alias, "index": index, "definition": definition,
                            "datasource_id": ds.get("id"),
                            "datasource_name": ds.get("name")})
    return entries


def _claim_spans(entries):
    entries.sort(key=lambda e: (
        -e["specificity"][0], -e["specificity"][1], e["start"],
        str(e["alias"]).lower(), str(e.get("datasource_id") or ""), e["index"]))
    occupied: list[tuple[int, int]] = []
    groups: list[list] = []
    for entry in entries:
        span = (entry["start"], entry["end"])
        joined = next((g for g in groups
                       if (g[0]["start"], g[0]["end"]) == span), None)
        if joined is not None:
            joined.append(entry)          # same text of the question: competing meanings
            continue
        if any(entry["start"] < used_end and entry["end"] > used_start
               for used_start, used_end in occupied):
            continue                      # a more specific phrase already claimed this text
        occupied.append(span)
        groups.append([entry])

    return groups


def _resolve_group(members, question, analysis):
    valid = [m for m in members if m["definition"]]
    if not valid:
        # Stale contracts claim their phrase (same rule as resolve_mdl) but resolve
        # nothing — a broken specific concept must not fall back to a broader one.
        return
    term = question[members[0]["start"]:members[0]["end"]]
    by_meaning: dict[tuple, list] = {}
    for member in valid:
        by_meaning.setdefault(_meaning_key(member), []).append(member)
    if len(by_meaning) == 1:
        copies = _sorted_entries(next(iter(by_meaning.values())))
        if len(copies) == 1:
            analysis.resolved.append(_as_contract(copies[0]))
            return
        primary, reason = _replica_authority(copies)
        if primary is not None:
            analysis.resolved.append(_as_contract(primary))
        else:
            analysis.conflicts.append({
                "concept_id": copies[0]["definition"].get("concept_id"),
                "term": term, "reason": reason,
                "detail": [_admin_detail(c) for c in copies]})
        return
    _resolve_ambiguity(by_meaning, term, analysis)


def _resolve_ambiguity(by_meaning, term, analysis):
    # Several distinct meanings share this phrase. Meanings that are themselves a
    # valid replica set surface as ONE option (their primary); broken sets keep a
    # representative so the ambiguity is still reported to the person asking.
    choices, detail = [], []
    for meaning_members in by_meaning.values():
        copies = _sorted_entries(meaning_members)
        representative = copies[0]
        if len(copies) > 1:
            primary, _reason = _replica_authority(copies)
            if primary is not None:
                representative = primary
        choices.append((_option(representative), representative))
        detail.extend(_admin_detail(c) for c in copies)
    choices.sort(key=lambda pair: (pair[0]["name"].lower(), pair[0]["id"] or ""))
    analysis.ambiguities.append({
        "term": term,
        "options": [option for option, _rep in choices],
        # Aligned with `options`, SERVER-side only (the 409 payload and the ask both
        # send `options` alone): the identity a stored choice is re-matched against on
        # replay, and the full contract to inject once it is.
        "meanings": [{"concept_id": rep["definition"].get("concept_id") or None,
                      "datasource_id": rep.get("datasource_id"),
                      "name": rep["definition"]["name"],
                      "table": rep["definition"].get("table") or ""}
                     for _opt, rep in choices],
        "contracts": [_as_contract(rep) for _opt, rep in choices],
        "detail": detail,
    })


def analyze(datasources, question: str) -> Analysis:
    """Resolve the question's KPI phrases across every datasource this turn can see.

    Deterministic and order-independent by construction: candidates from all sources are
    pooled, spans are claimed by phrase specificity, and every tie-break sorts on stable
    values (names, concept ids, datasource ids) — never on input position.
    """
    analysis = Analysis()
    for members in _claim_spans(_question_entries(datasources, question)):
        _resolve_group(members, question, analysis)
    return analysis


def blocking_error(analysis: Analysis) -> SemanticError:
    """The refusal a strict tenant gets instead of an order-dependent guess."""
    if analysis.conflicts:
        conflict = analysis.conflicts[0]
        subject = conflict.get("concept_id") or conflict.get("term") or "concept"
        return SemanticError(
            "SEMANTIC_AUTHORITY_CONFLICT",
            f"'{subject}' is curated on more than one datasource without a valid "
            f"replica contract ({conflict['reason']}) — an admin must mark one "
            "primary source in the Knowledge Base", [])
    ambiguity = analysis.ambiguities[0]
    names = [o["name"] for o in ambiguity["options"]]
    return SemanticError(
        "SEMANTIC_AMBIGUOUS",
        f"'{ambiguity['term']}' has more than one curated meaning: {names} — "
        "ask again naming the one you mean", ambiguity["options"])


def audit_detail(analysis: Analysis) -> dict:
    """What the observe-mode audit event records: enough for an admin to curate the
    collision away, nothing an end user typed beyond the colliding term itself."""
    return {
        "ambiguities": [{"term": a["term"], "options": a["options"],
                         "detail": a["detail"]} for a in analysis.ambiguities],
        "conflicts": analysis.conflicts,
        "resolved": [{"name": c.get("name"), "concept_id": c.get("concept_id"),
                      "datasource_id": c.get("datasource_id")}
                     for c in analysis.resolved],
    }


def _catalog_index(datasources):
    by_alias: dict[str, dict] = {}
    by_concept: dict[str, list] = {}
    for ds in datasources or []:
        mdl = ds.get("mdl") or {}
        kept, _rejected = semantic_contract.partition_definitions(
            mdl, mdl.get("business_concepts"))
        for definition in kept:
            entry = {"definition": definition, "index": 0,
                     "datasource_id": ds.get("id"), "datasource_name": ds.get("name")}
            concept_id = str(definition.get("concept_id") or "")
            if concept_id:
                by_concept.setdefault(concept_id, []).append(entry)
            for alias in [definition["name"], *(definition.get("synonyms") or [])]:
                bucket = by_alias.setdefault(alias.lower(), {"alias": alias, "members": []})
                bucket["members"].append(entry)
    return by_alias, by_concept


def findings(datasources) -> list[dict]:
    """Question-independent collision scan for the Knowledge Base panel.

    Admin-facing on purpose — datasource and table names included — so the panel can say
    exactly which two rows collide. Runs over the SAME scoped views the answering path
    reads (own + inherited curation), because a warning about a catalog the model never
    sees would have admins fixing problems that do not exist.
    """
    by_alias, by_concept = _catalog_index(datasources)
    out = []
    for bucket in by_alias.values():
        meanings = {}
        for member in bucket["members"]:
            meanings.setdefault(_meaning_key(member), []).append(member)
        if len(meanings) <= 1:
            continue
        out.append({"kind": "alias_collision", "alias": bucket["alias"],
                    "detail": [_admin_detail(m)
                               for m in _sorted_entries(bucket["members"])]})
    for concept_id, copies in by_concept.items():
        if len(copies) < 2:
            continue
        primary, reason = _replica_authority(_sorted_entries(copies))
        if primary is None:
            out.append({"kind": "authority_conflict", "concept_id": concept_id,
                        "reason": reason,
                        "detail": [_admin_detail(c)
                                   for c in _sorted_entries(copies)]})
    out.sort(key=lambda f: (f["kind"], str(f.get("alias") or f.get("concept_id") or "")))
    return out
