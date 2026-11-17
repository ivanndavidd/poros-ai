"""Memory store + recall, namespaced by (tenant_id, user_id)."""
import math
from .. import store
from ..rag.embeddings import embed_query

MEMORY_KINDS = ("fact", "preference", "context")


def _cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def remember(tenant_id: str, user_id: str, text: str, kind: str = "fact") -> str:
    if kind not in MEMORY_KINDS:
        kind = "fact"
    emb = embed_query(tenant_id, text)
    return store.add_memory(tenant_id, user_id, kind, text, emb)


def recall(tenant_id: str, user_id: str, query: str, top_k: int = 5) -> list:
    """Return the user's most relevant remembered items (this tenant+user only).
    ttl_hours (Memory config) expires AUTO-captured notes only — admin-written
    memories never expire (0/blank = auto notes never expire either)."""
    if not user_id:
        return []
    mems = store.list_memories(tenant_id, user_id)      # (tenant,user) scoped — never crosses tenants
    ttl = 0
    try:
        from .conversation import get_cfg
        ttl = int(get_cfg(tenant_id).get("ttl_hours") or 0)
    except Exception:
        pass
    if ttl > 0:
        cutoff = store.ts_after(-ttl * 3600)
        mems = [m for m in mems if m.get("source") != "auto" or (m.get("created") or "") >= cutoff]
    if not mems:
        return []
    qv = embed_query(tenant_id, query)
    scored = sorted(((_cosine(qv, m["embedding"]), m) for m in mems), key=lambda x: x[0], reverse=True)
    out = []
    for score, m in scored[:top_k]:
        if score <= 0.01:
            continue
        out.append({"id": m["id"], "text": m["text"], "kind": m["kind"], "source": m.get("source", "admin"),
                    "score": round(float(score), 4), "created": m["created"]})
    return out
