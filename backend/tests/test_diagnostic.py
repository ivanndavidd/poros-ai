"""Diagnostic flow (Level 3): WHY-intent recognition, guarded hypothesis mode, cross-source
reconciliation, and the agent's search_docs tool. (The up-front plan decomposition is gone
since chat overhaul item 3: the one chat agent decides its own queries.)

Run:  cd backend && python -m pytest tests/test_diagnostic.py -q
Fully offline — llm / rag / sql_agent are monkeypatched; these verify WIRING and
guardrails (fallbacks, caps, injection conditions), not model quality.
"""
import json
import os, sys
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)

from app.llm import gatekeeper                        # noqa: E402
from app.agent import orchestrator, sql_agent           # noqa: E402
from app.skills import registry                       # noqa: E402
from app.skills.registry import Ctx                   # noqa: E402


def _ctx(q="kenapa penjualan turun bulan ini?", diagnostic=False):
    return Ctx(tenant_id="t_x", role="Admin", role_policy={}, question=q,
               answer_purpose="answer_simple", diagnostic=diagnostic)


def _llm_result(text):
    return SimpleNamespace(text=text, model="fake", provider="fake",
                           prompt_tokens=1, completion_tokens=1, cost=0.0)


# ---- gatekeeper: diagnostic intent ----
def test_heuristic_flags_why_questions_as_diagnostic():
    for q in ("kenapa penjualan turun bulan ini?", "mengapa conversion drop?",
              "why did revenue decrease?", "apa penyebab GMV turun?"):
        g = gatekeeper._heuristic(q)
        assert g.intent == "diagnostic" and g.complexity == "complex", q
    # plain lookups stay data_query
    assert gatekeeper._heuristic("berapa total gmv shopee bulan ini").intent == "data_query"
    # doc questions keep doc_question even with a why-word
    assert gatekeeper._heuristic("kenapa SOP retur mewajibkan approval?").intent == "doc_question"


def test_screen_keeps_heuristic_diagnostic_when_model_says_data_query(monkeypatch):
    verdict = json.dumps({"allow": True, "intent": "data_query", "complexity": "complex",
                          "injection": False, "pii_request": False, "reasons": []})
    monkeypatch.setattr(gatekeeper.gateway, "complete", lambda *a, **k: _llm_result(verdict))
    g = gatekeeper.screen("t_x", "kenapa penjualan turun bulan ini?")
    assert g.allow and g.intent == "diagnostic"


def test_merge_docs_dedupes_by_citation():
    ev = {"docs": {"ok": True, "results": [{"citation": "a.md#chunk0", "text": "x"}],
                   "citations": ["a.md#chunk0"]}}
    orchestrator._merge_docs(ev, {"results": [{"citation": "a.md#chunk0", "text": "x"},
                                              {"citation": "b.md#chunk1", "text": "y"}]})
    assert [h["citation"] for h in ev["docs"]["results"]] == ["a.md#chunk0", "b.md#chunk1"]


# ---- hypothesis + cross-source injection in synthesis ----
def _capture_sys(monkeypatch):
    captured = {}

    def fake_complete(tid, purpose, messages, **kw):
        captured["sys"] = messages[0]["content"]
        return _llm_result("jawaban")

    monkeypatch.setattr(registry.llm, "complete", fake_complete)
    return captured


def test_hypothesis_block_injected_only_for_diagnostic(monkeypatch):
    ev = {"sql": {"ok": True, "columns": ["a"], "rows": [[1]], "safe_sql": "S"}}
    cap = _capture_sys(monkeypatch)
    registry._synthesize(_ctx(diagnostic=True), ev)
    assert "DIAGNOSTIC MODE" in cap["sys"]                    # hypothesis rules present
    registry._synthesize(_ctx(diagnostic=False), ev)
    assert "DIAGNOSTIC MODE" not in cap["sys"]                # hypothesis rules only for why


def test_cross_source_injected_only_when_sql_and_docs(monkeypatch):
    cap = _capture_sys(monkeypatch)
    both = {"sql": {"ok": True, "columns": ["a"], "rows": [[1]], "safe_sql": "S"},
            "docs": {"results": [{"citation": "t.md#chunk0", "citation_label": "t.md — bagian 1",
                                  "text": "target 100", "filename": "t.md"}], "citations": ["t.md#chunk0"]}}
    registry._synthesize(_ctx(), both)
    assert "CROSS-SOURCE" in cap["sys"]
    registry._synthesize(_ctx(), {"sql": both["sql"]})        # SQL only -> no cross-source rule
    assert "CROSS-SOURCE" not in cap["sys"]


# ---- sql_agent: search_docs tool in the loop ----
def test_agent_loop_search_docs_tool(monkeypatch):
    script = [
        SimpleNamespace(text="", model="fake", provider="fake", prompt_tokens=1, completion_tokens=1,
                        cost=0.0, tool_calls=[{"id": "c1", "name": "search_docs",
                                               "arguments": {"query": "target penjualan"}}]),
        SimpleNamespace(text="Target 100 [t.pptx — Slide 2].", model="fake", provider="fake",
                        prompt_tokens=1, completion_tokens=1, cost=0.0, tool_calls=[]),
    ]
    seen_msgs = {}

    def fake_tools(tid, purpose, messages, tools, **kw):
        seen_msgs["last"] = messages
        return script.pop(0)

    hit = {"citation": "t.pptx#chunk3", "citation_label": "t.pptx — Slide 2",
           "text": "target penjualan 100", "filename": "t.pptx", "chunk_index": 3,
           "document_id": "d", "score": 0.9}
    monkeypatch.setattr(sql_agent.llm, "complete_tools", fake_tools)
    monkeypatch.setattr("app.rag.retrieve", lambda *a, **k: [hit])
    out = sql_agent.run_sql_agent(_ctx(), {"mdl": {"tables": {}}}, enable_docs=True)
    assert out["docs"]["citations"] == ["t.pptx#chunk3"]      # pulled doc surfaces as evidence
    tool_msg = [m for m in seen_msgs["last"] if m.get("role") == "tool"][0]
    assert "t.pptx — Slide 2" in tool_msg["content"]          # label fed back for citation
    assert out["answer"].startswith("Target 100")


def test_agent_docs_tool_absent_when_disabled(monkeypatch):
    seen = {}

    def fake_tools(tid, purpose, messages, tools, **kw):
        seen["tools"] = tools
        return SimpleNamespace(text="done", model="fake", provider="fake",
                               prompt_tokens=1, completion_tokens=1, cost=0.0, tool_calls=[])

    monkeypatch.setattr(sql_agent.llm, "complete_tools", fake_tools)
    sql_agent.run_sql_agent(_ctx(), {"mdl": {"tables": {}}}, enable_docs=False)
    assert all(t["function"]["name"] != "search_docs" for t in seen["tools"])


def test_hypothesis_guidance_follows_the_diagnostic_flag(monkeypatch):
    seen = {}

    def fake_tools(tid, purpose, messages, tools, **kw):
        seen["messages"] = messages
        return SimpleNamespace(text="ok", model="fake", provider="fake",
                               prompt_tokens=1, completion_tokens=1, cost=0.0, tool_calls=[])

    monkeypatch.setattr(sql_agent.llm, "complete_tools", fake_tools)
    sql_agent.run_sql_agent(_ctx(diagnostic=True), {"mdl": {"tables": {}}})
    assert "DIAGNOSTIC MODE" in seen["messages"][0]["content"]
    sql_agent.run_sql_agent(_ctx(diagnostic=False), {"mdl": {"tables": {}}})
    assert "DIAGNOSTIC MODE" not in seen["messages"][0]["content"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
