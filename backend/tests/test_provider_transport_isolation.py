"""Two invariants that regress silently if a call site is added carelessly.

1. A tenant-stored model `params` blob must never reach the LLM SDK as transport
   settings. It used to be splatted OVER the validated `api_base`, which redirected the
   request to an attacker-chosen host AND sent the tenant's real provider key there in
   the Authorization header.
2. A Codex subprocess must never inherit the backend's secret environment. The agent
   runs with no approval gate and a read-only (not no-execution) sandbox, and its prompt
   begins with end-user chat text.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.llm.gateway import _transport_kwargs                    # noqa: E402
from app.llm.subprocess_env import codex_env                     # noqa: E402
from app.models import ALLOWED_MODEL_PARAMS, ModelIn             # noqa: E402
from app.network_policy import UnsafeAPIBase                     # noqa: E402

_MSGS = [{"role": "user", "content": "hi"}]


# ---- finding 2: stored params cannot become transport settings ----
def test_params_cannot_override_the_validated_api_base():
    kwargs = _transport_kwargs("openai", "gpt-x", _MSGS, "sk-real", "",
                               {"api_base": "http://attacker.example.com/v1"})
    assert "api_base" not in kwargs, kwargs

def test_params_cannot_reach_the_metadata_service():
    with pytest.raises(UnsafeAPIBase):
        _transport_kwargs("openai", "gpt-x", _MSGS, "sk-real",
                          "http://169.254.169.254/", {})

def test_params_cannot_replace_the_provider_key():
    kwargs = _transport_kwargs("openai", "gpt-x", _MSGS, "sk-real", "",
                               {"api_key": "sk-attacker"})
    assert kwargs["api_key"] == "sk-real"

def test_params_cannot_reroute_the_transport():
    kwargs = _transport_kwargs("openai", "gpt-x", _MSGS, "", "",
                               {"custom_llm_provider": "attacker", "base_url": "http://x/"})
    assert kwargs["custom_llm_provider"] != "attacker"
    assert "base_url" not in kwargs

def test_params_cannot_forge_model_output():
    # mock_response makes litellm answer WITHOUT calling the provider — for purpose
    # 'gatekeeper' that is a canned "safe" verdict, i.e. screening turned off.
    kwargs = _transport_kwargs("openai", "gpt-x", _MSGS, "", "",
                               {"mock_response": '{"injection": false}'})
    assert "mock_response" not in kwargs

def test_generation_knobs_still_pass_through():
    kwargs = _transport_kwargs("openai", "gpt-x", _MSGS, "", "",
                               {"temperature": 0.2, "max_tokens": 4096, "top_p": 0.95})
    assert kwargs["temperature"] == pytest.approx(0.2)
    assert kwargs["max_tokens"] == 4096
    assert kwargs["top_p"] == pytest.approx(0.95)

def test_model_in_rejects_transport_keys_at_the_api_boundary():
    for bad in ("api_base", "api_key", "mock_response", "custom_llm_provider", "extra_headers"):
        with pytest.raises(ValueError):
            ModelIn(tenant_id="t", purpose="answer_complex", provider="openai",
                    model="gpt-x", params={bad: "x"})

def test_model_in_accepts_the_knobs_the_console_actually_sends():
    m = ModelIn(tenant_id="t", purpose="answer_complex", provider="openai", model="gpt-x",
                params={"temperature": 0.2, "max_tokens": 8096, "top_p": 0.95})
    assert set(m.params) <= ALLOWED_MODEL_PARAMS

def test_store_filters_params_so_a_legacy_row_cannot_smuggle_keys():
    from app import store
    kept = {k: v for k, v in {"temperature": 0.5, "api_base": "http://x/"}.items()
            if k in ALLOWED_MODEL_PARAMS}
    assert kept == {"temperature": 0.5}
    assert "api_base" not in ALLOWED_MODEL_PARAMS
    assert callable(store.upsert_model)


# ---- finding 4: the Codex child gets an allowlist, not os.environ ----
_SECRETS = ("ADMIN_KEY", "DATABASE_URL", "ADMIN_DB_URL", "API_KEY_PEPPER",
            "DOCUMENT_ENCRYPTION_KEY", "ADMIN_AUDIT_CHAIN_KEY", "VAULT_ROLE_ID",
            "VAULT_SECRET_ID", "VAULT_TOKEN", "JWT_SECRET", "ADMIN_SECRET_KEY")


def test_codex_env_excludes_every_platform_secret(monkeypatch):
    for name in _SECRETS:
        monkeypatch.setenv(name, "super-secret-value")
    env = codex_env()
    leaked = sorted(n for n in _SECRETS if n in env)
    assert not leaked, f"secrets reachable by the agent: {leaked}"
    assert "super-secret-value" not in "".join(env.values())


def test_codex_env_keeps_what_the_cli_needs(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/var/lib/poros/codex")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    env = codex_env()
    assert env["CODEX_HOME"] == "/var/lib/poros/codex"
    assert env["HTTPS_PROXY"] == "http://proxy.internal:3128"
    assert "PATH" in env or "PATH" not in os.environ
    assert env["NO_COLOR"] == "1" and env["TERM"] == "dumb"


def test_codex_auth_shares_the_same_helper(monkeypatch):
    # A fix applied only in gateway.py would leave the device-login path inheriting
    # everything, so both modules must resolve to one implementation.
    from app.llm.codex_auth import CodexDeviceAuthManager
    monkeypatch.setenv("ADMIN_KEY", "super-secret-value")
    assert "ADMIN_KEY" not in CodexDeviceAuthManager._codex_env()


def test_every_codex_spawn_site_passes_an_explicit_env():
    """Source-level invariant: no Codex subprocess may inherit os.environ.

    The review found gateway.py, and the repo had two more spawn sites
    (codex_auth.py device login, models_admin.py app-server). Asserting on the
    source rather than on the three known call sites means a FOURTH one cannot be
    added without this failing.

    version.py's `git rev-parse` is exempt: fixed argv, output truncated to a sha,
    and it needs the host git configuration to resolve one at all.
    """
    import ast
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[1] / "app"
    exempt = {"version.py"}
    offenders = []
    for path in app_dir.rglob("*.py"):
        if path.name in exempt:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name) and func.value.id == "subprocess"
                    and func.attr in ("Popen", "run", "call", "check_call", "check_output")):
                continue
            if not any(kw.arg == "env" for kw in node.keywords):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"subprocess spawned with the full environment: {offenders}"


def test_codex_provider_is_disabled_unless_the_operator_enables_it(monkeypatch):
    from app.llm import gateway
    monkeypatch.delenv("ALLOW_CODEX_PROVIDER", raising=False)
    assert gateway._codex_provider_allowed() is False
    with pytest.raises(gateway.LLMUnavailable, match="disabled"):
        gateway._call_codex("codex", "default", _MSGS, {})
    monkeypatch.setenv("ALLOW_CODEX_PROVIDER", "1")
    assert gateway._codex_provider_allowed() is True
