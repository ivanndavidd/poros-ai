"""Purpose-aware model roles (auto model switching): design-list roles Simple/Gatekeeper
route cheap purposes to a cheap model while Primary keeps the hard ones.

- gateway._resolve_attempts: purpose 'gatekeeper' -> role Gatekeeper (else Simple), purpose
  'answer_simple' -> role Simple; every chain degrades to Primary then Fallback; legacy
  per-purpose config still works when the design list is empty.
- router.pick_answer_purpose: 'simple' complexity only routes to answer_simple when it
  resolves to a genuinely DIFFERENT model than answer_complex.

Run:  cd backend && python -m pytest tests/test_model_roles.py -q
Offline — store lookups are monkeypatched; no DB, no provider.
"""
import os, sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)

from app.llm import gateway, router  # noqa: E402


def _design(models):
    return {"models": models}


_PRIMARY = {"name": "gpt-5.6-luna", "provider": "openai", "role": "Primary", "enabled": True,
            "max_tokens": 4096}
_SIMPLE = {"name": "gpt-5.6-mini", "provider": "openai", "role": "Simple", "enabled": True,
           "max_tokens": 2048}
_GATE = {"name": "gpt-5.6-mini", "provider": "openai", "role": "Gatekeeper", "enabled": True,
         "max_tokens": 1500}
_FALLBACK = {"name": "gpt-4o-mini", "provider": "openai", "role": "Fallback", "enabled": True}


def _patch_store(monkeypatch, models, legacy=None):
    monkeypatch.setattr(gateway.store, "get_tenant_config",
                        lambda tid, section: _design(models) if section == "models" else {})
    monkeypatch.setattr(gateway.store, "get_provider",
                        lambda tid, prov: {"api_key": "k", "api_base": ""})
    monkeypatch.setattr(gateway.store, "get_model",
                        lambda tid, purpose: (legacy or {}).get(purpose))


def _models(attempts):
    return [a[1] for a in attempts]


# ---- gateway resolution ----
def test_answer_complex_stays_on_primary(monkeypatch):
    _patch_store(monkeypatch, [_PRIMARY, _SIMPLE, _GATE, _FALLBACK])
    assert _models(gateway._resolve_attempts("t", "answer_complex", {})) == \
        ["gpt-5.6-luna", "gpt-4o-mini"]                    # Simple/Gatekeeper NOT in the chain


def test_answer_simple_prefers_simple_then_primary(monkeypatch):
    _patch_store(monkeypatch, [_PRIMARY, _SIMPLE, _GATE, _FALLBACK])
    assert _models(gateway._resolve_attempts("t", "answer_simple", {})) == \
        ["gpt-5.6-mini", "gpt-5.6-luna", "gpt-4o-mini"]    # dead cheap model degrades to Primary


def test_gatekeeper_prefers_gatekeeper_then_simple(monkeypatch):
    _patch_store(monkeypatch, [_PRIMARY, _SIMPLE, _GATE])
    got = gateway._resolve_attempts("t", "gatekeeper", {})
    assert _models(got) == ["gpt-5.6-mini", "gpt-5.6-luna"]
    assert got[0][4].get("max_tokens") == 1500             # the Gatekeeper card's params win
    _patch_store(monkeypatch, [_PRIMARY, _SIMPLE])         # no Gatekeeper role -> Simple steps in
    got = gateway._resolve_attempts("t", "gatekeeper", {})
    assert got[0][4].get("max_tokens") == 2048


def test_disabled_special_role_is_skipped(monkeypatch):
    _patch_store(monkeypatch, [_PRIMARY, {**_SIMPLE, "enabled": False}])
    assert _models(gateway._resolve_attempts("t", "answer_simple", {})) == ["gpt-5.6-luna"]


def test_simple_only_design_list_serves_simple_purposes(monkeypatch):
    # no Primary: answer_simple/gatekeeper resolve via the Simple card; answer_complex
    # falls through to the legacy per-purpose config path (empty here)
    _patch_store(monkeypatch, [_SIMPLE])
    assert _models(gateway._resolve_attempts("t", "answer_simple", {})) == ["gpt-5.6-mini"]
    assert gateway._resolve_attempts("t", "answer_complex", {}) == []


def test_legacy_per_purpose_config_still_used_without_design_list(monkeypatch):
    legacy = {"answer_simple": {"enabled": True, "provider": "openai", "model": "legacy-mini",
                                "params": {}, "fallback_model": None}}
    _patch_store(monkeypatch, [], legacy=legacy)
    assert _models(gateway._resolve_attempts("t", "answer_simple", {})) == ["legacy-mini"]


def test_override_params_reach_every_attempt(monkeypatch):
    _patch_store(monkeypatch, [_PRIMARY, _SIMPLE])
    got = gateway._resolve_attempts("t", "answer_simple", {"max_tokens": 999})
    assert all(a[4]["max_tokens"] == 999 for a in got)     # caller override beats card params


# ---- router integration ----
def test_router_routes_simple_when_distinct_simple_model(monkeypatch):
    _patch_store(monkeypatch, [_PRIMARY, _SIMPLE, _GATE])
    monkeypatch.setattr(router.store, "get_budget", lambda tid: None)
    assert router.pick_answer_purpose("t", "simple") == "answer_simple"
    assert router.pick_answer_purpose("t", "complex") == "answer_complex"


def test_router_keeps_complex_when_only_primary(monkeypatch):
    # answer_simple would resolve to the SAME Primary -> no point flagging it 'simple'
    _patch_store(monkeypatch, [_PRIMARY])
    monkeypatch.setattr(router.store, "get_budget", lambda tid: None)
    assert router.pick_answer_purpose("t", "simple") == "answer_complex"


def test_router_budget_downgrade_to_simple(monkeypatch):
    _patch_store(monkeypatch, [_PRIMARY, _SIMPLE])
    monkeypatch.setattr(router.store, "get_budget", lambda tid: 10.0)
    monkeypatch.setattr(router, "_spent_this_period", lambda tid: 11.0)
    assert router.pick_answer_purpose("t", "complex") == "answer_simple"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
