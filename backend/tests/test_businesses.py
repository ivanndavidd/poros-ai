"""Businesses: the knowledge scope between a role and a datasource.

Curation used to live in one place per datasource (``datasources.mdl``). It now lives per
(business, datasource) pair so 1P and 3P can describe the SAME physical table differently,
and so one business can span datasources — Acme 3P reads L2+L3 while 1P reads L2 only.

What is worth testing here is the part a UI cannot be trusted with: a business may only
NARROW what its datasource already permits, curation must not outlive the table it
describes, and deleting a tenant / datasource / role must not leave scopes pointing at
something that no longer exists.
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store                                        # noqa: E402
from app.main import app                                     # noqa: E402

client = TestClient(app)
AK = {"X-Admin-Key": "admin-dev-key"}

MDL = {"tables": {"orders": ["order_id", "gmv", "month_id"],
                  "returns": ["return_id", "qty"],
                  "stock": ["sku", "on_hand"]},
       "allowed_tables": ["orders", "returns"],   # `stock` is outside the connection's own
       "pii_columns": [], "denied_functions": [], "allowed_functions": []}


@pytest.fixture
def tenant():
    tid = store.create_tenant("BIZ-test")
    store.create_role(tid, "admin", {"full_access": True, "can_see_pii": True})
    yield tid
    store.delete_tenant(tid)


@pytest.fixture
def ds(tenant):
    return tenant, store.create_datasource(tenant, "duckdb", "L2", {"path": "l2.duckdb"}, MDL)


def _knowledge(**over):
    return {**store.blank_knowledge(), **over}


# ---- store: businesses ----
def test_create_list_and_read_back(tenant):
    bid = store.create_business(tenant, "3P", "Marketplace / seller-managed")
    listed = store.list_businesses(tenant)
    assert [b["id"] for b in listed] == [bid]
    assert listed[0]["name"] == "3P"
    assert listed[0]["descr"] == "Marketplace / seller-managed"
    assert listed[0]["status"] == "active"
    assert listed[0]["scopes"] == {} and listed[0]["roles"] == []
    assert store.get_business(bid)["tenant_id"] == tenant
    assert store.get_business_by_name(tenant, "3p")["id"] == bid   # lookup is case-blind


def test_list_is_ordered_by_name(tenant):
    """Postgres gives no implicit row order; the console tree must not reshuffle."""
    for name in ("GDV", "1P", "3P"):
        store.create_business(tenant, name)
    assert [b["name"] for b in store.list_businesses(tenant)] == ["1P", "3P", "GDV"]


def test_update_and_pause(tenant):
    bid = store.create_business(tenant, "1P")
    store.update_business(bid, name="1P Retail", descr="own inventory", status="paused")
    b = store.get_business(bid)
    assert (b["name"], b["descr"], b["status"]) == ("1P Retail", "own inventory", "paused")
    assert store.list_businesses(tenant, include_paused=False) == []


def test_update_with_nothing_to_change_is_a_noop(tenant):
    bid = store.create_business(tenant, "1P")
    store.update_business(bid)
    assert store.get_business(bid)["name"] == "1P"


# ---- store: (business, datasource) knowledge ----
def test_absent_scope_is_not_an_empty_one(ds):
    """None means 'this datasource is not part of the business', which the answering path
    must treat as unreachable — not as 'in scope but nothing curated yet'."""
    tenant, did = ds
    bid = store.create_business(tenant, "3P")
    assert store.get_business_knowledge(bid, did) is None
    store.set_business_knowledge(bid, did, _knowledge())
    assert store.get_business_knowledge(bid, did) == store.blank_knowledge()


def test_knowledge_round_trip_and_replace(ds):
    tenant, did = ds
    bid = store.create_business(tenant, "3P")
    store.set_business_knowledge(bid, did, _knowledge(
        allowed_tables=["orders"], descriptions={"orders": "one row per order"},
        metrics=[{"name": "GMV", "definition": "SUM(gmv)"}]))
    got = store.get_business_knowledge(bid, did)
    assert got["allowed_tables"] == ["orders"]
    assert got["descriptions"] == {"orders": "one row per order"}
    store.set_business_knowledge(bid, did, _knowledge(allowed_tables=["returns"]))
    assert store.get_business_knowledge(bid, did)["allowed_tables"] == ["returns"]
    assert store.get_business_knowledge(bid, did)["descriptions"] == {}


def test_clean_knowledge_drops_unknown_and_mistyped(ds):
    """The answering path reads this JSON, so it must not become a bag for arbitrary
    caller-supplied fields, and a wrong type must not reach govern() as a surprise."""
    tenant, did = ds
    bid = store.create_business(tenant, "3P")
    store.set_business_knowledge(bid, did, {"allowed_tables": ["orders"],
                                            "pii_columns": ["gmv"],     # not per-business
                                            "descriptions": "not a dict",
                                            "surprise": {"a": 1}})
    got = store.get_business_knowledge(bid, did)
    assert set(got) == set(store.KNOWLEDGE_KEYS)
    assert got["allowed_tables"] == ["orders"]
    assert got["descriptions"] == {}


def test_scopes_and_roles_ride_along_on_list(ds):
    tenant, did = ds
    bid = store.create_business(tenant, "3P")
    store.set_business_knowledge(bid, did, _knowledge(allowed_tables=["orders"]))
    store.set_role_businesses(tenant, "admin", [bid])
    b = store.list_businesses(tenant)[0]
    assert b["datasource_ids"] == [did]
    assert b["scopes"][did]["allowed_tables"] == ["orders"]
    assert b["roles"] == ["admin"]


def test_reverse_lookup_from_datasource(ds):
    tenant, did = ds
    a = store.create_business(tenant, "1P")
    b = store.create_business(tenant, "3P")
    store.create_business(tenant, "GDV")            # never scoped to this datasource
    for bid in (a, b):
        store.set_business_knowledge(bid, did, _knowledge())
    assert [x["name"] for x in store.list_datasource_businesses(did)] == ["1P", "3P"]


# ---- store: curation must not outlive its table ----
def test_prune_drops_vanished_tables_and_their_descriptions(ds):
    """84 Acme tables once vanished from a scan while their allowlist entries stayed, so
    questions routed to names govern() then refused."""
    tenant, did = ds
    bid = store.create_business(tenant, "3P")
    store.set_business_knowledge(bid, did, _knowledge(
        allowed_tables=["orders", "returns"],
        descriptions={"orders": "kept", "orders.gmv": "kept too", "returns": "gone",
                      "returns.qty": "gone too"}))
    removed = store.prune_business_tables(did, ["orders"])
    assert removed == {bid: ["returns"]}
    got = store.get_business_knowledge(bid, did)
    assert got["allowed_tables"] == ["orders"]
    assert got["descriptions"] == {"orders": "kept", "orders.gmv": "kept too"}


def test_prune_is_quiet_when_nothing_vanished(ds):
    tenant, did = ds
    bid = store.create_business(tenant, "3P")
    store.set_business_knowledge(bid, did, _knowledge(allowed_tables=["orders"]))
    assert store.prune_business_tables(did, ["orders", "returns"]) == {}


def test_datasource_of_another_tenant_cannot_be_scoped_in(ds):
    """The tenant is derived from the two ids, not passed in. Scoping one tenant's
    datasource into another tenant's business is a leak no downstream check could undo."""
    tenant, _ = ds
    other = store.create_tenant("BIZ-foreign")
    try:
        foreign_ds = store.create_datasource(other, "duckdb", "X", {"path": "x.duckdb"}, MDL)
        bid = store.create_business(tenant, "3P")
        with pytest.raises(ValueError, match="different tenants"):
            store.set_business_knowledge(bid, foreign_ds, _knowledge())
        with pytest.raises(ValueError, match="does not exist"):
            store.set_business_knowledge(bid, "ds_ghost", _knowledge())
        assert store.get_business_knowledge(bid, foreign_ds) is None
    finally:
        store.delete_tenant(other)


# ---- store: role -> businesses ----
def test_role_assignment_replaces_and_dedupes(tenant):
    a = store.create_business(tenant, "1P")
    b = store.create_business(tenant, "3P")
    store.set_role_businesses(tenant, "admin", [a, b, a])
    assert [x["id"] for x in store.list_role_businesses(tenant, "admin")] == [a, b]
    store.set_role_businesses(tenant, "admin", [b])
    assert [x["id"] for x in store.list_role_businesses(tenant, "admin")] == [b]
    store.set_role_businesses(tenant, "admin", [])
    assert store.list_role_businesses(tenant, "admin") == []


# ---- store: nothing may be left dangling ----
def test_deleting_a_business_takes_its_scopes_and_grants(ds):
    tenant, did = ds
    bid = store.create_business(tenant, "3P")
    store.set_business_knowledge(bid, did, _knowledge())
    store.set_role_businesses(tenant, "admin", [bid])
    store.delete_business(bid)
    assert store.get_business(bid) is None
    assert store.get_business_knowledge(bid, did) is None
    assert store.list_role_businesses(tenant, "admin") == []


def test_deleting_a_datasource_detaches_it_from_businesses(ds):
    tenant, did = ds
    bid = store.create_business(tenant, "3P")
    store.set_business_knowledge(bid, did, _knowledge(allowed_tables=["orders"]))
    store.delete_datasource(did)
    assert store.get_business_knowledge(bid, did) is None
    assert store.list_businesses(tenant)[0]["datasource_ids"] == []


def test_deleting_a_role_takes_its_grants(ds):
    """Otherwise a later role created with the same name silently inherits them."""
    tenant, _ = ds
    bid = store.create_business(tenant, "3P")
    store.set_role_businesses(tenant, "admin", [bid])
    store.delete_role(tenant, "admin")
    assert store.list_role_businesses(tenant, "admin") == []
    store.create_role(tenant, "admin", {"full_access": True})
    assert store.list_role_businesses(tenant, "admin") == []


def test_deleting_a_tenant_leaves_no_orphan_scope():
    tid = store.create_tenant("BIZ-cascade")
    store.create_role(tid, "admin", {"full_access": True})
    did = store.create_datasource(tid, "duckdb", "L2", {"path": "x.duckdb"}, MDL)
    bid = store.create_business(tid, "3P")
    store.set_business_knowledge(bid, did, _knowledge(allowed_tables=["orders"]))
    store.set_role_businesses(tid, "admin", [bid])
    store.delete_tenant(tid)
    assert store.list_businesses(tid) == []
    assert store.get_business(bid) is None
    assert store.get_business_knowledge(bid, did) is None
    assert store.list_role_businesses(tid, "admin") == []


# ---- API ----
def test_crud_over_http_and_duplicate_names(tenant):
    r = client.post(f"/admin/tenants/{tenant}/businesses", headers=AK,
                    json={"name": "3P", "descr": "marketplace"})
    assert r.status_code == 200
    bid = r.json()["id"]
    clash = client.post(f"/admin/tenants/{tenant}/businesses", headers=AK, json={"name": "3p"})
    assert clash.status_code == 409 and clash.json()["detail"]["code"] == "BUSINESS_EXISTS"

    other = client.post(f"/admin/tenants/{tenant}/businesses", headers=AK,
                        json={"name": "1P"}).json()["id"]
    rename = client.patch(f"/admin/tenants/{tenant}/businesses/{other}", headers=AK,
                          json={"name": "3P"})
    assert rename.status_code == 409

    ok = client.patch(f"/admin/tenants/{tenant}/businesses/{bid}", headers=AK,
                      json={"status": "paused"})
    assert ok.status_code == 200 and ok.json()["status"] == "paused"
    assert client.delete(f"/admin/tenants/{tenant}/businesses/{bid}",
                         headers=AK).status_code == 200
    assert [b["name"] for b in client.get(f"/admin/tenants/{tenant}/businesses",
                                          headers=AK).json()] == ["1P"]


def test_business_of_another_tenant_is_not_reachable(tenant):
    """The path's tenant id is what ownership was checked against, so the business id has to
    be re-checked against it — otherwise it is reachable by guessing."""
    other = store.create_tenant("BIZ-other")
    try:
        foreign = store.create_business(other, "3P")
        base = f"/admin/tenants/{tenant}/businesses/{foreign}"
        assert client.patch(base, headers=AK, json={"descr": "x"}).status_code == 404
        assert client.delete(base, headers=AK).status_code == 404
        assert client.get(f"{base}/datasources/ds_x/knowledge", headers=AK).status_code == 404
        assert client.patch(f"{base}/datasources/ds_x/knowledge", headers=AK,
                            json={}).status_code == 404
    finally:
        store.delete_tenant(other)


def test_knowledge_patch_attaches_then_narrows(ds):
    tenant, did = ds
    bid = client.post(f"/admin/tenants/{tenant}/businesses", headers=AK,
                      json={"name": "3P"}).json()["id"]
    url = f"/admin/tenants/{tenant}/businesses/{bid}/datasources/{did}/knowledge"

    before = client.get(url, headers=AK).json()
    assert before["in_business"] is False
    assert before["datasource"]["allowed_tables"] == ["orders", "returns"]

    # an empty body is how a datasource JOINS a business
    assert client.patch(url, headers=AK, json={}).status_code == 200
    assert client.get(url, headers=AK).json()["in_business"] is True

    r = client.patch(url, headers=AK, json={"allowed_tables": ["orders"],
                                            "descriptions": {"orders": "per order"}})
    assert r.json()["knowledge"]["allowed_tables"] == ["orders"]
    # unsent fields are left alone
    r2 = client.patch(url, headers=AK, json={"metrics": [{"name": "GMV"}]})
    assert r2.json()["knowledge"]["descriptions"] == {"orders": "per order"}


def test_business_may_only_narrow_never_widen(ds):
    """`stock` exists in the schema but not in the connection's own allowlist. A business
    asking for it must not be able to reach past its datasource — and the drop is reported,
    not silent."""
    tenant, did = ds
    bid = store.create_business(tenant, "3P")
    url = f"/admin/tenants/{tenant}/businesses/{bid}/datasources/{did}/knowledge"
    r = client.patch(url, headers=AK, json={"allowed_tables": ["orders", "stock", "ghost"]})
    assert r.status_code == 200
    assert r.json()["knowledge"]["allowed_tables"] == ["orders"]
    assert sorted(r.json()["dropped_tables"]) == ["ghost", "stock"]


def test_concepts_are_validated_against_the_business_slice(ds):
    """A concept pointing outside the business's own tables is a bug the admin should see
    now, not a govern() denial at answer time."""
    tenant, did = ds
    bid = store.create_business(tenant, "3P")
    url = f"/admin/tenants/{tenant}/businesses/{bid}/datasources/{did}/knowledge"
    good = client.patch(url, headers=AK, json={
        "allowed_tables": ["orders"],
        "business_concepts": [{"name": "GMV", "table": "orders", "metric": "gmv",
                               "aggregation": "SUM"}]})
    assert good.status_code == 200
    assert good.json()["knowledge"]["business_concepts"][0]["table"] == "orders"

    bad = client.patch(url, headers=AK, json={
        "business_concepts": [{"name": "Returns", "table": "returns", "metric": "qty"}]})
    assert bad.status_code == 400
    assert bad.json()["detail"]["code"] == "SEMANTIC_CONTRACT_INVALID"


def test_narrowing_the_allowlist_reports_an_orphaned_concept(ds):
    """Concepts are re-validated on EVERY save, not only when edited. Dropping a table the
    concept points at would otherwise store a contract govern() refuses at answer time."""
    tenant, did = ds
    bid = store.create_business(tenant, "3P")
    url = f"/admin/tenants/{tenant}/businesses/{bid}/datasources/{did}/knowledge"
    client.patch(url, headers=AK, json={
        "allowed_tables": ["orders", "returns"],
        "business_concepts": [{"name": "GMV", "table": "orders", "metric": "gmv"}]})
    orphan = client.patch(url, headers=AK, json={"allowed_tables": ["returns"]})
    assert orphan.status_code == 400
    assert orphan.json()["detail"]["code"] == "SEMANTIC_CONTRACT_INVALID"
    # refused, not half-applied
    assert client.get(url, headers=AK).json()["knowledge"]["allowed_tables"] ==         ["orders", "returns"]
    # dropping the table AND the concept together is accepted
    ok = client.patch(url, headers=AK, json={"allowed_tables": ["returns"],
                                            "business_concepts": []})
    assert ok.status_code == 200 and ok.json()["knowledge"]["allowed_tables"] == ["returns"]


def test_rescan_drops_vanished_tables_from_every_business(tmp_path):
    """End-to-end on a real DuckDB file: the wiring, not just prune_business_tables()."""
    import duckdb
    path = str(tmp_path / "scan.duckdb")
    con = duckdb.connect(path)
    con.execute("CREATE TABLE orders(order_id INTEGER, gmv DOUBLE)")
    con.execute("CREATE TABLE seasonal(id INTEGER)")
    con.close()

    tid = store.create_tenant("BIZ-rescan")
    try:
        store.create_role(tid, "admin", {"full_access": True})
        created = client.post("/admin/datasources", headers=AK, json={
            "tenant_id": tid, "type": "duckdb", "name": "Scan", "conn": {"path": path}})
        assert created.status_code == 200, created.text
        did = created.json()["id"]
        assert set(store.get_datasource(did)["mdl"]["tables"]) == {"orders", "seasonal"}

        bid = store.create_business(tid, "3P")
        store.set_business_knowledge(bid, did, _knowledge(
            allowed_tables=["orders", "seasonal"],
            descriptions={"seasonal": "kept until the table goes"}))

        con = duckdb.connect(path)
        con.execute("DROP TABLE seasonal")
        con.close()

        r = client.post(f"/admin/datasources/{did}/rescan", headers=AK)
        assert r.status_code == 200, r.text
        assert r.json()["business_pruned"] == {bid: ["seasonal"]}
        got = store.get_business_knowledge(bid, did)
        assert got["allowed_tables"] == ["orders"]
        assert got["descriptions"] == {}
    finally:
        store.delete_tenant(tid)


def test_detaching_a_datasource_keeps_the_datasource(ds):
    tenant, did = ds
    bid = store.create_business(tenant, "3P")
    store.set_business_knowledge(bid, did, _knowledge(allowed_tables=["orders"]))
    r = client.delete(f"/admin/tenants/{tenant}/businesses/{bid}/datasources/{did}", headers=AK)
    assert r.status_code == 200
    assert store.get_business_knowledge(bid, did) is None
    assert store.get_datasource(did) is not None


def test_role_grants_over_http(tenant):
    bid = store.create_business(tenant, "3P")
    base = f"/admin/tenants/{tenant}/roles"
    assert client.get(f"{base}/admin/businesses", headers=AK).json() == []
    r = client.put(f"{base}/admin/businesses", headers=AK, json={"business_ids": [bid]})
    assert r.status_code == 200 and [x["id"] for x in r.json()] == [bid]

    foreign = client.put(f"{base}/admin/businesses", headers=AK,
                         json={"business_ids": ["b_nope"]})
    assert foreign.status_code == 400
    assert foreign.json()["detail"]["code"] == "BUSINESS_NOT_FOUND"
    # rejected outright: a half-applied grant is indistinguishable from a deliberate one
    assert [x["id"] for x in client.get(f"{base}/admin/businesses", headers=AK).json()] == [bid]

    assert client.get(f"{base}/ghost/businesses", headers=AK).status_code == 404
    assert client.put(f"{base}/ghost/businesses", headers=AK,
                      json={"business_ids": []}).status_code == 404
