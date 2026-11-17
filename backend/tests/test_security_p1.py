"""Focused regression tests for P1 hardening."""
import os
import time
import uuid

import jwt
import psycopg2
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import auth, store
from app.llm import gateway

store.init()


def _delete_tenant_config(tenant_id):
    c = store._c()
    c.execute("DELETE FROM tenant_config WHERE tenant_id=?", (tenant_id,))
    c.execute("DELETE FROM tenant_budgets WHERE tenant_id=?", (tenant_id,))
    c.execute("DELETE FROM budget_reservations WHERE tenant_id=?", (tenant_id,))
    c.execute("DELETE FROM usage_log WHERE tenant_id=?", (tenant_id,))
    c.commit(); c.close()


def test_production_jwt_requires_asymmetric_issuer_and_audience(monkeypatch):
    tenant_id = "p1-jwt-" + uuid.uuid4().hex[:8]
    now = int(time.time())
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("JWT_ALLOW_HS256_STAGING", raising=False)
    store.set_jwt_config(tenant_id, {
        "secret": "legacy-shared-secret",
        "algorithms": ["HS256"],
        "issuer": "https://issuer.example/",
        "audience": "poros-consumer",
    })
    token = jwt.encode(
        {"sub": "u1", "role": "viewer", "iat": now, "exp": now + 300,
         "iss": "https://issuer.example/", "aud": "poros-consumer"},
        "legacy-shared-secret",
        algorithm="HS256",
    )
    assert auth.verify_end_user(tenant_id, token)["error"] == "JWT_ASYMMETRIC_REQUIRED"
    _delete_tenant_config(tenant_id)


def test_production_allows_explicit_tenant_hs256_for_staging(monkeypatch):
    tenant_id = "p1-jwt-staging-" + uuid.uuid4().hex[:8]
    issuer = "consumer-app-staging"
    audience = "poros-consumer-app"
    secret = uuid.uuid4().hex + uuid.uuid4().hex
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("JWT_ALLOW_HS256_STAGING", "1")
    cfg = {
        "secret": secret,
        "algorithms": ["HS256"],
        "issuer": issuer,
        "audience": audience,
        "role_claim": "role",
    }
    assert auth.validate_jwt_config(cfg) is True
    store.set_jwt_config(tenant_id, cfg)
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "acme-user@example.test",
            "role": "Admin",
            "iat": now,
            "exp": now + 300,
            "iss": issuer,
            "aud": audience,
        },
        secret,
        algorithm="HS256",
    )

    verified = auth.verify_end_user(tenant_id, token)

    assert verified["verified"] is True
    assert verified["user_id"] == "acme-user@example.test"
    assert verified["claims"]["iss"] == issuer
    assert verified["claims"]["aud"] == audience
    _delete_tenant_config(tenant_id)


def test_staging_hs256_exception_never_uses_global_secret(monkeypatch):
    tenant_id = "p1-jwt-no-tenant-secret-" + uuid.uuid4().hex[:8]
    issuer = "consumer-app-staging"
    audience = "poros-consumer-app"
    global_secret = uuid.uuid4().hex + uuid.uuid4().hex
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("JWT_ALLOW_HS256_STAGING", "1")
    monkeypatch.setenv("JWT_SECRET", global_secret)
    store.set_jwt_config(tenant_id, {
        "algorithms": ["HS256"],
        "issuer": issuer,
        "audience": audience,
    })
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "u1",
            "role": "Admin",
            "iat": now,
            "exp": now + 300,
            "iss": issuer,
            "aud": audience,
        },
        global_secret,
        algorithm="HS256",
    )

    assert auth.verify_end_user(tenant_id, token)["error"] == "JWT_ASYMMETRIC_REQUIRED"
    _delete_tenant_config(tenant_id)


def test_production_rs256_public_key_verification(monkeypatch):
    tenant_id = "p1-rsa-" + uuid.uuid4().hex[:8]
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    issuer = "https://issuer.example/"
    audience = "poros-consumer"
    store.set_jwt_config(tenant_id, {
        "public_key": public_pem,
        "algorithms": ["RS256"],
        "issuer": issuer,
        "audience": audience,
        "role_claim": "groups",
    })
    monkeypatch.setenv("APP_ENV", "production")
    now = int(time.time())
    token = jwt.encode(
        {"sub": "user-1", "groups": ["viewer"], "iat": now, "exp": now + 300,
         "iss": issuer, "aud": audience},
        private_pem,
        algorithm="RS256",
    )
    verified = auth.verify_end_user(tenant_id, token)
    assert verified["verified"] is True
    assert verified["user_id"] == "user-1"
    assert verified["role_claim"] == "groups"
    _delete_tenant_config(tenant_id)


def test_budget_reservation_is_atomic_headroom(monkeypatch):
    tenant_id = "p1-budget-" + uuid.uuid4().hex[:8]
    store.set_budget(tenant_id, 1.0)
    store.log_usage(tenant_id, "internal-key", "viewer", "chat", 0, "OK", cost=0.80)
    first = store.reserve_budget(tenant_id, amount=0.10)
    assert first and first["amount"] == pytest.approx(0.10)
    with pytest.raises(store.BudgetExceeded):
        store.reserve_budget(tenant_id, amount=0.15)
    store.release_budget(first["id"])
    second = store.reserve_budget(tenant_id, amount=0.15)
    assert second
    store.release_budget(second["id"])
    _delete_tenant_config(tenant_id)


def test_llm_gateway_blocks_unreserved_background_call_over_budget(monkeypatch):
    tenant_id = "p1-llm-budget-" + uuid.uuid4().hex[:8]
    store.set_budget(tenant_id, 0.01)
    called = {"value": False}

    def fake(*args, **kwargs):
        called["value"] = True
        return gateway.LLMResult(text="must not run", model="fake", provider="fake")

    monkeypatch.setattr(gateway, "_complete_unbudgeted", fake)
    with pytest.raises(store.BudgetExceeded):
        gateway.complete(tenant_id, "answer_simple", [{"role": "user", "content": "x"}])
    assert called["value"] is False
    _delete_tenant_config(tenant_id)


def test_admin_audit_chain_is_append_only():
    marker = uuid.uuid4().hex
    first_id = store.log_admin_audit(
        "req-" + marker + "-1",
        {"user_id": "u1", "email": "admin@example.test", "role": "admin", "via": "session"},
        "PATCH", "/admin/tenants/t1", 200, tenant_id="t1", source_ip="127.0.0.1",
        user_agent="pytest",
    )
    second_id = store.log_admin_audit(
        "req-" + marker + "-2",
        {"user_id": "u1", "email": "admin@example.test", "role": "admin", "via": "session"},
        "DELETE", "/admin/tenants/t1", 200, tenant_id="t1", source_ip="127.0.0.1",
        user_agent="pytest",
    )
    rows = {row["event_id"]: row for row in store.list_admin_audit(1000)}
    assert rows[second_id]["previous_hash"] == rows[first_id]["event_hash"]
    assert len(rows[first_id]["event_hash"]) == 64


def test_rls_policies_are_installed(monkeypatch):
    schema = "p1_" + uuid.uuid4().hex[:12]
    admin = psycopg2.connect(store.DB_URL)
    admin.autocommit = True
    admin.cursor().execute(f'CREATE SCHEMA "{schema}"')
    admin.close()
    try:
        monkeypatch.setenv("ADMIN_DB_SCHEMA", schema)
        monkeypatch.setenv("ADMIN_DB_RLS", "1")
        store.init()
        c = store._c()
        row = c.execute(
            """SELECT relrowsecurity,relforcerowsecurity
               FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname=? AND c.relname='datasources'""",
            (schema,),
        ).fetchone()
        policy = c.execute(
            """SELECT policyname FROM pg_policies
               WHERE schemaname=? AND tablename='datasources'""",
            (schema,),
        ).fetchone()
        c.close()
        assert row["relrowsecurity"] is True and row["relforcerowsecurity"] is True
        assert policy["policyname"] == "poros_tenant_isolation_datasources"
    finally:
        admin = psycopg2.connect(store.DB_URL)
        admin.autocommit = True
        admin.cursor().execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()
