import threading
from types import SimpleNamespace

import pytest

from app import context
from app.agent import orchestrator
from app.skills.registry import Ctx, _record_usage


def _gate():
    return SimpleNamespace(
        allow=True,
        engine="llm:test-gate",
        intent="data_query",
        complexity="complex",
        reasons=[],
    )


def test_cancelled_usage_contains_work_already_billed():
    event = threading.Event()
    usage = [{
        "model": "test-model",
        "prompt_tokens": 12,
        "completion_tokens": 3,
        "cost": 0.25,
    }]
    context.set_cancellation(event, "trace-stop", usage)
    event.set()
    try:
        with pytest.raises(context.PipelineCancelled) as stopped:
            context.check_cancelled()
        assert stopped.value.trace_id == "trace-stop"
        assert stopped.value.usage == {
            "model": "test-model",
            "prompt_tokens": 12,
            "completion_tokens": 3,
            "cost": 0.25,
        }
    finally:
        context.set_cancellation()


def test_usage_is_recorded_before_stop_propagates():
    event = threading.Event()
    ctx = Ctx("tenant", "Admin", {})
    context.set_cancellation(event, "trace-stop", ctx.usage)
    event.set()
    result = SimpleNamespace(
        model="test-model",
        prompt_tokens=7,
        completion_tokens=2,
        cost=0.1,
    )
    try:
        with pytest.raises(context.PipelineCancelled):
            _record_usage(ctx, result)
        assert ctx.usage[0]["prompt_tokens"] == 7
        assert ctx.usage[0]["cost"] == pytest.approx(0.1)
    finally:
        context.set_cancellation()


def test_stop_after_gatekeeper_never_writes_answer_audit(monkeypatch):
    event = threading.Event()
    audits = []

    def log_audit(_tenant, _trace, event_name, **_kwargs):
        audits.append(event_name)

    def screen(*_a, **_k):
        event.set()
        return _gate()

    monkeypatch.setattr(orchestrator.store, "log_audit", log_audit)
    monkeypatch.setattr(orchestrator.llm, "screen", screen)
    context.set_cancellation(event, "trace-stop")
    try:
        with pytest.raises(context.PipelineCancelled):
            orchestrator.run_agent(
                {"tenant_id": "tenant", "role": "Admin"},
                "analisa penjualan",
                trace_id="trace-stop",
            )
        assert audits == ["question"]
        assert "answer" not in audits
    finally:
        context.set_cancellation()
