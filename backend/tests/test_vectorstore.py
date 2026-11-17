"""pgvector vector-store seam — offline (fake psycopg2 connection, no live Postgres).

Mirrors test_secrets_backend's approach of faking the transport (_http there,
_conn here) so the SQL construction + row mapping + fallback logic are verified
without any external service. The default sqlite path stays covered by the rest
of the suite (conftest forces VECTOR_BACKEND unset + offline embedder).
"""
import pytest
from app import vectorstore as vs
from app.rag import retriever


# ---------- fake psycopg2 ----------
class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.calls = []            # (sql, params) issued through this cursor
    def execute(self, sql, params=None):
        self.calls.append((sql, params))
    def fetchall(self):
        return self._rows
    def close(self):
        pass


class FakeConn:
    def __init__(self, rows=None, raise_on_execute=False):
        self.cursor_obj = FakeCursor(rows or [])
        self.committed = False
        self._raise = raise_on_execute
    def cursor(self):
        if self._raise:
            raise RuntimeError("boom")
        return self.cursor_obj
    def commit(self):
        self.committed = True
    def close(self):
        pass


@pytest.fixture
def pg(monkeypatch):
    """Enable pgvector with a tiny dim and stub out schema creation."""
    monkeypatch.setenv("VECTOR_BACKEND", "pgvector")
    monkeypatch.setenv("VECTOR_PG_DIM", "4")
    monkeypatch.setenv("VECTOR_PG_TABLE", "doc_chunks_vec")
    monkeypatch.setattr(vs, "_ensure_schema", lambda conn: None)
    monkeypatch.setattr(vs, "_ensured", True, raising=False)
    return monkeypatch


# ---------- backend selection ----------
def test_backend_defaults_to_sqlite(monkeypatch):
    monkeypatch.delenv("VECTOR_BACKEND", raising=False)
    assert vs.backend() == "sqlite"
    assert vs.enabled() is False


def test_pgvector_enabled(monkeypatch):
    monkeypatch.setenv("VECTOR_BACKEND", "pgvector")
    assert vs.enabled() is True


def test_vec_literal():
    assert vs._vec_literal([0.1, 2, 3.0]) == "[0.1,2.0,3.0]"


# ---------- search ----------
def test_search_disabled_returns_none(monkeypatch):
    monkeypatch.delenv("VECTOR_BACKEND", raising=False)
    assert vs.search_chunks("t1", [0.1, 0.2, 0.3, 0.4], "analyst", 5) is None


def test_search_dim_mismatch_returns_none(pg):
    # query dim (3) != VECTOR_PG_DIM (4) -> skip pgvector (retriever falls back)
    assert vs.search_chunks("t1", [0.1, 0.2, 0.3], "analyst", 5) is None


def test_search_builds_query_and_maps_rows(pg):
    rows = [("ch_1", "doc_1", 0, "hello world", ["analyst"], "sop.pdf", 0.87)]
    fake = FakeConn(rows=rows)
    pg.setattr(vs, "_conn", lambda: fake)
    out = vs.search_chunks("t1", [0.1, 0.2, 0.3, 0.4], "analyst", 5)
    assert out == [{"id": "ch_1", "document_id": "doc_1", "chunk_index": 0, "text": "hello world",
                    "acl_roles": ["analyst"], "filename": "sop.pdf", "score": 0.87}]
    sql, params = fake.cursor_obj.calls[-1]
    assert "ORDER BY embedding <=> %s::vector" in sql
    assert "archived = false" in sql
    assert "jsonb_array_length" in sql          # ACL: empty acl = visible to all
    # no paused datasources -> the exclusion is short-circuited by the boolean, not by
    # omitting the clause, so the SQL text stays constant and plan-cacheable
    assert params == ("[0.1,0.2,0.3,0.4]", "t1", "analyst", True, [],
                      "[0.1,0.2,0.3,0.4]", 5)


def test_search_excludes_paused_datasources(pg):
    """A disabled datasource must stop feeding document answers. The scan path filters it
    with a SQL join; the mirror has no join, so the ids are passed in."""
    fake = FakeConn(rows=[])
    pg.setattr(vs, "_conn", lambda: fake)
    vs.search_chunks("t1", [0.1, 0.2, 0.3, 0.4], "analyst", 5,
                     exclude_datasources=["ds_acme_l2"])
    sql, params = fake.cursor_obj.calls[-1]
    assert "datasource_id = ANY(%s)" in sql
    assert params[3] is False and params[4] == ["ds_acme_l2"]


def test_search_error_returns_none(pg):
    pg.setattr(vs, "_conn", lambda: FakeConn(raise_on_execute=True))
    assert vs.search_chunks("t1", [0.1, 0.2, 0.3, 0.4], "analyst", 5) is None


# ---------- writes ----------
def test_upsert_skips_dim_mismatch(pg):
    fake = FakeConn()
    pg.setattr(vs, "_conn", lambda: fake)
    vs.upsert_chunks([
        {"id": "ch_ok", "tenant_id": "t1", "document_id": "d1", "chunk_index": 0,
         "text": "a", "embedding": [1, 2, 3, 4], "acl_roles": [], "filename": "f", "archived": False},
        {"id": "ch_bad", "tenant_id": "t1", "document_id": "d1", "chunk_index": 1,
         "text": "b", "embedding": [1, 2], "acl_roles": [], "filename": "f", "archived": False},
    ])
    inserts = [c for c in fake.cursor_obj.calls if "INSERT INTO" in c[0]]
    assert len(inserts) == 1
    assert inserts[0][1][0] == "ch_ok"
    assert fake.committed is True


def test_delete_documents_uses_any(pg):
    fake = FakeConn()
    pg.setattr(vs, "_conn", lambda: fake)
    vs.delete_documents(["d1", "d2"])
    sql, params = fake.cursor_obj.calls[-1]
    assert "DELETE FROM" in sql and "= ANY(%s)" in sql
    assert params == (["d1", "d2"],)


def test_writes_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("VECTOR_BACKEND", raising=False)
    called = []
    monkeypatch.setattr(vs, "_conn", lambda: called.append(1))
    vs.upsert_chunks([{"id": "x", "embedding": [1, 2, 3, 4]}])
    vs.delete_documents(["d1"])
    vs.set_archived("d1", True)
    assert called == []          # no connection attempted in sqlite mode


# ---------- rebuild ----------
def test_rebuild_reads_full_and_upserts(pg):
    from app import store
    chunks = [{"id": "c1", "tenant_id": "t1", "document_id": "d1", "chunk_index": 0,
               "text": "x", "embedding": [1, 2, 3, 4], "acl_roles": [], "filename": "f", "archived": False}]
    pg.setattr(store, "list_tenants", lambda: [{"id": "t1"}])
    pg.setattr(store, "iter_chunks_full", lambda tid: chunks)
    seen = {"deleted": [], "upserted": []}
    pg.setattr(vs, "delete_tenant", lambda tid: seen["deleted"].append(tid))
    pg.setattr(vs, "upsert_chunks", lambda rows: seen["upserted"].extend(rows))
    res = vs.rebuild()
    assert res["rebuilt_chunks"] == 1
    assert seen["deleted"] == ["t1"]
    assert seen["upserted"] == chunks


# ---------- retriever integration (fallback + pgvector path) ----------
def test_retriever_uses_pgvector_hits(monkeypatch):
    monkeypatch.setattr(vs, "enabled", lambda: True)
    monkeypatch.setattr(retriever, "_reranker", lambda: None)
    monkeypatch.setattr("app.rag.retriever.embed_query", lambda t, q: [0.1, 0.2, 0.3, 0.4])
    hit = {"id": "ch_1", "document_id": "doc_1", "chunk_index": 2, "text": "[Slide 3] governed answer",
           "acl_roles": ["analyst"], "filename": "sop.pptx", "score": 0.9}
    monkeypatch.setattr(vs, "search_chunks", lambda t, q, r, k, **_kw: [hit])
    # retrieve() reads the paused-datasource ids live (fail-CLOSED: a swallowed error
    # here would make a switched-off source searchable again). This file is offline.
    monkeypatch.setattr("app.rag.retriever.store.disabled_datasource_ids", lambda t: [])
    out = retriever.retrieve("t1", "how to", "analyst", top_k=3)
    assert len(out) == 1
    assert out[0]["document_id"] == "doc_1"
    assert out[0]["citation_label"] == "sop.pptx — Slide 3"   # human citation from the [Slide N] marker


def test_retriever_falls_back_to_sqlite_when_pgvector_errs(monkeypatch):
    monkeypatch.setattr(vs, "enabled", lambda: True)
    monkeypatch.setattr(vs, "search_chunks", lambda t, q, r, k, **_kw: None)   # hard pgvector error
    # retrieve() reads the paused-datasource ids live (fail-CLOSED: a swallowed error
    # here would make a switched-off source searchable again). This file is offline.
    monkeypatch.setattr("app.rag.retriever.store.disabled_datasource_ids", lambda t: [])
    monkeypatch.setattr(retriever, "_reranker", lambda: None)
    monkeypatch.setattr("app.rag.retriever.embed_query", lambda t, q: [0.1, 0.2, 0.3, 0.4])
    chunk = {"id": "c1", "document_id": "d1", "chunk_index": 0, "text": "from sqlite",
             "acl_roles": ["analyst"], "filename": "f.md", "embedding": [0.1, 0.2, 0.3, 0.4]}
    monkeypatch.setattr("app.rag.retriever.store.iter_chunks", lambda t: [chunk])
    out = retriever.retrieve("t1", "q", "analyst", top_k=3)
    assert len(out) == 1 and out[0]["text"] == "from sqlite"
