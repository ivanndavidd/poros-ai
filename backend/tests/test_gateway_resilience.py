"""Gateway recovery for reasoning models LiteLLM doesn't know (drop_params can't strip
what it can't recognise, so provider rejections leak through as BadRequestError):

- sampling params: temperature/top_p rejected ("Only the default (1)") -> retry without them
- function tools: "Function tools with reasoning_effort are not supported ... set
  reasoning_effort to 'none'" -> retry with reasoning_effort='none'

Run:  cd backend && python -m pytest tests/test_gateway_resilience.py -q
Offline — litellm is faked; these verify the RETRY WIRING, not any provider.
"""
import os, sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)

from app.llm import gateway  # noqa: E402


class _FakeLitellm:
    """completion() raises the scripted errors in order, then returns a marker."""
    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = []              # kwargs snapshot per attempt

    def completion(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.errors:
            raise RuntimeError(self.errors.pop(0))
        return {"ok": True}


_TEMP_ERR = ("litellm.BadRequestError: OpenAIException - Unsupported value: 'temperature' "
             "does not support 0.2 with this model. Only the default (1) value is supported.")
_TOOLS_ERR = ("litellm.BadRequestError: OpenAIException - Function tools with reasoning_effort "
              "are not supported for gpt-5.6-luna in /v1/chat/completions. To use function "
              "tools, use /v1/responses or set reasoning_effort to 'none'.")


def test_sampling_rejection_retries_without_temperature_and_top_p():
    fake = _FakeLitellm([_TEMP_ERR])
    out = gateway._completion(fake, {"model": "m", "messages": [], "temperature": 0.2, "top_p": 0.9})
    assert out == {"ok": True}
    assert len(fake.calls) == 2
    assert "temperature" not in fake.calls[1] and "top_p" not in fake.calls[1]


def test_tools_reasoning_rejection_retries_with_effort_none():
    fake = _FakeLitellm([_TOOLS_ERR])
    out = gateway._completion(fake, {"model": "m", "messages": [], "tools": [{"x": 1}]})
    assert out == {"ok": True}
    assert fake.calls[1]["reasoning_effort"] == "none"
    assert fake.calls[1]["tools"] == [{"x": 1}]          # tools kept — only the effort changed


def test_both_recoveries_compose_in_one_call():
    fake = _FakeLitellm([_TEMP_ERR, _TOOLS_ERR])
    out = gateway._completion(fake, {"model": "m", "messages": [], "temperature": 0,
                                     "tools": [{"x": 1}]})
    assert out == {"ok": True}
    assert len(fake.calls) == 3
    assert "temperature" not in fake.calls[2] and fake.calls[2]["reasoning_effort"] == "none"


def test_unrelated_error_is_not_retried():
    fake = _FakeLitellm(["rate limit exceeded, try again later"])
    with pytest.raises(RuntimeError):
        gateway._completion(fake, {"model": "m", "messages": [], "temperature": 0})


def test_every_call_carries_a_timeout():
    """An untimed provider call can pin a job worker on a dead connection forever —
    live 2026-08-31: chat jobs sat 'queued' while every worker hung inside one."""
    fake = _FakeLitellm([])
    gateway._completion(fake, {"model": "m", "messages": []})
    assert fake.calls[0]["timeout"] == pytest.approx(600.0)
    # a caller's explicit ceiling wins over the default
    fake = _FakeLitellm([])
    gateway._completion(fake, {"model": "m", "messages": [], "timeout": 42})
    assert fake.calls[0]["timeout"] == 42
    # and the env knob moves the default, floored well clear of instant cutoffs
    os.environ["LLM_TIMEOUT_SEC"] = "90"
    try:
        assert gateway._llm_timeout() == pytest.approx(90.0)
        os.environ["LLM_TIMEOUT_SEC"] = "5"
        assert gateway._llm_timeout() == pytest.approx(30.0)
        os.environ["LLM_TIMEOUT_SEC"] = "nope"
        assert gateway._llm_timeout() == pytest.approx(600.0)
    finally:
        os.environ.pop("LLM_TIMEOUT_SEC", None)
    assert len(fake.calls) == 1                          # no blind retry on non-param errors


def test_reasoning_retry_skipped_without_tools():
    # the reasoning_effort recovery is tools-specific; a plain completion must not loop
    fake = _FakeLitellm([_TOOLS_ERR])
    with pytest.raises(RuntimeError):
        gateway._completion(fake, {"model": "m", "messages": []})
    assert len(fake.calls) == 1


def test_structured_tool_adapter_parses_fenced_tool_call():
    result = gateway.LLMResult(
        text='```json\n{"tool_calls":[{"name":"run_sql","arguments":'
             '{"sql":"SELECT 1"}}]}\n```',
        model="deepseek-reasoner",
        provider="deepseek",
        prompt_tokens=12,
        completion_tokens=8,
        cost=0.002,
    )
    out = gateway._emulated_tool_result(result)
    assert out.model == "deepseek-reasoner"
    assert out.tool_calls[0]["name"] == "run_sql"
    assert out.tool_calls[0]["arguments"] == {"sql": "SELECT 1"}
    assert out.tool_calls[0]["id"].startswith("emulated_")


def test_structured_tool_adapter_accepts_plain_prose_as_safe_final_answer():
    result = gateway.LLMResult(
        text="The governed result shows six months of sales.",
        model="deepseek-reasoner",
        provider="deepseek",
    )
    out = gateway._emulated_tool_result(result)
    assert out.tool_calls == []
    assert out.text == "The governed result shows six months of sales."


def test_structured_tool_adapter_preserves_tool_observation_history():
    messages = gateway._emulated_tool_messages([
        {"role": "system", "content": "Investigate with tools."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "c1",
                "type": "function",
                "function": {
                    "name": "run_sql",
                    "arguments": '{"sql":"SELECT 1"}',
                },
            }],
        },
        {"role": "tool", "tool_call_id": "c1", "content": '{"rows":[[1]]}'},
    ], [{
        "type": "function",
        "function": {
            "name": "run_sql",
            "description": "Run governed SQL",
            "parameters": {"type": "object"},
        },
    }])
    assert "STRUCTURED TOOL ADAPTER" in messages[0]["content"]
    assert "PREVIOUS TOOL CALLS" in messages[1]["content"]
    assert messages[2]["role"] == "user"
    assert "TOOL RESULT (c1)" in messages[2]["content"]


def test_non_native_model_uses_structured_tool_adapter(monkeypatch):
    captured = {}

    class _NoNativeTools:
        pass

    monkeypatch.setattr(
        gateway,
        "_resolve_attempts",
        lambda *_a, **_k: [
            ("deepseek", "deepseek-reasoner", "secret", "", {"max_tokens": 1000}),
        ],
    )
    monkeypatch.setattr(gateway, "_litellm", lambda: _NoNativeTools())
    monkeypatch.setattr(
        gateway, "_supports_native_function_calling", lambda *_a, **_k: False,
    )

    def fake_call(_litellm, provider, model, _key, _base, messages, _params):
        captured["messages"] = messages
        return gateway.LLMResult(
            text='{"tool_calls":[{"name":"run_sql","arguments":{"sql":"SELECT 1"}}]}',
            model=model,
            provider=provider,
        )

    monkeypatch.setattr(gateway, "_call", fake_call)
    monkeypatch.setattr(gateway, "_log_call", lambda *_a, **_k: None)
    out = gateway._complete_tools_unbudgeted(
        "t", "answer_complex",
        [{"role": "user", "content": "inspect data"}],
        [{
            "type": "function",
            "function": {
                "name": "run_sql",
                "description": "Run governed SQL",
                "parameters": {"type": "object"},
            },
        }],
    )
    assert out.tool_calls[0]["name"] == "run_sql"
    assert "STRUCTURED TOOL ADAPTER" in captured["messages"][0]["content"]


def test_supports_tools_uses_adapter_for_non_native_model(monkeypatch):
    monkeypatch.setattr(
        gateway,
        "_resolve_attempts",
        lambda *_a, **_k: [
            ("deepseek", "deepseek-reasoner", "secret", "", {}),
        ],
    )
    monkeypatch.setattr(gateway, "_litellm", lambda: object())
    assert gateway.supports_tools("t", "answer_complex") is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
