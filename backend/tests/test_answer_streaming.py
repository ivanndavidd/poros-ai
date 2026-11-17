"""Offline tests for live answer streaming (answer_delta while the model generates).

Covers the three layers:
- gateway: stream=True chunk loop forwards content deltas + fallback emits on_restart
- registry._synthesize: deltas become answer_delta events; template fallback resets
- research_agent: per-round narration is emitted as reasoning_delta
"""
from types import SimpleNamespace

import pytest

from app.llm import gateway
from app.skills import registry
from app.skills.registry import Ctx


def _ctx(**kw):
    base = dict(tenant_id="t_x", role="Analyst", role_policy={"full_access": True},
                question="berapa total penjualan?", answer_purpose="answer_complex")
    base.update(kw)
    return Ctx(**base)


# ---- gateway streaming -------------------------------------------------------

class _StreamLite:
    """Fake litellm: completion(stream=True) yields content chunks."""

    def __init__(self, pieces=("Hal", "o ", "dunia")):
        self.pieces = list(pieces)
        self.kwargs = None

    def completion(self, **kwargs):
        self.kwargs = kwargs
        assert kwargs.get("stream") is True

        def gen():
            for piece in self.pieces:
                yield {"choices": [{"delta": {"content": piece}}]}
            yield {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 3}}
        return gen()

    def stream_chunk_builder(self, chunks, messages=None):
        text = "".join(
            (c.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
            for c in chunks
        )
        return {"choices": [{"message": {"content": text}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3}}

    def completion_cost(self, completion_response=None, **_kw):
        return 0.002


def test_gateway_streams_deltas_live(monkeypatch):
    lite = _StreamLite()
    monkeypatch.setattr(gateway, "_litellm", lambda: lite)
    monkeypatch.setattr(gateway, "_resolve_attempts",
                        lambda *_a, **_k: [("openai", "m1", "key", "", {})])
    monkeypatch.setattr(gateway, "_standalone_budget_reservation", lambda _t: None)

    deltas = []
    res = gateway.complete("t_x", "answer_complex",
                           [{"role": "user", "content": "q"}], on_delta=deltas.append)
    assert deltas == ["Hal", "o ", "dunia"]
    assert res.text == "Halo dunia"
    assert res.prompt_tokens == 5 and res.completion_tokens == 3
    assert res.cost == pytest.approx(0.002)


def test_gateway_without_on_delta_stays_non_streaming(monkeypatch):
    calls = {}

    class _Plain:
        def completion(self, **kwargs):
            calls.update(kwargs)
            return {"choices": [{"message": {"content": "jawab"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

        def completion_cost(self, completion_response=None, **_kw):
            return 0.0

    monkeypatch.setattr(gateway, "_litellm", lambda: _Plain())
    monkeypatch.setattr(gateway, "_resolve_attempts",
                        lambda *_a, **_k: [("openai", "m1", "key", "", {})])
    monkeypatch.setattr(gateway, "_standalone_budget_reservation", lambda _t: None)

    res = gateway.complete("t_x", "answer_complex", [{"role": "user", "content": "q"}])
    assert res.text == "jawab"
    assert "stream" not in calls


def test_gateway_fallback_after_partial_stream_calls_on_restart(monkeypatch):
    class _Flaky(_StreamLite):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def completion(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                def bad():
                    yield {"choices": [{"delta": {"content": "draf lama "}}]}
                    raise RuntimeError("provider mati")
                return bad()
            return super().completion(**kwargs)

    monkeypatch.setattr(gateway, "_litellm", lambda: _Flaky())
    monkeypatch.setattr(gateway, "_resolve_attempts", lambda *_a, **_k: [
        ("openai", "m-primary", "key", "", {}),
        ("openai", "m-fallback", "key", "", {}),
    ])
    monkeypatch.setattr(gateway, "_standalone_budget_reservation", lambda _t: None)

    deltas, resets = [], []
    res = gateway.complete("t_x", "answer_complex", [{"role": "user", "content": "q"}],
                           on_delta=deltas.append,
                           on_restart=lambda: resets.append(True))
    assert resets == [True]                       # partial first attempt was cleared
    assert res.text == "Halo dunia"
    assert deltas == ["draf lama ", "Hal", "o ", "dunia"]


# ---- synthesis -> answer_delta events ---------------------------------------

def _fake_llm_result(text):
    return SimpleNamespace(text=text, model="fake-model", provider="fake",
                           prompt_tokens=3, completion_tokens=2, cost=0.001)


def test_synthesis_streams_answer_delta_events(monkeypatch):
    def fake_complete(_tid, _purpose, _messages, on_delta=None, on_restart=None, **_kw):
        for piece in ("Jawaban ", "mengalir."):
            if on_delta:
                on_delta(piece)
        return _fake_llm_result("Jawaban mengalir.")

    monkeypatch.setattr(registry.llm, "complete", fake_complete)
    events = []
    out = registry.skill_generate_report(_ctx(), {}, emit=events.append)
    assert out["streamed"] is True
    assert out["text"] == "Jawaban mengalir."
    stream = [e for e in events if e["type"] == "answer_delta"]
    assert "".join(e["text"] for e in stream) == "Jawaban mengalir."


def test_synthesis_without_emit_does_not_stream(monkeypatch):
    seen = {}

    def fake_complete(_tid, _purpose, _messages, on_delta=None, **_kw):
        seen["on_delta"] = on_delta
        return _fake_llm_result("jawaban")

    monkeypatch.setattr(registry.llm, "complete", fake_complete)
    out = registry.skill_generate_report(_ctx(), {})
    assert out["streamed"] is False
    assert seen["on_delta"] is None


def test_template_fallback_resets_partial_stream(monkeypatch):
    def fake_complete(_tid, _purpose, _messages, on_delta=None, on_restart=None, **_kw):
        if on_delta:
            on_delta("separuh jawaban yang tidak akan selesai")
        raise RuntimeError("model down")

    monkeypatch.setattr(registry.llm, "complete", fake_complete)
    events = []
    out = registry.skill_generate_report(_ctx(), {}, emit=events.append)
    assert out["streamed"] is False               # the shown text was withdrawn
    assert any(e["type"] == "answer_reset" for e in events)
    assert out["text"]                            # template answer still produced


def test_emulated_tool_note_surfaces_as_turn_text():
    """JSON-adapter models can narrate progress via "note" alongside tool_calls."""
    res = gateway.LLMResult(
        text='{"note":"Saya cek tren revenue dulu.",'
             '"tool_calls":[{"name":"run_sql","arguments":{"sql":"SELECT 1"}}]}',
        model="m", provider="p",
    )
    tr = gateway._emulated_tool_result(res)
    assert tr.text == "Saya cek tren revenue dulu."
    assert tr.tool_calls and tr.tool_calls[0]["name"] == "run_sql"


def test_emulated_final_still_wins_over_note():
    res = gateway.LLMResult(
        text='{"note":"catatan","final":"jawaban akhir"}', model="m", provider="p",
    )
    tr = gateway._emulated_tool_result(res)
    assert tr.text == "jawaban akhir"
    assert tr.tool_calls == []
