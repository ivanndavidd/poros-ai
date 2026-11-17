"""Per-answer model badge: orchestrator._models_used resolves which model actually wrote
the answer + the distinct roster (gatekeeper first, then the answer pipeline) for the UI.

Run:  cd backend && python -m pytest tests/test_model_badge.py -q
Offline — pure function, no LLM/DB.
"""
import os, sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)

from app.agent import orchestrator as orch  # noqa: E402


def _u(model):
    return {"model": model, "prompt_tokens": 1, "completion_tokens": 1, "cost": 0.0}


def test_answer_model_is_last_usage_entry():
    # gatekeeper (mini) screened, planner+agent+synthesis ran on luna -> luna answered
    usage = [_u("gpt-5.4-mini"), _u("gpt-5.6-luna"), _u("gpt-5.6-luna")]
    answer, models = orch._models_used("llm:gpt-5.4-mini", usage)
    assert answer == "gpt-5.6-luna"
    assert models == ["gpt-5.4-mini", "gpt-5.6-luna"]      # gatekeeper first, deduped


def test_simple_question_answered_by_cheap_model():
    # gatekeeper + answer both on the cheap model -> single distinct model
    usage = [_u("gpt-5.4-mini")]
    answer, models = orch._models_used("llm:gpt-5.4-mini", usage)
    assert answer == "gpt-5.4-mini" and models == ["gpt-5.4-mini"]


def test_the_last_call_is_the_answer_model():
    # the call that runs last is the model that produced the final text
    usage = [_u("gpt-5.6-luna"), _u("gpt-5.6-luna")]   # agent loop + synthesis, both luna
    answer, models = orch._models_used("llm:gpt-5.4-mini", usage)
    assert answer == "gpt-5.6-luna"


def test_heuristic_gatekeeper_contributes_no_model():
    # gate.engine "heuristic" (no LLM screen) -> only the pipeline models appear
    usage = [_u("gpt-5.6-luna")]
    answer, models = orch._models_used("heuristic", usage)
    assert answer == "gpt-5.6-luna" and models == ["gpt-5.6-luna"]


def test_no_llm_calls_yields_none():
    # rule-based SQL + no synthesis LLM -> nothing to badge
    answer, models = orch._models_used("heuristic", [])
    assert answer is None and models == []


def test_gatekeeper_only_model_still_rostered():
    # gatekeeper on mini, then everything rule-based (empty usage) -> mini still shown,
    # but there is no answer model
    answer, models = orch._models_used("llm:gpt-5.4-mini", [])
    assert answer is None and models == ["gpt-5.4-mini"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
