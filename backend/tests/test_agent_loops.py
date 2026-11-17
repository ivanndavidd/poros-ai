"""Loop engineering: agentic RAG query-rewrite + the SQL agent's retries. The
verifier only reports since chat overhaul item 6 (no repair loop).

Run:  cd backend && python -m pytest tests/test_agent_loops.py -q

Fully offline — llm.complete / rag.retrieve are monkeypatched, so these verify the
LOOP WIRING (feedback signal, termination, keep-only-if-better) not model quality.
"""
import os, sys
from types import SimpleNamespace

import pytest  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)

from app.skills import registry                     # noqa: E402
from app.skills.registry import Ctx, skill_search_docs  # noqa: E402
from app.agent import orchestrator, sql_agent       # noqa: E402


def _ctx(q="struktur tim store management"):
    return Ctx(tenant_id="t_x", role="Admin", role_policy={}, question=q, answer_purpose="answer_simple")


def _llm_result(text):
    return SimpleNamespace(text=text, model="fake", provider="fake",
                           prompt_tokens=1, completion_tokens=1, cost=0.0)


def _hit(citation, rerank=None, text="isi"):
    h = {"text": text, "score": 0.5, "document_id": "d", "filename": citation.split("#")[0],
         "chunk_index": int(citation.split("chunk")[1]), "citation": citation}
    if rerank is not None:
        h["rerank_score"] = rerank
    return h


# ---- agentic RAG: query rewrite ----
def test_search_docs_rewrites_weak_query(monkeypatch):
    calls = []

    def fake_retrieve(tid, q, role, top_k=4):
        calls.append(q)
        return [_hit("a.pptx#chunk0", rerank=-3.0)] if len(calls) == 1 \
            else [_hit("a.pptx#chunk5", rerank=2.1)]

    monkeypatch.setattr(registry.rag, "retrieve", fake_retrieve)
    monkeypatch.setattr(registry.llm, "available", lambda: True)
    monkeypatch.setattr(registry.llm, "complete",
                        lambda *a, **k: _llm_result("store management team structure"))
    out = skill_search_docs(_ctx())
    assert calls == ["struktur tim store management", "store management team structure"]
    assert out["results"][0]["citation"] == "a.pptx#chunk5"      # better set won
    assert out["rewritten_query"] == "store management team structure"


def test_search_docs_no_rewrite_when_strong(monkeypatch):
    calls = []
    monkeypatch.setattr(registry.rag, "retrieve",
                        lambda *a, **k: calls.append(1) or [_hit("a.pptx#chunk2", rerank=1.5)])
    monkeypatch.setattr(registry.llm, "available", lambda: True)
    monkeypatch.setattr(registry.llm, "complete",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("llm must not be called")))
    out = skill_search_docs(_ctx())
    assert len(calls) == 1 and "rewritten_query" not in out


def test_search_docs_loop_skipped_without_rerank_signal(monkeypatch):
    """No reranker (offline embedder) -> no signal -> no loop: deterministic single pass."""
    calls = []
    monkeypatch.setattr(registry.rag, "retrieve",
                        lambda *a, **k: calls.append(1) or [_hit("a.pptx#chunk0")])   # no rerank_score
    monkeypatch.setattr(registry.llm, "available", lambda: True)
    monkeypatch.setattr(registry.llm, "complete",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("llm must not be called")))
    out = skill_search_docs(_ctx())
    assert len(calls) == 1 and "rewritten_query" not in out


def test_search_docs_keeps_original_when_rewrite_not_better(monkeypatch):
    calls = []

    def fake_retrieve(tid, q, role, top_k=4):
        calls.append(q)
        return [_hit("a.pptx#chunk0", rerank=-3.0)] if len(calls) == 1 \
            else [_hit("a.pptx#chunk9", rerank=-5.0)]            # rewrite is WORSE

    monkeypatch.setattr(registry.rag, "retrieve", fake_retrieve)
    monkeypatch.setattr(registry.llm, "available", lambda: True)
    monkeypatch.setattr(registry.llm, "complete", lambda *a, **k: _llm_result("another query"))
    out = skill_search_docs(_ctx())
    assert len(calls) == 2
    assert out["results"][0]["citation"] == "a.pptx#chunk0"      # original kept
    assert "rewritten_query" not in out


# ---- the verifier reports, nothing rewrites (chat overhaul item 6) ----
_EVIDENCE = {"docs": {"results": [{"filename": "sop.md", "text": "Refund 30 hari", "chunk_index": 0,
                                   "document_id": "d", "citation": "sop.md#chunk0"}],
                      "citations": ["sop.md#chunk0"]}}
_GATE = SimpleNamespace(pii_request=False)


def test_a_missing_citation_is_reported_and_there_is_no_repair_path():
    vr = orchestrator.verify("kebijakan refund?", "Refund berlaku 30 hari.", _EVIDENCE, _GATE)
    assert vr["ok"] is False and vr["checks"]["citations_present"] is False
    assert not hasattr(orchestrator, "_verify_and_repair")


def test_sql_agent_retries_successful_but_empty_query(monkeypatch):
    script = [
        SimpleNamespace(
            text="",
            tool_calls=[{
                "id": "c1", "name": "run_sql",
                "arguments": {
                    "sql": "SELECT region FROM sales WHERE day = '2026-07-28'",
                },
            }],
            model="text-only-model", provider="fake",
            prompt_tokens=1, completion_tokens=1, cost=0.0,
        ),
        SimpleNamespace(
            text="",
            tool_calls=[{
                "id": "c2", "name": "run_sql",
                "arguments": {"sql": "SELECT region FROM sales"},
            }],
            model="text-only-model", provider="fake",
            prompt_tokens=1, completion_tokens=1, cost=0.0,
        ),
        SimpleNamespace(
            text="evidence collected", tool_calls=[],
            model="text-only-model", provider="fake",
            prompt_tokens=1, completion_tokens=1, cost=0.0,
        ),
    ]
    monkeypatch.setattr(
        sql_agent.llm, "complete_tools", lambda *_a, **_k: script.pop(0),
    )

    def fake_execute(_ctx, ds, sql, engine):
        rows = [] if "2026-07-28" in sql else [["Jawa"]]
        return {
            "ok": True,
            "columns": ["region"],
            "rows": rows,
            "safe_sql": sql,
            "generated_sql": sql,
            "engine": engine,
            "dialect": "postgres",
            "datasource_id": ds["id"],
            "pii_columns": [],
        }

    monkeypatch.setattr(sql_agent, "execute_governed", fake_execute)
    out = sql_agent.run_sql_agent(
        Ctx(
            tenant_id="t_x", role="Admin", role_policy={"full_access": True},
            question="analisis seluruh data", answer_purpose="answer_complex",
        ),
        {
            "id": "ds_sales",
            "name": "Sales",
            "type": "postgres",
            "mdl": {
                "tables": {"sales": ["day", "region"]},
                "allowed_tables": ["sales"],
                "pii_columns": [],
            },
        },
        max_turns=3,
    )
    assert len(out["queries"]) == 2
    assert len(out["results"]) == 1
    assert out["sql"]["rows"] == [["Jawa"]]


def test_sql_agent_redacts_the_driver_error_it_feeds_back(monkeypatch):
    """Finding 11: the retry message carried the driver's text verbatim.

    Every other piece of tool output the loop sends is masked, but a failed query went
    back as `ERROR <code>: <raw driver message>` — and drivers quote the offending ROW
    VALUE in constraint/type errors, so this was a PII channel straight into the prompt.
    """
    email = "budi.santoso@acme.co.id"
    sent = []
    script = [
        SimpleNamespace(
            text="", tool_calls=[{"id": "c1", "name": "run_sql",
                                  "arguments": {"sql": "SELECT region FROM sales"}}],
            model="m", provider="fake", prompt_tokens=1, completion_tokens=1, cost=0.0),
        SimpleNamespace(
            text="done", tool_calls=[],
            model="m", provider="fake", prompt_tokens=1, completion_tokens=1, cost=0.0),
    ]

    def fake_complete_tools(_tenant, _purpose, messages, _tools=None, **_kw):
        sent.extend(str(m.get("content") or "") for m in messages)
        return script.pop(0)

    monkeypatch.setattr(sql_agent.llm, "complete_tools", fake_complete_tools)
    monkeypatch.setattr(sql_agent, "execute_governed", lambda *_a, **_k: {
        "ok": False, "code": "DRY_RUN_ERROR",
        "error": f"invalid input syntax for value '{email}' at row 3",
    })

    sql_agent.run_sql_agent(
        Ctx(tenant_id="t_x", role="Admin", role_policy={"full_access": True},
            question="total gmv", answer_purpose="answer_complex"),
        {"id": "ds_sales", "name": "Sales", "type": "postgres",
         "mdl": {"tables": {"sales": ["day", "region"]}, "allowed_tables": ["sales"],
                 "pii_columns": []}},
        max_turns=2,
    )

    blob = "\n".join(sent)
    assert email not in blob, "raw driver error reached the model"
    assert "[PII:EMAIL_1]" in blob, "error text was dropped instead of redacted"
    assert "DRY_RUN_ERROR" in blob, "the model still needs the error CODE to retry"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
