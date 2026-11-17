"""Memory v2: auto-capture (the AI writes its own notes) + conversation memory.

Run:  cd backend && python -m pytest tests/test_memory_v2.py -q

Fully offline — llm.complete/available are monkeypatched and embeddings run on the
deterministic offline tier (conftest sets ST_EMBED_DISABLE=1). These verify the
WIRING: config gating, dedup, TTL, history strategies, and the orchestrator path.
"""
import json
import os, sys
from types import SimpleNamespace

import pytest  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)

from app import store  # noqa: E402
from app.memory import capture, conversation, recall as recall_mod  # noqa: E402
from app.memory.capture import auto_capture, _parse  # noqa: E402
from app.memory.conversation import build_context, MEMORY_DEFAULTS  # noqa: E402
from app.memory.recall import recall  # noqa: E402


def _llm_result(text):
    return SimpleNamespace(text=text, model="fake", provider="fake",
                           prompt_tokens=1, completion_tokens=1, cost=0.0)


def _pr(tid):
    return {"tenant_id": tid, "key": "k_test", "role": "admin"}


# ---------- auto-capture ----------
def test_capture_stores_then_dedups(monkeypatch):
    store.init()
    tid, uid = "t_mv2_cap", "u1"
    payload = json.dumps([{"kind": "fact", "text": "Budi adalah PIC gudang Jakarta"},
                          {"kind": "preference", "text": "Suka jawaban berbentuk tabel ringkas"}])
    monkeypatch.setattr(capture.llm, "available", lambda: True)
    monkeypatch.setattr(capture.llm, "complete", lambda *a, **k: _llm_result(payload))

    stored = auto_capture(_pr(tid), uid, "siapa PIC gudang jakarta?", "Budi.", trace_id="tr1")
    assert [s["kind"] for s in stored] == ["fact", "preference"]
    mems = store.list_memories(tid, uid)
    assert len(mems) == 2 and all(m["source"] == "auto" for m in mems)

    # same extraction again -> every item is a duplicate -> nothing new stored
    again = auto_capture(_pr(tid), uid, "siapa PIC gudang jakarta?", "Budi.")
    assert again == [] and len(store.list_memories(tid, uid)) == 2


def test_capture_gated_by_config_and_user(monkeypatch):
    store.init()
    tid = "t_mv2_gate"
    monkeypatch.setattr(capture.llm, "available", lambda: True)
    monkeypatch.setattr(capture.llm, "complete",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("llm must not be called")))
    store.set_tenant_config(tid, "memory", {"auto_memory": False})
    assert auto_capture(_pr(tid), "u1", "q", "a") == []          # toggle off
    store.set_tenant_config(tid, "memory", {"auto_memory": True, "persistent": False})
    assert auto_capture(_pr(tid), "u1", "q", "a") == []          # store disabled
    store.set_tenant_config(tid, "memory", {"auto_memory": True, "persistent": True})
    assert auto_capture(_pr(tid), "", "q", "a") == []            # no verified user -> never


def test_capture_parse_is_defensive():
    ok = _parse('```json\n[{"kind":"fact","text":"catatan yang cukup panjang"}]\n```')
    assert ok == [{"kind": "fact", "text": "catatan yang cukup panjang"}]
    assert _parse("bukan json") == []
    assert _parse('{"kind":"fact"}') == []                        # not a list
    assert _parse('[{"kind":"weird","text":"catatan valid panjang"}]')[0]["kind"] == "fact"
    assert _parse('[{"kind":"fact","text":"pendek"}]') == []      # too short to be durable


# ---------- TTL: expires AUTO notes only ----------
def test_ttl_filters_auto_memories_only():
    store.init()
    tid, uid, txt = "t_mv2_ttl", "u1", "catatan penting user"
    from app.rag.embeddings import embed_query
    emb = embed_query(tid, txt)
    m_admin = store.add_memory(tid, uid, "fact", txt, emb, source="admin")
    m_auto = store.add_memory(tid, uid, "fact", txt + " (auto)", embed_query(tid, txt + " (auto)"), source="auto")
    # age the auto one far past any TTL
    c = store._c(); c.execute("UPDATE memories SET created='2020-01-01T00:00:00' WHERE id=?", (m_auto,))
    c.commit(); c.close()

    store.set_tenant_config(tid, "memory", {"ttl_hours": 1})
    got = {m["id"] for m in recall(tid, uid, txt, top_k=5)}
    assert m_admin in got and m_auto not in got                   # admin never expires

    store.set_tenant_config(tid, "memory", {"ttl_hours": 0})
    got = {m["id"] for m in recall(tid, uid, txt, top_k=5)}
    assert m_admin in got and m_auto in got                       # 0 = auto never expires either


# ---------- conversation memory ----------
def _hist(n=4):
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"pesan ke-{i}"} for i in range(n)]


def test_conversation_buffer_window_and_cap():
    cfg = {**MEMORY_DEFAULTS, "memory_type": "Buffer window", "context_window": 2}
    out = build_context("t_x", _hist(4), cfg)
    assert "pesan ke-2" in out and "pesan ke-3" in out and "pesan ke-0" not in out
    tiny = build_context("t_x", _hist(4), {**cfg, "max_memory_tokens": 100})
    assert len(tiny) <= 400 and tiny.endswith("pesan ke-3")       # keeps the TAIL (newest turns)


def test_conversation_none_and_junk():
    assert build_context("t_x", _hist(4), {**MEMORY_DEFAULTS, "memory_type": "None"}) == ""
    assert build_context("t_x", [], MEMORY_DEFAULTS) == ""
    junk = [{"role": "system", "content": "x"}, {"nope": 1}, {"role": "user", "content": "  "}]
    assert build_context("t_x", junk, {**MEMORY_DEFAULTS, "memory_type": "Buffer window"}) == ""


def test_conversation_buffer_plus_summary(monkeypatch):
    monkeypatch.setattr(conversation.llm, "available", lambda: True)
    monkeypatch.setattr(conversation.llm, "complete", lambda *a, **k: _llm_result("RINGKASAN LAMA"))
    cfg = {**MEMORY_DEFAULTS, "memory_type": "Buffer + summary", "context_window": 1}
    out = build_context("t_x", _hist(4), cfg)
    assert "RINGKASAN LAMA" in out and "pesan ke-3" in out and "pesan ke-1" not in out


def test_conversation_summary_failure_falls_back(monkeypatch):
    monkeypatch.setattr(conversation.llm, "available", lambda: True)
    monkeypatch.setattr(conversation.llm, "complete",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    cfg = {**MEMORY_DEFAULTS, "memory_type": "Summary only", "context_window": 2}
    out = build_context("t_x", _hist(4), cfg)
    assert "pesan ke-3" in out                                    # window fallback, never breaks chat


# ---------- orchestrator wiring (history -> prompt, auto-capture scheduled) ----------
def test_run_agent_history_and_sync_capture(monkeypatch):
    store.init()
    tid, uid = "t_mv2_run", "u9"
    from app.agent import orchestrator
    from app.skills import registry

    gate = SimpleNamespace(allow=True, intent="doc_question", complexity="simple",
                           injection=False, pii_request=False, reasons=[], engine="fake",
                           prompt_tokens=0, completion_tokens=0, cost=0.0)
    monkeypatch.setattr(orchestrator.llm, "screen", lambda *_a, **_k: gate)
    monkeypatch.setattr(orchestrator.llm, "pick_answer_purpose", lambda t, c: "answer_simple")
    # NOTE: registry.llm / capture.llm / orchestrator.llm are the SAME module object —
    # one dispatcher must serve both the synthesis call and the capture-extraction call.
    prompts = []
    payload = json.dumps([{"kind": "context", "text": "User menangani area gudang Jakarta"}])

    def fake_complete(t, p, messages, **kw):
        sys0 = messages[0]["content"] if messages and messages[0].get("role") == "system" else ""
        if "long-term memory notes" in sys0:          # capture._SYS marker
            return _llm_result(payload)
        prompts.append(messages)
        return _llm_result("Jawaban singkat tanpa angka.")
    monkeypatch.setattr(registry.llm, "complete", fake_complete)
    monkeypatch.setattr(registry.llm, "available", lambda: True)
    monkeypatch.setenv("MEMORY_CAPTURE_SYNC", "1")

    history = [{"role": "user", "content": "total tiket gudang Jakarta bulan ini?"},
               {"role": "assistant", "content": "Ada 40 tiket."}]
    out = orchestrator.run_agent(_pr(tid), "kalau bulan lalu bagaimana?", user_id=uid, history=history)

    assert "conversation_history" in out["plan"] and "auto_memory" in out["plan"]
    # the earlier turns are chat messages of their own before the question (overhaul item 8)
    assert any([(m["role"], m["content"]) for m in msgs[-3:-1]]
               == [("user", "total tiket gudang Jakarta bulan ini?"), ("assistant", "Ada 40 tiket.")]
               and msgs[-1]["content"].startswith("Question: kalau bulan lalu") for msgs in prompts)
    assert out["captured_memory"] and out["captured_memory"][0]["kind"] == "context"
    mems = store.list_memories(tid, uid)
    assert len(mems) == 1 and mems[0]["source"] == "auto"


# ---------- admin bulk delete ----------
def test_bulk_delete_scopes_to_user():
    store.init()
    tid = "t_mv2_del"
    from app.rag.embeddings import embed_query
    store.add_memory(tid, "u1", "fact", "milik u1 a", embed_query(tid, "a"))
    store.add_memory(tid, "u1", "fact", "milik u1 b", embed_query(tid, "b"))
    store.add_memory(tid, "u2", "fact", "milik u2", embed_query(tid, "c"))
    assert store.delete_memories(tid, "u1") == 2
    left = store.list_memories(tid)
    assert len(left) == 1 and left[0]["user_id"] == "u2"
