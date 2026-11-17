"""Phase 3: the business scope is RESOLVED per turn and ENFORCED at the choke point.

Phases 1 and 2 stored and edited the slice. This is where it starts changing answers: the
datasources a turn can see, the catalog the model is shown, and the tables govern() will
accept are all the business's — so a 3P question can no longer be answered from a 1P table
that merely matched by name.

Two properties are worth more than the rest:

  * a business NARROWS and never widens — enforcement is in govern(), not in the router
    that chose the datasource, because only govern() sees every query;
  * an unresolvable business is an ERROR, never a silent fall back to the whole tenant.
    Falling back is precisely the failure this layer exists to prevent.

The toggle stays OFF by default, so a tenant that has not adopted businesses answers
exactly as it did before.
"""
import sys
from pathlib import Path

import duckdb
import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import business_scope, store                          # noqa: E402
from app.governance.engine import govern                       # noqa: E402
from app.governance.policy import WrenError                    # noqa: E402
from app.main import app                                       # noqa: E402
from app.schema_scan import scan_connector                     # noqa: E402
from app.connectors.factory import make_connector              # noqa: E402

client = TestClient(app)
AK = {"X-Admin-Key": "admin-dev-key"}

OFFLINE, MARKETPLACE = "offline_sales", "mp_orders"


@pytest.fixture
def world(tmp_path):
    """One DuckDB with two tables, split into two businesses that share the connection."""
    path = str(tmp_path / "acme.duckdb")
    con = duckdb.connect(path)
    con.execute(f"CREATE TABLE {OFFLINE}(month_id INTEGER, gmv DOUBLE, buyer_email VARCHAR)")
    con.execute(f"INSERT INTO {OFFLINE} VALUES (202607, 100.0, 'a@b.com')")
    con.execute(f"CREATE TABLE {MARKETPLACE}(month_id INTEGER, gmv DOUBLE, brand VARCHAR)")
    con.execute(f"INSERT INTO {MARKETPLACE} VALUES (202607, 250.0, 'Philips')")
    con.close()

    tid = store.create_tenant("BIZ-enforce")
    store.create_role(tid, "analyst", {"full_access": True, "can_see_pii": True})
    mdl = scan_connector(make_connector("duckdb", {"path": path}))
    did = store.create_datasource(tid, "duckdb", "Acme L2", {"path": path}, mdl)

    one_p = store.create_business(tid, "1P", "Penjualan offline")
    three_p = store.create_business(tid, "3P", "Marketplace")
    store.set_business_knowledge(one_p, did, {"allowed_tables": [OFFLINE]})
    store.set_business_knowledge(three_p, did, {"allowed_tables": [MARKETPLACE]})
    store.set_role_businesses(tid, "analyst", [one_p, three_p])
    yield {"tid": tid, "did": did, "1P": one_p, "3P": three_p, "mdl": mdl}
    store.delete_tenant(tid)


def _hdr(tid):
    key = client.post("/admin/apikeys", headers=AK,
                      json={"tenant_id": tid, "label": "pytest"}).json()["key"]
    token = pyjwt.encode({"sub": "u", "role": "analyst"}, "test-secret", algorithm="HS256")
    return {"X-API-Key": key, "Authorization": f"Bearer {token}"}


def _query(tid, sql, **body):
    return client.post("/v1/query", headers=_hdr(tid), json={"sql": sql, **body})


# ---- resolution ----
def test_no_business_asked_and_none_required_is_the_old_behaviour(world):
    assert business_scope.resolve(world["tid"], "analyst", None) is None


def test_resolves_by_name_case_blind_and_by_id(world):
    for ref in ("3P", "3p", world["3P"]):
        assert business_scope.resolve(world["tid"], "analyst", ref)["id"] == world["3P"]


def test_a_business_the_role_was_not_granted_is_not_reachable(world):
    """Existing-for-the-tenant is not the test; granted-to-this-role is. Otherwise naming
    a business you were never given is an escalation."""
    store.create_business(world["tid"], "GDV", "internal")
    with pytest.raises(business_scope.BusinessError) as exc:
        business_scope.resolve(world["tid"], "analyst", "GDV")
    assert exc.value.code == "BUSINESS_NOT_FOUND"
    assert sorted(c["name"] for c in exc.value.choices) == ["1P", "3P"]


def test_paused_business_is_not_a_choice(world):
    store.update_business(world["3P"], status="paused")
    assert [c["name"] for c in business_scope.choices(world["tid"], "analyst")] == ["1P"]
    with pytest.raises(business_scope.BusinessError):
        business_scope.resolve(world["tid"], "analyst", "3P")


def test_required_toggle_demands_a_choice_and_offers_the_menu(world):
    store.set_tenant_config(world["tid"], "business", {"required": True})
    with pytest.raises(business_scope.BusinessError) as exc:
        business_scope.resolve(world["tid"], "analyst", None)
    assert exc.value.code == "BUSINESS_REQUIRED"
    assert sorted(c["name"] for c in exc.value.choices) == ["1P", "3P"]


def test_required_but_role_has_none_says_so_distinctly(world):
    """A different fix from BUSINESS_REQUIRED: nothing the end user picks can help."""
    store.set_tenant_config(world["tid"], "business", {"required": True})
    store.set_role_businesses(world["tid"], "analyst", [])
    with pytest.raises(business_scope.BusinessError) as exc:
        business_scope.resolve(world["tid"], "analyst", None)
    assert exc.value.code == "BUSINESS_NOT_GRANTED"


# ---- the view every answering path receives ----
def test_visible_datasources_is_unchanged_without_a_business(world):
    plain = business_scope.visible_datasources(world["tid"], None)
    assert [d["id"] for d in plain] == [world["did"]]
    assert sorted(plain[0]["mdl"]["allowed_tables"]) == sorted([MARKETPLACE, OFFLINE])


def test_visible_datasources_scopes_tables_but_keeps_the_technical_layer(world):
    one_p = store.get_business(world["1P"])
    scoped = business_scope.visible_datasources(world["tid"], one_p)[0]
    assert scoped["mdl"]["allowed_tables"] == [OFFLINE]
    # the schema and PII list belong to the CONNECTION and must survive the narrowing
    assert set(scoped["mdl"]["tables"]) == {OFFLINE, MARKETPLACE}
    assert scoped["mdl"]["pii_columns"] == world["mdl"]["pii_columns"]
    assert scoped["mdl"]["business"]["name"] == "1P"


def test_a_datasource_outside_the_business_disappears_entirely(world):
    store.delete_business_scope(world["1P"], world["did"])
    one_p = store.get_business(world["1P"])
    assert business_scope.visible_datasources(world["tid"], one_p) == []


def test_curation_swaps_with_the_business(world):
    store.set_business_knowledge(world["3P"], world["did"], {
        "allowed_tables": [MARKETPLACE],
        "descriptions": {MARKETPLACE: "satu baris per pesanan marketplace"}})
    three_p = store.get_business(world["3P"])
    one_p = store.get_business(world["1P"])
    assert business_scope.visible_datasources(world["tid"], three_p)[0]["mdl"]["descriptions"]
    assert business_scope.visible_datasources(world["tid"], one_p)[0]["mdl"]["descriptions"] == {}


# ---- govern(): the choke point ----
def test_govern_names_the_business_when_refusing(world):
    scoped = business_scope.visible_datasources(
        world["tid"], store.get_business(world["1P"]))[0]
    with pytest.raises(WrenError) as exc:
        govern(f"SELECT gmv FROM {MARKETPLACE}", "duckdb", scoped["mdl"],
               {"full_access": True, "can_see_pii": True}, tenant_id=world["tid"])
    assert exc.value.code == "BUSINESS_TABLE_DENIED"
    assert "1P" in exc.value.message and MARKETPLACE in exc.value.message


def test_govern_still_allows_the_business_own_tables(world):
    scoped = business_scope.visible_datasources(
        world["tid"], store.get_business(world["1P"]))[0]
    safe = govern(f"SELECT gmv FROM {OFFLINE}", "duckdb", scoped["mdl"],
                  {"full_access": True, "can_see_pii": True}, tenant_id=world["tid"])
    assert OFFLINE in safe


def test_a_stored_scope_that_widened_is_still_refused(world):
    """Defence in depth: the save path intersects, but govern() enforces. A row written by
    an older build, a script, or a bug must not be able to widen the slice."""
    store.set_business_knowledge(world["1P"], world["did"], {})
    scoped = business_scope.visible_datasources(
        world["tid"], store.get_business(world["1P"]))[0]
    # hand-widen the view the way a bad row would
    scoped["mdl"]["allowed_tables"] = [OFFLINE, MARKETPLACE]
    with pytest.raises(WrenError):
        govern(f"SELECT gmv FROM {MARKETPLACE}", "duckdb", scoped["mdl"],
               {"full_access": True, "can_see_pii": True}, tenant_id=world["tid"])


# ---- /v1/query end to end ----
def test_query_in_the_wrong_business_is_refused_and_right_one_answers(world):
    tid = world["tid"]
    denied = _query(tid, f"SELECT gmv FROM {MARKETPLACE}", business="1P")
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "BUSINESS_TABLE_DENIED"

    ok = _query(tid, f"SELECT gmv FROM {MARKETPLACE}", business="3P")
    assert ok.status_code == 200, ok.text
    assert ok.json()["rows"] == [[250.0]]


def test_query_without_a_business_is_unchanged_while_the_toggle_is_off(world):
    both = _query(world["tid"], f"SELECT gmv FROM {MARKETPLACE}")
    assert both.status_code == 200 and both.json()["rows"] == [[250.0]]


def test_query_with_the_toggle_on_asks_for_a_business(world):
    store.set_tenant_config(world["tid"], "business", {"required": True})
    r = _query(world["tid"], f"SELECT gmv FROM {MARKETPLACE}")
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["code"] == "BUSINESS_REQUIRED"
    assert sorted(c["name"] for c in body["businesses"]) == ["1P", "3P"]


def test_query_naming_an_unavailable_business_is_refused_not_widened(world):
    r = _query(world["tid"], f"SELECT gmv FROM {MARKETPLACE}", business="GDV")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "BUSINESS_NOT_FOUND"


def test_pinning_a_datasource_cannot_escape_the_business(world):
    """datasource_id is caller-supplied; it must not be a way around the slice."""
    r = _query(world["tid"], f"SELECT gmv FROM {MARKETPLACE}",
               business="1P", datasource_id=world["did"])
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "BUSINESS_TABLE_DENIED"


# ---- what a slice INHERITS from the warehouse ----
# The reported symptom: Acme L2 carried 652 scanned descriptions and the console showed
# "0/9 described" for business 1P, because scoped_view replaced every curation key with the
# business's own -- empty ones included. Descriptions and reference_values are facts about
# the warehouse, so a business that has curated nothing yet must still see them.
DS_ANNOTATED = {
    "id": "ds1", "name": "Acme L2", "type": "bigquery", "enabled": 1,
    "mdl": {
        "tables": {"offline_sales": ["month_id", "gmv", "brand"],
                   "mp_orders": ["month_id", "gmv", "seller"]},
        "allowed_tables": ["offline_sales", "mp_orders"],
        "descriptions": {"offline_sales": "Penjualan toko offline",
                         "offline_sales.gmv": "Nilai transaksi",
                         "mp_orders": "Order marketplace 3P",
                         "mp_orders.seller": "Nama seller"},
        "reference_values": {"brand": ["Philipsome"], "seller": ["Erafone"]},
        # a catalog spanning BOTH tables: generic, 1P-only and 3P-only entries
        "metrics": [{"name": "GMV", "definition": "SUM(gmv)"},
                    {"name": "GMV Offline", "definition": "SUM(offline_sales.gmv)"},
                    {"name": "GMV 3P", "definition": "SUM(mp_orders.gmv)"}],
        "business_concepts": [
            {"name": "GMV", "table": "offline_sales", "metric": "gmv"},
            {"name": "Order 3P", "table": "mp_orders", "metric": "gmv"}],
        "routing_aliases": ["penjualan acme", "gmv"],
        "examples": [{"q": "gmv offline juli", "sql": "SELECT SUM(gmv) FROM offline_sales"},
                     {"q": "gmv 3p juli", "sql": "SELECT SUM(gmv) FROM mp_orders"},
                     {"q": "bandingkan", "sql": "SELECT 1 FROM offline_sales JOIN mp_orders USING (month_id)"}],
    },
}


def _slice(allowed, **knowledge):
    k = store.blank_knowledge()
    k["allowed_tables"] = allowed
    k.update(knowledge)
    return business_scope.scoped_view(DS_ANNOTATED, k, {"id": "b1", "name": "1P"})["mdl"]


def test_an_uncurated_business_still_sees_the_warehouses_own_descriptions():
    """This is the bug: 652 descriptions dropped out of every prompt silently."""
    mdl = _slice(["offline_sales"])
    assert mdl["descriptions"]["offline_sales"] == "Penjualan toko offline"
    assert mdl["descriptions"]["offline_sales.gmv"] == "Nilai transaksi"


def test_inherited_descriptions_do_not_cross_between_slices():
    """A 1P prompt carrying "Order marketplace 3P" would leak the shape of a slice the role
    was never granted -- through the layer built to hide it."""
    mdl = _slice(["offline_sales"])
    assert "mp_orders" not in mdl["descriptions"]
    assert "mp_orders.seller" not in mdl["descriptions"]


def test_a_business_description_wins_over_the_scanned_one():
    """The point of per-business curation: the same table can mean two things."""
    mdl = _slice(["offline_sales"],
                 descriptions={"offline_sales": "Khusus 1P: hanya kanal toko"})
    assert mdl["descriptions"]["offline_sales"] == "Khusus 1P: hanya kanal toko"
    assert mdl["descriptions"]["offline_sales.gmv"] == "Nilai transaksi"   # still inherited


def test_a_stale_business_key_outside_the_slice_is_dropped_too():
    """Saved while the allowlist was wider; narrowing must not leave it behind."""
    mdl = _slice(["offline_sales"], descriptions={"mp_orders": "milik 3P"})
    assert "mp_orders" not in mdl["descriptions"]


def test_reference_values_are_inherited_for_columns_in_the_slice():
    """Without these the catalog pruner loses its value signal and entity confirmation
    goes permanently quiet -- the Philips/Philipsome case reopens."""
    assert _slice(["offline_sales"])["reference_values"] == {"brand": ["Philipsome"]}


def test_reference_values_of_another_slices_dimension_are_not_inherited():
    """`seller` exists only on mp_orders, so it is 3P's dimension, not 1P's."""
    assert "seller" not in _slice(["offline_sales"])["reference_values"]


def test_a_business_with_no_tables_inherits_nothing():
    mdl = _slice([])
    assert mdl["descriptions"] == {} and mdl["reference_values"] == {}


def test_the_whole_semantic_catalog_is_inherited_not_blanked():
    """The console read "0r · 0c · 0m · 0d · 0q" for a business while the model was being
    sent the datasource's entire catalog. Acme curates ONLY metrics, so blanking them was the
    most expensive half of the bug."""
    mdl = _slice(["offline_sales"])
    assert [m["name"] for m in mdl["metrics"]] == ["GMV", "GMV Offline"]
    assert [c["name"] for c in mdl["business_concepts"]] == ["GMV"]
    assert mdl["routing_aliases"] == ["penjualan acme", "gmv"]
    assert [e["q"] for e in mdl["examples"]] == ["gmv offline juli"]


def test_a_metric_naming_another_slices_table_is_not_inherited():
    """`SUM(mp_orders.gmv)` is 3P's definition; offering it to 1P names a table it cannot
    read and describes data it was never granted."""
    assert "GMV 3P" not in [m["name"] for m in _slice(["offline_sales"])["metrics"]]


def test_a_generic_metric_is_inherited_because_it_names_no_table():
    """`SUM(gmv)` is the tenant's KPI vocabulary, not a claim about one table."""
    assert "GMV" in [m["name"] for m in _slice(["offline_sales"])["metrics"]]


def test_a_concept_pointing_outside_the_slice_is_not_inherited():
    """Concepts are ENFORCED: inheriting one that points outside would make govern() refuse
    every question it matched, which is worse than not having it."""
    assert "Order 3P" not in [c["name"] for c in _slice(["offline_sales"])["business_concepts"]]


def test_an_example_joining_out_of_slice_tables_is_not_inherited():
    """Useful to the catalog pruner, a leak here: the SQL text itself names mp_orders."""
    assert "bandingkan" not in [e["q"] for e in _slice(["offline_sales"])["examples"]]


def test_a_business_definition_shadows_the_inherited_one_by_name():
    """Inheritance is a DEFAULT, not a lock -- an override replaces, never duplicates."""
    mdl = _slice(["offline_sales"],
                 metrics=[{"name": "gmv", "definition": "SUM(gmv) * 1.11"}])
    gmv = [m for m in mdl["metrics"] if m["name"].lower() == "gmv"]
    assert len(gmv) == 1 and gmv[0]["definition"] == "SUM(gmv) * 1.11"


def test_an_own_example_is_kept_even_when_it_names_the_other_slice():
    """The business authored it against its own allowlist; second-guessing an admin's own
    row is not this function's job, and govern() still refuses the query itself."""
    mine = {"q": "milikku", "sql": "SELECT 1 FROM mp_orders"}
    assert mine in _slice(["offline_sales"], examples=[mine])["examples"]


def test_the_console_is_told_the_same_thing_the_model_reads(world):
    """The panel reporting "0 described" about a prompt carrying hundreds is how an admin
    ends up curating a problem that does not exist."""
    r = client.get(f"/admin/tenants/{world['tid']}/businesses/{world['1P']}"
                   f"/datasources/{world['did']}/knowledge", headers=AK)
    assert r.status_code == 200, r.text
    assert "inherited" in r.json()
    assert set(r.json()["inherited"]) == {
        "descriptions", "reference_values", "metrics", "business_concepts",
        "routing_aliases", "examples"}
