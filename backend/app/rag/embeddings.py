"""Embeddings with graceful tiers:

  1. tenant 'embedding' model via LiteLLM gateway (e.g. OpenAI text-embedding-3) — if configured
  2. sentence-transformers (local neural) — if installed
  3. built-in offline hashing embedder — always available, zero deps

The offline embedder uses feature-hashed word + char-trigram counts, L2-normalised.
It's crude but deterministic and good enough for demo retrieval without a network.

Env knobs:
  ST_EMBED_MODEL   sentence-transformers model (default: multilingual — Indonesian
                   questions over English docs need cross-language embeddings)
  ST_EMBED_DISABLE =1 forces the offline tier (tests: deterministic, no downloads)
Switching embedders makes OLD stored vectors incompatible (different space/dim) —
run POST /admin/tenants/{id}/reindex-docs afterwards (rag.ingest.reindex_tenant).
"""
import hashlib, math, os, re
from .. import store
from ..network_policy import validate_api_base

_DIM = 256
_WORD = re.compile(r"[a-z0-9]+")


def _hash_idx(token: str, dim: int) -> int:
    return int.from_bytes(hashlib.md5(token.encode()).digest()[:4], "little") % dim


def _offline_embed(text: str, dim: int = _DIM) -> list:
    vec = [0.0] * dim
    words = _WORD.findall((text or "").lower())
    for w in words:
        vec[_hash_idx("w:" + w, dim)] += 1.0
        for i in range(len(w) - 2):                # char trigrams for short-text robustness
            vec[_hash_idx("t:" + w[i:i + 3], dim)] += 0.5
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


_ST_CACHE = {}


_DEFAULT_ST = "paraphrase-multilingual-MiniLM-L12-v2"


def _st_model_name(tenant_id: str = None) -> str:
    """Resolve the sentence-transformers model: per-tenant Document-config choice wins,
    then env ST_EMBED_MODEL, then the multilingual default. A stale/blank/'offline' config
    value is IGNORED (falls through) so it can never silently downgrade a tenant."""
    if tenant_id:
        try:
            m = (store.get_tenant_config(tenant_id, "document") or {}).get("embedding_model")
            m = str(m or "").strip()
            if m and m.lower() not in ("offline", "default", "auto"):
                return m
        except Exception:
            pass
    return os.getenv("ST_EMBED_MODEL", _DEFAULT_ST)


def _needs_e5_prefix(name: str) -> bool:
    """E5 models expect 'query: ' / 'passage: ' prefixes for good retrieval quality."""
    return "e5" in (name or "").lower()


def _sentence_transformers(tenant_id: str = None):
    if os.getenv("ST_EMBED_DISABLE") == "1":
        return None
    name = _st_model_name(tenant_id)
    if name not in _ST_CACHE:
        try:
            from sentence_transformers import SentenceTransformer
            _ST_CACHE[name] = SentenceTransformer(name)
        except Exception:
            _ST_CACHE[name] = None
    return _ST_CACHE[name]


def _gateway_embed(tenant_id, texts):
    cfg = store.get_model(tenant_id, "embedding")
    if not cfg or not cfg.get("enabled") or not cfg.get("model"):
        return None
    try:
        import litellm
        prov = store.get_provider(tenant_id, cfg["provider"]) or {}
        api_base = validate_api_base(
            prov.get("api_base") or "",
            purpose=f"{cfg['provider']} embedding api_base",
        )
        resp = litellm.embedding(model=cfg["model"], input=texts,
                                 api_key=prov.get("api_key") or None,
                                 api_base=api_base or None)
        return [d["embedding"] for d in resp["data"]]
    except Exception:
        return None


def embedder_name(tenant_id: str) -> str:
    cfg = store.get_model(tenant_id, "embedding")
    if cfg and cfg.get("enabled") and cfg.get("model"):
        return f"gateway:{cfg['model']}"
    if _sentence_transformers(tenant_id) is not None:
        return f"sentence-transformers:{_st_model_name(tenant_id)}"
    return f"offline-hash-{_DIM}"


def embed_texts(tenant_id: str, texts: list, is_query: bool = False) -> list:
    if not texts:
        return []
    g = _gateway_embed(tenant_id, texts)
    if g is not None:
        return g
    st = _sentence_transformers(tenant_id)
    if st is not None:
        src = texts
        if _needs_e5_prefix(_st_model_name(tenant_id)):        # e5 needs query:/passage: prefixes
            pre = "query: " if is_query else "passage: "
            src = [pre + (t or "") for t in texts]
        return [list(map(float, v)) for v in st.encode(src, normalize_embeddings=True)]
    return [_offline_embed(t) for t in texts]


def embed_query(tenant_id: str, query: str) -> list:
    return embed_texts(tenant_id, [query], is_query=True)[0]
