"""Live round-trip smoke test for the pgvector backend (needs a running Postgres+pgvector).

Unlike tests/test_vectorstore.py (fake connection), this exercises the REAL SQL against
a real pgvector instance — catching anything the fake can't (extension present, `<=>`
operator, HNSW index, jsonb ACL operators, ::vector casts).

Usage (from backend/):
    set PGVECTOR_PASSWORD=<local-only-password>
    docker compose -f ../deploy/pgvector/docker-compose.yml up -d
    set PGPASSWORD=%PGVECTOR_PASSWORD%
    set VECTOR_BACKEND=pgvector
    set VECTOR_PG_DSN=postgresql://poros@localhost:5433/poros_vec
    set VECTOR_PG_DIM=4
    python ../deploy/pgvector/smoke.py

Prints OK + the ranked ids, or raises. Cleans up its own rows.
"""
import os, sys

os.environ.setdefault("VECTOR_BACKEND", "pgvector")
os.environ.setdefault("VECTOR_PG_DIM", "4")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from app import vectorstore as vs   # noqa: E402

assert vs.enabled(), "set VECTOR_BACKEND=pgvector first"
T = "smoke_tenant"

# two chunks; the query vector is closest to ch_a
rows = [
    {"id": "smoke_a", "tenant_id": T, "document_id": "smoke_doc", "datasource_id": "ds",
     "chunk_index": 0, "text": "alpha", "embedding": [1, 0, 0, 0], "acl_roles": [], "filename": "f.md", "archived": False},
    {"id": "smoke_b", "tenant_id": T, "document_id": "smoke_doc", "datasource_id": "ds",
     "chunk_index": 1, "text": "beta", "embedding": [0, 1, 0, 0], "acl_roles": ["other_role"], "filename": "f.md", "archived": False},
]
vs.delete_tenant(T)
vs.upsert_chunks(rows)

hits = vs.search_chunks(T, [0.9, 0.1, 0, 0], role="", top_k=5)
assert hits is not None, "pgvector search returned None (hard error) — check the DSN/extension"
ids = [h["id"] for h in hits]
# ch_b has an ACL that excludes an empty role -> only ch_a is visible
assert ids == ["smoke_a"], f"expected only smoke_a (ACL should hide smoke_b), got {ids}"
assert hits[0]["score"] > 0.8, f"cosine score too low: {hits[0]['score']}"

# archive -> disappears from search
vs.set_archived("smoke_doc", True)
assert vs.search_chunks(T, [0.9, 0.1, 0, 0], role="", top_k=5) == [], "archived doc still searchable"

vs.delete_tenant(T)
print("OK — pgvector live round-trip passed:", ids, "score", round(hits[0]["score"], 3))
