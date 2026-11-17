"""store.py connection pool: reuse, and the two ways a pooled connection can go wrong.

A pooled connection carries state the next caller inherits — the schema its search_path
points at, and any transaction the previous caller left behind. Both are load-bearing:
the suite itself runs each xdist worker in its own ADMIN_DB_SCHEMA, and a connection
handed back after a failed statement is still in the aborted state where Postgres
rejects every following statement.
"""
import os
import uuid

import psycopg2
import pytest

from app import store


def _make_schema():
    name = "pool_" + uuid.uuid4().hex[:12]
    admin = psycopg2.connect(store.DB_URL); admin.autocommit = True
    admin.cursor().execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE; CREATE SCHEMA "{name}"')
    admin.close()
    return name


def _drop_schema(name):
    admin = psycopg2.connect(store.DB_URL); admin.autocommit = True
    admin.cursor().execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
    admin.close()


def _flush_pool():
    """Close every idle connection — including any still pointing at a dropped schema."""
    for conns in store._pools.values():
        for conn in conns:
            conn.close()
    store._pools.clear()


@pytest.fixture
def schema(monkeypatch):
    """An initialized throwaway schema, active for the duration of one test."""
    name = _make_schema()
    monkeypatch.setenv("ADMIN_DB_SCHEMA", name)
    store.init()
    yield name
    _flush_pool()
    _drop_schema(name)


@pytest.fixture
def connects(monkeypatch):
    """Counts psycopg2.connect() calls made from here on."""
    calls = []
    real = psycopg2.connect

    def counted(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(psycopg2, "connect", counted)
    return calls


def test_connections_are_reused(schema, connects):
    store.list_tenants()                       # warm: one connect at most
    del connects[:]
    for _ in range(20):
        store.list_tenants()
    assert connects == []


def test_pooling_can_be_switched_off(schema, connects, monkeypatch):
    monkeypatch.setattr(store, "_POOL_MAX_IDLE", 0)
    _flush_pool()
    for _ in range(3):
        store.list_tenants()
    assert len(connects) == 3


def test_idle_connections_are_bounded(schema, monkeypatch):
    monkeypatch.setattr(store, "_POOL_MAX_IDLE", 2)
    _flush_pool()
    open_conns = [store._c() for _ in range(5)]
    for c in open_conns:
        c.close()
    assert len(store._pools[schema]) == 2      # the other three were closed, not stockpiled


def test_schema_switch_does_not_leak_search_path(schema, monkeypatch):
    """The hazard the pool is keyed on: tests swap ADMIN_DB_SCHEMA under a live pool."""
    store.create_tenant("in-first-schema")
    other = _make_schema()
    try:
        monkeypatch.setenv("ADMIN_DB_SCHEMA", other)
        store.init()
        assert store.list_tenants() == []      # a leaked connection would show the other schema
        store.create_tenant("in-second-schema")

        monkeypatch.setenv("ADMIN_DB_SCHEMA", schema)
        assert [t["name"] for t in store.list_tenants()] == ["in-first-schema"]
    finally:
        monkeypatch.setenv("ADMIN_DB_SCHEMA", other)
        _flush_pool()
        _drop_schema(other)
        monkeypatch.setenv("ADMIN_DB_SCHEMA", schema)


def test_aborted_statement_does_not_poison_the_pool(schema):
    store.create_tenant("survivor")
    c = store._c()
    with pytest.raises(psycopg2.Error):
        c.execute("SELECT * FROM table_that_does_not_exist")
    c.close()                                  # handed back mid-abort
    assert [t["name"] for t in store.list_tenants()] == ["survivor"]


def test_uncommitted_write_is_rolled_back_on_return(schema):
    c = store._c()
    c.execute("INSERT INTO tenants(id,name,created) VALUES('t_leak','leak',?)", (store.now(),))
    c.close()                                  # never committed
    assert store.list_tenants() == []


def test_connection_dropped_while_idle_is_replaced(schema):
    store.list_tenants()
    idle = store._pools[schema]
    assert idle, "expected one connection waiting in the pool"
    idle[-1].close()                           # as if the server hung up on it
    assert store.list_tenants() == []          # checkout notices and connects again


def test_double_close_does_not_hand_the_same_connection_out_twice(schema):
    c = store._c()
    c.close()
    c.close()
    assert len(store._pools[schema]) == 1


def test_tenant_scope_is_reapplied_on_every_checkout(schema, monkeypatch):
    """RLS reads poros.tenant_id from the session, so a reused connection must not keep
    the previous request's scope."""
    def scope_now():
        c = store._c()
        try:
            return c.execute("SELECT current_setting('poros.tenant_id', true) v").fetchone()["v"]
        finally:
            c.close()

    monkeypatch.setattr(store, "get_tenant_db_scope", lambda: "t_first")
    assert scope_now() == "t_first"
    monkeypatch.setattr(store, "get_tenant_db_scope", lambda: "t_second")
    assert scope_now() == "t_second"


def test_search_path_survives_a_caller_side_rollback(schema):
    """store.reserve_budget rolls back mid-operation and keeps using the connection."""
    c = store._c()
    try:
        c.execute("INSERT INTO tenants(id,name,created) VALUES('t_x','x',?)", (store.now(),))
        c._c.rollback()
        row = c.execute("SELECT current_schema() s").fetchone()
        assert row["s"] == (os.environ.get("ADMIN_DB_SCHEMA") or "public")
    finally:
        c.close()
