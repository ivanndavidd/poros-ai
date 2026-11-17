"""Console authentication for the login panel (admin frontend).

Three kinds of principal reach the /admin API:
  * a SESSION user (console_users row) — role 'admin' sees every tenant,
    'tenant_manager' sees directly-owned plus tenant-group-assigned tenants;
  * an APPS GATEWAY user — when the console runs behind the company Apps Gateway
    (APPS_GATEWAY_TRUST_HEADER=1), identity comes from the X-User-Id header the
    gateway injects; the app does NO login/sessions itself. Resolved to / auto-
    provisioned as a console_users row so the SAME role + assignment model applies;
  * the legacy MASTER KEY (X-Admin-Key) — kept as break-glass/bootstrap and for
    scripts/tests. It acts as a platform admin (user_id None).

Passwords are salted PBKDF2-SHA256 (stdlib — no extra dependency). Sessions are
opaque bearer tokens stored HASHED with a TTL (store.console_sessions), so they
are individually revocable — unlike a signed JWT. Microsoft SSO (routers/console)
issues the exact same sessions, so everything below is provider-agnostic.

SECURITY — X-User-Id is trusted ONLY when APPS_GATEWAY_TRUST_HEADER is set. That flag
is the operator asserting the service is network-isolated so ONLY the gateway can
reach it (the gateway strips any client-sent X-User-Id and injects its own). Never
enable it on a service that is directly reachable — a client could then forge identity.
"""
import hashlib, hmac, os, secrets, time
from fastapi import Cookie, Depends, Header, HTTPException, Request
from . import store
from .auth import _admin_key

_PBKDF2_ITERS = 240_000
SESSION_COOKIE = "poros_console_session"


def _err(status, code, message):
    return HTTPException(status, {"code": code, "message": message})


# ---- password hashing (PBKDF2-SHA256, salted, self-describing format) ----
def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", (pw or "").encode(), salt.encode(), _PBKDF2_ITERS)
    return f"pbkdf2${_PBKDF2_ITERS}${salt}${dk.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, iters, salt, want = (stored or "").split("$")
        if algo != "pbkdf2":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", (pw or "").encode(), salt.encode(), int(iters))
        return hmac.compare_digest(dk.hex(), want)
    except (ValueError, TypeError):
        return False


# ---- shared login throttle (account + source IP, persisted in PostgreSQL) ----
_MAX_FAILS, _LOCK_SEC = 5, 60
try:
    _MAX_IP_FAILS = max(_MAX_FAILS, int(os.getenv("LOGIN_IP_MAX_FAILURES", "20")))
except ValueError:
    _MAX_IP_FAILS = 20


def _login_subjects(email: str, source_ip: str = ""):
    values = ["email:" + (email or "").strip().lower()]
    if source_ip:
        values.append("ip:" + source_ip.strip().lower())
    return [hashlib.sha256(value.encode()).hexdigest() for value in values]


def check_login_allowed(email: str, source_ip: str = ""):
    retry = max(
        (store.console_login_retry_after(subject)
         for subject in _login_subjects(email, source_ip)),
        default=0,
    )
    if retry:
        error = _err(429, "LOGIN_LOCKED",
                     "terlalu banyak percobaan gagal — coba lagi sebentar lagi")
        error.headers = {"Retry-After": str(retry)}
        raise error


def note_login_result(email: str, ok: bool, source_ip: str = ""):
    for index, subject in enumerate(_login_subjects(email, source_ip)):
        # Account lockout stays deliberately strict. The IP bucket is broader so
        # several users behind one corporate NAT do not lock each other out.
        threshold = _MAX_FAILS if index == 0 else _MAX_IP_FAILS
        store.note_console_login_attempt(subject, ok, threshold, _LOCK_SEC)


# ---- roles + Apps Gateway (hybrid) identity ----
_ROLES = ("admin", "tenant_manager")            # single source of truth (imported by routers/console)


def apps_gateway_enabled() -> bool:
    """True when the console runs behind the company Apps Gateway and should trust the
    gateway-injected X-User-Id header. OFF by default — see the SECURITY note at top."""
    return (os.getenv("APPS_GATEWAY_TRUST_HEADER", "") or "").strip().lower() in ("1", "true", "yes", "on")


def _gw_auto_provision() -> bool:
    return (os.getenv("APPS_GATEWAY_AUTO_PROVISION", "1") or "").strip().lower() not in ("0", "false", "no", "off")


def _gw_default_role() -> str:
    r = (os.getenv("APPS_GATEWAY_DEFAULT_ROLE", "tenant_manager") or "").strip()
    return r if r in _ROLES else "tenant_manager"


def _gateway_principal(email: str) -> dict:
    """Resolve an Apps Gateway user (the email in X-User-Id) to a console principal. A
    first-seen email is auto-provisioned as a console_users row (default role
    tenant_manager, provider 'gateway') unless APPS_GATEWAY_AUTO_PROVISION=0, in which
    case an admin must pre-create the account. The app role is ALWAYS read from the
    console_users row — NEVER from a gateway 'admin' flag (that flag governs the gateway
    itself, not this app), so promotion to admin is a deliberate in-app action."""
    u = store.get_console_user_by_email(email)
    if not u:
        if not _gw_auto_provision():
            raise _err(403, "GATEWAY_USER_NOT_PROVISIONED",
                       f"akun {email} belum terdaftar di console — minta admin membuatkannya")
        uid = store.create_console_user(email, email.split("@")[0], _gw_default_role(), "", "gateway")
        u = store.get_console_user(uid)
    if not u.get("active"):
        raise _err(403, "ACCOUNT_DISABLED", "akun Anda dinonaktifkan")
    store.touch_console_login(u["id"])
    return {"user_id": u["id"], "email": u["email"], "name": u["name"], "role": u["role"],
            "provider": u.get("provider") or "gateway", "via": "gateway"}


# ---- principals ----
def current_principal(x_admin_key: str = Header(None, alias="X-Admin-Key"),
                      authorization: str = Header(None, alias="Authorization"),
                      x_user_id: str = Header(None, alias="X-User-Id"),
                      session_cookie: str = Cookie(None, alias=SESSION_COOKIE)):
    """Resolve who is calling: an Apps Gateway user (X-User-Id, only when trusted), a
    session bearer token, or the master admin key. The frontend sends its one stored
    credential in BOTH X-Admin-Key and Authorization, so each is tried as a session
    first, then compared (constant-time) against the master key."""
    # 1. Apps Gateway identity — authoritative when enabled (the gateway injects a verified
    # X-User-Id; the app never logs the user in). An absent header falls through, so
    # master-key break-glass and scripts still work when reaching the app directly.
    if apps_gateway_enabled():
        email = (x_user_id or "").strip().lower()
        if email and "@" in email:
            return _gateway_principal(email)
    key = _admin_key().encode()
    # An explicit break-glass header must override a stale/non-admin session cookie.
    if x_admin_key and hmac.compare_digest(str(x_admin_key).encode(), key):
        return {"user_id": None, "email": "", "name": "Platform Admin",
                "role": "admin", "provider": "master_key", "via": "master_key"}
    token = (session_cookie or "").strip() or None
    if authorization and authorization.lower().startswith("bearer "):
        # Backward-compatible for scripts/old clients. The browser console now uses the
        # HttpOnly cookie, so JavaScript never receives or persists a session token.
        token = authorization.split(" ", 1)[1].strip() or token
    if token:
        u = store.get_console_session(token)
        if u:
            return {"user_id": u["id"], "email": u["email"], "name": u["name"],
                    "role": u["role"], "provider": u.get("provider") or "local", "via": "session"}
    for cand in (token,):
        if cand and hmac.compare_digest(str(cand).encode(), key):
            return {"user_id": None, "email": "", "name": "Platform Admin",
                    "role": "admin", "provider": "master_key", "via": "master_key"}
    if token and token.startswith("st_"):     # a real session token that no longer resolves
        raise _err(401, "SESSION_EXPIRED", "sesi berakhir atau dicabut — silakan login lagi")
    if x_admin_key or token:                  # attempted master-key auth (legacy error shape)
        raise _err(401, "ADMIN_KEY_INVALID", "X-Admin-Key missing or incorrect")
    raise _err(401, "AUTH_REQUIRED", "sign in first (session token or X-Admin-Key)")


def require_admin_role(p: dict = Depends(current_principal)):
    """Platform-admin-only endpoints (user management, vector-store infra)."""
    if p["role"] != "admin":
        raise _err(403, "ADMIN_ONLY", "hanya platform admin yang boleh mengakses ini")
    return p


def ensure_tenant_access(p: dict, tenant_id: str):
    """A tenant_manager may touch directly-owned or group-assigned tenants.

    Missing and unauthorized tenants intentionally produce the same 403 response so
    callers cannot probe tenant existence. Admin (session or master key) passes all.
    """
    if p["role"] == "admin":
        return
    if not store.console_user_can_access_tenant(p.get("user_id"), tenant_id):
        raise _err(403, "TENANT_FORBIDDEN",
                   "tenant ini belum di-assign ke akun atau kelompok Anda")


def console_guard(request: Request, p: dict = Depends(current_principal)):
    """Router-level guard for every /admin route: authenticates, then enforces tenant
    access for any route whose path names a tenant directly ({tid}) or indirectly
    ({did} datasource, {doc_id} document, {key_id} API-key record). Routes that carry the tenant
    in the BODY instead call ensure_tenant_access() in the endpoint itself."""
    if p["role"] == "admin":
        return p
    pp = request.path_params
    tid = pp.get("tid")
    if not tid and pp.get("did"):
        ds = store.get_datasource(pp["did"])
        tid = ds["tenant_id"] if ds else None
    if not tid and pp.get("doc_id"):
        doc = store.get_document(pp["doc_id"])
        tid = doc["tenant_id"] if doc else None
    if not tid and pp.get("key_id"):
        k = store.get_api_key_by_id(pp["key_id"])
        tid = k["tenant_id"] if k else None
    if tid:
        ensure_tenant_access(p, tid)
    return p
