"""Offline tests for the wired Document-config knobs:

  - chunk_size / chunk_overlap  -> control chunking (words) in ingest
  - max_documents               -> cap active docs per tenant at upload
  - auto_reindex                -> auto re-embed the tenant when the embedder was switched

Hermetic: conftest pins the offline embedder (ST_EMBED_DISABLE=1) and leaves VECTOR_BACKEND
unset (pgvector off -> sqlite scan mode), and each test uses its OWN tenant so there is no
dependency on seed data or test-module ordering.
"""
import io

from fastapi.testclient import TestClient

from app import jobs, seed, store
from app.rag import ingest

seed.run()                       # idempotent: skips a non-empty store
from app.main import app         # noqa: E402

client = TestClient(app)
AK = {"X-Admin-Key": "admin-dev-key"}


# ---------------- chunk_size / chunk_overlap (pure) ----------------
def test_chunk_params_defaults():
    assert ingest._chunk_params(None) == (180, 40)
    assert ingest._chunk_params({}) == (180, 40)


def test_chunk_params_clamped():
    # size floored to 40, overlap can never exceed size//2
    assert ingest._chunk_params({"chunk_size": 5, "chunk_overlap": 999}) == (40, 20)
    # size capped at 800
    assert ingest._chunk_params({"chunk_size": 100000, "chunk_overlap": 30}) == (800, 30)


def test_smaller_chunk_size_makes_more_chunks():
    text = "\n\n".join("kata " * 50 for _ in range(20))     # 20 paragraphs x 50 words
    many = ingest.chunk_text(text, target_words=60, overlap=5)
    few = ingest.chunk_text(text, target_words=500, overlap=5)
    assert len(many) > len(few) >= 1


# ---------------- helpers for the endpoint tests ----------------
def _docs_tenant(name):
    tid = store.create_tenant(name)
    dsid = client.post("/admin/datasources", headers=AK,
                       json={"tenant_id": tid, "type": "docs", "name": "Docs", "conn": {}}).json()["id"]
    return tid, dsid


def _upload(tid, dsid, fname, body=b"# Doc\n\nIsi dokumen contoh untuk retrieval demo."):
    response = client.post("/admin/documents", headers=AK,
                           data={"tenant_id": tid, "datasource_id": dsid, "acl_roles": ""},
                           files={"file": (fname, io.BytesIO(body), "text/markdown")})
    return _finish_upload(response)


class _CompletedJobResponse:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def _finish_upload(response):
    if response.status_code != 202:
        return response
    job_id = response.json()["job_id"]
    while jobs.public(job_id)["status"] in {"queued", "running"}:
        assert jobs.run_one("doc-test-worker")
    state = jobs.public(job_id)
    assert state["status"] == "succeeded", state
    return _CompletedJobResponse(state["result"])


# ---------------- max_documents ----------------
def test_max_documents_caps_active_docs():
    tid, dsid = _docs_tenant("wire-maxdocs")
    store.set_tenant_config(tid, "document", {"max_documents": 1})
    assert _upload(tid, dsid, "a.md").status_code == 200          # first: ok
    r = _upload(tid, dsid, "b.md")                                # second: over the cap
    assert r.status_code == 400 and r.json()["detail"]["code"] == "MAX_DOCUMENTS"


def test_max_documents_allows_same_filename_replacement():
    tid, dsid = _docs_tenant("wire-maxdocs-repl")
    store.set_tenant_config(tid, "document", {"max_documents": 1})
    assert _upload(tid, dsid, "a.md").status_code == 200
    # same filename = a NEW VERSION (old auto-archived) -> replacement never trips the cap
    assert _upload(tid, dsid, "a.md").status_code == 200


def test_max_documents_zero_is_unlimited():
    tid, dsid = _docs_tenant("wire-maxdocs-zero")
    store.set_tenant_config(tid, "document", {"max_documents": 0})
    for fn in ("a.md", "b.md", "c.md"):
        assert _upload(tid, dsid, fn).status_code == 200


# ---------------- auto_reindex ----------------
def test_auto_reindex_reembeds_on_embedder_switch():
    tid, dsid = _docs_tenant("wire-autoreindex")
    store.set_tenant_config(tid, "document", {"auto_reindex": True})
    up = _upload(tid, dsid, "old.md")
    assert up.status_code == 200
    doc_id = up.json()["document_id"]
    # simulate a model switch: pretend this doc was embedded by an older model
    store.set_document_embedder(doc_id, "some-old-embedder")
    up2 = _upload(tid, dsid, "new.md")               # upload now must auto-reindex the stale doc
    assert up2.status_code == 200
    assert up2.json().get("auto_reindexed", {}).get("reindexed") is True
    cur = up2.json()["embedder"]
    docs = {d["id"]: d for d in store.list_documents(tid)}
    assert docs[doc_id]["embedder"] == cur           # stale doc re-embedded to the current model


def test_auto_reindex_noop_when_all_current():
    tid, dsid = _docs_tenant("wire-autoreindex-noop")
    store.set_tenant_config(tid, "document", {"auto_reindex": True})
    assert _upload(tid, dsid, "a.md").status_code == 200
    up2 = _upload(tid, dsid, "b.md")                 # no embedder mismatch -> nothing to reindex
    assert up2.status_code == 200 and "auto_reindexed" not in up2.json()


# ---------------- per-document config (chunk/overlap/ocr) ----------------
_MANY_PARAS = ("\n\n".join("kata " * 30 for _ in range(12))).encode()   # 12 paragraphs x 30 words


def _upload_cfg(tid, dsid, fname, **cfg):
    response = client.post("/admin/documents", headers=AK,
                           data={"tenant_id": tid, "datasource_id": dsid, "acl_roles": "", **cfg},
                           files={"file": (fname, io.BytesIO(_MANY_PARAS), "text/markdown")})
    return _finish_upload(response)


def test_upload_stores_per_document_config():
    tid, dsid = _docs_tenant("wire-perdoc")
    r = _upload_cfg(tid, dsid, "a.md", chunk_size=40, chunk_overlap=5, ocr="false")
    assert r.status_code == 200
    assert r.json()["config"] == {"chunk_size": 40, "chunk_overlap": 5, "ocr": False}
    assert store.list_documents(tid, dsid)[0]["config"]["chunk_size"] == 40   # persisted on the doc


def test_two_docs_can_have_different_config():
    tid, dsid = _docs_tenant("wire-perdoc-diff")
    small = _upload_cfg(tid, dsid, "small.md", chunk_size=40, chunk_overlap=5).json()
    big = _upload_cfg(tid, dsid, "big.md", chunk_size=400, chunk_overlap=5).json()
    assert small["config"]["chunk_size"] == 40 and big["config"]["chunk_size"] == 400
    assert small["n_chunks"] > big["n_chunks"]        # smaller chunk size -> more chunks


def test_edit_document_config_reembeds():
    tid, dsid = _docs_tenant("wire-perdoc-edit")
    up = _upload_cfg(tid, dsid, "doc.md", chunk_size=400, chunk_overlap=5).json()
    doc_id, before = up["document_id"], up["n_chunks"]
    r = client.patch(f"/admin/documents/{doc_id}/config", headers=AK,
                     json={"chunk_size": 40, "chunk_overlap": 5})
    assert r.status_code == 200 and r.json()["n_chunks"] > before   # re-chunked smaller -> more chunks
    doc = store.get_document(doc_id)
    assert doc["config"]["chunk_size"] == 40 and doc["n_chunks"] == r.json()["n_chunks"]
