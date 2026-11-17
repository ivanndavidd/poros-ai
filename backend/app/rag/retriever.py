"""Retrieval over the per-tenant vector store with role ACL + citations.

Default store = SQLite (chunks scanned + cosine in Python). This is the pluggable
seam: swap _scan_cosine for pgvector/Qdrant later without touching callers.

Two-stage ranking: cosine recall (oversampled) -> optional CROSS-ENCODER rerank
(query+chunk scored together — much sharper than cosine alone). Env knobs:
  RAG_RERANK=0        disable the second stage
  RAG_RERANK_MODEL    cross-encoder model (default: multilingual mmarco)
Chunks stored with a DIFFERENT embedder (dim mismatch after a model switch) are
skipped with a warning — run POST /admin/tenants/{id}/reindex-docs to fix.
"""
import logging, math, os, re
from .. import store
from .. import vectorstore as vs
from .embeddings import embed_query

# extractors embed location markers in the chunk text ([Slide 3] / [Page 2]) — used to
# build HUMAN-readable citations (end users can't do anything with "#chunk54").
_MARKER = re.compile(r"\[(Slide|Page) (\d+)\]")


def _page_label(ch) -> str:
    """'Slide 3' / 'Slide 3–5' / 'Page 2' from the markers in the chunk text;
    fallback 'bagian N' (1-based) for plain files without page structure."""
    ms = _MARKER.findall(ch.get("text") or "")
    if ms:
        kind = ms[0][0]
        nums = sorted({int(n) for _, n in ms})
        return f"{kind} {nums[0]}" if len(nums) == 1 else f"{kind} {nums[0]}–{nums[-1]}"
    return f"bagian {ch['chunk_index'] + 1}"

_RERANK_CACHE = {}
_log = logging.getLogger("rag")


def _reranker():
    if os.getenv("RAG_RERANK", "1") == "0" or os.getenv("ST_EMBED_DISABLE") == "1":
        return None
    name = os.getenv("RAG_RERANK_MODEL", "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1")
    if name not in _RERANK_CACHE:
        try:
            # CPU tuning (measured on the dev box): threads cores-2 + max_length 256
            # + one warm predict (compiles the graph) took 8 pairs from ~5-17s to ~1.2s.
            import torch
            torch.set_num_threads(max(4, (os.cpu_count() or 4) - 2))
            from sentence_transformers import CrossEncoder
            ce = CrossEncoder(name, max_length=int(os.getenv("RAG_RERANK_MAXLEN", "256")))
            ce.predict([("warmup", "warmup")])
            _RERANK_CACHE[name] = ce
        except Exception:
            _RERANK_CACHE[name] = None
    return _RERANK_CACHE[name]


def _cosine(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _allowed(chunk_acl: list, role: str) -> bool:
    """Empty ACL = visible to all roles in the tenant; else role must be listed."""
    return not chunk_acl or role in chunk_acl


def _scan_cosine(tenant_id: str, qv: list, role: str) -> list:
    """SQLite path: scan the tenant's chunks + cosine in Python. Returns [(cos, chunk)]
    sorted best-first. This is the pluggable seam pgvector replaces when enabled."""
    scored, stale = [], 0
    for ch in store.iter_chunks(tenant_id):          # tenant isolation: only this tenant's chunks
        if not _allowed(ch["acl_roles"], role):
            continue
        if len(ch["embedding"] or []) != len(qv):    # stored with a different embedder
            stale += 1
            continue
        scored.append((_cosine(qv, ch["embedding"]), ch))
    if stale:
        _log.warning("tenant %s: %d chunk(s) skipped — embedded with a different model; "
                     "run POST /admin/tenants/%s/reindex-docs", tenant_id, stale, tenant_id)
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


def pool_size(top_k: int) -> int:
    """How many cosine candidates a top-k answer draws from — oversampled for the second
    stage, capped: every extra pair costs the cross-encoder ~150ms CPU (RAG_RERANK_POOL)."""
    cap = max(top_k, int(os.getenv("RAG_RERANK_POOL", "8")))
    return min(max(top_k * 3, top_k), cap)


def _cosine_pool(tenant_id: str, query: str, role: str, size: int) -> list:
    """[(cosine, chunk)] best-first: the first stage only."""
    qv = embed_query(tenant_id, query)
    # pgvector does ACL + archived filtering + cosine recall IN Postgres (ANN index);
    # None = it errored, so fall back to the SQLite scan (admin.db still has every vector).
    if vs.enabled():
        # The scan path filters paused datasources in SQL (store.iter_chunks); the mirror
        # has no such join, so hand it the paused ids read live from the metadata store.
        hits = vs.search_chunks(tenant_id, qv, role, size,
                                exclude_datasources=store.disabled_datasource_ids(tenant_id))
        if hits is not None:
            return [(h["score"], h) for h in hits]
    return _scan_cosine(tenant_id, qv, role)[:size]


def _reranked(query: str, pool: list, top_k: int) -> list:
    rr = _reranker()
    if rr is not None and len(pool) > 1:
        try:
            rs = rr.predict([(query, ch["text"]) for _, ch in pool])
            pool = sorted(((float(r), cos, ch) for r, (cos, ch) in zip(rs, pool)),
                          key=lambda x: x[0], reverse=True)
            return [_hit(ch, cos, rerank=r) for r, cos, ch in pool[:top_k]]
        except Exception as e:                        # rerank is best-effort — never break retrieval
            _log.warning("rerank failed, falling back to cosine order: %s", e)
    return [_hit(ch, cos) for cos, ch in pool[:top_k]]


def retrieve(tenant_id: str, query: str, role: str = "", top_k: int = 5) -> list:
    """Return top-k chunks the role may see, each with a citation + similarity."""
    return _reranked(query, _cosine_pool(tenant_id, query, role, pool_size(top_k)), top_k)


def _hit(ch, cosine_score, rerank=None):
    label = _page_label(ch)
    out = {
        "text": ch["text"],
        "score": round(float(cosine_score), 4),
        "document_id": ch["document_id"],
        "filename": ch["filename"],
        "chunk_index": ch["chunk_index"],
        "citation": f'{ch["filename"]}#chunk{ch["chunk_index"]}',    # machine format (stable)
        "page_label": label,
        "citation_label": f'{ch["filename"]} — {label}',             # what humans should see
    }
    if rerank is not None:
        out["rerank_score"] = round(float(rerank), 4)
    return out
