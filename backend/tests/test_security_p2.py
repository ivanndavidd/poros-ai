"""Focused offline/local-Postgres regression tests for P2 hardening."""
import asyncio
import io
import json
import os
import uuid

import pytest
from cryptography.exceptions import InvalidTag
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import document_crypto, network_policy, store, upload_security
from app.console_auth import check_login_allowed, note_login_result
from app.main import app


store.init()
client = TestClient(app)


def _clean_tenant(tenant_id):
    c = store._c()
    for table in ("usage_log", "audit_log"):
        c.execute(f"DELETE FROM {table} WHERE tenant_id=?", (tenant_id,))
    c.commit()
    c.close()
    if store.get_tenant(tenant_id):
        store.delete_tenant(tenant_id)


def test_api_base_policy_keeps_local_dev_but_blocks_metadata(monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.delenv("LLM_API_BASE_ALLOWLIST", raising=False)
    monkeypatch.delenv("LLM_ALLOW_PRIVATE_BASE", raising=False)
    assert network_policy.validate_api_base("http://localhost:11434/v1") == (
        "http://localhost:11434/v1"
    )
    with pytest.raises(network_policy.UnsafeAPIBase):
        network_policy.validate_api_base("http://169.254.169.254/latest")


def test_request_id_is_bounded_and_security_headers_are_present():
    supplied = "x" * 200
    response = client.get(
        "/dangerously-skip-login/health",
        headers={"X-Request-Id": supplied},
    )
    assert response.status_code == 200
    assert response.headers["x-request-id"].startswith("req-")
    assert response.headers["x-request-id"] != supplied
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "same-origin"
    assert "camera=()" in response.headers["permissions-policy"]


def test_production_api_base_allowlist_is_exact(monkeypatch):
    public = {network_policy.ipaddress.ip_address("8.8.8.8")}
    monkeypatch.setattr(network_policy, "_addresses", lambda host, port: public)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LLM_API_BASE_ALLOWLIST", "https://api.example.test")
    monkeypatch.delenv("LLM_ALLOW_INSECURE_BASE", raising=False)
    assert network_policy.validate_api_base("https://api.example.test/v1").startswith(
        "https://api.example.test"
    )
    with pytest.raises(network_policy.UnsafeAPIBase):
        network_policy.validate_api_base("https://api.example.test:8443/v1")
    with pytest.raises(network_policy.UnsafeAPIBase):
        network_policy.validate_api_base("https://other.example.test/v1")


class _AsyncUpload:
    def __init__(self, data):
        self.source = io.BytesIO(data)

    async def read(self, size=-1):
        return self.source.read(size)


def test_upload_is_bounded_and_signature_checked(monkeypatch):
    monkeypatch.setenv("DOCUMENT_MAX_UPLOAD_MB", "1")
    with pytest.raises(upload_security.UploadRejected) as too_large:
        asyncio.run(upload_security.read_bounded(_AsyncUpload(b"x" * (1024 * 1024 + 1))))
    assert too_large.value.code == "FILE_TOO_LARGE"
    with pytest.raises(upload_security.UploadRejected) as bad_pdf:
        upload_security.validate("report.pdf", "application/pdf", b"not a PDF")
    assert bad_pdf.value.code == "FILE_SIGNATURE_INVALID"
    assert upload_security.validate("notes.md", "text/plain", b"# valid\n") == (
        "notes.md",
        "text/markdown",
    )


def test_shared_rate_bucket_and_persistent_login_lockout():
    marker = uuid.uuid4().hex
    bucket = "p2-rate-" + marker
    store.reset_rate_limit(bucket)
    try:
        assert store.consume_rate_limit("t-rate", bucket, 2, now_ts=1000)[0] is True
        assert store.consume_rate_limit("t-rate", bucket, 2, now_ts=1000)[0] is True
        denied = store.consume_rate_limit("t-rate", bucket, 2, now_ts=1000)
        assert denied[0] is False and denied[1] > 0
        assert store.consume_rate_limit("t-rate", bucket, 2, now_ts=1030)[0] is True
    finally:
        store.reset_rate_limit(bucket)

    email = f"{marker}@p2-test.local"
    for _ in range(5):
        note_login_result(email, False)
    with pytest.raises(HTTPException) as locked:
        check_login_allowed(email)
    assert locked.value.status_code == 429
    note_login_result(email, True)
    check_login_allowed(email)


def test_document_original_is_encrypted_and_tenant_bound():
    tenant_id = store.create_tenant("P2 encrypted document")
    doc_id = store.create_document(
        tenant_id, "", "secret.md", "text/markdown", [], 0
    )
    plaintext = b"# Internal\nrevenue plan"
    try:
        path = store.save_document_file(tenant_id, doc_id, "secret.md", plaintext)
        with open(path, "rb") as source:
            raw = source.read()
        assert document_crypto.encrypted(raw)
        assert plaintext not in raw
        assert store.read_document_file(tenant_id, doc_id) == plaintext
        with pytest.raises(InvalidTag):
            document_crypto.decrypt(raw, tenant_id + "-other")
        assert store.document_storage_bytes(tenant_id) == len(plaintext)
    finally:
        _clean_tenant(tenant_id)


def test_console_chat_session_payload_is_encrypted_and_owner_scoped():
    tenant_id = store.create_tenant("P2 encrypted chat")
    session = {
        "id": "session-" + uuid.uuid4().hex,
        "title": "Revenue question",
        "created": "2026-07-24T10:00:00",
        "updated": "2026-07-24T10:01:00",
        "messages": [{"role": "user", "content": "berapa revenue rahasia?"}],
    }
    try:
        store.upsert_console_chat_session(tenant_id, "owner-1", session)
        c = store._c()
        raw = c.execute(
            "SELECT payload FROM console_chat_sessions WHERE tenant_id=?",
            (tenant_id,),
        ).fetchone()["payload"]
        c.close()
        assert raw.startswith(("enc:", "vault:"))
        assert "revenue rahasia" not in raw
        assert store.list_console_chat_sessions(tenant_id, "owner-2") == []
        loaded = store.list_console_chat_sessions(tenant_id, "owner-1")
        assert loaded[0]["messages"] == session["messages"]
    finally:
        _clean_tenant(tenant_id)


def test_monitoring_attributes_current_and_legacy_usage_without_double_count():
    tenant_id = store.create_tenant("P2 per-user monitoring")
    try:
        store.log_usage(
            tenant_id, "key-current", "viewer", "chat", 0, "OK",
            model="model-a", prompt_tokens=10, completion_tokens=5, cost=0.01,
            user_id="consumer-user-1", trace_id="trace-current",
        )
        store.log_audit(
            tenant_id, "trace-current", "answer", role="viewer",
            user_id="consumer-user-1", status="ok", cost=0.01,
            detail={"prompt_tokens": 10, "completion_tokens": 5},
        )
        store.log_usage(
            tenant_id, "key-legacy", "viewer", "chat", 0, "OK",
            model="model-a", prompt_tokens=7, completion_tokens=3, cost=0.02,
            user_id="", trace_id="trace-legacy",
        )
        store.log_audit(
            tenant_id, "trace-legacy", "answer", role="viewer",
            user_id="consumer-user-1", status="ok", cost=0.02,
            detail={"prompt_tokens": 7, "completion_tokens": 3},
        )
        result = store.monitoring_summary()
        user = next(
            row for row in result["per_user"]
            if row["tenant_id"] == tenant_id and row["user_id"] == "consumer-user-1"
        )
        tenant = next(row for row in result["per_tenant"] if row["id"] == tenant_id)
        assert user["tokens"] == 25
        assert user["requests"] == 2
        assert user["cost"] == pytest.approx(0.03)
        assert tenant["tokens"] == 25
        assert tenant["requests"] == 2
    finally:
        _clean_tenant(tenant_id)
