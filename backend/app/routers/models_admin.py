"""Model Setup (Feature 1.4) admin endpoints: per-tenant LLM providers + model
assignments + budget, plus a live test. Panels for every provider are driven by
PROVIDER_CATALOG so each tenant can be configured independently.
"""
import json
import queue
import re
import shutil
import subprocess
import threading
import time
import requests
from fastapi import APIRouter, Depends, HTTPException, Response
from .. import store
from ..console_auth import (
    console_guard, current_principal, ensure_tenant_access, require_admin_role,
)
from ..models import ProviderIn, ModelIn, BudgetIn
from .. import llm
from ..llm.codex_auth import CodexAuthError, manager as codex_auth_manager
from ..llm.subprocess_env import codex_env
from ..network_policy import UnsafeAPIBase, validate_api_base
from ..api_errors import log_internal, raise_safe

router = APIRouter(prefix="/admin", tags=["model setup (1.4)"], dependencies=[Depends(console_guard)])

_CODEX_MODELS_TTL_SEC = 60
_codex_models_cache = {"at": 0.0, "models": []}
_codex_models_lock = threading.Lock()
_OPENAI_MODELS_TTL_SEC = 60
_openai_models_cache = {}
_openai_models_lock = threading.Lock()

# Catalog drives the per-provider input panels + model suggestions in the UI.
PROVIDER_CATALOG = [
    {"id": "openai", "label": "OpenAI", "needs": ["api_key"],
     "models": [
         "gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
         "gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano",
         "gpt-4o", "gpt-4o-mini",
     ]},
    {"id": "codex", "label": "Codex (device login)", "needs": ["codex_device"],
     # Codex CLI accepts the model id through --model. Availability still depends on
     # the ChatGPT plan/account used for the backend host's device login.
     "models": [
         "gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
         "gpt-5.5", "gpt-5.4", "gpt-5.4-mini",
     ],
     "auth": {"type": "device_code", "url": "https://auth.openai.com/codex/device",
              "command": "codex login --device-auth"}},
    {"id": "anthropic", "label": "Anthropic (Claude)", "needs": ["api_key"],
     "models": [
         "claude-fable-5", "claude-opus-5", "claude-sonnet-5",
         "claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5",
         "claude-haiku-4-5-20251001",
     ]},
    {"id": "gemini", "label": "Google Gemini", "needs": ["api_key"],
     "models": ["gemini-2.0-flash", "gemini-1.5-pro", "gemini-1.5-flash"]},
    {"id": "azure", "label": "Azure OpenAI", "needs": ["api_key", "api_base"],
     "models": ["gpt-4o-mini", "gpt-4o"]},
    {"id": "mistral", "label": "Mistral", "needs": ["api_key"],
     "models": ["mistral-small-latest", "mistral-large-latest"]},
    {"id": "groq", "label": "Groq", "needs": ["api_key"],
     "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]},
    {"id": "deepseek", "label": "DeepSeek", "needs": ["api_key"],
     "models": ["deepseek-v4-pro", "deepseek-v4-flash"]},
    {"id": "ollama", "label": "Ollama (local)", "needs": ["api_base"],
     "models": ["llama3.1", "qwen2.5", "mistral"]},
]
# Each "purpose" the router/gatekeeper consume.
PURPOSES = [
    {"id": "gatekeeper", "label": "Gatekeeper (screens every prompt)", "hint": "cheap/fast model"},
    {"id": "answer_simple", "label": "Answer — simple", "hint": "cheap model for greetings, follow-up rewrites, memory and summaries"},
    {"id": "answer_complex", "label": "Answer — complex", "hint": "stronger model for complex questions"},
    {"id": "embedding", "label": "Embeddings (doc-RAG/memory)", "hint": "optional; offline embedder used if unset"},
]


def _discover_codex_models() -> list[str]:
    """Read the picker-visible catalog for the ChatGPT identity logged into Codex.

    Codex OAuth availability is account/workspace-specific and is not the same as
    the OpenAI API model catalog. App-server's model/list is the authoritative local
    picker source. Fail closed to the curated catalog so an absent/older CLI never
    makes the whole provider-catalog endpoint unavailable.
    """
    exe = shutil.which("codex")
    if not exe:
        return []
    now = time.monotonic()
    with _codex_models_lock:
        if now - _codex_models_cache["at"] < _CODEX_MODELS_TTL_SEC:
            return list(_codex_models_cache["models"])

        proc = None
        try:
            proc = subprocess.Popen(
                [exe, "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                env=codex_env(),   # third Codex spawn site: never inherit ADMIN_KEY/ADMIN_DB_URL/provider keys
            )
            messages = [
                {"method": "initialize", "id": 0, "params": {"clientInfo": {
                    "name": "poros", "title": "Poros", "version": "0.2.0"}}},
                {"method": "initialized", "params": {}},
                {"method": "model/list", "id": 6,
                 "params": {"limit": 100, "includeHidden": False}},
            ]
            for message in messages:
                proc.stdin.write(json.dumps(message) + "\n")
                proc.stdin.flush()

            output = queue.Queue()
            threading.Thread(
                target=_copy_codex_stdout, args=(proc.stdout, output), daemon=True
            ).start()
            models = _read_codex_models(output, time.monotonic() + 8)
            if models:
                _codex_models_cache.update(at=time.monotonic(), models=models)
                return list(models)
        except Exception as exc:
            log_internal("CODEX_MODEL_DISCOVERY_FAILED", exc, context="codex model/list")
        finally:
            if proc is not None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        return []


def _copy_codex_stdout(stdout, output: queue.Queue) -> None:
    for line in stdout:
        output.put(line)


def _read_codex_models(output: queue.Queue, deadline: float) -> list[str]:
    while time.monotonic() < deadline:
        try:
            line = output.get(timeout=max(0.05, deadline - time.monotonic()))
        except queue.Empty:
            return []
        try:
            response = json.loads(line)
        except (TypeError, ValueError):
            continue
        if response.get("id") != 6:
            continue
        rows = (response.get("result") or {}).get("data") or []
        models = [
            str(row.get("model") or row.get("id") or "").strip()
            for row in rows if not row.get("hidden")
        ]
        return list(dict.fromkeys(model for model in models if model))
    return []


def _openai_text_model(model_id: str) -> bool:
    """Keep text/reasoning models from GET /v1/models out of the mixed media catalog."""
    model = model_id.lower()
    if not model.startswith(("gpt-", "chatgpt-", "o1", "o3", "o4")):
        return False
    return not any(part in model for part in (
        "audio", "realtime", "transcribe", "tts", "image", "moderation",
        "embedding", "search-preview",
    ))


def _discover_openai_models(tenant_id: str) -> list[str]:
    """List language models visible to this tenant's OpenAI API credential."""
    if not tenant_id:
        return []
    now = time.monotonic()
    with _openai_models_lock:
        cached = _openai_models_cache.get(tenant_id) or {}
        if now - cached.get("at", 0.0) < _OPENAI_MODELS_TTL_SEC:
            return list(cached.get("models") or [])
        try:
            provider = store.get_provider(tenant_id, "openai") or {}
            api_key = provider.get("api_key") or ""
            if not api_key:
                return []
            base = (provider.get("api_base") or "https://api.openai.com").rstrip("/")
            url = f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models"
            response = requests.get(
                url, headers={"Authorization": f"Bearer {api_key}"},
                timeout=8, allow_redirects=False,
            )
            response.raise_for_status()
            rows = (response.json() or {}).get("data") or []
            models = [str(row.get("id") or "").strip() for row in rows]
            models = sorted(dict.fromkeys(model for model in models
                                          if model and _openai_text_model(model)))
            if models:
                # Prefer stable aliases over dated snapshots in the initial scroll.
                models.sort(key=lambda model: (bool(re.search(r"-20\d{2}(?:-|$)", model)), model))
                _openai_models_cache[tenant_id] = {"at": time.monotonic(), "models": models}
                return list(models)
        except Exception as exc:
            log_internal("OPENAI_MODEL_DISCOVERY_FAILED", exc,
                         context=f"OpenAI model list tenant={tenant_id}")
        return []


@router.get("/provider-catalog")
def provider_catalog(tenant_id: str = "", p: dict = Depends(current_principal)):
    if tenant_id:
        ensure_tenant_access(p, tenant_id)
    codex_models = _discover_codex_models()
    openai_models = _discover_openai_models(tenant_id)
    providers = []
    for provider in PROVIDER_CATALOG:
        models = provider["models"]
        if provider["id"] == "codex":
            models = codex_models or models
        elif provider["id"] == "openai":
            models = openai_models or models
        providers.append({**provider, "models": models})
    return {"providers": providers, "purposes": PURPOSES,
            "litellm_installed": llm.available(), "codex_cli_installed": shutil.which("codex") is not None}


def _codex_auth_payload(p: dict) -> dict:
    can_manage = p.get("role") == "admin"
    return {
        **codex_auth_manager.auth_status(),
        "device_url": "https://auth.openai.com/codex/device",
        "command": "codex login --device-auth",
        "can_manage": can_manage,
        "login": codex_auth_manager.snapshot(include_device_code=can_manage),
    }


def _raise_codex_auth(exc: CodexAuthError):
    log_internal(exc.code, exc, context="managed Codex device login")
    raise HTTPException(exc.status_code, {
        "code": exc.code,
        "message": exc.message,
    }) from exc


@router.get("/codex-auth/status")
def codex_auth_status(response: Response, p: dict = Depends(current_principal)):
    """Report host-level Codex auth without exposing cached OAuth credentials."""
    response.headers["Cache-Control"] = "no-store"
    return _codex_auth_payload(p)


@router.post("/codex-auth/device/start")
def codex_auth_device_start(p: dict = Depends(require_admin_role)):
    """Start a structured device-code flow inside the backend container."""
    try:
        codex_auth_manager.start()
    except CodexAuthError as exc:
        _raise_codex_auth(exc)
    return _codex_auth_payload(p)


@router.post("/codex-auth/device/cancel")
def codex_auth_device_cancel(p: dict = Depends(require_admin_role)):
    codex_auth_manager.cancel()
    return _codex_auth_payload(p)


@router.post("/codex-auth/logout")
def codex_auth_logout(p: dict = Depends(require_admin_role)):
    try:
        codex_auth_manager.logout()
    except CodexAuthError as exc:
        _raise_codex_auth(exc)
    return _codex_auth_payload(p)


@router.get("/tenants/{tid}/providers")
def list_providers(tid: str):
    return store.list_providers(tid)            # api_key masked

@router.post("/providers")
def upsert_provider(b: ProviderIn, p: dict = Depends(current_principal)):
    ensure_tenant_access(p, b.tenant_id)
    try:
        api_base = validate_api_base(b.api_base, purpose=f"{b.provider} api_base")
    except UnsafeAPIBase as exc:
        raise HTTPException(400, {
            "code": "API_BASE_BLOCKED",
            "message": str(exc),
        }) from exc
    store.upsert_provider(b.tenant_id, b.provider, b.label, b.api_key, api_base, b.extra)
    return {"ok": True}

@router.delete("/tenants/{tid}/providers/{provider}")
def delete_provider(tid: str, provider: str):
    store.delete_provider(tid, provider); return {"ok": True}


@router.get("/tenants/{tid}/models")
def list_models(tid: str):
    return store.list_models(tid)

@router.post("/models")
def upsert_model(b: ModelIn, p: dict = Depends(current_principal)):
    ensure_tenant_access(p, b.tenant_id)
    store.upsert_model(b.tenant_id, b.purpose, b.provider, b.model, b.params,
                       b.monthly_budget_usd, b.fallback_model, b.enabled)
    return {"ok": True}

@router.delete("/tenants/{tid}/models/{purpose}")
def delete_model(tid: str, purpose: str):
    store.delete_model(tid, purpose); return {"ok": True}


@router.get("/tenants/{tid}/budget")
def get_budget(tid: str):
    return {"monthly_usd": store.get_budget(tid)}

@router.post("/budget")
def set_budget(b: BudgetIn, p: dict = Depends(current_principal)):
    ensure_tenant_access(p, b.tenant_id)
    store.set_budget(b.tenant_id, b.monthly_usd); return {"ok": True}


@router.post("/tenants/{tid}/llm-test")
def llm_test(tid: str, purpose: str = "answer_simple"):
    """Validate a configured model end-to-end with a tiny completion."""
    try:
        # 500, not 5: a reasoning model spends max_tokens on internal thinking first — at 5
        # it returns EMPTY text, making the connection test report ok with no output.
        r = llm.complete(tid, purpose, [{"role": "user", "content": "Reply with the single word: ok"}],
                         temperature=0, max_tokens=500)
    except llm.LLMUnavailable as e:
        raise_safe(400, "LLM_UNAVAILABLE",
                   "Model tidak dapat dihubungi. Periksa konfigurasi provider dan log backend.",
                   e, context=f"llm-test tenant={tid} purpose={purpose}")
    return {"ok": True, "model": r.model, "provider": r.provider, "text": r.text,
            "prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens, "cost": r.cost}
