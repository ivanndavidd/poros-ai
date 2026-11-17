"""Login panel + role isolation tests (console auth).

Covers: local email+password sessions, the legacy master-key fallback, platform-admin
vs tenant_manager visibility (direct ownership plus tenant-group assignment), session
revocation on deactivate, the self-lockout guard, and the config-gated SSO seam.
"""
import os, secrets, sys, tempfile
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

_TMP = tempfile.mkdtemp(prefix="adatest_")
os.environ.setdefault("ADMIN_DB_URL", "postgresql://postgres:postgres@localhost:5432/poros_test")
os.environ.setdefault("ADMIN_SAMPLE_DB", os.path.join(_TMP, "sample.duckdb"))
os.environ.setdefault("JWT_SECRET", "test-secret")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from app import store  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import console as console_router  # noqa: E402

client = TestClient(app)
AK = {"X-Admin-Key": "admin-dev-key"}


@pytest.fixture(autouse=True)
def _cleanup():
    """Console tests must not leave tenants behind: test_smoke picks tenants[0] from the
    GLOBAL list, so a stray 'CA-*' tenant could hijack it. Users are cleaned too."""
    client.cookies.clear()
    yield
    client.cookies.clear()
    for group in store.list_tenant_groups():
        if group["name"].startswith("CA-"):
            store.delete_tenant_group(group["id"])
    for t in store.list_tenants():
        if t["name"].startswith("CA-"):
            store.delete_tenant(t["id"])
    for u in store.list_console_users():
        if u["email"].endswith("@ca-test.local"):
            store.delete_console_user(u["id"])


def _bearer(tok):
    return {"Authorization": "Bearer " + tok}


def _mk_user(email, role, password="secret-123", provider="local"):
    r = client.post("/auth/users", headers=AK,
                    json={"email": email, "name": email.split("@")[0], "role": role,
                          "password": password, "provider": provider})
    assert r.status_code == 200, r.text
    return r.json()


def _login(email, password="secret-123"):
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    assert "token" not in r.json()                    # never exposed to browser JavaScript
    cookie = r.headers.get("set-cookie", "")
    assert "poros_console_session=" in cookie
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    return client.cookies.get("poros_console_session")


def test_master_key_still_acts_as_platform_admin():
    me = client.get("/auth/me", headers=AK)
    assert me.status_code == 200 and me.json()["role"] == "admin" and me.json()["via"] == "master_key"
    assert client.get("/admin/tenants", headers=AK).status_code == 200


def test_local_login_me_logout():
    _mk_user("alice@ca-test.local", "admin")
    # wrong password -> one generic error (no user enumeration)
    bad = client.post("/auth/login", json={"email": "alice@ca-test.local", "password": "WRONG"})
    assert bad.status_code == 401 and bad.json()["detail"]["code"] == "LOGIN_FAILED"
    tok = _login("alice@ca-test.local")
    me_response = client.get("/auth/me", headers=_bearer(tok))
    assert me_response.status_code == 200, me_response.text
    me = me_response.json()
    assert me["role"] == "admin" and me["email"] == "alice@ca-test.local" and me["via"] == "session"
    assert client.post("/auth/logout", headers=_bearer(tok)).status_code == 200
    dead = client.get("/auth/me", headers=_bearer(tok))
    assert dead.status_code == 401 and dead.json()["detail"]["code"] == "SESSION_EXPIRED"


def test_user_management_is_admin_only_and_validates():
    _mk_user("mgr@ca-test.local", "tenant_manager")
    tok = _login("mgr@ca-test.local")
    assert client.get("/auth/users", headers=_bearer(tok)).status_code == 403
    # short password / bad role / duplicate email are rejected
    assert client.post("/auth/users", headers=AK, json={"email": "x@ca-test.local", "password": "short", "role": "admin"}).status_code == 400
    assert client.post("/auth/users", headers=AK, json={"email": "x@ca-test.local", "password": "secret-123", "role": "boss"}).status_code == 400
    assert client.post("/auth/users", headers=AK, json={"email": "mgr@ca-test.local", "password": "secret-123", "role": "admin"}).status_code == 409


def test_tenant_manager_sees_only_owned_admin_sees_all():
    a = _mk_user("mgr-a@ca-test.local", "tenant_manager")
    b = _mk_user("mgr-b@ca-test.local", "tenant_manager")
    ta, tb = _login("mgr-a@ca-test.local"), _login("mgr-b@ca-test.local")
    tid_a = client.post("/admin/tenants", headers=_bearer(ta), json={"name": "CA-A"}).json()["id"]
    tid_b = client.post("/admin/tenants", headers=_bearer(tb), json={"name": "CA-B"}).json()["id"]
    assert store.get_tenant(tid_a)["owner_id"] == a["id"]     # creator becomes owner

    lst_a = client.get("/admin/tenants", headers=_bearer(ta)).json()
    assert {t["id"] for t in lst_a} == {tid_a}                # manager A: only their tenant
    lst_admin = {t["id"] for t in client.get("/admin/tenants", headers=AK).json()}
    assert {tid_a, tid_b} <= lst_admin                        # admin sees BOTH managers' tenants
    by_id = {t["id"]: t for t in client.get("/admin/tenants", headers=AK).json()}
    assert by_id[tid_a]["owner_email"] == "mgr-a@ca-test.local"

    # cross-tenant access is forbidden for a manager: path-scoped AND body-scoped routes
    assert client.get(f"/admin/tenants/{tid_b}/datasources", headers=_bearer(ta)).status_code == 403
    assert client.get(f"/admin/tenants/{tid_b}/usage", headers=_bearer(ta)).status_code == 403
    assert client.patch(f"/admin/tenants/{tid_b}", headers=_bearer(ta), json={"name": "CA-hijack"}).status_code == 403
    assert client.post("/admin/apikeys", headers=_bearer(ta), json={"tenant_id": tid_b}).status_code == 403
    assert client.post("/admin/roles", headers=_bearer(ta), json={"tenant_id": tid_b, "name": "r", "policy": {}}).status_code == 403
    # ... while their own tenant works
    assert client.get(f"/admin/tenants/{tid_a}/roles", headers=_bearer(ta)).status_code == 200
    assert client.post("/admin/apikeys", headers=_bearer(ta), json={"tenant_id": tid_a}).status_code == 200

    # monitoring rollup is scoped to the manager's tenants
    mon = client.get("/admin/monitoring", headers=_bearer(ta)).json()
    assert {t["id"] for t in mon["per_tenant"]} == {tid_a}
    # vector infra endpoints are platform-admin only
    assert client.get("/admin/vector/status", headers=_bearer(ta)).status_code == 403


def test_tenant_groups_assign_multiple_tenants_to_manager():
    manager = _mk_user("grouped@ca-test.local", "tenant_manager")
    token = _login("grouped@ca-test.local")
    tenant_a = client.post("/admin/tenants", headers=AK, json={"name": "CA-Group-A1"}).json()["id"]
    tenant_b = client.post("/admin/tenants", headers=AK, json={"name": "CA-Group-A2"}).json()["id"]
    tenant_other = client.post("/admin/tenants", headers=AK, json={"name": "CA-Other"}).json()["id"]

    # A newly provisioned manager has no tenant until ownership/group assignment exists.
    assert client.get("/admin/tenants", headers=_bearer(token)).json() == []
    assert client.post(
        "/admin/tenant-groups",
        headers=_bearer(token),
        json={"name": "CA-Forbidden"},
    ).status_code == 403

    made = client.post(
        "/admin/tenant-groups",
        headers=AK,
        json={
            "name": "CA-Kelompok A",
            "descr": "Dua tenant untuk tim A",
            "tenant_ids": [tenant_a, tenant_b],
            "user_ids": [manager["id"]],
        },
    )
    assert made.status_code == 200, made.text
    group = made.json()
    assert set(group["tenant_ids"]) == {tenant_a, tenant_b}
    assert group["user_ids"] == [manager["id"]]

    visible = client.get("/admin/tenants", headers=_bearer(token))
    assert visible.status_code == 200
    assert {t["id"] for t in visible.json()} == {tenant_a, tenant_b}
    assert client.get(
        f"/admin/tenants/{tenant_a}/roles", headers=_bearer(token)
    ).status_code == 200
    assert client.get(
        f"/admin/tenants/{tenant_other}/roles", headers=_bearer(token)
    ).status_code == 403
    assert {t["id"] for t in client.get(
        "/admin/monitoring", headers=_bearer(token)
    ).json()["per_tenant"]} == {tenant_a, tenant_b}

    # Removing the group revokes only group-derived access; tenants remain intact.
    removed = client.delete(f"/admin/tenant-groups/{group['id']}", headers=AK)
    assert removed.status_code == 200
    assert client.get("/admin/tenants", headers=_bearer(token)).json() == []
    assert store.get_tenant(tenant_a) and store.get_tenant(tenant_b)


def test_tenant_group_validation_and_membership_patch():
    manager = _mk_user("group-patch@ca-test.local", "tenant_manager")
    admin = _mk_user("group-admin@ca-test.local", "admin")
    tenant = client.post("/admin/tenants", headers=AK, json={"name": "CA-Patch"}).json()["id"]
    made = client.post(
        "/admin/tenant-groups",
        headers=AK,
        json={"name": "CA-Patch Group"},
    )
    assert made.status_code == 200
    gid = made.json()["id"]

    duplicate = client.post(
        "/admin/tenant-groups", headers=AK, json={"name": "ca-patch group"}
    )
    assert duplicate.status_code == 409
    invalid_admin = client.patch(
        f"/admin/tenant-groups/{gid}",
        headers=AK,
        json={"user_ids": [admin["id"]]},
    )
    assert invalid_admin.status_code == 400
    invalid_tenant = client.patch(
        f"/admin/tenant-groups/{gid}",
        headers=AK,
        json={"tenant_ids": ["missing-tenant"]},
    )
    assert invalid_tenant.status_code == 400

    patched = client.patch(
        f"/admin/tenant-groups/{gid}",
        headers=AK,
        json={"tenant_ids": [tenant], "user_ids": [manager["id"]]},
    )
    assert patched.status_code == 200
    assert patched.json()["tenant_ids"] == [tenant]
    assert patched.json()["user_ids"] == [manager["id"]]

    # Group access cannot linger invisibly on a user promoted out of manager role.
    promoted = client.patch(
        f"/admin/users/{manager['id']}", headers=AK, json={"role": "admin"}
    )
    assert promoted.status_code == 200
    refreshed = client.get("/admin/tenant-groups", headers=AK).json()
    assert next(g for g in refreshed if g["id"] == gid)["user_ids"] == []


def test_owner_reassignment_admin_only():
    _mk_user("mgr-c@ca-test.local", "tenant_manager")
    tc = _login("mgr-c@ca-test.local")
    tid = client.post("/admin/tenants", headers=AK, json={"name": "CA-unowned"}).json()["id"]
    assert store.get_tenant(tid)["owner_id"] is None          # master key creates unowned
    assert client.get("/admin/tenants", headers=_bearer(tc)).json() == []
    # a manager may NOT grab ownership; an admin may assign it
    assert client.patch(f"/admin/tenants/{tid}", headers=_bearer(tc), json={"owner_email": "mgr-c@ca-test.local"}).status_code == 403
    r = client.patch(f"/admin/tenants/{tid}", headers=AK, json={"owner_email": "mgr-c@ca-test.local"})
    assert r.status_code == 200
    assert {t["id"] for t in client.get("/admin/tenants", headers=_bearer(tc)).json()} == {tid}


def test_deactivation_revokes_sessions_and_blocks_login():
    u = _mk_user("mgr-d@ca-test.local", "tenant_manager")
    tok = _login("mgr-d@ca-test.local")
    assert client.get("/auth/me", headers=_bearer(tok)).status_code == 200
    assert client.patch(f"/auth/users/{u['id']}", headers=AK, json={"active": False}).status_code == 200
    assert client.get("/auth/me", headers=_bearer(tok)).status_code == 401     # session revoked NOW
    assert client.post("/auth/login", json={"email": "mgr-d@ca-test.local", "password": "secret-123"}).status_code == 401


def test_admin_cannot_lock_themselves_out():
    adm = _mk_user("root@ca-test.local", "admin")
    tok = _login("root@ca-test.local")
    assert client.patch(f"/auth/users/{adm['id']}", headers=_bearer(tok), json={"role": "tenant_manager"}).status_code == 400
    assert client.patch(f"/auth/users/{adm['id']}", headers=_bearer(tok), json={"active": False}).status_code == 400
    assert client.delete(f"/auth/users/{adm['id']}", headers=_bearer(tok)).status_code == 400


def test_password_reset_via_admin():
    u = _mk_user("mgr-e@ca-test.local", "tenant_manager")
    assert client.patch(f"/auth/users/{u['id']}", headers=AK, json={"password": "brand-new-pw"}).status_code == 200
    assert client.post("/auth/login", json={"email": "mgr-e@ca-test.local", "password": "secret-123"}).status_code == 401
    assert client.post("/auth/login", json={"email": "mgr-e@ca-test.local", "password": "brand-new-pw"}).status_code == 200


def test_sso_seam_is_config_gated():
    assert client.get("/auth/sso/microsoft/status").json() == {"configured": False}
    assert client.get("/auth/sso/microsoft/login").status_code == 409
    assert client.get("/auth/sso/google/status").json() == {"configured": False}
    assert client.get("/auth/sso/google/login").status_code == 409
    # an SSO-only account exists without a password and can't log in locally
    _mk_user("sso@ca-test.local", "tenant_manager", provider="microsoft", password="")
    r = client.post("/auth/login", json={"email": "sso@ca-test.local", "password": ""})
    assert r.status_code == 401


def _start_google_login(monkeypatch):
    monkeypatch.setenv("GOOGLE_SSO_CLIENT_ID", "google-client-test")
    monkeypatch.setenv("GOOGLE_SSO_CLIENT_SECRET", "google-secret-test")
    login = client.get("/auth/sso/google/login", follow_redirects=False)
    assert login.status_code in (302, 307)
    cookie = login.headers.get("set-cookie", "")
    assert "poros_google_sso_state=" in cookie
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    parsed = urlparse(login.headers["location"])
    query = parse_qs(parsed.query)
    assert parsed.netloc == "accounts.google.com"
    assert query["client_id"] == ["google-client-test"]
    assert query["redirect_uri"] == ["http://localhost:8000/auth/sso/google/callback"]
    assert query["scope"] == ["openid email profile"]
    assert query["state"] and query["nonce"]
    return query["state"][0], query["nonce"][0]


def test_google_sso_redirect_and_verified_callback(monkeypatch):
    state, nonce = _start_google_login(monkeypatch)
    cfg = client.get("/dangerously-skip-login/console-config").json()
    assert cfg["google_sso_configured"] is True
    assert cfg["microsoft_sso_configured"] is False

    monkeypatch.setattr(console_router, "_exchange_google_code", lambda config, code: {
        "aud": config["client_id"],
        "email": "google@ca-test.local",
        "email_verified": True,
        "name": "Google Test",
        "nonce": nonce,
    })
    callback = client.get(
        f"/auth/sso/google/callback?code=one-time-code&state={state}",
        follow_redirects=False,
    )
    assert callback.status_code in (302, 307)
    location = callback.headers["location"]
    assert location == "http://localhost:5173/#sso_ok=1"
    assert "poros_console_session=" in callback.headers.get("set-cookie", "")
    assert "HttpOnly" in callback.headers.get("set-cookie", "")
    me = client.get("/admin/whoami")
    assert me.status_code == 200
    assert me.json()["email"] == "google@ca-test.local"
    created = store.get_console_user_by_email("google@ca-test.local")
    assert created["provider"] == "google"
    assert created["role"] == "tenant_manager"


def test_google_token_verification_allows_bounded_clock_skew(monkeypatch):
    from google.oauth2 import id_token as google_id_token

    seen = {}

    def fake_verify(raw_token, request, audience, clock_skew_in_seconds=0):
        seen.update({
            "raw_token": raw_token,
            "audience": audience,
            "clock_skew": clock_skew_in_seconds,
        })
        return {"sub": "google-subject"}

    monkeypatch.setattr(google_id_token, "verify_oauth2_token", fake_verify)
    monkeypatch.setenv("SSO_CLOCK_SKEW_SEC", "120")
    assert console_router._verify_google_id_token("signed-token", "client-id") == {
        "sub": "google-subject"
    }
    assert seen == {
        "raw_token": "signed-token",
        "audience": "client-id",
        "clock_skew": 120,
    }

    monkeypatch.setenv("SSO_CLOCK_SKEW_SEC", "999")
    assert console_router._sso_clock_skew() == 300
    monkeypatch.setenv("SSO_CLOCK_SKEW_SEC", "invalid")
    assert console_router._sso_clock_skew() == 120


def test_google_exchange_error_is_sanitized_before_browser_redirect(monkeypatch):
    state, _nonce = _start_google_login(monkeypatch)

    def fail_exchange(_config, _code):
        raise RuntimeError("provider-internal-detail-that-must-not-reach-browser")

    monkeypatch.setattr(console_router, "_exchange_google_code", fail_exchange)
    response = client.get(
        f"/auth/sso/google/callback?code=bad-code&state={state}",
        follow_redirects=False,
    )
    location = unquote(response.headers["location"])
    assert response.status_code in (302, 307)
    assert "Login Google gagal. Silakan coba lagi." in location
    assert "provider-internal-detail" not in location


def test_google_sso_rejects_nonce_unverified_email_and_wrong_domain(monkeypatch):
    state, _ = _start_google_login(monkeypatch)
    monkeypatch.setattr(console_router, "_exchange_google_code", lambda config, code: {
        "email": "google@ca-test.local",
        "email_verified": True,
        "nonce": "wrong-nonce",
    })
    bad_nonce = client.get(
        f"/auth/sso/google/callback?code=code&state={state}",
        follow_redirects=False,
    )
    assert "#sso_error=" in bad_nonce.headers["location"]
    assert store.get_console_user_by_email("google@ca-test.local") is None

    state, nonce = _start_google_login(monkeypatch)
    monkeypatch.setattr(console_router, "_exchange_google_code", lambda config, code: {
        "email": "google@ca-test.local",
        "email_verified": False,
        "nonce": nonce,
    })
    unverified = client.get(
        f"/auth/sso/google/callback?code=code&state={state}",
        follow_redirects=False,
    )
    assert "#sso_error=" in unverified.headers["location"]
    assert store.get_console_user_by_email("google@ca-test.local") is None

    monkeypatch.setenv("GOOGLE_SSO_ALLOWED_DOMAINS", "example.com")
    state, nonce = _start_google_login(monkeypatch)
    monkeypatch.setattr(console_router, "_exchange_google_code", lambda config, code: {
        "email": "personal@gmail.com",
        "email_verified": True,
        "nonce": nonce,
    })
    wrong_domain = client.get(
        f"/auth/sso/google/callback?code=code&state={state}",
        follow_redirects=False,
    )
    assert "#sso_error=" in wrong_domain.headers["location"]
    assert store.get_console_user_by_email("personal@gmail.com") is None


# ---- Apps Gateway (hybrid) identity ---------------------------------------------------
# When the console runs behind the company Apps Gateway, identity arrives as the
# gateway-injected X-User-Id header instead of a login. Trust is opt-in (network-isolation
# assertion) and everything maps onto the SAME console_users role + ownership model.
def _gw(email):
    return {"X-User-Id": email}


def test_gateway_header_ignored_when_disabled():
    # default (flag OFF): X-User-Id is NOT trusted — no identity, no account auto-created,
    # so a standalone deployment can't be bypassed by a forged header
    assert client.get("/admin/whoami", headers=_gw("intruder@ca-test.local")).status_code == 401
    assert store.get_console_user_by_email("intruder@ca-test.local") is None


def test_gateway_identity_auto_provisions_and_scopes(monkeypatch):
    monkeypatch.setenv("APPS_GATEWAY_TRUST_HEADER", "1")
    # first request auto-provisions a tenant_manager — role is NEVER inferred from the gateway
    me = client.get("/admin/whoami", headers=_gw("gw-a@ca-test.local"))
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["via"] == "gateway" and body["role"] == "tenant_manager"
    assert body["email"] == "gw-a@ca-test.local"
    uid = body["user_id"]
    # a tenant they create is owned by them; only they + admin see it
    tid = client.post("/admin/tenants", headers=_gw("gw-a@ca-test.local"), json={"name": "CA-GW"}).json()["id"]
    assert store.get_tenant(tid)["owner_id"] == uid
    assert {t["id"] for t in client.get("/admin/tenants", headers=_gw("gw-a@ca-test.local")).json()} == {tid}
    # a gateway user is not admin: platform-admin endpoints stay forbidden
    assert client.get("/auth/users", headers=_gw("gw-a@ca-test.local")).status_code == 403
    # a different gateway user cannot see the first's tenant
    assert client.get("/admin/tenants", headers=_gw("gw-b@ca-test.local")).json() == []
    # a repeat login reuses the SAME row (no duplicate account per request)
    assert client.get("/admin/whoami", headers=_gw("gw-a@ca-test.local")).json()["user_id"] == uid


def test_gateway_respects_deactivation(monkeypatch):
    monkeypatch.setenv("APPS_GATEWAY_TRUST_HEADER", "1")
    uid = client.get("/admin/whoami", headers=_gw("gw-c@ca-test.local")).json()["user_id"]
    assert client.patch(f"/auth/users/{uid}", headers=AK, json={"active": False}).status_code == 200
    r = client.get("/admin/whoami", headers=_gw("gw-c@ca-test.local"))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "ACCOUNT_DISABLED"


def test_gateway_no_provision_requires_preexisting(monkeypatch):
    monkeypatch.setenv("APPS_GATEWAY_TRUST_HEADER", "1")
    monkeypatch.setenv("APPS_GATEWAY_AUTO_PROVISION", "0")
    r = client.get("/admin/whoami", headers=_gw("stranger@ca-test.local"))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "GATEWAY_USER_NOT_PROVISIONED"
    _mk_user("known@ca-test.local", "tenant_manager")     # admin pre-creates it
    assert client.get("/admin/whoami", headers=_gw("known@ca-test.local")).status_code == 200


def test_gateway_falls_back_to_master_key_without_header(monkeypatch):
    monkeypatch.setenv("APPS_GATEWAY_TRUST_HEADER", "1")
    # flag ON but NO X-User-Id: master-key break-glass still works; nothing at all -> 401
    assert client.get("/admin/whoami", headers=AK).json()["via"] == "master_key"
    assert client.get("/admin/whoami").status_code == 401


def test_gateway_first_admin_bootstrap(monkeypatch):
    monkeypatch.setenv("APPS_GATEWAY_TRUST_HEADER", "1")
    # the shared test DB may hold admins from other tests — park them so the
    # "no active admin exists" precondition genuinely holds, restore after
    parked = [u["id"] for u in store.list_console_users()
              if u["role"] == "admin" and u.get("active")]
    for uid in parked:
        store.update_console_user(uid, active=False)
    try:
        me = client.get("/admin/whoami", headers=_gw("boot@ca-test.local")).json()
        assert me["role"] == "tenant_manager"
        r = client.post("/admin/users/bootstrap-first-admin", headers=_gw("boot@ca-test.local"))
        assert r.status_code == 200, r.text
        assert r.json()["user"]["role"] == "admin"
        # effective immediately — the gateway principal re-reads the row per request
        assert client.get("/admin/whoami", headers=_gw("boot@ca-test.local")).json()["role"] == "admin"
        # and refuses forever after (the door closes once one admin exists)
        client.get("/admin/whoami", headers=_gw("late@ca-test.local"))
        r2 = client.post("/admin/users/bootstrap-first-admin", headers=_gw("late@ca-test.local"))
        assert r2.status_code == 409 and r2.json()["detail"]["code"] == "ADMIN_EXISTS"
        assert client.get("/admin/whoami", headers=_gw("late@ca-test.local")).json()["role"] == "tenant_manager"
    finally:
        for uid in parked:
            store.update_console_user(uid, active=True)


def test_admin_users_mirror_matches_auth_users():
    # the gateway never forwards /auth/**, so user management must work on /admin/users;
    # same handlers, same admin-only guard
    _mk_user("root@ca-test.local", "admin")
    tok = _login("root@ca-test.local")
    listed = client.get("/admin/users", headers=_bearer(tok))
    assert listed.status_code == 200
    assert any(u["email"] == "root@ca-test.local" for u in listed.json())
    fixture_password = secrets.token_urlsafe(18)
    made = client.post("/admin/users", headers=_bearer(tok),
                       json={"email": "made@ca-test.local", "name": "made",
                             "role": "tenant_manager", "password": fixture_password,
                             "provider": "local"})
    assert made.status_code == 200
    uid = made.json()["id"]
    assert client.patch(f"/admin/users/{uid}", headers=_bearer(tok),
                        json={"role": "admin"}).json()["role"] == "admin"
    assert client.delete(f"/admin/users/{uid}", headers=_bearer(tok)).status_code == 200
    # non-admin stays forbidden on the mirror
    _mk_user("mgr2@ca-test.local", "tenant_manager")
    tok2 = _login("mgr2@ca-test.local")
    assert client.get("/admin/users", headers=_bearer(tok2)).status_code == 403
    assert client.post("/admin/users/bootstrap-first-admin",
                       headers=_bearer(tok2)).status_code == 409


def test_public_gateway_surface_is_anonymous(monkeypatch):
    # health + mode discovery need no credential; every response echoes the correlation id
    h = client.get("/dangerously-skip-login/health")
    assert h.status_code == 200 and h.json()["status"] == "ok"
    assert h.headers.get("X-Request-Id")
    config = client.get("/dangerously-skip-login/console-config")
    assert config.json()["gateway"] is False
    assert "no-store" in config.headers["Cache-Control"]
    monkeypatch.setenv("APPS_GATEWAY_TRUST_HEADER", "1")
    assert client.get("/dangerously-skip-login/console-config").json()["gateway"] is True


def test_public_surface_reports_build_version():
    """Operators confirm a rebuild via the anonymous surface: both public endpoints
    carry version+built (git sha in dev, BUILD_INFO in the image, 'unknown' at worst)."""
    for path in ("/dangerously-skip-login/health", "/dangerously-skip-login/console-config"):
        body = client.get(path).json()
        assert body.get("version")           # never empty — falls back to "unknown"
        assert "built" in body
