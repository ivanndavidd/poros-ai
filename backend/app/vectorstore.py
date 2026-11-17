"""Pluggable vector store for doc-RAG retrieval — SQLite (default) | pgvector.

Selected by env VECTOR_BACKEND, mirroring the SECRETS_BACKEND seam. In the default
`sqlite` mode this module is inert: the retriever keeps scanning admin.db + doing
cosine in Python (unchanged, zero services — what dev and the test-suite use).

In `pgvector` mode the SQLite admin.db STAYS the source of truth for chunk
text/ACL/metadata; Postgres holds a searchable MIRROR of the vectors, written
through on ingest / reindex / archive / delete and rebuildable from SQLite at any
time (rebuild()). Similarity search then runs IN Postgres over an HNSW index
(cosine `<=>`), which is what lets doc-RAG scale past the linear SQLite scan.

Design notes:
- Mirror is DERIVED, never authoritative. Every write is best-effort: a Postgres
  hiccup logs + drifts the mirror but never breaks ingest; rebuild() re-syncs.
- search_chunks() returns None on a hard Postgres error (NOT []), so the retriever
  can fall back to the SQLite scan (admin.db still has every vector). [] means
  "searched fine, nothing matched".
- One fixed embedding dimension per Postgres column (VECTOR_PG_DIM, default 384 =
  the default multilingual sentence-transformers model). Vectors of another length
  are skipped on write + search (mirrors the SQLite dim-mismatch guard); after an
  embedder switch, change the dim and rebuild().

Env:
  VECTOR_BACKEND   sqlite (default) | pgvector
  VECTOR_PG_DSN    libpq DSN supplied through local env / secret manager
  VECTOR_PG_DIM    embedding dimension of the vector column (default 384)
  VECTOR_PG_TABLE  table name (default doc_chunks_vec)

No hard dependency on the `pgvector` python package — vectors are sent as text
literals cast with `::vector`, which the extension accepts. psycopg2 is already a
requirement (Postgres connector).
"""
import json, logging, os, threading

_log = logging.getLogger("vectorstore")
_lock = threading.Lock()
_ensured = False


def backend() -> str:
    return (os.getenv("VECTOR_BACKEND") or "sqlite").strip().lower()


def enabled() -> bool:
    """True when a real vector DB backs retrieval (currently: pgvector)."""
    return backend() == "pgvector"


def _dim() -> int:
    try:
        return int(os.getenv("VECTOR_PG_DIM", "384"))
    except ValueError:
        return 384


def _table() -> str:
    # constrained to identifier chars — it is interpolated into DDL/DML
    t = (os.getenv("VECTOR_PG_TABLE") or "doc_chunks_vec").strip()
    return t if t.replace("_", "").isalnum() else "doc_chunks_vec"


def _vec_literal(v) -> str:
    """[0.1, 0.2] -> '[0.1,0.2]' — pgvector accepts this cast with ::vector."""
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


# ---- Postgres plumbing (opt-in; imported lazily so sqlite mode needs nothing) ----
def _conn():
    import psycopg2  # already a requirement (Postgres data-source connector)
    dsn = os.getenv("VECTOR_PG_DSN") or ""
    return psycopg2.connect(dsn) if dsn else psycopg2.connect()   # fall back to PG* env


def _ensure_schema(conn):
    """Create extension + table + HNSW cosine index once per process."""
    global _ensured
    if _ensured:
        return
    with _lock:
        if _ensured:
            return
        tbl, dim = _table(), _dim()
        cur = conn.cursor()
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        cur.execute(
            f"CREATE TABLE IF NOT EXISTS {tbl} ("
            "  id text PRIMARY KEY,"
            "  tenant_id text NOT NULL,"
            "  document_id text NOT NULL,"
            "  datasource_id text,"
            "  chunk_index int,"
            "  text text,"
            "  acl_roles jsonb NOT NULL DEFAULT '[]'::jsonb,"
            "  filename text,"
            "  archived boolean NOT NULL DEFAULT false,"
            f"  embedding vector({dim})"
            ")")
        # filtered-search support + fast ANN recall
        cur.execute(f"CREATE INDEX IF NOT EXISTS ix_{tbl}_tenant ON {tbl}(tenant_id, archived)")
        cur.execute(f"CREATE INDEX IF NOT EXISTS ix_{tbl}_doc ON {tbl}(document_id)")
        cur.execute(f"CREATE INDEX IF NOT EXISTS ix_{tbl}_hnsw ON {tbl} "
                    f"USING hnsw (embedding vector_cosine_ops)")
        conn.commit(); cur.close()
        _ensured = True


# ---- search (called by the retriever when enabled()) ----
def search_chunks(tenant_id: str, query_vec: list, role: str, limit: int,
                  exclude_datasources: list = None):
    """Top-`limit` non-archived chunks the role may see, ranked by cosine similarity,
    computed IN Postgres over the HNSW index. Returns a list of hit dicts, [] for a
    clean no-match, or None on a hard error (retriever then falls back to SQLite).

    `exclude_datasources` carries the tenant's PAUSED datasource ids. The caller reads them
    live from the metadata store rather than this mirror keeping its own flag — mirror
    writes here are deliberately best-effort, and a switched-off source must not stay
    searchable because one write was dropped.
    """
    if not enabled():
        return None
    if len(query_vec or []) != _dim():
        _log.warning("query embedding dim %d != VECTOR_PG_DIM %d — skipping pgvector search",
                     len(query_vec or []), _dim())
        return None
    tbl = _table()
    qv = _vec_literal(query_vec)
    try:
        conn = _conn()
        _ensure_schema(conn)
        cur = conn.cursor()
        # ACL: empty [] = visible to all roles in the tenant; else role must be an element.
        paused = [str(x) for x in (exclude_datasources or [])]
        cur.execute(
            f"SELECT id, document_id, chunk_index, text, acl_roles, filename, "
            f"       1 - (embedding <=> %s::vector) AS score "
            f"FROM {tbl} "
            f"WHERE tenant_id = %s AND archived = false "
            f"  AND (jsonb_array_length(COALESCE(acl_roles,'[]'::jsonb)) = 0 "
            f"       OR COALESCE(acl_roles,'[]'::jsonb) ? %s) "
            f"  AND (%s::boolean OR datasource_id IS NULL OR NOT (datasource_id = ANY(%s))) "
            f"ORDER BY embedding <=> %s::vector "
            f"LIMIT %s",
            (qv, tenant_id, role or "", not paused, paused, qv, int(limit)))
        rows = cur.fetchall(); cur.close(); conn.close()
        out = []
        for r in rows:
            acl = r[4] if isinstance(r[4], list) else json.loads(r[4] or "[]")
            out.append({"id": r[0], "document_id": r[1], "chunk_index": r[2], "text": r[3],
                        "acl_roles": acl, "filename": r[5], "score": float(r[6])})
        return out
    except Exception as e:
        _log.warning("pgvector search failed (%s) — retriever will fall back to SQLite", e)
        return None


# ---- write-through (called by store.py; no-op unless enabled()) ----
def upsert_chunks(rows: list):
    """rows: dicts {id, tenant_id, document_id, datasource_id, chunk_index, text,
    embedding, acl_roles, filename, archived}. Dim-mismatched vectors are skipped."""
    if not enabled() or not rows:
        return
    tbl, dim = _table(), _dim()
    try:
        conn = _conn(); _ensure_schema(conn); cur = conn.cursor()
        kept = 0
        for r in rows:
            emb = r.get("embedding") or []
            if len(emb) != dim:
                continue
            cur.execute(
                f"INSERT INTO {tbl}(id,tenant_id,document_id,datasource_id,chunk_index,text,"
                f"acl_roles,filename,archived,embedding) "
                f"VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s::vector) "
                f"ON CONFLICT (id) DO UPDATE SET "
                f"text=EXCLUDED.text, acl_roles=EXCLUDED.acl_roles, filename=EXCLUDED.filename, "
                f"archived=EXCLUDED.archived, embedding=EXCLUDED.embedding",
                (r["id"], r["tenant_id"], r["document_id"], r.get("datasource_id"),
                 r.get("chunk_index"), r.get("text"), json.dumps(r.get("acl_roles") or []),
                 r.get("filename"), bool(r.get("archived")), _vec_literal(emb)))
            kept += 1
        conn.commit(); cur.close(); conn.close()
        skipped = len(rows) - kept
        if skipped:
            _log.warning("pgvector upsert skipped %d/%d chunk(s) with dim != %d (rebuild after "
                         "an embedder/dim change)", skipped, len(rows), dim)
    except Exception as e:
        _log.warning("pgvector upsert failed (%s) — mirror will drift until rebuild()", e)


def update_embeddings(pairs: list):
    """pairs: (chunk_id, embedding). UPDATE only — used by reindex; rows must already
    exist in the mirror (full population goes through upsert_chunks / rebuild)."""
    if not enabled() or not pairs:
        return
    tbl, dim = _table(), _dim()
    try:
        conn = _conn(); _ensure_schema(conn); cur = conn.cursor()
        for cid, emb in pairs:
            if len(emb or []) != dim:
                continue
            cur.execute(f"UPDATE {tbl} SET embedding=%s::vector WHERE id=%s",
                        (_vec_literal(emb), cid))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        _log.warning("pgvector update_embeddings failed (%s) — rebuild() to re-sync", e)


def set_archived(document_id: str, archived: bool):
    if not enabled():
        return
    tbl = _table()
    try:
        conn = _conn(); _ensure_schema(conn); cur = conn.cursor()
        cur.execute(f"UPDATE {tbl} SET archived=%s WHERE document_id=%s", (bool(archived), document_id))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        _log.warning("pgvector set_archived failed (%s)", e)


def delete_documents(doc_ids: list):
    if not enabled() or not doc_ids:
        return
    tbl = _table()
    try:
        conn = _conn(); _ensure_schema(conn); cur = conn.cursor()
        cur.execute(f"DELETE FROM {tbl} WHERE document_id = ANY(%s)", (list(doc_ids),))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        _log.warning("pgvector delete_documents failed (%s)", e)


def delete_tenant(tenant_id: str):
    if not enabled():
        return
    tbl = _table()
    try:
        conn = _conn(); _ensure_schema(conn); cur = conn.cursor()
        cur.execute(f"DELETE FROM {tbl} WHERE tenant_id=%s", (tenant_id,))
        conn.commit(); cur.close(); conn.close()
    except Exception as e:
        _log.warning("pgvector delete_tenant failed (%s)", e)


# ---- (re)build the mirror from the SQLite source of truth ----
def rebuild(tenant_id: str = None) -> dict:
    """Repopulate the Postgres mirror from admin.db — for the initial switch to
    pgvector, or to recover after drift. Deletes the tenant's mirror rows then
    re-inserts every chunk (including archived, so the archived flag round-trips)."""
    if not enabled():
        return {"backend": backend(), "rebuilt": 0, "note": "VECTOR_BACKEND != pgvector — nothing to do"}
    from . import store   # lazy: avoids import cycle (store imports this module)
    tenants = [tenant_id] if tenant_id else [t["id"] for t in store.list_tenants()]
    total = 0
    for tid in tenants:
        delete_tenant(tid)
        chunks = store.iter_chunks_full(tid)
        upsert_chunks(chunks)
        total += sum(1 for c in chunks if len(c.get("embedding") or []) == _dim())
    return {"backend": "pgvector", "table": _table(), "dim": _dim(),
            "tenants": len(tenants), "rebuilt_chunks": total}
