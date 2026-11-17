"""Provider-agnostic LLM gateway (LiteLLM).

Resolves per-tenant provider credentials + model assignments from the store and
calls LiteLLM, capturing token usage and cost. Supports fallback. Raises
LLMUnavailable when nothing is configured / LiteLLM is missing, so callers can
fall back to rule-based behaviour.
"""
import json
import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Optional
from .. import store
from ..api_errors import log_internal
from ..context import check_cancelled, get_budget_reservation, get_request_id
from ..network_policy import validate_api_base
from .subprocess_env import codex_env

_log = logging.getLogger("poros.llm")

# LiteLLM provider names keyed by our provider id.
_PROVIDER_MAP = {
    "openai": "openai", "anthropic": "anthropic", "gemini": "gemini",
    "google": "gemini", "vertex": "vertex_ai", "azure": "azure",
    "mistral": "mistral", "groq": "groq", "deepseek": "deepseek",
    "ollama": "ollama", "cohere": "cohere",
}


class LLMUnavailable(Exception):
    pass


@dataclass
class LLMResult:
    text: str
    model: str
    provider: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    raw: dict = field(default_factory=dict)


def _litellm():
    try:
        import litellm
        litellm.drop_params = True          # ignore params a provider doesn't support
        litellm.suppress_debug_info = True
        return litellm
    except Exception:
        return None


def available() -> bool:
    return _litellm() is not None


def _message_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or item))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def _messages_to_prompt(messages: list) -> str:
    lines = [
        "You are being used as the tenant's LLM backend. "
        "Answer only from the conversation below and return the final answer text."
    ]
    for m in messages or []:
        role = str(m.get("role", "user")).upper() if isinstance(m, dict) else "USER"
        content = _message_text(m.get("content") if isinstance(m, dict) else m).strip()
        if content:
            lines.append(f"{role}:\n{content}")
    return "\n\n".join(lines).strip()


def _codex_provider_allowed() -> bool:
    """Local subprocess execution is opt-in for the OPERATOR, not for a tenant manager.

    Selecting provider `codex` is one `POST /admin/models` away, and it turns an LLM call
    into a coding agent running on the backend host. Whether that is acceptable is a
    deployment decision, so it needs an env flag rather than a tenant setting.
    """
    return (os.getenv("ALLOW_CODEX_PROVIDER") or "").strip().lower() in ("1", "true", "yes", "on")


def _call_codex(provider, model, messages, params):
    if not _codex_provider_allowed():
        raise LLMUnavailable(
            "the codex provider is disabled on this deployment "
            "(set ALLOW_CODEX_PROVIDER=1 to enable local subprocess execution)")
    exe = shutil.which("codex")
    if not exe:
        raise LLMUnavailable("Codex CLI is not installed on the backend host")

    prompt = _messages_to_prompt(messages)
    if not prompt:
        raise LLMUnavailable("empty prompt for Codex CLI provider")

    try:
        timeout = int((params or {}).get("timeout_sec") or os.getenv("CODEX_EXEC_TIMEOUT_SEC", "120"))
    except Exception:
        timeout = 120

    fd, out_path = tempfile.mkstemp(prefix="codex-llm-", suffix=".txt")
    os.close(fd)
    args = [
        exe, "-a", "never", "exec",
        "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "--ignore-rules",
        "--sandbox", "read-only", "--color", "never",
        "--output-last-message", out_path,
    ]
    if model and model not in ("default", "codex-default"):
        args.extend(["--model", model])
    args.append("-")

    try:
        r = subprocess.run(args, input=prompt, text=True, encoding="utf-8", errors="replace",
                           capture_output=True, timeout=timeout, env=codex_env())
        text = ""
        try:
            with open(out_path, "r", encoding="utf-8") as f:
                text = f.read().strip()
        except Exception:
            text = ""
        if r.returncode != 0:
            # The agent's stderr can carry file contents and command output, and this
            # message is interpolated into a job's stored error_message and into the /v1
            # research stop_reason — both reach the caller. Log it, never return it.
            log_internal("codex CLI failed", RuntimeError((r.stderr or r.stdout or "").strip()[:2000]),
                         context=f"codex provider={provider} model={model or 'default'} rc={r.returncode}")
            raise LLMUnavailable("Codex CLI failed; check the backend log for this request id")
        if not text:
            text = (r.stdout or "").strip()
        if not text:
            raise LLMUnavailable("Codex CLI returned no final message")
        # `raw` deliberately empty: the full agent transcript is not diagnostic data worth
        # carrying on a result object that gets persisted in encrypted job results.
        return LLMResult(text=text, model=model or "codex-default", provider=provider,
                         prompt_tokens=0, completion_tokens=0, cost=0.0, raw={})
    except subprocess.TimeoutExpired:
        raise LLMUnavailable(f"Codex CLI timed out after {timeout}s")
    finally:
        try:
            os.unlink(out_path)
        except Exception:
            pass


_SAMPLING_KEYS = ("temperature", "top_p")


def _llm_timeout() -> float:
    """Ceiling on ONE provider call (env LLM_TIMEOUT_SEC, default 600s).

    Without it a hung provider connection pins a job worker FOREVER — live 2026-08-31:
    chat jobs sat "queued" for minutes because the few workers were stuck inside stalled
    calls with no timeout at all. Generous on purpose: for streaming this is the
    transport's per-read gap (a healthy stream emitting deltas is never cut, only a dead
    connection); for non-streaming it bounds the silent wait for the response, which at
    10 minutes is beyond anything an interactive pipeline should keep a worker for."""
    try:
        return max(30.0, float(os.getenv("LLM_TIMEOUT_SEC", "600")))
    except ValueError:
        return 600.0


def _sampling_param_rejected(err) -> bool:
    """True when a completion failed because the model refuses a non-default sampling
    param. Reasoning models (OpenAI o-series / GPT-5, deepseek-reasoner, ...) accept
    only temperature=1 and often no top_p at all. LiteLLM's drop_params can only strip
    what it recognises, so an unknown/too-new model name leaks the param to the provider,
    which raises BadRequestError like "temperature does not support 0.2. Only the default
    (1)" or "top_p is not supported with this model". Detect that so we retry without them."""
    s = str(err).lower()
    named = any(k in s for k in _SAMPLING_KEYS)
    refused = any(t in s for t in ("unsupported", "not supported", "does not support",
                                   "only the default", "only default"))
    return named and refused


def _reasoning_tools_rejected(err) -> bool:
    """True when a tool-calling completion failed because the model runs with reasoning
    enabled and refuses function tools on /v1/chat/completions (e.g. gpt-5.6-luna:
    \"Function tools with reasoning_effort are not supported ... set reasoning_effort to
    'none'\"). Recoverable by retrying with reasoning_effort='none' — the tool-loop turns
    (SQL writing against a fully-described schema) don't need deep reasoning."""
    s = str(err).lower()
    return "reasoning_effort" in s and ("not supported" in s or "'none'" in s)


def _completion(litellm, kwargs):
    """litellm.completion with targeted one-shot recoveries for reasoning models the
    LiteLLM catalog doesn't know (so drop_params can't help): drop temperature/top_p when
    the model rejects sampling params; set reasoning_effort='none' when the model refuses
    function tools while reasoning is enabled. Each recovery applies at most once.
    Every call gets a timeout (caller's own wins) — an untimed call can pin a job
    worker on a dead connection indefinitely."""
    kwargs.setdefault("timeout", _llm_timeout())
    for _ in range(3):                     # initial attempt + one of each recovery
        try:
            return litellm.completion(**kwargs)
        except Exception as e:
            if _sampling_param_rejected(e) and any(k in kwargs for k in _SAMPLING_KEYS):
                for k in _SAMPLING_KEYS:
                    kwargs.pop(k, None)
                continue
            if (_reasoning_tools_rejected(e) and kwargs.get("tools")
                    and kwargs.get("reasoning_effort") != "none"):
                kwargs["reasoning_effort"] = "none"
                continue
            raise
    return litellm.completion(**kwargs)    # both recoveries applied — final attempt


def _inject_request_id(kwargs):
    """Best-effort: tag the downstream LLM call with the current request's X-Request-Id so ONE
    user request can be traced through to the provider — `metadata` for LiteLLM callbacks/logs,
    `extra_headers` as an actual header on the provider HTTP call. Never clobbers caller values."""
    rid = get_request_id()
    if not rid:
        return
    md = dict(kwargs.get("metadata") or {})
    md.setdefault("request_id", rid)
    kwargs["metadata"] = md
    eh = dict(kwargs.get("extra_headers") or {})
    eh.setdefault("X-Request-Id", rid)
    kwargs["extra_headers"] = eh


def _log_call(tenant_id, purpose, model, provider, ptok, ctok, cost):
    """One INFO line per LLM call, tagged with the request's X-Request-Id, so a user request can
    be followed through to each model call (grep by rid). Never raises."""
    try:
        _log.info("llm_call rid=%s tenant=%s purpose=%s provider=%s model=%s ptok=%d ctok=%d cost=%.4f",
                  get_request_id() or "-", tenant_id, purpose, provider, model,
                  int(ptok or 0), int(ctok or 0), float(cost or 0.0))
    except Exception:
        pass


def _consume_stream(stream, on_delta):
    """Forward each content delta to ``on_delta`` AS the model generates it.
    ``check_cancelled`` inside the chunk loop means Stop truly interrupts a generation
    mid-stream instead of waiting for the full completion."""
    chunks, parts = [], []
    for chunk in stream:
        check_cancelled()
        chunks.append(chunk)
        choices = _g(chunk, "choices") or []
        if not choices:
            continue
        piece = _g(_g(choices[0], "delta"), "content")
        if piece:
            parts.append(piece)
            try:
                on_delta(piece)
            except Exception:
                pass                       # a broken UI callback must never kill the answer
    return chunks, parts


def _stream_accounting(litellm, chunks, messages):
    """Rebuild the full response for usage/cost. Best-effort: whatever was assigned
    before a failure is kept (a cost lookup miss must not discard the rebuilt response)."""
    resp, usage, cost = None, {}, 0.0
    try:
        resp = litellm.stream_chunk_builder(chunks, messages=messages)
        usage = _g(resp, "usage") or {}
        cost = float(litellm.completion_cost(completion_response=resp) or 0.0)
    except Exception:
        pass                               # usage accounting is best-effort on streams
    return resp, usage, cost


def _stream_call(litellm, kwargs, model, provider, on_delta):
    """Streaming completion: real over-the-wire streaming, then usage accounting."""
    kwargs = dict(kwargs)
    kwargs["stream"] = True
    # OpenAI-compatible providers report token usage on the final chunk with this flag;
    # litellm.drop_params strips it for providers that don't know it.
    kwargs["stream_options"] = {"include_usage": True}
    stream = _completion(litellm, kwargs)
    chunks, parts = _consume_stream(stream, on_delta)
    resp, usage, cost = _stream_accounting(litellm, chunks, kwargs.get("messages"))
    text = "".join(parts)
    if not text and resp is not None:
        try:
            text = resp["choices"][0]["message"]["content"] or ""
        except Exception:
            text = ""
    return LLMResult(text=text.strip(), model=model, provider=provider,
                     prompt_tokens=int(_g(usage, "prompt_tokens") or 0),
                     completion_tokens=int(_g(usage, "completion_tokens") or 0),
                     cost=cost, raw=dict(resp) if isinstance(resp, dict) else {})


# Keys a stored params blob must never contribute, beyond the transport settings that are
# re-asserted below. `mock_response` makes litellm return canned text without calling the
# provider at all — for purpose 'gatekeeper' that is a forged "safe" verdict, i.e. prompt
# screening switched off tenant-wide.
_FORBIDDEN_TRANSPORT_KEYS = frozenset({
    "mock_response", "mock_tool_calls", "base_url", "api_version", "organization",
    "extra_headers", "headers", "client", "http_client", "hooks", "callbacks",
})


def _transport_kwargs(provider, model, messages, api_key, api_base, params, **extra):
    """Build SDK kwargs so the caller's `params` can never redirect the call.

    `params` is `llm_models.params`, a free-form tenant-supplied blob. Splatting it OVER
    the transport settings let a stored `{"api_base": "http://attacker/"}` replace the
    endpoint that `validate_api_base` had just cleared — and the tenant's real provider
    key went along with it in the Authorization header. `mock_response` could likewise
    forge model output, including a canned gatekeeper verdict that disables screening.

    Generation knobs go in FIRST; every security-relevant key is then asserted on top and
    re-validated here, at the one place that builds the call.
    """
    kwargs = {k: v for k, v in (params or {}).items() if k not in _FORBIDDEN_TRANSPORT_KEYS}
    kwargs.update(model=model, messages=messages, **extra)
    kwargs["custom_llm_provider"] = _PROVIDER_MAP.get(provider, provider)
    if api_key:
        kwargs["api_key"] = api_key
    else:
        kwargs.pop("api_key", None)
    if api_base:
        kwargs["api_base"] = validate_api_base(api_base, purpose=f"{provider} api_base")
    else:
        kwargs.pop("api_base", None)
    kwargs.pop("base_url", None)          # litellm's alias for api_base
    _inject_request_id(kwargs)
    return kwargs


def _call(litellm, provider, model, api_key, api_base, messages, params, on_delta=None):
    kwargs = _transport_kwargs(provider, model, messages, api_key, api_base, params)
    if on_delta is not None:
        return _stream_call(litellm, kwargs, model, provider, on_delta)
    resp = _completion(litellm, kwargs)
    text = resp["choices"][0]["message"]["content"]
    usage = resp.get("usage", {}) or {}
    try:
        cost = float(litellm.completion_cost(completion_response=resp) or 0.0)
    except Exception:
        cost = 0.0
    return LLMResult(text=text.strip() if text else "", model=model, provider=provider,
                     prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                     completion_tokens=int(usage.get("completion_tokens", 0) or 0),
                     cost=cost, raw=dict(resp))


def _design_models(tenant_id):
    cfg = store.get_tenant_config(tenant_id, "models") or {}
    return cfg.get("models", []) if isinstance(cfg, dict) else []


# Purpose -> preferred design-list roles, tried in order BEFORE Primary. Lets a tenant pin a
# cheap model to the high-frequency/low-stakes purposes while Primary keeps the hard ones:
# gatekeeper screens EVERY prompt; answer_simple serves greetings, the follow-up rewrite,
# memory capture and history summarization (every analysis answer is Primary since chat
# overhaul item 7, unless the period's budget is spent). Anything else (answer_complex, ...) stays on Primary.
_PURPOSE_ROLES = {
    "gatekeeper": ("Gatekeeper", "Simple"),
    "answer_simple": ("Simple",),
}


def _resolve_attempts(tenant_id: str, purpose: str, override: dict):
    """Ordered list of (provider, model, api_key, api_base, params) to try.
    Prefers the design model list, purpose-aware: a role matching the purpose
    (_PURPOSE_ROLES) is tried first, then Primary, then Fallback — so a dead cheap
    model degrades to Primary instead of failing. Else the legacy per-purpose config."""
    out = []
    models = _design_models(tenant_id)

    def _role(r):
        return next((m for m in models if m.get("role") == r and m.get("enabled", True) and m.get("name")), None)

    chain = []
    for r in _PURPOSE_ROLES.get(purpose, ()):
        special = _role(r)
        if special:
            chain.append(special)
            break
    primary = _role("Primary")
    if primary:
        chain.append(primary)
        fallback = _role("Fallback")
        if fallback:
            chain.append(fallback)
    if chain:
        seen = set()
        for m in chain:
            if id(m) in seen:
                continue
            seen.add(id(m))
            prov = store.get_provider(tenant_id, m["provider"]) or {}
            params = {k: v for k, v in {"temperature": m.get("temperature"), "max_tokens": m.get("max_tokens"), "top_p": m.get("top_p")}.items() if v is not None}
            params.update(override)
            out.append((m["provider"], m["name"], prov.get("api_key", ""), prov.get("api_base", ""), params))
        return out
    cfg = store.get_model(tenant_id, purpose)
    if cfg and cfg.get("enabled") and cfg.get("model"):
        prov = store.get_provider(tenant_id, cfg["provider"])
        ak, ab = (prov or {}).get("api_key", ""), (prov or {}).get("api_base", "")
        params = {**(cfg.get("params") or {}), **override}
        out.append((cfg["provider"], cfg["model"], ak, ab, params))
        if cfg.get("fallback_model"):
            out.append((cfg["provider"], cfg["fallback_model"], ak, ab, params))
    return out


def first_model(tenant_id: str, purpose: str):
    """(provider, model) that would actually serve this purpose, or None. Lets the router
    check whether answer_simple resolves to a genuinely DIFFERENT (cheaper) model than
    answer_complex — with only a Primary configured both purposes resolve identically."""
    try:
        attempts = _resolve_attempts(tenant_id, purpose, {})
    except Exception:
        return None
    return (attempts[0][0], attempts[0][1]) if attempts else None


def is_configured(tenant_id: str, purpose: str = "answer_complex") -> bool:
    """True when at least one model attempt resolves for (tenant, purpose) — i.e. the
    tenant has a usable model set. Lets callers fail FAST (before doing expensive work
    such as DB sampling) when no model is configured, instead of erroring only at the
    completion call after the cost was already spent."""
    try:
        return bool(_resolve_attempts(tenant_id, purpose, {}))
    except Exception:
        return False


def _supports_native_function_calling(provider: str, model: str, litellm=None) -> bool:
    """Return whether this exact provider/model has native function-call transport."""
    if provider == "codex":
        return False
    litellm = litellm or _litellm()
    if litellm is None:
        return False
    prefix = _PROVIDER_MAP.get(provider, provider)
    for candidate in (f"{prefix}/{model}", model):
        try:
            if litellm.supports_function_calling(model=candidate):
                return True
        except Exception:
            pass
    return False


def supports_tools(tenant_id: str, purpose: str = "answer_complex") -> bool:
    """Return whether the configured chain can participate in Poros's tool loop.

    Native tools are an optimisation, not a routing requirement. Models without them
    use structured JSON actions and therefore keep the same research flow.
    """
    try:
        attempts = _resolve_attempts(tenant_id, purpose, {})
    except Exception:
        return False
    if not attempts:
        return False
    for provider, _model, *_rest in attempts:
        if provider == "codex":
            if shutil.which("codex"):
                return True
        elif _litellm() is not None:
            return True
    return False


def _restart_stream(on_restart):
    try:
        on_restart()
    except Exception:
        pass


def _complete_attempt(litellm, provider, model, ak, ab, messages, params, on_delta):
    if provider == "codex":
        res = _call_codex(provider, model, messages, params)
        if on_delta is not None and res.text:
            on_delta(res.text)       # codex has no stream; deliver once, ASAP
        return res
    return _call(litellm, provider, model, ak, ab, messages, params, on_delta=on_delta)


def _complete_unbudgeted(tenant_id: str, purpose: str, messages: list,
                         on_delta=None, on_restart=None, **override) -> LLMResult:
    """Run a completion for (tenant, purpose). Raises LLMUnavailable if unusable.

    ``on_delta(text)`` (optional) receives content deltas live while the model
    generates — real streaming. If an attempt fails AFTER it already emitted deltas,
    ``on_restart()`` is called before the fallback attempt so the consumer can clear
    the partial text instead of appending a second copy."""
    attempts = _resolve_attempts(tenant_id, purpose, override)
    if not attempts:
        raise LLMUnavailable(f"no model configured for purpose '{purpose}'")
    last = None
    litellm = None
    emitted = 0

    def _delta(piece):
        nonlocal emitted
        emitted += 1
        on_delta(piece)

    for provider, model, ak, ab, params in attempts:
        check_cancelled()
        if emitted and on_restart is not None:
            _restart_stream(on_restart)
            emitted = 0
        try:
            # fetched once and kept OUTSIDE the attempt: a failed attempt must not
            # re-import the module for the fallback (and codex-only chains never need it)
            if provider != "codex" and litellm is None:
                litellm = _litellm()
                if litellm is None:
                    raise LLMUnavailable("litellm not installed")
            res = _complete_attempt(
                litellm, provider, model, ak, ab, messages, params,
                _delta if on_delta is not None else None)
            _log_call(tenant_id, purpose, res.model, res.provider, res.prompt_tokens, res.completion_tokens, res.cost)
            return res
        except Exception as e:
            last = e
            check_cancelled()  # never spend on a fallback after Stop was pressed
    raise LLMUnavailable(f"all model attempts failed: {last}")


def _standalone_budget_reservation(tenant_id):
    current = get_budget_reservation()
    if current and store.budget_reservation_active(current, tenant_id):
        return None
    amount = float(os.getenv("BUDGET_RESERVE_USD_PER_LLM_CALL", "0.05"))
    return store.reserve_budget(tenant_id, amount=amount, ttl_sec=300)


def complete(tenant_id: str, purpose: str, messages: list,
             on_delta=None, on_restart=None, **override) -> LLMResult:
    """Budget-gated completion. Pass ``on_delta`` to stream content deltas live.

    Consumer chat normally owns one larger request reservation. Background/admin LLM
    work that runs without it receives a smaller per-call reservation here.
    """
    reservation = _standalone_budget_reservation(tenant_id)
    try:
        return _complete_unbudgeted(tenant_id, purpose, messages,
                                    on_delta=on_delta, on_restart=on_restart, **override)
    finally:
        store.release_budget(reservation["id"] if reservation else None)


def _g(o, k):
    """Get key/attr from a litellm response object (dict-like OR pydantic)."""
    if o is None:
        return None
    if isinstance(o, dict) or hasattr(o, "get"):
        try:
            return o.get(k)
        except Exception:
            pass
    return getattr(o, k, None)


@dataclass
class ToolResult:
    text: str
    tool_calls: list           # [{"id", "name", "arguments": dict}]
    model: str
    provider: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0


def _tool_protocol(tools: list) -> str:
    specs = []
    for tool in tools or []:
        fn = (tool or {}).get("function") or {}
        if not fn.get("name"):
            continue
        specs.append({
            "name": fn["name"],
            "description": fn.get("description") or "",
            "parameters": fn.get("parameters") or {
                "type": "object", "properties": {},
            },
        })
    return (
        "STRUCTURED TOOL ADAPTER (mandatory): this model has no native function-call "
        "transport, but the application still requires the same tool workflow. Return "
        "EXACTLY one JSON object and no markdown or commentary. To request tools use "
        '{"tool_calls":[{"name":"tool_name","arguments":{}}]}. Multiple independent '
        "calls SHOULD be included in the same array whenever the research state lists "
        "several independent missing requirements; do not spend one model turn per "
        "independent action. You MAY add \"note\": one short sentence, in the user's "
        "language, saying what you are doing now and why — it is shown live to the user "
        "as progress narration (never put figures there that are not already in the "
        "evidence). When a normal final response is appropriate "
        'use {"final":"response text"}. Never claim a tool ran until its TOOL RESULT is '
        "present in the conversation. Available tools:\n"
        + json.dumps(specs, ensure_ascii=False, separators=(",", ":"))
    )


def _render_emulated_calls(calls: list) -> str:
    rendered = []
    for call in calls:
        function = _g(call, "function") or {}
        rendered.append({
            "id": _g(call, "id"),
            "name": _g(function, "name"),
            "arguments": _g(function, "arguments") or {},
        })
    return json.dumps(rendered, ensure_ascii=False, default=str)


def _emulated_message(message, protocol: str, protocol_added: bool):
    item = dict(message) if isinstance(message, dict) else {
        "role": "user", "content": str(message),
    }
    role = str(item.get("role") or "user")
    content = _message_text(item.get("content")).strip()
    calls = item.get("tool_calls") or []
    if calls:
        history = _render_emulated_calls(calls)
        content = (
            (content + "\n" if content else "")
            + "PREVIOUS TOOL CALLS: "
            + history
        )
    if role == "tool":
        call_id = str(item.get("tool_call_id") or "")
        role = "user"
        content = f"TOOL RESULT ({call_id}):\n{content}"
    if role == "system" and not protocol_added:
        content = (content + "\n\n" if content else "") + protocol
        protocol_added = True
    return {"role": role, "content": content}, protocol_added


def _emulated_tool_messages(messages: list, tools: list) -> list:
    """Flatten native tool history into ordinary chat plus a strict JSON protocol."""
    protocol = _tool_protocol(tools)
    out = []
    protocol_added = False
    for message in messages or []:
        item, protocol_added = _emulated_message(
            message, protocol, protocol_added,
        )
        out.append(item)
    if not protocol_added:
        out.insert(0, {"role": "system", "content": protocol})
    return out


def _json_object(text: str):
    raw = (text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    start = raw.find("{")
    if start < 0:
        return None
    try:
        parsed, _end = json.JSONDecoder().raw_decode(raw[start:])
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _tool_result(result: LLMResult, text: str, calls: list) -> ToolResult:
    return ToolResult(
        text=text,
        tool_calls=calls,
        model=result.model,
        provider=result.provider,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        cost=result.cost,
    )


def _plain_emulated_result(result: LLMResult) -> ToolResult:
    # Prose cannot trigger a tool, so it is safe as a final response.
    final_text = str(result.text or "").strip()
    if not final_text:
        raise LLMUnavailable("structured tool adapter received an empty response")
    return _tool_result(result, final_text, [])


def _emulated_arguments(value) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalize_emulated_call(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name") or raw.get("tool")
    if not name:
        return None
    return {
        "id": str(raw.get("id") or f"emulated_{uuid.uuid4().hex}"),
        "name": str(name),
        "arguments": _emulated_arguments(raw.get("arguments") or {}),
    }


def _emulated_calls(payload: dict) -> list:
    raw_calls = payload.get("tool_calls") or []
    if isinstance(raw_calls, dict):
        raw_calls = [raw_calls]
    if not isinstance(raw_calls, list):
        return []
    return [
        normalized for raw in raw_calls
        if (normalized := _normalize_emulated_call(raw)) is not None
    ]


def _emulated_final(payload: dict) -> str:
    final = payload.get("final")
    if final is None:
        final = payload.get("text") or payload.get("answer") or ""
    return str(final or "").strip()


def _emulated_tool_result(result: LLMResult) -> ToolResult:
    payload = _json_object(result.text)
    if payload is None:
        return _plain_emulated_result(result)
    calls = _emulated_calls(payload)
    final = _emulated_final(payload)
    if not calls and not str(final or "").strip():
        raise LLMUnavailable(
            "structured tool adapter returned neither tool_calls nor a final response"
        )
    # Alongside tool calls, surface the model's optional "note" as the turn's text —
    # mirrors native transports, where prose may accompany tool_calls. Callers use it
    # as live progress narration; it is never a final answer (calls take precedence).
    if calls and not final:
        note = str(payload.get("note") or "").strip()
        if note:
            return _tool_result(result, note, calls)
    return _tool_result(result, final, calls)


def _complete_tools_unbudgeted(tenant_id: str, purpose: str, messages: list, tools: list, **override) -> ToolResult:
    """Tool-loop completion with native tools or structured JSON action emulation.

    ``messages`` may contain assistant ``tool_calls`` and role=tool results. Models
    without native function calling receive an equivalent plain-chat protocol; Poros
    still executes and governs every requested action.
    """
    attempts = _resolve_attempts(tenant_id, purpose, override)
    if not attempts:
        raise LLMUnavailable("no model configured")
    last = None
    litellm = None
    for provider, model, ak, ab, params in attempts:
        check_cancelled()
        try:
            if provider != "codex" and litellm is None:
                litellm = _litellm()
                if litellm is None:
                    raise LLMUnavailable("litellm not installed")
            if not _supports_native_function_calling(provider, model, litellm):
                adapted = _emulated_tool_messages(messages, tools)
                if provider == "codex":
                    plain = _call_codex(provider, model, adapted, params)
                else:
                    plain = _call(
                        litellm, provider, model, ak, ab, adapted, params,
                    )
                tr = _emulated_tool_result(plain)
                _log_call(
                    tenant_id, purpose, tr.model, tr.provider,
                    tr.prompt_tokens, tr.completion_tokens, tr.cost,
                )
                return tr
            kwargs = _transport_kwargs(provider, model, messages, ak, ab, params, tools=tools)
            resp = _completion(litellm, kwargs)
            msg = resp["choices"][0]["message"]
            tcs = []
            for tc in (_g(msg, "tool_calls") or []):
                fn = _g(tc, "function") or {}
                raw = _g(fn, "arguments")
                try:
                    args = json.loads(raw) if isinstance(raw, str) else (raw or {})
                except Exception:
                    args = {}
                tcs.append({"id": _g(tc, "id"), "name": _g(fn, "name"), "arguments": args})
            usage = resp.get("usage", {}) or {}
            try:
                cost = float(litellm.completion_cost(completion_response=resp) or 0.0)
            except Exception:
                cost = 0.0
            tr = ToolResult(text=(_g(msg, "content") or "").strip(), tool_calls=tcs,
                            model=model, provider=provider,
                            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
                            completion_tokens=int(usage.get("completion_tokens", 0) or 0), cost=cost)
            _log_call(tenant_id, purpose, tr.model, tr.provider, tr.prompt_tokens, tr.completion_tokens, tr.cost)
            return tr
        except Exception as e:
            last = e
            check_cancelled()
    raise LLMUnavailable(f"tool completion failed: {last}")


def complete_tools(tenant_id: str, purpose: str, messages: list, tools: list, **override) -> ToolResult:
    reservation = _standalone_budget_reservation(tenant_id)
    try:
        return _complete_tools_unbudgeted(
            tenant_id, purpose, messages, tools, **override
        )
    finally:
        store.release_budget(reservation["id"] if reservation else None)
