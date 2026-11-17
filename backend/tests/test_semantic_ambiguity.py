"""Design-doc phases 0+1: DATASOURCE ORDER IS NOT A RESOLUTION RULE.

Before the resolver, a KPI phrase curated in more than one place was settled by
``semantic_contract.resolve_datasources(...)[0]`` — reverse two connections and the same
question changes meaning. These tests pin the replacement:

  * the most specific phrase wins its span across ALL visible datasources;
  * one surviving meaning resolves deterministically — every fixture is asserted in both
    input orders;
  * the same concept_id on several sources is ONE meaning only with a shared equivalence
    contract and exactly one primary — anything else is a typed conflict, never a coin
    toss;
  * concepts without concept_id are never merged by display name;
  * more than one meaning is a typed AMBIGUITY whose options name meanings, not tables.

Enforcement is per-tenant (``config/semantic.strict_metrics``, default OFF): un-flagged
tenants answer byte-identically and the collision becomes a ``semantic_collision`` audit
event (observe mode), which is also pinned here.
"""
import sys
from pathlib import Path

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import semantic_contract, semantic_resolver, store     # noqa: E402
from app.agent import orchestrator as orch                      # noqa: E402
from app.main import app                                        # noqa: E402

client = TestClient(app)
AK = {"X-Admin-Key": "admin-dev-key"}

TABLES = {"mp_orders": ["month_id", "gmv", "brand"],
          "offline_sales": ["month_id", "gmv", "store"]}


def concept(name, table="mp_orders", **extra):
    return {"name": name, "table": table, "metric": "gmv", "aggregation": "SUM", **extra}


def ds(ident, name, concepts, tables=TABLES):
    return {"id": ident, "name": name, "type": "duckdb", "enabled": 1,
            "mdl": {"tables": tables, "allowed_tables": list(tables),
                    "business_concepts": concepts}}


def both_orders(a, b, question):
    """The core phase-0 invariant, applied to every fixture: reversing the datasource
    list must not change what the question resolves to."""
    return (semantic_resolver.analyze([a, b], question),
            semantic_resolver.analyze([b, a], question))


# ---- ambiguity: one phrase, several meanings ----
def test_two_datasources_sharing_alias_is_ambiguous_in_both_orders():
    a = ds("ds_a", "Datamart", [concept("GMV", table="offline_sales",
                                        description="offline take")])
    b = ds("ds_b", "Acme L3", [concept("GMV", description="marketplace take")])
    fwd, rev = both_orders(a, b, "berapa GMV bulan Juli 2026")
    for analysis in (fwd, rev):
        assert analysis.blocking and not analysis.resolved
        assert [o["name"] for o in analysis.ambiguities[0]["options"]] == ["GMV", "GMV"]
    # identical payload regardless of order — this is the exit criterion
    assert fwd.ambiguities == rev.ambiguities


def test_same_mdl_two_concepts_sharing_synonym_is_ambiguous():
    one = ds("ds_a", "Acme L3", [
        concept("Gross GMV", synonyms=["GMV"], description="before cancellation"),
        concept("Net GMV", table="offline_sales", synonyms=["GMV"],
                description="after cancellation"),
    ])
    analysis = semantic_resolver.analyze([one], "gmv minggu ini")
    assert analysis.blocking
    assert sorted(o["name"] for o in analysis.ambiguities[0]["options"]) == [
        "Gross GMV", "Net GMV"]


def test_unclassified_equal_names_are_never_merged():
    """Two rows both named GMV may be two different numbers — no concept_id, no merge."""
    a = ds("ds_a", "A", [concept("GMV")])
    b = ds("ds_b", "B", [concept("GMV", table="offline_sales")])
    fwd, _rev = both_orders(a, b, "total gmv")
    assert fwd.blocking and len(fwd.ambiguities[0]["options"]) == 2


def test_options_name_meanings_not_physical_tables():
    a = ds("ds_a", "A", [concept("GMV", description="marketplace revenue")])
    b = ds("ds_b", "B", [concept("GMV", table="offline_sales",
                                 formula="SUM(gmv)")])
    analysis = semantic_resolver.analyze([a, b], "gmv?")
    for option in analysis.ambiguities[0]["options"]:
        assert set(option) == {"id", "name", "descr"}
        assert "offline_sales" not in option["descr"] or option["descr"] == "SUM(gmv)"
    # the admin-facing detail DOES carry the physical location (for the KB panel/audit)
    assert {d["table"] for d in analysis.ambiguities[0]["detail"]} == {
        "mp_orders", "offline_sales"}


# ---- specificity: the longest phrase owns its span ----
def test_specific_phrase_beats_generic_across_sources():
    """The doc's own curation shape: both concepts carry the generic synonym "GMV", so
    the bare word is ambiguous while the full phrase resolves without a question."""
    generic = ds("ds_a", "Datamart", [concept("GMV", table="offline_sales")])
    specific = ds("ds_b", "Acme L3", [concept("GMV Marketplace", synonyms=["GMV"])])
    for analysis in both_orders(generic, specific, "gmv marketplace bulan Juli"):
        assert not analysis.blocking
        assert [c["name"] for c in analysis.resolved] == ["GMV Marketplace"]
        assert analysis.winner_datasource_id == "ds_b"
    # the bare generic word still collides
    assert semantic_resolver.analyze([generic, specific], "berapa gmv?").blocking
    # ...and a question matching ONLY one concept's alias resolves without a question
    only_one = semantic_resolver.analyze(
        [ds("ds_a", "Datamart", [concept("NMV", table="offline_sales")]), specific],
        "berapa nmv?")
    assert not only_one.blocking and [c["name"] for c in only_one.resolved] == ["NMV"]


def test_two_explicit_concepts_resolve_independently():
    a = ds("ds_a", "Offline", [concept("GMV Offline", table="offline_sales")])
    b = ds("ds_b", "Marketplace", [concept("GMV Marketplace")])
    for analysis in both_orders(a, b, "bandingkan gmv offline dan gmv marketplace"):
        assert not analysis.blocking
        assert sorted(c["name"] for c in analysis.resolved) == [
            "GMV Marketplace", "GMV Offline"]
        assert analysis.winner_datasource_id is None      # spans two sources
        assert analysis.route_datasource_id() == "ds_b"   # stable name-sorted pick


def test_stale_specific_contract_still_claims_its_phrase():
    """A broken specific concept must not fall back to a broader valid one — same rule
    resolve_mdl applies within one MDL, kept across sources."""
    broken = ds("ds_a", "A", [concept("offline sales", table="vanished_table")])
    generic = ds("ds_b", "B", [concept("sales")])
    for analysis in both_orders(broken, generic, "offline sales bulan ini"):
        assert not analysis.resolved and not analysis.blocking


def test_no_curated_match_is_silent():
    analysis = semantic_resolver.analyze(
        [ds("ds_a", "A", [concept("GMV")])], "jumlah pesanan minggu ini")
    assert not analysis.blocking and not analysis.resolved


# ---- replicas: same concept_id on several sources ----
def replica(role, table="mp_orders", contract="eq_gmv"):
    return concept("GMV", table=table, concept_id="gmv_marketplace",
                   source_binding={"role": role, "equivalence_contract_id": contract})


def test_replicas_with_one_primary_resolve_to_the_primary():
    a = ds("ds_a", "Replica", [replica("fallback", table="offline_sales")])
    b = ds("ds_b", "Primary", [replica("primary")])
    for analysis in both_orders(a, b, "berapa gmv?"):
        assert not analysis.blocking
        assert [c["datasource_id"] for c in analysis.resolved] == ["ds_b"]
        assert analysis.winner_datasource_id == "ds_b"


def test_replicas_with_two_primaries_are_a_typed_conflict():
    a = ds("ds_a", "A", [replica("primary", table="offline_sales")])
    b = ds("ds_b", "B", [replica("primary")])
    for analysis in both_orders(a, b, "berapa gmv?"):
        assert analysis.conflicts and not analysis.resolved
    error = semantic_resolver.blocking_error(semantic_resolver.analyze([a, b], "gmv"))
    assert error.code == "SEMANTIC_AUTHORITY_CONFLICT"


def test_replica_without_equivalence_contract_is_a_conflict():
    """A missing binding never implies equivalence."""
    bare = concept("GMV", table="offline_sales", concept_id="gmv_marketplace")
    a = ds("ds_a", "A", [bare])
    b = ds("ds_b", "B", [replica("primary")])
    for analysis in both_orders(a, b, "gmv bulan ini"):
        assert analysis.conflicts
        assert "equivalence" in analysis.conflicts[0]["reason"]


# ---- v2 field validation (semantic_contract) ----
def test_concept_id_and_binding_shapes_are_validated():
    mdl = {"tables": TABLES, "allowed_tables": list(TABLES)}
    with pytest.raises(ValueError, match="concept_id"):
        semantic_contract.validate_definitions(mdl, [concept("GMV", concept_id="Not Valid!")])
    with pytest.raises(ValueError, match="duplicate concept_id"):
        semantic_contract.validate_definitions(mdl, [
            concept("GMV", concept_id="gmv"),
            concept("NMV", table="offline_sales", concept_id="gmv")])
    with pytest.raises(ValueError, match="equivalence_contract_id"):
        semantic_contract.validate_definitions(mdl, [
            concept("GMV", concept_id="gmv", source_binding={"role": "primary"})])
    with pytest.raises(ValueError, match="requires a concept_id"):
        semantic_contract.validate_definitions(mdl, [
            concept("GMV", source_binding={"role": "primary",
                                           "equivalence_contract_id": "eq"})])
    out = semantic_contract.validate_definitions(mdl, [replica("primary")])
    assert out[0]["concept_id"] == "gmv_marketplace"
    assert out[0]["source_binding"]["role"] == "primary"
    # legacy concepts stay byte-identical: no empty v2 keys appear
    legacy = semantic_contract.validate_definitions(mdl, [concept("NMV")])
    assert "concept_id" not in legacy[0] and "source_binding" not in legacy[0]


# ---- findings: the question-independent KB scan ----
def test_findings_report_collisions_and_broken_authority():
    def nmv(contract):
        return concept("NMV", concept_id="nmv_std", source_binding={
            "role": "primary", "equivalence_contract_id": contract})

    a = ds("ds_a", "A", [concept("GMV", description="offline"), nmv("eq_1")])
    b = ds("ds_b", "B", [concept("GMV", table="offline_sales", description="mp"),
                         nmv("eq_2")])
    found = semantic_resolver.findings([a, b])
    assert sorted(f["kind"] for f in found) == ["alias_collision", "authority_conflict"]
    collision = next(f for f in found if f["kind"] == "alias_collision")
    assert collision["alias"] == "GMV"
    conflict = next(f for f in found if f["kind"] == "authority_conflict")
    assert conflict["concept_id"] == "nmv_std"
    assert {d["datasource_name"] for d in conflict["detail"]} == {"A", "B"}


def test_findings_accept_a_clean_catalog():
    a = ds("ds_a", "A", [replica("primary")])
    b = ds("ds_b", "B", [replica("fallback", table="offline_sales")])
    assert semantic_resolver.findings([a, b]) == []


# ---- the gate: observe by default, refuse when the tenant opts in ----
@pytest.fixture
def tenant():
    tid = store.create_tenant("SEM-resolver")
    store.create_role(tid, "analyst", {"full_access": True})
    yield tid
    store.delete_tenant(tid)


COLLIDING = [ds("ds_a", "Datamart", [concept("GMV", table="offline_sales",
                                             description="offline")]),
             ds("ds_b", "Acme L3", [concept("GMV", description="marketplace")])]


def _gate(tid, message, sql_ds=COLLIDING, pinned=None):
    return orch._semantic_gate(tid, "trace-1", "analyst", "u", message,
                               sql_ds, pinned, lambda *_: None)


def test_gate_default_is_observe_only_with_an_audit_trail(tenant):
    analysis, strict = _gate(tenant, "berapa gmv bulan juli")
    assert analysis.blocking and strict is False        # nothing raised: answers unchanged
    rows = [r for r in store.list_audit(tenant) if r["event"] == "semantic_collision"]
    assert rows and rows[0]["status"] == "observed"
    assert rows[0]["detail"]["ambiguities"][0]["term"].lower() == "gmv"


def test_gate_strict_ambiguity_is_returned_as_an_open_reading(tenant):
    """An ambiguity does not raise HERE — run_agent hands its meanings to the model as an
    open reading (chat overhaul item 5). The audit trail says so ("assumed")."""
    store.set_tenant_config(tenant, "semantic", {"strict_metrics": True})
    analysis, strict = _gate(tenant, "berapa gmv bulan juli")
    assert strict is True and analysis.ambiguities
    rows = [r for r in store.list_audit(tenant) if r["event"] == "semantic_collision"]
    assert rows and rows[0]["status"] == "assumed"


def test_gate_strict_conflict_still_raises_the_typed_refusal(tenant):
    """Broken replica authority is an admin's problem — no reading a person corrects
    fixes it."""
    conflicted = [ds("ds_a", "A", [replica("primary", table="offline_sales")]),
                  ds("ds_b", "B", [replica("primary")])]
    store.set_tenant_config(tenant, "semantic", {"strict_metrics": True})
    with pytest.raises(semantic_resolver.SemanticError) as exc:
        _gate(tenant, "berapa gmv bulan juli", conflicted)
    assert exc.value.code == "SEMANTIC_AUTHORITY_CONFLICT"
    rows = [r for r in store.list_audit(tenant) if r["event"] == "semantic_collision"]
    assert rows and rows[0]["status"] == "enforced"


def test_gate_strict_resolves_specific_questions_normally(tenant):
    store.set_tenant_config(tenant, "semantic", {"strict_metrics": True})
    specific = [COLLIDING[0],
                ds("ds_b", "Acme L3", [concept("GMV Marketplace")])]
    analysis, strict = _gate(tenant, "gmv marketplace bulan juli", specific)
    assert strict is True and not analysis.blocking
    assert analysis.route_datasource_id() == "ds_b"


def test_gate_respects_a_pinned_datasource(tenant):
    """A pin narrows the scope the resolver sees: no cross-source ambiguity remains."""
    analysis, _ = _gate(tenant, "berapa gmv", pinned="ds_b")
    assert not analysis.blocking
    assert [c["datasource_id"] for c in analysis.resolved] == ["ds_b"]


def test_semantic_config_defaults_and_roundtrip(tenant):
    r = client.get(f"/admin/tenants/{tenant}/config/semantic", headers=AK)
    assert r.status_code == 200 and r.json() == {"strict_metrics": False}
    assert client.post(f"/admin/tenants/{tenant}/config/semantic", headers=AK,
                       json={"data": {"strict_metrics": True}}).status_code == 200
    assert client.get(f"/admin/tenants/{tenant}/config/semantic",
                      headers=AK).json()["strict_metrics"] is True


# ---- end to end: a strict tenant's chat ambiguity is READ; a conflict still refuses ----
def _consumer(tenant):
    key = client.post("/admin/apikeys", headers=AK,
                      json={"tenant_id": tenant, "label": "pytest"}).json()["key"]
    token = pyjwt.encode({"sub": "u", "role": "analyst"}, "test-secret", algorithm="HS256")
    return {"X-API-Key": key, "Authorization": f"Bearer {token}"}


def test_chat_strict_ambiguity_is_answered_with_an_open_reading(tenant, tmp_path):
    """No question and no token: a 200 answer carrying the meanings as a reading. The
    deeper e2e lives in test_metric_confirmation."""
    for ident, name, concepts in (
            ("A", "Datamart", [concept("GMV", table="offline_sales",
                                       description="offline take")]),
            ("B", "Acme L3", [concept("GMV", description="marketplace take")])):
        store.create_datasource(
            tenant, "duckdb", name, {"path": str(tmp_path / f"{ident}.duckdb")},
            {"tables": TABLES, "allowed_tables": list(TABLES),
             "business_concepts": concepts})
    store.set_tenant_config(tenant, "semantic", {"strict_metrics": True})
    r = client.post("/v1/chat", json={"message": "berapa total GMV bulan Juli 2026"},
                    headers=_consumer(tenant))
    assert r.status_code == 200
    out = r.json()
    assert "ask_metric" not in out["plan"] and out["status"] != "awaiting_confirmation"
    assert "confirmation" not in out
    [reading] = out["assumptions"]
    assert reading["kind"] == "metric"
    assert [o["name"] for o in reading["options"]] == ["GMV", "GMV"]
    assert {o["descr"] for o in reading["options"]} == {"offline take", "marketplace take"}


def test_chat_strict_authority_conflict_still_refuses(tenant, tmp_path):
    """No menu a person picks can fix a broken replica set — that stays a 409."""
    for ident, name, binding_role in (("A", "Datamart", "primary"),
                                      ("B", "Acme L3", "primary")):
        store.create_datasource(
            tenant, "duckdb", name, {"path": str(tmp_path / f"{ident}.duckdb")},
            {"tables": TABLES, "allowed_tables": list(TABLES),
             "business_concepts": [replica(binding_role)]})
    store.set_tenant_config(tenant, "semantic", {"strict_metrics": True})
    r = client.post("/v1/chat", json={"message": "berapa total GMV bulan Juli 2026"},
                    headers=_consumer(tenant))
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "SEMANTIC_AUTHORITY_CONFLICT"
