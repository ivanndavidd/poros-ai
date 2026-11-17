"""An elliptical follow-up is made self-contained BEFORE the deterministic layers read it.

Live Acme case (2026-09-24): turn 1 asked for GMV per client per month as graph + table
and was answered for one brand; turn 2 "show me for each client" came back as the ORDER
COUNT per client. History reached the model as context, but the request contract, the
precision cue, the router and the confirmations all read the raw message — so the
follow-up carried no metric, no chart and no time bucket, and the model guessed.

The rewrite is a model call whose output is gated deterministically (the user's own
words only) and re-screened by the gatekeeper; on any doubt the turn stays as typed.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import followup, store                                  # noqa: E402
from app.agent import orchestrator as orch                       # noqa: E402
from app.main import app                                         # noqa: E402

client = TestClient(app)
AK = {"X-Admin-Key": "admin-dev-key"}

FIRST = ("Berapa total GMV per bulan dari masing masing brand. generate graph and table to "
         "show GMV per client per month dari January sampai bulan ini")
ANSWERED = ("GMV SAMSUNG Januari 2026 Rp120.000.000. Tabel lengkap berisi 279 baris; grafik "
            "hanya menampilkan brand utama.")
FOLLOWUP = "show me for each cliend"          # verbatim, typo included
REWRITE = ("Show the total GMV per client per month dari January sampai bulan ini as a "
           "graph and table")
MODEL = "fake-mini"
ROLE = "analyst"
EVENT = orch._FOLLOWUP_EVENT
ADOPTED = "rewritten"
# the e2e conversation: a plain prior question and the short follow-up it explains
PRIOR_Q = "berapa total gmv bulanan per brand"
TYPED = "show me for each client"
STANDALONE = "berapa total gmv bulanan per client per brand"


def _turn(role, content):
    return {"role": role, "content": content}


HISTORY = [_turn("user", FIRST), _turn("assistant", ANSWERED)]


def _gate(intent="data_query", allow=True, engine="heuristic", **kw):
    return SimpleNamespace(allow=allow, intent=intent, complexity=kw.get("complexity", "simple"),
                           injection=False, pii_request=False, reasons=[], engine=engine,
                           prompt_tokens=kw.get("prompt_tokens", 0),
                           completion_tokens=kw.get("completion_tokens", 0),
                           cost=kw.get("cost", 0.0))


def _res(text, prompt_tokens=40, completion_tokens=12):
    return SimpleNamespace(text=text, model=MODEL, provider="fake",
                           prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                           cost=0.0001)


def _usage(model, prompt_tokens, completion_tokens, cost=0.0):
    return {"model": model, "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens, "cost": cost}


# ---- when to even try -------------------------------------------------------------
def test_candidate_is_a_short_data_follow_up_with_a_prior_user_turn():
    sql = orch._SQL_INTENTS
    assert followup.is_candidate(FOLLOWUP, HISTORY, _gate(), None, sql) is True
    # cold open: nothing to contextualize against
    assert followup.is_candidate(FOLLOWUP, [], _gate(), None, sql) is False
    assert followup.is_candidate(FOLLOWUP, None, _gate(), None, sql) is False
    # the client echoed the current turn back — still no prior turn
    assert followup.is_candidate(FOLLOWUP, [_turn("user", FOLLOWUP)], _gate(), None, sql) is False
    # a long message states its own context
    assert followup.is_candidate(FIRST, HISTORY, _gate(), None, sql) is False
    # only data lanes; a blocked turn never gets here anyway
    assert followup.is_candidate(FOLLOWUP, HISTORY, _gate(intent="doc_question"), None, sql) is False
    assert followup.is_candidate(FOLLOWUP, HISTORY, _gate(allow=False, intent="unsafe"), None, sql) is False
    # a structured confirmation reply is folded by the module that asked
    assert followup.is_candidate(FOLLOWUP, HISTORY, _gate(), {"token": "pc_x", "choice_id": "a"}, sql) is False


def test_replies_to_our_own_confirmation_questions_are_left_to_their_modules():
    from app import business_confirm
    for prefix in (business_confirm.ASK_PREFIX_ID, business_confirm.ASK_PREFIX_EN):
        history = [_turn("user", FIRST), _turn("assistant", prefix + " …")]
        assert followup.is_candidate("3P", history, _gate(), None, orch._SQL_INTENTS) is False


def test_a_correction_after_an_assumed_reading_is_an_ordinary_follow_up():
    """Chat overhaul item 5: names are no longer asked, the answer states its reading.
    "no, Philips Lighting" after it is a short follow-up to make self-contained."""
    history = [_turn("user", FIRST),
               _turn("assistant", "Saya membaca 'Philips' sebagai brand Philipsome. GMV ...")]
    assert followup.is_candidate("bukan, maksud saya Philips Lighting", history, _gate(),
                                 None, orch._SQL_INTENTS) is True


def test_a_message_naming_a_business_metric_stands_on_its_own():
    sql = orch._SQL_INTENTS
    cue = orch._BUSINESS_PRECISION_CUE
    assert followup.is_candidate("gmv brand philips bulan juli", HISTORY, _gate(), None, sql,
                                 standalone_cue=cue) is False
    assert followup.is_candidate(FOLLOWUP, HISTORY, _gate(), None, sql, standalone_cue=cue) is True


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("FOLLOWUP_REWRITE", "0")
    assert followup.is_candidate(FOLLOWUP, HISTORY, _gate(), None, orch._SQL_INTENTS) is False
    monkeypatch.setenv("FOLLOWUP_REWRITE", "1")
    assert followup.is_candidate(FOLLOWUP, HISTORY, _gate(), None, orch._SQL_INTENTS) is True


# ---- the guard: the user's own words, nothing else --------------------------------
def test_guard_accepts_a_draft_made_of_the_users_own_words():
    assert followup.guard(REWRITE, FOLLOWUP, [FIRST]) is None
    # plural / extension leniency: clients<->client, monthly<->month
    assert followup.guard("GMV per clients monthly", FOLLOWUP, [FIRST]) is None


def test_guard_rejects_what_the_user_never_said():
    # the assistant's answer named Samsung; the USER never did
    assert followup.guard("Show GMV per month for Samsung", FOLLOWUP, [FIRST]) == "samsung"
    # a year the user never typed is an invented filter, even if plausible
    assert followup.guard("GMV per client from January 2026", FOLLOWUP, [FIRST]) == "2026"
    # a new metric
    assert followup.guard("show NMV for each client", FOLLOWUP, [FIRST]) == "nmv"
    # a new output
    assert followup.guard("GMV per client as a dashboard", FOLLOWUP, [FIRST]) == "dashboard"


# ---- the model call ---------------------------------------------------------------
def test_rewrite_uses_the_cheap_lane_and_never_sends_assistant_turns(monkeypatch):
    seen = {}

    def fake_complete(tenant_id, purpose, messages, **kw):
        seen.update(purpose=purpose, messages=messages)
        return _res(f'"{REWRITE}"')            # wrapped in quotes: cleaned

    monkeypatch.setattr(followup.gateway, "complete", fake_complete)
    draft = followup.rewrite("t1", FOLLOWUP, HISTORY)
    assert draft.question == REWRITE and draft.reason == ADOPTED
    assert draft.usage == _usage(MODEL, 40, 12, 0.0001)
    assert seen["purpose"] == "answer_simple"
    blob = json.dumps(seen["messages"], ensure_ascii=False)
    assert FIRST in blob and FOLLOWUP in blob
    assert "SAMSUNG" not in blob and "279" not in blob     # assistant text never leaves


def test_rewrite_falls_back_when_the_model_is_missing_unchanged_or_invents(monkeypatch):
    def down(*_a, **_k):
        raise RuntimeError("provider down")
    monkeypatch.setattr(followup.gateway, "complete", down)
    assert followup.rewrite("t1", FOLLOWUP, HISTORY) == followup.Draft(None, "unavailable")

    monkeypatch.setattr(followup.gateway, "complete", lambda *a, **k: _res("Show me for each cliend."))
    assert followup.rewrite("t1", FOLLOWUP, HISTORY).reason == "unchanged"

    monkeypatch.setattr(followup.gateway, "complete",
                        lambda *a, **k: _res("Show GMV per month for Samsung only"))
    draft = followup.rewrite("t1", FOLLOWUP, HISTORY)
    assert draft.question is None and draft.reason == "ungrounded:samsung"
    assert draft.usage["prompt_tokens"] == 40             # the rejected call is still billed

    monkeypatch.setattr(followup.gateway, "complete", lambda *a, **k: _res(""))
    assert followup.rewrite("t1", FOLLOWUP, HISTORY).reason == "empty"
    assert followup.rewrite("t1", FOLLOWUP, []).reason == "no_prior_turn"


# ---- orchestrator wiring ----------------------------------------------------------
def _quiet(*_args):
    """Progress callback that swallows status events."""


def test_a_draft_the_gatekeeper_rejects_is_discarded(monkeypatch):
    audits = []
    monkeypatch.setattr(orch.followup, "rewrite",
                        lambda *a, **k: followup.Draft(REWRITE, ADOPTED, MODEL, _usage(MODEL, 40, 12)))
    monkeypatch.setattr(orch.llm, "screen",
                        lambda *a, **k: _gate(intent="unsafe", allow=False, engine="llm:fake",
                                              prompt_tokens=9, completion_tokens=3))
    monkeypatch.setattr(orch.store, "log_audit",
                        lambda tid, trace, event, **kw: audits.append((event, kw)))
    gate = _gate()
    message, out_gate, usage, interpreted = orch._contextualize_followup(
        "t1", "tr", ROLE, "u", FOLLOWUP, HISTORY, gate, None, _quiet)
    assert (message, out_gate, interpreted) == (FOLLOWUP, gate, None)
    assert [u["model"] for u in usage] == [MODEL, "fake"]     # both calls metered
    assert audits == [(EVENT, {"role": ROLE, "user_id": "u", "model": MODEL, "status": "discarded",
                               "detail": {"reason": "rescreen:unsafe", "original": FOLLOWUP,
                                          "rewritten": REWRITE}})]


def test_an_adopted_draft_replaces_the_message_and_its_gate(monkeypatch):
    audits = []
    monkeypatch.setattr(orch.followup, "rewrite",
                        lambda *a, **k: followup.Draft(REWRITE, ADOPTED, MODEL, None))
    regate = _gate(engine="llm:fake", complexity="complex", prompt_tokens=9, completion_tokens=3)
    monkeypatch.setattr(orch.llm, "screen", lambda *a, **k: regate)
    monkeypatch.setattr(orch.store, "log_audit",
                        lambda tid, trace, event, **kw: audits.append((event, kw["status"])))
    typed = _gate(engine="llm:fake", prompt_tokens=7, completion_tokens=2)
    message, out_gate, usage, interpreted = orch._contextualize_followup(
        "t1", "tr", ROLE, "u", FOLLOWUP, HISTORY, typed, None, _quiet)
    assert message == REWRITE and out_gate is regate
    assert interpreted == {"original": FOLLOWUP, "question": REWRITE}
    assert usage == [_usage("fake", 7, 2)]                     # the typed message's screening
    assert audits == [(EVENT, ADOPTED)]


def test_not_a_candidate_costs_nothing(monkeypatch):
    monkeypatch.setattr(orch.followup, "rewrite",
                        lambda *a, **k: pytest.fail("rewrite must not be called"))
    gate = _gate()
    assert orch._contextualize_followup("t1", "tr", ROLE, "u", FIRST, HISTORY, gate, None,
                                        _quiet) == (FIRST, gate, [], None)


# ---- /v1/chat end to end (heuristic gatekeeper, rule-based SQL over a real DuckDB) --
@pytest.fixture
def world(tmp_path):
    import duckdb

    tid = store.create_tenant("FOLLOWUP-e2e")
    store.create_role(tid, ROLE, {"full_access": True, "can_see_pii": True})
    path = str(tmp_path / "acme.duckdb")
    con = duckdb.connect(path)
    con.execute("CREATE TABLE mp_orders(month_id INTEGER, gmv DOUBLE, brand VARCHAR)")
    con.execute("INSERT INTO mp_orders VALUES (202607, 250.0, 'Samsung'), (202608, 90.0, 'Sharp')")
    con.close()
    store.create_datasource(
        tid, "duckdb", "Acme", {"path": path},
        {"tables": {"mp_orders": ["month_id", "gmv", "brand"],
                    "clients": ["client_name", "region"]},
         "allowed_tables": ["mp_orders", "clients"]})
    key = client.post("/admin/apikeys", headers=AK,
                      json={"tenant_id": tid, "label": "pytest"}).json()["key"]
    token = pyjwt.encode({"sub": "u", "role": ROLE}, "test-secret", algorithm="HS256")
    yield {"tid": tid, "headers": {"X-API-Key": key, "Authorization": f"Bearer {token}"}}
    store.delete_tenant(tid)


def _chat(world, message, history):
    r = client.post("/v1/chat", headers=world["headers"],
                    json={"message": message, "history": history})
    assert r.status_code == 200, r.text
    return r.json()


def _audit(tid, event):
    return [r for r in store.list_audit(tid) if r["event"] == event]


def test_chat_answers_the_follow_up_as_the_standalone_question(world, monkeypatch):
    """The request contract, the plan, the audit trail and the reply all reflect the
    self-contained question; the typed text stays on the "question" audit event."""
    prior = [_turn("user", PRIOR_Q),
             _turn("assistant", "GMV bulanan per brand: Samsung 250, Sharp 90.")]
    monkeypatch.setattr(followup, "_ask_model", lambda *a, **k: _res(STANDALONE))
    out = _chat(world, TYPED, prior)
    assert out["interpreted_question"] == STANDALONE
    assert out["plan"][0] == "contextualize_followup"
    # the contract came from the rewritten question: client + brand, monthly — and the
    # delivered set (mp_orders) has no client column, which the status now NAMES
    assert out["status"] == "partial"
    assert "dimension:client" in out["missing_requirements"]
    answer = _audit(world["tid"], "answer")[0]
    assert answer["detail"]["request_contract"]["dimensions"] == ["client", "brand"]
    assert answer["detail"]["request_contract"]["time_bucket"] == "month"
    rewritten = _audit(world["tid"], EVENT)
    assert [r["status"] for r in rewritten] == [ADOPTED]
    assert rewritten[0]["detail"] == {"original": TYPED, "rewritten": STANDALONE}
    assert _audit(world["tid"], "question")[0]["detail"]["text"] == TYPED


def test_chat_without_a_model_answers_exactly_as_typed(world, monkeypatch):
    def down(*_a, **_k):
        raise RuntimeError("provider down")
    monkeypatch.setattr(followup, "_ask_model", down)
    out = _chat(world, TYPED, [_turn("user", PRIOR_Q)])
    assert out["interpreted_question"] is None
    assert "contextualize_followup" not in out["plan"]
    assert _audit(world["tid"], EVENT) == []
