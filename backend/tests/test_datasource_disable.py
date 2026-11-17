"""Pausing a datasource: kept, reversible, and invisible to EVERY answering path.

The point of the feature is that a source can be taken out of service without losing its
connection config or its hand-written MDL curation — deleting and re-adding loses both.

The tests that matter are the enforcement ones. Hiding a card in the console would be
cosmetic: the orchestrator's router, the cross-datasource planner, /v1/query and document
retrieval each select datasources independently, so the filter has to live where they all
pass through (store.list_datasources) rather than in the UI.
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

MDL = {"tables": {"orders": ["order_id", "gmv"]}, "allowed_tables": ["orders"],
       "pii_columns": [], "denied_functions": [], "allowed_functions": []}


@pytest.fixture
def tenant():
    tid = store.create_tenant("DSD-disable-test")
    yield tid
    store.delete_tenant(tid)


@pytest.fixture
def two_sources(tenant):
    keep = store.create_datasource(tenant, "duckdb", "Keep", {"path": "x.duckdb"}, MDL)
    pause = store.create_datasource(tenant, "duckdb", "Pause", {"path": "y.duckdb"}, MDL)
    return tenant, keep, pause


# ---- the flag itself ----
def test_existing_rows_read_as_enabled(two_sources):
    """The migration adds the column with no backfill, so every pre-existing row is NULL.
    COALESCE(enabled,1) is the only correct test — a plain `enabled=1` would have made
    every datasource in the product vanish on deploy."""
    _tenant, keep, _pause = two_sources
    assert store.get_datasource(keep)["enabled"] is True


def test_disable_then_enable_is_lossless(two_sources):
    _tenant, _keep, pause = two_sources
    before = store.get_datasource(pause)
    store.set_datasource_enabled(pause, False)
    assert store.get_datasource(pause)["enabled"] is False
    store.set_datasource_enabled(pause, True)
    after = store.get_datasource(pause)
    assert after["enabled"] is True
    assert after["conn"] == before["conn"] and after["mdl"] == before["mdl"]


# ---- enforcement: the answering paths ----
def test_list_datasources_hides_it_by_default(two_sources):
    """The default is the safe one so a future caller that forgets the flag still gets the
    filtered list."""
    tenant, keep, pause = two_sources
    store.set_datasource_enabled(pause, False)
    ids = {d["id"] for d in store.list_datasources(tenant)}
    assert ids == {keep}


def test_admin_console_still_sees_it(two_sources):
    """The one caller that must NOT filter — otherwise the card disappears and there is no
    way to switch it back on."""
    tenant, keep, pause = two_sources
    store.set_datasource_enabled(pause, False)
    ids = {d["id"] for d in store.list_datasources(tenant, include_disabled=True)}
    assert ids == {keep, pause}
    r = client.get(f"/admin/tenants/{tenant}/datasources", headers=AK)
    assert r.status_code == 200
    by_id = {d["id"]: d for d in r.json()}
    assert by_id[pause]["enabled"] is False and by_id[keep]["enabled"] is True


def test_v1_query_cannot_reach_a_paused_source(two_sources):
    """Naming the id explicitly must not bypass the pause."""
    from app.routers import consumer
    tenant, keep, pause = two_sources
    store.set_datasource_enabled(pause, False)
    assert consumer._pick_sql_ds(tenant, None)["id"] == keep
    with pytest.raises(Exception):
        consumer._pick_sql_ds(tenant, pause)


def test_paused_source_documents_stop_being_retrieved(two_sources):
    """A pause that still fed document answers would be a broken promise: the source is
    "off" for SQL but its PDFs keep showing up as citations."""
    tenant, keep, pause = two_sources
    for ds_id, name in ((keep, "keep.md"), (pause, "pause.md")):
        doc = store.create_document(tenant, ds_id, name, "text/markdown", [], 1)
        store.add_chunks(tenant, doc, [(0, f"isi {name}", [0.1, 0.2, 0.3, 0.4])], [])
    assert {c["filename"] for c in store.iter_chunks(tenant)} == {"keep.md", "pause.md"}
    store.set_datasource_enabled(pause, False)
    assert {c["filename"] for c in store.iter_chunks(tenant)} == {"keep.md"}


def test_rebuild_still_sees_everything(two_sources):
    """iter_chunks_full feeds the pgvector REBUILD, which must stay a faithful copy of the
    store — filtering there would silently drop chunks that come back when re-enabled."""
    tenant, _keep, pause = two_sources
    doc = store.create_document(tenant, pause, "p.md", "text/markdown", [], 1)
    store.add_chunks(tenant, doc, [(0, "isi", [0.1, 0.2, 0.3, 0.4])], [])
    store.set_datasource_enabled(pause, False)
    assert any(c["document_id"] == doc for c in store.iter_chunks_full(tenant))


# ---- the endpoint ----
def test_patch_toggles_and_reports(two_sources):
    _tenant, _keep, pause = two_sources
    r = client.patch(f"/admin/datasources/{pause}", headers=AK, json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert "path" not in str(r.json().get("conn")), "connection config leaked to the console"
    r = client.patch(f"/admin/datasources/{pause}", headers=AK, json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True


def test_patch_unknown_datasource_is_404():
    r = client.patch("/admin/datasources/ds_nope", headers=AK, json={"enabled": False})
    assert r.status_code == 404


def test_admin_repair_operations_still_work_while_paused(two_sources):
    """Deliberate: a source is usually paused BECAUSE it is broken, so rescan / auto-enrich
    / connection rotation must keep working on it — they read get_datasource, which is not
    filtered."""
    _tenant, _keep, pause = two_sources
    store.set_datasource_enabled(pause, False)
    assert store.get_datasource(pause) is not None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
