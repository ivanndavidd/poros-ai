"""Deep Research that gathers nothing must still reach governed SQL.

Live case (staging Acme Assistant, trace 7f58a232): the tenant's model answered every
round in prose instead of requesting tools, so the phase gate never opened, run_sql was
never offered, and the turn ended with 24 rounds, zero queries, and an "insufficient
evidence" answer. Web search rides on SQL evidence, so it was blocked by the same stall.
"""
from types import SimpleNamespace

import pytest

from app.agent import orchestrator as orch


def _gate(intent="prescriptive"):
    return SimpleNamespace(
        allow=True, intent=intent, complexity="complex", injection=False,
        pii_request=False, reasons=[], engine="fake-model",
        prompt_tokens=0, completion_tokens=0, cost=0.0,
    )


def _datasource():
    return {
        "id": "ds_sales", "name": "Sales", "type": "postgres", "conn": {},
        "mdl": {"tables": {"sales_monthly": ["month_id", "revenue"]},
                "allowed_tables": ["sales_monthly"], "pii_columns": []},
    }


def _sql_step():
    return {
        "ok": True, "columns": ["month_id", "revenue"], "rows": [[202601, 100]],
        "safe_sql": "SELECT month_id, revenue FROM sales_monthly",
        "generated_sql": "SELECT month_id, revenue FROM sales_monthly",
        "engine": "fake-model", "dialect": "postgres",
        "datasource_id": "ds_sales", "datasource_name": "Sales",
        "datasource_type": "postgres", "pii_columns": [],
    }


def _stalled_research():
    """What run_research returns when the model never drove the tool loop."""
    return {
        "ok": False, "sql": None, "results": [], "queries": [],
        "answer": None, "docs": None, "web": None,
        "coverage": {"required": ["adaptive_research"], "succeeded": [],
                     "missing": ["research stopped by no_tool_action"]},
        "research": {"adaptive": True, "completed": False, "rounds": 3,
                     "stop_reason": "no_tool_action", "tool_trace": [],
                     "tool_counts": {"run_sql": 0, "web_search": 0}},
    }


@pytest.fixture
def stubbed(monkeypatch):
    """Drive run_agent offline: no Postgres, no LLM, no connectors."""
    monkeypatch.setattr(orch.store, "log_audit", lambda *a, **k: None)
    monkeypatch.setattr(orch.store, "get_role", lambda *a, **k: {"policy": {"full_access": True}})
    monkeypatch.setattr(orch.store, "list_documents", lambda *a, **k: [])
    monkeypatch.setattr(orch.store, "list_datasources", lambda *a, **k: [_datasource()])
    monkeypatch.setattr(orch.llm, "screen", lambda *a, **k: _gate())
    monkeypatch.setattr(orch.llm, "pick_answer_purpose", lambda *a, **k: "answer_complex")
    monkeypatch.setattr(orch.memory, "get_cfg", lambda *a, **k: {})
    monkeypatch.setattr(orch.deep_analysis, "should_use", lambda *a, **k: True)
    monkeypatch.setattr(orch.semantic_contract, "resolve_datasources", lambda *a, **k: [])
    monkeypatch.setattr(orch.web_research, "enabled", lambda *a, **k: False)
    monkeypatch.setattr(orch.sql_agent, "usable", lambda *a, **k: True)
    monkeypatch.setattr(orch.research_agent, "usable", lambda *a, **k: True)
    monkeypatch.setattr(orch, "run_skill", lambda name, ctx, **kw: {"text": "jawaban", "streamed": False})
    monkeypatch.setattr(orch, "verify",
                        lambda *a, **k: {"ok": True, "issues": [], "checks": {}})
    monkeypatch.setattr(orch, "compute_confidence",
                        lambda *a, **k: {"level": "high", "score": 90, "signals": {}})
    return monkeypatch


def test_stalled_research_falls_back_to_the_governed_sql_agent(stubbed):
    calls = []
    stubbed.setattr(orch.research_agent, "run_research", lambda *a, **k: _stalled_research())

    def fake_sql_agent(ctx, ds, enable_docs=False, **kw):
        calls.append(ds["id"])
        step = _sql_step()
        return {"queries": [step], "results": [step], "sql": step, "answer": "dari SQL"}

    stubbed.setattr(orch.sql_agent, "run_sql_agent", fake_sql_agent)

    out = orch.run_agent({"tenant_id": "t_x", "role": "Admin"}, "gudang mana yang sebaiknya diperluas?")

    # the whole point: a governed query actually ran
    assert calls == ["ds_sales"]
    assert out["sql"]["ok"] is True
    assert "adaptive_research(stalled:no_tool_action)" in out["plan"]
    assert "agent_loop" in out["plan"]


def test_productive_research_is_not_second_guessed(stubbed):
    """Research that DID gather evidence must not be re-run through the agent loop."""
    step = _sql_step()
    productive = dict(_stalled_research())
    productive.update({"ok": True, "sql": step, "results": [step], "queries": [step],
                       "answer": "dari riset"})
    productive["research"] = {**productive["research"], "completed": True,
                              "stop_reason": "critic_approved",
                              "tool_counts": {"run_sql": 1, "web_search": 0}}
    stubbed.setattr(orch.research_agent, "run_research", lambda *a, **k: productive)

    called = []
    stubbed.setattr(orch.sql_agent, "run_sql_agent",
                    lambda *a, **k: called.append(1) or {"queries": [], "results": []})

    out = orch.run_agent({"tenant_id": "t_x", "role": "Admin"}, "gudang mana yang sebaiknya diperluas?")

    assert called == []
    assert not any(entry.startswith("adaptive_research(stalled") for entry in out["plan"])
