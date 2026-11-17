"""Regression tests for the July 2026 P0 security hardening."""
import pytest

from app import store
from app.governance._compat import WrenError
from app.governance.engine import govern
from app.skills.registry import (_evidence_text, mask_pii_for_llm,
                                 redact_text_for_llm)


MDL = {
    "tables": {"orders": ["amount", "created_at"], "customers": ["email", "region"]},
    "allowed_tables": ["orders", "customers"],
    "pii_columns": ["email"],
    "denied_functions": ["version", "current_user"],
    "allowed_functions": [],
}
ROLE = {"full_access": True, "can_see_pii": True}


@pytest.fixture(scope="module", autouse=True)
def _init_store():
    store.init()


def _blocked(sql, code, mdl=None):
    with pytest.raises(WrenError) as exc:
        govern(sql, "postgres", mdl or MDL, ROLE)
    assert exc.value.code == code


def test_sql_requires_a_real_mdl_table():
    _blocked("SELECT 1", "TABLE_REQUIRED")
    _blocked("WITH constants AS (SELECT 1 AS n) SELECT n FROM constants", "TABLE_REQUIRED")
    assert "FROM orders" in govern("SELECT SUM(amount) FROM orders", "postgres", MDL, ROLE)


def test_sql_function_allowlist_blocks_side_effects_and_unknowns():
    assert "CASE" in govern(
        "SELECT SUM(CASE WHEN amount > 0 AND amount < 100 THEN amount ELSE 0 END) FROM orders",
        "postgres", MDL, ROLE,
    )
    _blocked("SELECT pg_sleep(1) FROM orders", "BLOCKED_FUNCTION")
    _blocked("SELECT mystery_bucket(amount) FROM orders", "FUNCTION_NOT_ALLOWED")
    custom = {**MDL, "allowed_functions": ["mystery_bucket"]}
    assert "MYSTERY_BUCKET" in govern(
        "SELECT mystery_bucket(amount) FROM orders", "postgres", custom, ROLE
    )
    hard = {**MDL, "allowed_functions": ["pg_sleep"]}
    _blocked("SELECT pg_sleep(1) FROM orders", "BLOCKED_FUNCTION", hard)


def test_pii_alias_lineage_and_text_evidence_are_redacted():
    cols, rows = mask_pii_for_llm(
        ["contact", "region"],
        [["alpha.lab03@example.com", "Jakarta"]],
        MDL,
        "SELECT email AS contact, region FROM customers",
        "postgres",
    )
    assert rows == [["[PII:contact]", "Jakarta"]]

    evidence = {
        "sql": {
            "ok": True,
            "columns": cols,
            "rows": [["alpha.lab03@example.com", "Jakarta"]],
            "safe_sql": (
                "SELECT email AS contact, region FROM customers "
                "WHERE email='alpha.lab03@example.com'"
            ),
            "dialect": "postgres",
            "pii_columns": ["email"],
        },
        "docs": {
            "results": [{
                "citation": "doc#1",
                "citation_label": "policy.txt — bagian 1",
                "text": "Kontak alpha.lab03@example.com, telepon +62 812-3456-7890.",
            }]
        },
        "memory": {
            "results": [{
                "text": "Email favorit alpha.lab03@example.com; NIK 3174010101010001."
            }]
        },
    }
    prompt = _evidence_text(evidence, MDL)
    assert "alpha.lab03@example.com" not in prompt
    assert "+62 812-3456-7890" not in prompt
    assert "3174010101010001" not in prompt
    assert "[PII:contact]" in prompt
    # repeated identity receives the same stable token inside one evidence block
    assert prompt.count("[PII:EMAIL_1]") >= 2
    assert "alpha.lab03@example.com" not in redact_text_for_llm(
        "email alpha.lab03@example.com"
    )


def test_api_keys_are_hashed_and_usage_never_stores_plaintext():
    tid = store.create_tenant("P0 key test")
    raw = store.create_api_key(tid, label="consumer")
    resolved = store.get_api_key(raw)
    assert resolved and resolved["key"] != raw
    assert resolved["key_prefix"].startswith("sk_")
    assert "key_hash" not in resolved

    listed = store.list_api_keys(tid)
    assert listed[0]["key"] == resolved["key"]
    assert listed[0]["key"] != raw
    assert "key_hash" not in listed[0]

    c = store._c()
    dbrow = c.execute(
        "SELECT key,key_hash,key_prefix FROM api_keys WHERE key=?", (resolved["key"],)
    ).fetchone()
    c.close()
    assert raw not in dict(dbrow).values()
    assert len(dbrow["key_hash"]) == 64

    store.log_usage(tid, raw, "analyst", "chat", 0, "OK")
    c = store._c()
    usage = c.execute(
        "SELECT api_key FROM usage_log WHERE tenant_id=? ORDER BY id DESC LIMIT 1", (tid,)
    ).fetchone()
    c.close()
    assert usage["api_key"] == resolved["key"]
    assert usage["api_key"] != raw


def test_plaintext_api_key_migration_preserves_consumer_secret_and_scrubs_usage():
    tid = store.create_tenant("P0 migration test")
    raw = "sk_legacy_plaintext_key_for_migration"
    c = store._c()
    c.execute(
        """INSERT INTO api_keys(
               key,tenant_id,role,label,created,active,env,rate_limit,expiry,key_hash,key_prefix
           ) VALUES(?,?,?,?,?,1,?,?,?,?,?)""",
        (raw, tid, "", "legacy", store.now(), "Production", None, None, None, None),
    )
    c.execute(
        """INSERT INTO usage_log(tenant_id,api_key,role,action,rows,status,ts)
           VALUES(?,?,?,?,?,?,?)""",
        (tid, raw, "", "chat", 0, "OK", store.now()),
    )
    c.commit()
    c.close()

    store.init()
    resolved = store.get_api_key(raw)
    assert resolved and resolved["key"] != raw
    c = store._c()
    assert c.execute("SELECT COUNT(*) n FROM api_keys WHERE key=?", (raw,)).fetchone()["n"] == 0
    assert c.execute(
        "SELECT COUNT(*) n FROM usage_log WHERE api_key=?", (raw,)
    ).fetchone()["n"] == 0
    c.close()


def test_cors_is_origin_scoped_and_only_gateway_public_health_exists():
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    allowed = client.options(
        "/admin/whoami",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-admin-key,x-request-id",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"

    denied = client.options(
        "/admin/whoami",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers
    assert client.get("/health").status_code == 404
    assert client.get("/dangerously-skip-login/health").status_code == 200


def test_cors_allows_every_method_the_admin_routes_actually_use():
    """PUT was absent from allow_methods, so the role-grants editor (PUT
    roles/{name}/businesses) failed on the PREFLIGHT with a 400 and no request was ever
    sent. It went unnoticed because the production bundle is served from the app's own
    origin, where CORS does not apply at all -- only local dev and split-origin
    deployments hit it. A method list has to match the routes, so it is asserted here.
    """
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        r = client.options(
            "/admin/tenants/t/roles/analyst/businesses",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": "content-type,x-admin-key",
            },
        )
        assert r.status_code == 200, f"{method} preflight rejected: {r.text}"
        assert method in r.headers.get("access-control-allow-methods", ""), method


def test_cors_allows_the_idempotency_key_header():
    """Same failure mode as the PUT gap, for a HEADER: a split-origin consumer sending
    Idempotency-Key on /v1/chat/jobs (the design doc's dedup seam) must survive the
    preflight, or the browser never sends the request at all."""
    from fastapi.testclient import TestClient
    from app.main import app

    client = TestClient(app)
    r = client.options(
        "/v1/chat/jobs",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-api-key,authorization,idempotency-key",
        },
    )
    assert r.status_code == 200, r.text
    assert "idempotency-key" in r.headers.get("access-control-allow-headers", "").lower()


def test_production_docs_and_cors_are_closed_by_default(monkeypatch):
    from app import runtime_config

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("EXPOSE_API_DOCS", raising=False)
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    assert runtime_config.docs_enabled() is False
    assert runtime_config.cors_origins() == []

    monkeypatch.setenv("CORS_ORIGINS", "*")
    with pytest.raises(RuntimeError):
        runtime_config.cors_origins()
    monkeypatch.setenv("CORS_ORIGINS", "https://poros.example.com")
    assert runtime_config.cors_origins() == ["https://poros.example.com"]
