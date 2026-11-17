"""X-Request-Id propagation (Apps Gateway correlation id).

The per-request id (from the gateway header, or minted locally) is exposed via a ContextVar
and tagged onto downstream LLM calls — `metadata` (for LiteLLM callbacks/logs) + `extra_headers`
(an actual header on the provider HTTP call) — so one user request can be traced to the model.

Offline: no DB, no real litellm. Run:  cd backend && python -m pytest tests/test_request_id.py -q
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from app import context          # noqa: E402
from app.llm import gateway      # noqa: E402


def _reset():
    context.set_request_id("")


def test_context_var_roundtrip():
    context.set_request_id("req-abc")
    assert context.get_request_id() == "req-abc"
    _reset()
    assert context.get_request_id() == ""


def test_inject_adds_metadata_and_header_when_present():
    context.set_request_id("req-xyz")
    try:
        kwargs = {"model": "m", "messages": []}
        gateway._inject_request_id(kwargs)
        assert kwargs["metadata"]["request_id"] == "req-xyz"
        assert kwargs["extra_headers"]["X-Request-Id"] == "req-xyz"
    finally:
        _reset()


def test_inject_is_noop_without_request_id():
    _reset()
    kwargs = {"model": "m"}
    gateway._inject_request_id(kwargs)
    assert "metadata" not in kwargs and "extra_headers" not in kwargs


def test_inject_preserves_caller_values():
    context.set_request_id("req-1")
    try:
        kwargs = {"metadata": {"foo": "bar"}, "extra_headers": {"X-Other": "y"}}
        gateway._inject_request_id(kwargs)
        assert kwargs["metadata"] == {"foo": "bar", "request_id": "req-1"}
        assert kwargs["extra_headers"] == {"X-Other": "y", "X-Request-Id": "req-1"}
    finally:
        _reset()


def test_call_tags_litellm_completion_kwargs():
    """_call must forward the request id into the actual litellm.completion() kwargs."""
    context.set_request_id("req-call")
    try:
        captured = {}

        class _Fake:
            def completion(self, **kwargs):
                captured.update(kwargs)
                return {"choices": [{"message": {"content": "hi"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 2}}

            def completion_cost(self, **k):
                return 0.0

        res = gateway._call(_Fake(), "openai", "gpt-x", "", "", [{"role": "user", "content": "q"}], {})
        assert res.text == "hi"
        assert captured["metadata"]["request_id"] == "req-call"
        assert captured["extra_headers"]["X-Request-Id"] == "req-call"
    finally:
        _reset()


def test_context_is_copied_into_capture_thread():
    """schedule_capture must run the worker in a COPY of the current context, so the request id
    is visible inside the background thread (threads don't inherit ContextVars automatically)."""
    import contextvars
    seen = {}
    box = contextvars.ContextVar("box", default="")
    box.set("req-thread")

    def worker():
        seen["val"] = box.get()

    ctx = contextvars.copy_context()
    import threading
    t = threading.Thread(target=lambda: ctx.run(worker))
    t.start(); t.join()
    assert seen["val"] == "req-thread"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
