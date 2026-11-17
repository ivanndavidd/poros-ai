"""The audit trail read as chat sessions, and the replica hints the agent gets.

A consumer app sends its own conversation id (``session_id``) with each chat turn; every
audit row of the turn carries it, so the console's Audit Log shows one session as one
conversation (question, answer, status, feedback) with the turn's events behind a click.
A turn without a session id stays its own session.
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store                                    # noqa: E402
from app.agent import kb_agent                           # noqa: E402
from app.context import chat_session_var, clean_session_id, set_chat_session  # noqa: E402
from app.main import app                                 # noqa: E402
from app.skills.registry import replica_advice           # noqa: E402

client = TestClient(app)
AK = {"X-Admin-Key": "admin-dev-key"}


def _turn(tid, trace, question, answer, status="complete", user="ana@x.co"):
    store.log_audit(tid, trace, "question", role="analyst", user_id=user,
                    detail={"text": question})
    store.log_audit(tid, trace, "query", role="analyst", user_id=user, status="ok",
                    detail={"safe_sql": "SELECT 1"})
    store.log_audit(tid, trace, "answer", role="analyst", user_id=user, model="m1",
                    status=status, cost=0.01,
                    detail={"answer": answer, "plan": ["agent_loop(agentic)", "run_sql"],
                            "confidence": "high"})


def test_a_session_reads_as_one_conversation_with_feedback():
    tid = client.post("/admin/tenants", headers=AK, json={"name": "Audit Sessions Co"}).json()["id"]
    token = chat_session_var.set("")
    try:
        set_chat_session("acme-session-1")
        _turn(tid, "t1", "GMV bulan ini?", "GMV Rp 10 M.")
        _turn(tid, "t2", "kalau per marketplace?", "Shopee Rp 6 M, TikTok Rp 4 M.", "partial")
        set_chat_session("")
        _turn(tid, "t3", "halo", "Halo!")                 # no session id: its own session
        store.upsert_feedback(tid, "t2", "ana@x.co", "analyst", "unhelpful", "incomplete",
                              "kurang Lazada")
        store.log_audit(tid, "t2", "feedback", role="analyst", user_id="ana@x.co",
                        status="unhelpful", session_id=store.audit_session_of(tid, "t2"))

        res = client.get(f"/admin/tenants/{tid}/conversations", headers=AK)
        assert res.status_code == 200
        sessions = res.json()
        assert [s["key"] for s in sessions] == ["t3", "acme-session-1"]   # newest first
        convo = sessions[1]
        assert convo["session_id"] == "acme-session-1" and convo["user_id"] == "ana@x.co"
        assert [t["question"] for t in convo["turns"]] == ["GMV bulan ini?",
                                                          "kalau per marketplace?"]
        second = convo["turns"][1]
        assert second["answer"].startswith("Shopee") and second["status"] == "partial"
        assert second["plan"] == ["agent_loop(agentic)", "run_sql"]
        assert second["queries"] == 1 and second["cost"] == pytest.approx(0.01)
        assert second["feedback"][0]["rating"] == "unhelpful"
        assert second["feedback"][0]["comment"] == "kurang Lazada"
        assert [e["event"] for e in second["events"]] == ["question", "query", "answer",
                                                         "feedback"]
        assert convo["feedback"] == {"helpful": 0, "unhelpful": 1}

        only = client.get(f"/admin/tenants/{tid}/conversations?user_id=nobody@x.co",
                          headers=AK).json()
        assert only == []
    finally:
        chat_session_var.reset(token)
        store.delete_tenant(tid)


def test_a_session_id_that_is_not_a_plain_token_is_dropped():
    assert clean_session_id(" s_1:abc ") == "s_1:abc"
    assert clean_session_id("x" * 129) == ""
    assert clean_session_id("a b") == ""
    assert clean_session_id("<script>") == ""
    assert clean_session_id(None) == ""


GMV_L2 = {"name": "GMV", "table": "acme_store_order_mp", "formula": "SUM(COALESCE(new_gmv, gmv))",
          "datasource_id": "ds_l2", "datasource_name": "Acme L2",
          "source_binding": {"role": "primary", "equivalence_contract_id": "acme_orders"}}
GMV_L3 = {"name": "GMV", "table": "acme_store_order_mp_ai_curated", "metric": "gmv",
          "aggregation": "SUM", "datasource_id": "ds_l3", "datasource_name": "Acme L3",
          "source_binding": {"role": "fallback", "equivalence_contract_id": "acme_orders"}}


def test_a_refused_scan_names_the_same_kpi_on_the_other_copy():
    sql = "SELECT marketplace, SUM(COALESCE(new_gmv, gmv)) FROM acme_store_order_mp GROUP BY 1"
    tip = replica_advice(sql, "bigquery", "ds_l2", [GMV_L2, GMV_L3])
    assert "acme_store_order_mp_ai_curated" in tip and "ds_l3" in tip
    assert "fallback" in tip and "SUM(gmv)" in tip
    # a table no concept names, or a concept without replicas, gives no hint
    assert replica_advice("SELECT 1 FROM other_table", "bigquery", "ds_l2",
                          [GMV_L2, GMV_L3]) == ""
    assert replica_advice(sql, "bigquery", "ds_l2", [{**GMV_L2, "source_binding": {}}]) == ""


def test_definitions_say_which_copy_is_primary():
    assert kb_agent._replica(GMV_L2)["copy"] == "primary"
    assert kb_agent._replica(GMV_L3)["copy"] == "fallback"
    assert "fallback" in kb_agent._replica(GMV_L3)["note"]
    assert kb_agent._replica({"name": "NMV"}) == {}
