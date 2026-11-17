"""Consumer response feedback: trusted trace binding, tenant isolation, and upsert."""
import jwt
import pytest
from fastapi.testclient import TestClient

from app import store
from app.main import app


client = TestClient(app)
AK = {"X-Admin-Key": "admin-dev-key"}


def _tenant(name):
    tid = store.create_tenant(name)
    store.create_role(tid, "viewer", {"authorized_tables": []})
    key = store.create_api_key(tid, label="feedback-test")
    return tid, key


def _headers(key, user="feedback-user", include_sub=True):
    claims = {"role": "viewer"}
    if include_sub:
        claims["sub"] = user
    token = jwt.encode(claims, "test-secret", algorithm="HS256")
    return {"X-API-Key": key, "Authorization": f"Bearer {token}"}


@pytest.fixture
def feedback_tenants():
    a = _tenant("FB-A")
    b = _tenant("FB-B")
    yield a, b
    store.delete_tenant(a[0])
    store.delete_tenant(b[0])


def _audit_answer(tid, trace="trace-feedback-1"):
    store.log_audit(tid, trace, "question", role="viewer", user_id="feedback-user",
                    detail={"text": "Berapa total penjualan?"})
    store.log_audit(tid, trace, "answer", role="viewer", user_id="feedback-user",
                    status="ok", detail={"answer": "Total penjualan adalah 100."})
    return trace


def test_feedback_uses_trusted_audit_question_answer_and_is_listed(feedback_tenants):
    (tid, key), _ = feedback_tenants
    trace = _audit_answer(tid)
    r = client.post("/v1/feedback", headers=_headers(key), json={
        "trace_id": trace,
        "rating": "unhelpful",
        "reason": "incorrect",
        "comment": "Seharusnya 120.",
        "question": "PERTANYAAN PALSU",
        "answer": "JAWABAN PALSU",
    })
    assert r.status_code == 200, r.text
    rows = client.get(f"/admin/tenants/{tid}/feedback", headers=AK).json()
    assert len(rows) == 1
    assert rows[0]["question"] == "Berapa total penjualan?"
    assert rows[0]["answer"] == "Total penjualan adalah 100."
    assert rows[0]["rating"] == "unhelpful"
    assert rows[0]["comment"] == "Seharusnya 120."


def test_same_user_and_trace_updates_instead_of_duplicating(feedback_tenants):
    (tid, key), _ = feedback_tenants
    trace = _audit_answer(tid, "trace-upsert")
    first = client.post("/v1/feedback", headers=_headers(key), json={
        "trace_id": trace, "rating": "unhelpful", "reason": "incomplete",
    }).json()
    second = client.post("/v1/feedback", headers=_headers(key), json={
        "trace_id": trace, "rating": "helpful", "comment": "Sudah diperbaiki.",
    }).json()
    rows = store.list_feedback(tid)
    assert len(rows) == 1
    assert first["id"] == second["id"] == rows[0]["id"]
    assert rows[0]["rating"] == "helpful"
    assert rows[0]["reason"] == ""
    assert rows[0]["comment"] == "Sudah diperbaiki."


def test_feedback_trace_cannot_cross_tenants(feedback_tenants):
    (tid_a, _), (_, key_b) = feedback_tenants
    trace = _audit_answer(tid_a, "trace-tenant-a")
    r = client.post("/v1/feedback", headers=_headers(key_b), json={
        "trace_id": trace, "rating": "helpful",
    })
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "FEEDBACK_TARGET_NOT_FOUND"


def test_feedback_validates_rating_reason_and_user_sub(feedback_tenants):
    (tid, key), _ = feedback_tenants
    trace = _audit_answer(tid, "trace-validation")
    bad_rating = client.post("/v1/feedback", headers=_headers(key), json={
        "trace_id": trace, "rating": "five-stars",
    })
    assert bad_rating.status_code == 400
    assert bad_rating.json()["detail"]["code"] == "FEEDBACK_RATING_INVALID"

    bad_reason = client.post("/v1/feedback", headers=_headers(key), json={
        "trace_id": trace, "rating": "unhelpful", "reason": "made-up",
    })
    assert bad_reason.status_code == 400
    assert bad_reason.json()["detail"]["code"] == "FEEDBACK_REASON_INVALID"

    no_sub = client.post("/v1/feedback", headers=_headers(key, include_sub=False), json={
        "trace_id": trace, "rating": "helpful",
    })
    assert no_sub.status_code == 403
    assert no_sub.json()["detail"]["code"] == "FEEDBACK_USER_REQUIRED"
