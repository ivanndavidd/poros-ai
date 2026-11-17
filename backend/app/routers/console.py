"""Login-panel API (/auth): local email+password sessions, admin-managed console
users, and CONFIG-GATED Microsoft / Google SSO seams.

Microsoft: the full OIDC authorization-code flow against Entra ID stays dormant
until the app registration exists — set
  MS_SSO_CLIENT_ID / MS_SSO_CLIENT_SECRET      (required to activate)
  MS_SSO_TENANT_ID                             (directory id; default 'organizations')
  MS_SSO_REDIRECT_URI                          (default http://localhost:8000/auth/sso/microsoft/callback
                                                — register this exact URL in Entra ID)
  MS_SSO_AUTO_PROVISION=0                      (default 1: first SSO login auto-creates
                                                the user with MS_SSO_DEFAULT_ROLE,
                                                default 'tenant_manager')

Google: the equivalent server-side OIDC flow stays dormant until these exist:
  GOOGLE_SSO_CLIENT_ID / GOOGLE_SSO_CLIENT_SECRET
  GOOGLE_SSO_REDIRECT_URI                      (default http://localhost:8000/auth/sso/google/callback)
  GOOGLE_SSO_ALLOWED_DOMAINS                   (optional comma-separated email domains)
  GOOGLE_SSO_AUTO_PROVISION / GOOGLE_SSO_DEFAULT_ROLE
  SSO_CLOCK_SKEW_SEC                          (default 120; tolerated iat/exp drift)

  CONSOLE_URL                                  (frontend origin to land back on;
                                                default http://localhost:5173)
Until configured, each provider reports configured:false and its button is hidden.
"""
import base64, hashlib, hmac, os, secrets, time
from urllib.parse import quote, urlencode
from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from .. import store
from ..api_errors import log_internal
from ..runtime_config import production        # one source of truth for hardened-mode
from ..console_auth import (_ROLES, _err, check_login_allowed, current_principal, hash_password,
                            note_login_result, require_admin_role, verify_password, SESSION_COOKIE)

router = APIRouter(prefix="/auth", tags=["console auth (login panel)"])


def _session_hours():
    return int(os.getenv("CONSOLE_SESSION_HOURS", "12"))


def _sso_clock_skew() -> int:
    """Small bounded tolerance for normal clock drift between the host and IdP."""
    try:
        value = int((os.getenv("SSO_CLOCK_SKEW_SEC") or "120").strip())
    except ValueError:
        value = 120
    return max(0, min(value, 300))


def _cookie_secure() -> bool:
    explicit = (os.getenv("CONSOLE_COOKIE_SECURE") or "").strip().lower()
    if explicit:
        return explicit in ("1", "true", "yes", "on")
    return production()


def _set_session_cookie(response: Response, token: str):
    """Store console sessions outside JavaScript/localStorage."""
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=_session_hours() * 3600,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path="/",
    )
    return response


def _clear_session_cookie(response: Response):
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path="/",
    )
    return response


class LoginIn(BaseModel):
    email: str
    password: str

class UserIn(BaseModel):
    email: str
    name: str = ""
    role: str = "tenant_manager"
    password: str = ""              # required for provider 'local'
    provider: str = "local"         # 'microsoft'/'google' = SSO-only (no password)

class UserPatchIn(BaseModel):
    name: str = None
    role: str = None
    active: bool = None
    password: str = None            # set = reset password (local accounts)


def _pub(u):
    return {k: u.get(k) for k in ("id", "email", "name", "role", "provider", "active",
                                  "created", "last_login")}


# ---- session lifecycle ----
@router.post("/login")
def login(b: LoginIn, response: Response, request: Request):
    email = b.email.strip().lower()
    source_ip = request.client.host if request.client else ""
    check_login_allowed(email, source_ip)
    u = store.get_console_user_by_email(email, with_hash=True)
    ok = bool(u and u["active"] and u.get("password_hash") and verify_password(b.password, u["password_hash"]))
    note_login_result(email, ok, source_ip)
    if not ok:
        # one message for unknown email / wrong password / deactivated / SSO-only
        # account — no user enumeration through error differences
        raise _err(401, "LOGIN_FAILED", "email atau password salah")
    store.touch_console_login(u["id"])
    token = store.create_console_session(u["id"], _session_hours())
    _set_session_cookie(response, token)
    return {"ok": True, "user": _pub(u)}


@router.get("/me")
def me(p: dict = Depends(current_principal)):
    return p


@router.post("/logout")
def logout(request: Request, response: Response,
           authorization: str = Header(None, alias="Authorization")):
    token = request.cookies.get(SESSION_COOKIE)
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip() or token
    if token:
        store.delete_console_session(token)
    _clear_session_cookie(response)
    return {"ok": True}


# ---- console users (platform-admin only) ----
# Every endpoint is registered on TWO prefixes: /auth/users (standalone deploys) and
# /admin/users (behind the Apps Gateway). The gateway owns /auth/** and NEVER forwards
# it to the app, so without the /admin mirror user management is unreachable exactly
# where it is needed most — gateway deployments, where every account auto-provisions
# as tenant_manager and someone must be able to promote them.
users_router = APIRouter(prefix="/admin/users", tags=["console users (gateway path)"])


@users_router.post("/bootstrap-first-admin")
def bootstrap_first_admin(p: dict = Depends(current_principal)):
    """Fresh-deploy chicken-and-egg breaker: only an admin may promote accounts, but a
    gateway deployment starts with NO admin (auto-provision creates tenant_manager) and
    the master key cannot ride the gateway (it always injects X-User-Id, which wins).
    So: while no active admin account exists, the calling signed-in user may promote
    THEMSELVES; the moment one admin exists this refuses forever. The call is a POST
    under /admin/** and therefore lands in the admin audit log (actor + path)."""
    if any(u.get("role") == "admin" and u.get("active") for u in store.list_console_users()):
        raise _err(409, "ADMIN_EXISTS",
                   "sudah ada admin aktif — minta admin tersebut menaikkan role akun Anda")
    if not p.get("user_id"):
        raise _err(400, "NO_ACCOUNT",
                   "identitas master key sudah setara admin — login sebagai akun console untuk bootstrap")
    store.update_console_user(p["user_id"], role="admin")
    return {"ok": True, "user": _pub(store.get_console_user(p["user_id"]))}


@users_router.get("", dependencies=[Depends(require_admin_role)])
@router.get("/users", dependencies=[Depends(require_admin_role)])
def list_users():
    return [_pub(u) for u in store.list_console_users()]


@users_router.post("", dependencies=[Depends(require_admin_role)])
@router.post("/users", dependencies=[Depends(require_admin_role)])
def create_user(b: UserIn):
    email = b.email.strip().lower()
    if "@" not in email:
        raise _err(400, "EMAIL_INVALID", "alamat email tidak valid")
    if b.role not in _ROLES:
        raise _err(400, "ROLE_INVALID", f"role harus salah satu dari {_ROLES}")
    if b.provider not in ("local", "microsoft", "google"):
        raise _err(400, "PROVIDER_INVALID", "provider harus 'local', 'microsoft', atau 'google'")
    if store.get_console_user_by_email(email):
        raise _err(409, "EMAIL_TAKEN", "email ini sudah terdaftar")
    if b.provider == "local" and len(b.password) < 8:
        raise _err(400, "PASSWORD_TOO_SHORT", "password minimal 8 karakter")
    ph = hash_password(b.password) if b.provider == "local" else ""
    uid = store.create_console_user(email, b.name or email.split("@")[0], b.role, ph, b.provider)
    return _pub(store.get_console_user(uid))


@users_router.patch("/{uid}")
@router.patch("/users/{uid}")
def patch_user(uid: str, b: UserPatchIn, p: dict = Depends(require_admin_role)):
    u = store.get_console_user(uid)
    if not u:
        raise _err(404, "NOT_FOUND", "user tidak ditemukan")
    if b.role is not None and b.role not in _ROLES:
        raise _err(400, "ROLE_INVALID", f"role harus salah satu dari {_ROLES}")
    # an admin can't demote or deactivate THEMSELVES — no self-lockout
    if uid == p.get("user_id") and ((b.role and b.role != u["role"]) or b.active is False):
        raise _err(400, "SELF_LOCKOUT", "tidak bisa menurunkan role / menonaktifkan akun sendiri")
    if b.password is not None and len(b.password) < 8:
        raise _err(400, "PASSWORD_TOO_SHORT", "password minimal 8 karakter")
    store.update_console_user(uid, name=b.name, role=b.role, active=b.active,
                              password_hash=hash_password(b.password) if b.password else None)
    return _pub(store.get_console_user(uid))


@users_router.delete("/{uid}")
@router.delete("/users/{uid}")
def delete_user(uid: str, p: dict = Depends(require_admin_role)):
    if uid == p.get("user_id"):
        raise _err(400, "SELF_LOCKOUT", "tidak bisa menghapus akun sendiri")
    store.delete_console_user(uid)
    return {"ok": True}


# ---- Microsoft SSO (OIDC authorization-code; dormant until configured) ----
def _sso_cfg():
    cid, csec = os.getenv("MS_SSO_CLIENT_ID"), os.getenv("MS_SSO_CLIENT_SECRET")
    if not (cid and csec):
        return None
    tid = os.getenv("MS_SSO_TENANT_ID", "organizations")
    base = f"https://login.microsoftonline.com/{tid}/oauth2/v2.0"
    return {"client_id": cid, "client_secret": csec, "tenant": tid,
            "redirect_uri": os.getenv("MS_SSO_REDIRECT_URI",
                                      "http://localhost:8000/auth/sso/microsoft/callback"),
            "authorize_url": base + "/authorize", "token_url": base + "/token"}


_STATE_TTL = 600
_GOOGLE_STATE_COOKIE = "poros_google_sso_state"
_MICROSOFT_STATE_COOKIE = "poros_microsoft_sso_state"


def _console_url():
    return (os.getenv("CONSOLE_URL") or "http://localhost:5173").rstrip("/")


def _sso_fail(msg):
    """Return only an intentionally public SSO message to the browser.

    Provider/token exceptions may contain URLs, tenant identifiers, timestamps,
    or credential-adjacent diagnostics and must stay in server logs.
    """
    return RedirectResponse(_console_url() + "/#sso_error=" + quote(str(msg)[:180]))


def _oauth_state_secret():
    return (
        os.getenv("ADMIN_SECRET_KEY")
        or os.getenv("JWT_SECRET")
        or os.getenv("ADMIN_KEY")
        or ""
    ).encode("utf-8")


def _encode_oauth_state(state, nonce, issued):
    raw = f"{int(issued)}:{state}:{nonce}".encode("utf-8")
    signature = hmac.new(_oauth_state_secret(), raw, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(raw + b"." + signature).rstrip(b"=").decode("ascii")


def _decode_oauth_state(value):
    try:
        padded = (value or "") + "=" * (-len(value or "") % 4)
        packed = base64.urlsafe_b64decode(padded.encode("ascii"))
        raw, separator, signature = packed[:-33], packed[-33:-32], packed[-32:]
        if separator != b"." or not hmac.compare_digest(
            signature, hmac.new(_oauth_state_secret(), raw, hashlib.sha256).digest()
        ):
            return None
        issued_text, state, nonce = raw.decode("utf-8").split(":", 2)
        issued = int(issued_text)
        if issued < time.time() - _STATE_TTL or issued > time.time() + 60:
            return None
        return state, nonce
    except (ValueError, UnicodeError):
        return None


def _clear_oauth_state(response, cookie_name, path):
    response.delete_cookie(cookie_name, path=path)
    return response


def _finish_sso_login(provider, claims, auto_provision_env, default_role_env):
    """Create/reuse the console identity after provider-specific token validation."""
    email = (claims.get("email") or claims.get("preferred_username") or "").strip().lower()
    if "@" not in email:
        return _sso_fail("identity token has no usable email")
    u = store.get_console_user_by_email(email)
    if not u:
        if (os.getenv(auto_provision_env, "1")).strip().lower() in ("0", "false", "no"):
            return _sso_fail(f"akun {email} belum terdaftar — minta admin membuatkan user")
        role = os.getenv(default_role_env, "tenant_manager")
        uid = store.create_console_user(
            email,
            claims.get("name") or email.split("@")[0],
            role if role in _ROLES else "tenant_manager",
            "",
            provider,
        )
        u = store.get_console_user(uid)
    if not u["active"]:
        return _sso_fail("akun Anda dinonaktifkan")
    store.touch_console_login(u["id"])
    token = store.create_console_session(u["id"], _session_hours())
    response = RedirectResponse(_console_url() + "/#sso_ok=1")
    return _set_session_cookie(response, token)


@router.get("/sso/microsoft/status")
def sso_status():
    """Lets the login page decide whether to show the 'Sign in with Microsoft' button."""
    return {"configured": bool(_sso_cfg())}


@router.get("/sso/microsoft/login")
def sso_login():
    cfg = _sso_cfg()
    if not cfg:
        raise _err(409, "SSO_NOT_CONFIGURED",
                   "Microsoft SSO belum dikonfigurasi (MS_SSO_CLIENT_ID / MS_SSO_CLIENT_SECRET)")
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    q = urlencode({"client_id": cfg["client_id"], "response_type": "code",
                   "redirect_uri": cfg["redirect_uri"], "response_mode": "query",
                   "scope": "openid profile email", "state": state, "nonce": nonce})
    response = RedirectResponse(cfg["authorize_url"] + "?" + q)
    response.set_cookie(
        _MICROSOFT_STATE_COOKIE,
        _encode_oauth_state(state, nonce, time.time()),
        max_age=_STATE_TTL,
        httponly=True,
        secure=cfg["redirect_uri"].lower().startswith("https://"),
        samesite="lax",
        path="/auth/sso/microsoft",
    )
    return response


def _exchange_microsoft_code(cfg, code):
    import requests
    import jwt

    response = requests.post(cfg["token_url"], timeout=15, data={
        "client_id": cfg["client_id"], "client_secret": cfg["client_secret"],
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": cfg["redirect_uri"], "scope": "openid profile email"})
    body = response.json()
    raw_token = body.get("id_token")
    if response.status_code >= 400 or not raw_token:
        raise ValueError(body.get("error_description") or "no id_token in token response")
    jwks = f"https://login.microsoftonline.com/{cfg['tenant']}/discovery/v2.0/keys"
    signing_key = jwt.PyJWKClient(jwks, cache_keys=True, lifespan=300).get_signing_key_from_jwt(raw_token).key
    claims = jwt.decode(raw_token, signing_key, algorithms=["RS256"],
                        audience=cfg["client_id"], leeway=_sso_clock_skew(),
                        options={"require": ["exp", "iat"]})
    issuer = str(claims.get("iss") or "")
    if not issuer.startswith("https://login.microsoftonline.com/") or not issuer.endswith("/v2.0"):
        raise ValueError("id_token issuer mismatch")
    configured = cfg["tenant"].lower()
    if configured not in {"common", "organizations", "consumers"} and (
        str(claims.get("tid") or "").lower() != configured
    ):
        raise ValueError("id_token tenant mismatch")
    return claims


@router.get("/sso/microsoft/callback")
def sso_callback(request: Request, code: str = None, state: str = None,
                 error: str = None, error_description: str = None):
    """Entra ID lands here; success creates an HttpOnly session cookie."""
    cfg = _sso_cfg()
    if not cfg:
        return _clear_oauth_state(_sso_fail("Microsoft SSO not configured"),
                                  _MICROSOFT_STATE_COOKIE, "/auth/sso/microsoft")
    if error:
        log_internal("MICROSOFT_SSO_DENIED",
                     RuntimeError(error_description or error),
                     context="microsoft oauth callback")
        return _clear_oauth_state(_sso_fail("Login Microsoft dibatalkan atau ditolak."),
                                  _MICROSOFT_STATE_COOKIE, "/auth/sso/microsoft")
    saved = _decode_oauth_state(request.cookies.get(_MICROSOFT_STATE_COOKIE))
    if not code or saved is None or not secrets.compare_digest(state or "", saved[0]):
        return _clear_oauth_state(_sso_fail("invalid or expired state — coba login lagi"),
                                  _MICROSOFT_STATE_COOKIE, "/auth/sso/microsoft")
    try:
        claims = _exchange_microsoft_code(cfg, code)
    except Exception as exc:
        log_internal("MICROSOFT_SSO_EXCHANGE_FAILED", exc,
                     context="microsoft token exchange")
        return _clear_oauth_state(_sso_fail(
                                      "Login Microsoft gagal. Silakan coba lagi."),
                                  _MICROSOFT_STATE_COOKIE, "/auth/sso/microsoft")
    if not secrets.compare_digest(str(claims.get("nonce") or ""), saved[1]):
        return _clear_oauth_state(_sso_fail("Microsoft id_token nonce mismatch"),
                                  _MICROSOFT_STATE_COOKIE, "/auth/sso/microsoft")
    return _clear_oauth_state(_finish_sso_login(
        "microsoft", claims, "MS_SSO_AUTO_PROVISION", "MS_SSO_DEFAULT_ROLE"
    ), _MICROSOFT_STATE_COOKIE, "/auth/sso/microsoft")


# ---- Google SSO (OIDC authorization-code; dormant until configured) ----
def _google_sso_cfg():
    cid = (os.getenv("GOOGLE_SSO_CLIENT_ID") or "").strip()
    csec = (os.getenv("GOOGLE_SSO_CLIENT_SECRET") or "").strip()
    if not (cid and csec):
        return None
    return {
        "client_id": cid,
        "client_secret": csec,
        "redirect_uri": os.getenv(
            "GOOGLE_SSO_REDIRECT_URI",
            "http://localhost:8000/auth/sso/google/callback",
        ),
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
    }


def _verify_google_id_token(raw_token, client_id):
    """Verify Google's signature, issuer, audience, and expiry using official keys."""
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token as google_id_token

    return google_id_token.verify_oauth2_token(
        raw_token,
        Request(),
        client_id,
        clock_skew_in_seconds=_sso_clock_skew(),
    )


def _exchange_google_code(cfg, code):
    import requests

    response = requests.post(
        cfg["token_url"],
        timeout=15,
        data={
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cfg["redirect_uri"],
        },
    )
    try:
        body = response.json()
    except Exception:
        body = {}
    raw_token = body.get("id_token")
    if response.status_code >= 400 or not raw_token:
        raise ValueError(
            body.get("error_description") or body.get("error") or "Google token exchange failed"
        )
    return _verify_google_id_token(raw_token, cfg["client_id"])


def _google_state_secret():
    """Stable local secret so OAuth state survives Uvicorn reloads/multiple workers."""
    return _oauth_state_secret()


def _encode_google_state(state, nonce, issued):
    return _encode_oauth_state(state, nonce, issued)


def _decode_google_state(value):
    return _decode_oauth_state(value)


def _clear_google_state(response):
    response.delete_cookie(_GOOGLE_STATE_COOKIE, path="/auth/sso/google")
    return response


@router.get("/sso/google/status")
def google_sso_status():
    return {"configured": bool(_google_sso_cfg())}


@router.get("/sso/google/login")
def google_sso_login():
    cfg = _google_sso_cfg()
    if not cfg:
        raise _err(
            409,
            "SSO_NOT_CONFIGURED",
            "Google SSO belum dikonfigurasi (GOOGLE_SSO_CLIENT_ID / GOOGLE_SSO_CLIENT_SECRET)",
        )
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    query = urlencode({
        "client_id": cfg["client_id"],
        "response_type": "code",
        "redirect_uri": cfg["redirect_uri"],
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "prompt": "select_account",
    })
    response = RedirectResponse(cfg["authorize_url"] + "?" + query)
    response.set_cookie(
        _GOOGLE_STATE_COOKIE,
        _encode_google_state(state, nonce, time.time()),
        max_age=_STATE_TTL,
        httponly=True,
        secure=cfg["redirect_uri"].lower().startswith("https://"),
        samesite="lax",
        path="/auth/sso/google",
    )
    return response


@router.get("/sso/google/callback")
def google_sso_callback(request: Request, code: str = None, state: str = None,
                        error: str = None, error_description: str = None):
    cfg = _google_sso_cfg()
    if not cfg:
        return _clear_google_state(_sso_fail("Google SSO not configured"))
    if error:
        log_internal("GOOGLE_SSO_DENIED", RuntimeError(error_description or error),
                     context="google oauth callback")
        return _clear_google_state(_sso_fail("Login Google dibatalkan atau ditolak."))
    saved = _decode_google_state(request.cookies.get(_GOOGLE_STATE_COOKIE))
    if not code or saved is None or not secrets.compare_digest(state or "", saved[0]):
        return _clear_google_state(_sso_fail("invalid or expired state — coba login lagi"))
    try:
        claims = _exchange_google_code(cfg, code)
    except Exception as exc:
        log_internal("GOOGLE_SSO_EXCHANGE_FAILED", exc,
                     context="google token exchange")
        return _clear_google_state(_sso_fail(
            "Login Google gagal. Silakan coba lagi."
        ))
    if not secrets.compare_digest(str(claims.get("nonce") or ""), saved[1]):
        return _clear_google_state(_sso_fail("Google id_token nonce mismatch"))
    if claims.get("email_verified") not in (True, "true"):
        return _clear_google_state(_sso_fail("Google account email belum terverifikasi"))
    email = (claims.get("email") or "").strip().lower()
    allowed = {
        value.strip().lower().lstrip("@")
        for value in (os.getenv("GOOGLE_SSO_ALLOWED_DOMAINS") or "").split(",")
        if value.strip()
    }
    if allowed and ("@" not in email or email.rsplit("@", 1)[1] not in allowed):
        return _clear_google_state(_sso_fail("domain email Google tidak diizinkan"))
    return _clear_google_state(_finish_sso_login(
        "google", claims, "GOOGLE_SSO_AUTO_PROVISION", "GOOGLE_SSO_DEFAULT_ROLE"
    ))
