"""Regression coverage for no-SQL conversational and clarification routing when NO
tool-capable model is configured (the deterministic fallback). With a model, the one chat
agent answers every turn itself: see test_chat_overhaul.py section 3."""
import json
from types import SimpleNamespace

from app.agent import orchestrator
from app.llm import gatekeeper


def _llm_result(intent):
    text = json.dumps({"allow": True, "intent": intent, "complexity": "simple",
                       "injection": False, "pii_request": False, "reasons": []})
    return SimpleNamespace(text=text, model="fake-gate", provider="fake",
                           prompt_tokens=1, completion_tokens=1, cost=0.0)


def test_heuristic_routes_conversation_and_vague_requests_without_data():
    for message in ("test", "tes", "ping", "halo", "terima kasih", "who are you?"):
        assert gatekeeper._heuristic(message).intent == "smalltalk", message
    for message in ("", "cek", "coba", "yang mana", "..."):
        assert gatekeeper._heuristic(message).intent == "ambiguous", message


def test_heuristic_separates_document_data_and_mixed_questions():
    assert gatekeeper._heuristic("apa isi SOP retur barang?").intent == "doc_question"
    assert gatekeeper._heuristic("berapa langkah dalam SOP retur?").intent == "doc_question"
    assert gatekeeper._heuristic("berapa total penjualan bulan ini?").intent == "data_query"
    assert gatekeeper._heuristic("bandingkan penjualan dengan kebijakan target").intent == "data_query"


def test_expansion_and_potential_area_are_prescriptive_decisions():
    decision = gatekeeper._heuristic(
        "Kalau mau ekspansi gudang, area mana yang potensial untuk Acme?",
    )
    assert decision.intent == "prescriptive"
    assert decision.complexity == "complex"


def test_screen_deterministically_keeps_test_out_of_data_query(monkeypatch):
    monkeypatch.setattr(gatekeeper.gateway, "complete",
                        lambda *args, **kwargs: _llm_result("data_query"))
    assert gatekeeper.screen("t_x", "test").intent == "smalltalk"
    assert gatekeeper.screen("t_x", "cek").intent == "ambiguous"


def test_sql_is_opt_in_by_intent():
    assert orchestrator._requires_sql("data_query")
    assert orchestrator._requires_sql("diagnostic")
    assert orchestrator._requires_sql("prescriptive")
    assert not orchestrator._requires_sql("doc_question")
    assert not orchestrator._requires_sql("smalltalk")
    assert not orchestrator._requires_sql("ambiguous")


def test_smalltalk_run_agent_never_discovers_or_calls_data_tools(monkeypatch):
    audits, statuses = [], []
    gate = gatekeeper.GateDecision(True, "smalltalk", "simple", engine="heuristic")
    monkeypatch.setattr(orchestrator.llm, "screen", lambda *_: gate)
    monkeypatch.setattr(orchestrator.store, "log_audit",
                        lambda *args, **kwargs: audits.append((args, kwargs)))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("smalltalk must terminate before data/evidence tools")

    monkeypatch.setattr(orchestrator, "_pick_sql_ds", forbidden)
    monkeypatch.setattr(orchestrator, "run_skill", forbidden)
    monkeypatch.setattr(orchestrator.memory, "get_cfg", forbidden)

    out = orchestrator.run_agent({"tenant_id": "t_x", "role": "Admin"},
                                 "test", user_id="u_x", emit=statuses.append)

    assert out["answer"].startswith("Sistem aktif")
    assert out["sql"] is None
    assert out["plan"] == ["direct_response"]
    assert out["gatekeeper"]["intent"] == "smalltalk"
    assert all("SQL" not in event.get("text", "") for event in statuses)
    assert [args[2] for args, _ in audits] == ["question", "answer"]


def test_ambiguous_run_agent_requests_clarification_without_sql(monkeypatch):
    audits, statuses = [], []
    gate = gatekeeper.GateDecision(True, "ambiguous", "simple", engine="heuristic")
    monkeypatch.setattr(orchestrator.llm, "screen", lambda *_: gate)
    monkeypatch.setattr(orchestrator.store, "log_audit",
                        lambda *args, **kwargs: audits.append((args, kwargs)))
    monkeypatch.setattr(orchestrator, "_pick_sql_ds",
                        lambda *_: (_ for _ in ()).throw(AssertionError("SQL discovery called")))

    out = orchestrator.run_agent({"tenant_id": "t_x", "role": "Admin"},
                                 "cek", user_id="u_x", emit=statuses.append)

    assert "Sebutkan data atau dokumen" in out["answer"]
    assert out["sql"] is None
    assert out["plan"] == ["request_clarification"]
    assert all("SQL" not in event.get("text", "") for event in statuses)


def test_document_miss_does_not_fall_back_to_sql(monkeypatch):
    audits, skills = [], []
    gate = gatekeeper.GateDecision(True, "doc_question", "simple", engine="heuristic")
    monkeypatch.setattr(orchestrator.llm, "screen", lambda *_: gate)
    monkeypatch.setattr(orchestrator.llm, "pick_answer_purpose",
                        lambda *_: "answer_simple")
    monkeypatch.setattr(orchestrator.store, "log_audit",
                        lambda *args, **kwargs: audits.append((args, kwargs)))
    monkeypatch.setattr(orchestrator.store, "get_role",
                        lambda *_: {"policy": {"skills": ["search_docs", "generate_report", "run_sql"]}})
    monkeypatch.setattr(orchestrator.store, "list_documents", lambda *_: [])
    monkeypatch.setattr(orchestrator.memory, "get_cfg",
                        lambda *_: {"persistent": False, "cross_session_recall": False,
                                    "auto_memory": False})
    monkeypatch.setattr(orchestrator, "_pick_sql_ds",
                        lambda *_: (_ for _ in ()).throw(AssertionError("SQL discovery called")))

    def fake_skill(name, *_args, **_kwargs):
        skills.append(name)
        if name == "generate_report":
            return {"text": "Dokumen yang relevan belum tersedia."}
        raise AssertionError(f"unexpected evidence tool: {name}")

    monkeypatch.setattr(orchestrator, "run_skill", fake_skill)
    monkeypatch.setattr(orchestrator, "verify",
                        lambda *_a, **_k: {"ok": True, "issues": [], "checks": {"grounded": True}})
    monkeypatch.setattr(orchestrator, "compute_confidence",
                        lambda *_: {"level": "medium", "score": 70, "signals": {}})

    out = orchestrator.run_agent({"tenant_id": "t_x", "role": "Admin"},
                                 "apa isi SOP retur?")

    assert out["sql"] is None
    assert out["citations"] == []
    assert skills == ["generate_report"]
    assert all(args[2] != "query" for args, _ in audits)
