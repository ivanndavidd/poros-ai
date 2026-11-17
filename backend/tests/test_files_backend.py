"""FILES_BACKEND=db: original uploads live in Postgres so pods can be stateless.

The ReadWriteOnce PVC behind ``store.FILES_DIR`` is what pins the production deploy to
a single replica (a second pod cannot attach the volume, and an upload landing on pod A
answers NO_FILE on pod B). With the originals in the ``document_files`` table — the same
per-tenant AES-GCM envelope as on disk — the pod keeps no state worth a volume. Reads
stay REF-DRIVEN: a "db:<id>" file_path reads the table, a filesystem path reads disk,
so flipping the env in either direction never strands existing rows.
"""
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import document_crypto, store       # noqa: E402
from app.main import app                     # noqa: E402

client = TestClient(app)
AK = {"X-Admin-Key": "admin-dev-key"}


@pytest.fixture()
def tenant():
    tid = client.post("/admin/tenants", headers=AK,
                      json={"name": "FilesBackend Co"}).json()["id"]
    yield tid
    store.delete_tenant(tid)


def _doc(tid, name="laporan.pdf"):
    return store.create_document(tid, None, name, "application/pdf", [], 0)


def test_db_backend_roundtrip_encrypted_and_diskless(tenant, monkeypatch):
    monkeypatch.setenv("FILES_BACKEND", "db")
    doc_id = _doc(tenant)
    ref = store.save_document_file(tenant, doc_id, "laporan.pdf", b"ISI ASLI PDF")
    assert ref == f"db:{doc_id}"
    assert store.read_document_file(tenant, doc_id) == b"ISI ASLI PDF"
    # stored as the same AES-GCM envelope used on disk, never plaintext
    c = store._c()
    raw = c.execute("SELECT data FROM document_files WHERE document_id=?",
                    (doc_id,)).fetchone()
    c.close()
    assert document_crypto.encrypted(bytes(raw["data"]))
    # and nothing landed under FILES_DIR for this tenant — that is the whole point
    assert not os.path.exists(os.path.join(store.FILES_DIR, tenant))


def test_reads_are_ref_driven_across_backend_flips(tenant, monkeypatch):
    monkeypatch.delenv("FILES_BACKEND", raising=False)     # disk (default)
    on_disk = _doc(tenant, "a.txt")
    store.save_document_file(tenant, on_disk, "a.txt", b"dari disk")
    monkeypatch.setenv("FILES_BACKEND", "db")
    in_db = _doc(tenant, "b.txt")
    store.save_document_file(tenant, in_db, "b.txt", b"dari db")
    # the db backend still reads the old disk row
    assert store.read_document_file(tenant, on_disk) == b"dari disk"
    # and rolling the env back to disk still reads the db row
    monkeypatch.delenv("FILES_BACKEND", raising=False)
    assert store.read_document_file(tenant, in_db) == b"dari db"


def test_availability_check_and_download_understand_db_refs(tenant, monkeypatch):
    monkeypatch.setenv("FILES_BACKEND", "db")
    doc_id = _doc(tenant)
    store.save_document_file(tenant, doc_id, "laporan.pdf", b"x")
    assert store.document_file_available(store.get_document(doc_id))
    # the download endpoint used to os.path.exists() the ref and answer NO_FILE
    r = client.get(f"/admin/documents/{doc_id}/file", headers=AK)
    assert r.status_code == 200 and r.content == b"x"


def test_wrong_tenant_reads_nothing(tenant, monkeypatch):
    monkeypatch.setenv("FILES_BACKEND", "db")
    doc_id = _doc(tenant)
    store.save_document_file(tenant, doc_id, "laporan.pdf", b"rahasia")
    with pytest.raises(FileNotFoundError):
        store.read_document_file("t_lain", doc_id)


def test_document_delete_purges_the_row(tenant, monkeypatch):
    monkeypatch.setenv("FILES_BACKEND", "db")
    doc_id = _doc(tenant)
    store.save_document_file(tenant, doc_id, "laporan.pdf", b"x")
    store.delete_document(tenant, doc_id)
    c = store._c()
    n = c.execute("SELECT COUNT(*) n FROM document_files WHERE document_id=?",
                  (doc_id,)).fetchone()["n"]
    c.close()
    assert n == 0


def test_tenant_delete_takes_the_files_with_it(monkeypatch):
    monkeypatch.setenv("FILES_BACKEND", "db")
    tid = client.post("/admin/tenants", headers=AK,
                      json={"name": "FilesBackend Purge Co"}).json()["id"]
    doc_id = _doc(tid)
    store.save_document_file(tid, doc_id, "laporan.pdf", b"x")
    store.delete_tenant(tid)
    c = store._c()
    n = c.execute("SELECT COUNT(*) n FROM document_files WHERE tenant_id=?",
                  (tid,)).fetchone()["n"]
    c.close()
    assert n == 0
