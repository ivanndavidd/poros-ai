"""Offline tests for per-tenant embedding-model selection + E5 prefixing (embeddings.py).

No model downloads: the sentence-transformers loader is monkeypatched with a fake that
records the exact texts it was asked to encode.
"""
from app.rag import embeddings as emb


class FakeST:
    def __init__(self):
        self.seen = []

    def encode(self, texts, normalize_embeddings=True):
        self.seen.append(list(texts))
        return [[0.1, 0.2, 0.3] for _ in texts]


def _cfg(monkeypatch, model):
    monkeypatch.setattr(emb.store, "get_tenant_config", lambda tid, sec: {"embedding_model": model})


def test_model_name_from_tenant_config(monkeypatch):
    _cfg(monkeypatch, "intfloat/multilingual-e5-base")
    assert emb._st_model_name("t1") == "intfloat/multilingual-e5-base"


def test_stale_offline_value_ignored(monkeypatch):
    # a saved 'offline' string must NOT silently downgrade — falls through to the default
    _cfg(monkeypatch, "offline")
    assert emb._st_model_name("t1") == "paraphrase-multilingual-MiniLM-L12-v2"


def test_blank_config_uses_default(monkeypatch):
    monkeypatch.setattr(emb.store, "get_tenant_config", lambda tid, sec: {})
    assert emb._st_model_name("t1") == "paraphrase-multilingual-MiniLM-L12-v2"


def test_e5_prefixes_applied(monkeypatch):
    fake = FakeST()
    _cfg(monkeypatch, "intfloat/multilingual-e5-base")
    monkeypatch.setattr(emb, "_gateway_embed", lambda tid, texts: None)
    monkeypatch.setattr(emb, "_sentence_transformers", lambda tid=None: fake)
    emb.embed_texts("t1", ["dokumen isi"])       # passage side
    emb.embed_query("t1", "pertanyaan")          # query side
    assert fake.seen[0] == ["passage: dokumen isi"]
    assert fake.seen[1] == ["query: pertanyaan"]


def test_no_prefix_for_non_e5(monkeypatch):
    fake = FakeST()
    _cfg(monkeypatch, "paraphrase-multilingual-MiniLM-L12-v2")
    monkeypatch.setattr(emb, "_gateway_embed", lambda tid, texts: None)
    monkeypatch.setattr(emb, "_sentence_transformers", lambda tid=None: fake)
    emb.embed_texts("t1", ["dokumen isi"])
    emb.embed_query("t1", "pertanyaan")
    assert fake.seen[0] == ["dokumen isi"]
    assert fake.seen[1] == ["pertanyaan"]


def test_embedder_name_reflects_choice(monkeypatch):
    _cfg(monkeypatch, "BAAI/bge-m3")
    monkeypatch.setattr(emb.store, "get_model", lambda tid, purpose: None)      # no gateway embedding
    monkeypatch.setattr(emb, "_sentence_transformers", lambda tid=None: object())  # ST tier active
    assert emb.embedder_name("t1") == "sentence-transformers:BAAI/bge-m3"
