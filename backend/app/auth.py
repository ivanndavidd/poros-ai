import hmac
import ipaddress
import os
import socket
from functools import lru_cache
from urllib.parse import urlparse
from fastapi import Header, HTTPException
from . import store

DEV_ADMIN_KEY = "admin-dev-key"


def _admin_key() -> str:
    # read at call time (not import) so startup checks/tests see current env
    return (os.getenv("ADMIN_KEY") or "").strip()


def _err(status: int, code: str, message: str) -> HTTPException:
    """All API errors share one shape: detail={code, message}."""
    return HTTPException(status, {"code": code, "message": message})


def require_admin(x_admin_key: str = Header(None, alias="X-Admin-Key")):
    # constant-time compare — no timing side-channel on the admin key
    if not x_admin_key or not hmac.compare_digest(str(x_admin_key).encode(), _admin_key().encode()):
        raise _err(401, "ADMIN_KEY_INVALID", "X-Admin-Key missing or incorrect")
    return True


def require_api_key(x_api_key: str = Header(None, alias="X-API-Key")):
    if not x_api_key:
        raise _err(401, "API_KEY_MISSING", "X-API-Key header is required")
    k = store.get_api_key(x_api_key)
    if not k or not k["active"]:
        raise _err(401, "API_KEY_INVALID", "API key is invalid or has been revoked")
    exp = k.get("expiry")
    if exp and len(exp) >= 10 and exp[:10] < store.now()[:10]:
        raise _err(401, "API_KEY_EXPIRED", "API key has expired")
    return k  # {key, tenant_id, role, env, rate_limit, expiry, ...}


def end_user_identity(authorization: str = Header(None, alias="Authorization")):
    """Extract the RAW end-user bearer token (Feature 1.5 — strict Pattern B).

    Verification is deliberately NOT done here: which secret/algorithms/claims to
    verify against is per-tenant config, and the tenant is only known once the API
    key is resolved. `consumer._effective` — the single Pattern-B choke point —
    calls `verify_end_user(tenant_id, token)` below.

    Returns None (no bearer token) or {present: True, token: str}.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return {"present": True, "token": authorization.split(" ", 1)[1].strip()}


_SYMMETRIC_ALGS = {"HS256", "HS384", "HS512"}
_ASYMMETRIC_ALGS = {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}
# Clock-skew tolerance for end-user JWT verification (iat/nbf/exp). Consumer app hosts
# and this server rarely share a clock to the second; without leeway a token signed a few
# seconds "ahead" fails with ImmatureSignatureError (iat in the future). RFC 7519 allows a
# small leeway (a few minutes). Override with JWT_LEEWAY_SEC. Still: keep hosts NTP-synced.
_JWT_LEEWAY = int(os.getenv("JWT_LEEWAY_SEC", "120"))


def _production() -> bool:
    # Single source of truth: absent/unrecognised APP_ENV means HARDENED (runtime_config).
    from .runtime_config import production
    return production()


def allow_hs256_staging() -> bool:
    """Explicit compatibility escape hatch for staging consumers.

    Production remains asymmetric-only unless an operator deliberately enables
    this flag. Even then, only a tenant-scoped HS256 secret with issuer and
    audience is accepted; the global JWT_SECRET fallback stays disabled.
    """
    return (os.getenv("JWT_ALLOW_HS256_STAGING") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _allow_private_jwks() -> bool:
    return (os.getenv("JWT_JWKS_ALLOW_PRIVATE_HOSTS") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _validate_jwks_url(url: str):
    """Reject obvious SSRF targets before PyJWKClient performs the fetch."""
    parsed = urlparse(url or "")
    if parsed.scheme not in (("https",) if _production() else ("https", "http")):
        raise ValueError("JWKS URL must use HTTPS in production")
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("invalid JWKS URL")
    if _allow_private_jwks():
        return
    host = parsed.hostname.strip("[]").lower()
    if host in {"localhost", "localhost.localdomain"}:
        raise ValueError("private JWKS host is not allowed")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or 443)}
    except OSError as exc:
        raise ValueError("JWKS host cannot be resolved") from exc
    for value in addresses:
        ip = ipaddress.ip_address(value)
        if not ip.is_global:
            raise ValueError("private JWKS host is not allowed")


@lru_cache(maxsize=32)
def _jwk_client(url: str):
    import jwt
    _validate_jwks_url(url)
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=300)


def _jwks_signing_key(url: str, token: str):
    return _jwk_client(url).get_signing_key_from_jwt(token).key


def validate_jwt_config(cfg: dict, production: bool = None):
    """Validate verifier shape without fetching its JWKS endpoint."""
    cfg = cfg or {}
    production = _production() if production is None else bool(production)
    jwks_url = (cfg.get("jwks_url") or "").strip()
    public_key = (cfg.get("public_key") or "").strip()
    secret = cfg.get("secret") or ""
    algorithms = [str(a).upper() for a in (cfg.get("algorithms") or [])]
    if len(algorithms) != 1:
        raise ValueError("configure exactly one accepted JWT algorithm")
    algorithm = algorithms[0]
    if jwks_url and public_key:
        raise ValueError("use either jwks_url or public_key, not both")
    if jwks_url:
        _validate_jwks_url(jwks_url)
    if public_key:
        try:
            from cryptography.hazmat.primitives.serialization import load_pem_public_key
            load_pem_public_key(public_key.encode())
        except Exception as exc:
            raise ValueError("public_key is not a valid PEM public key") from exc
    asymmetric = bool(jwks_url or public_key)
    if asymmetric and algorithm not in _ASYMMETRIC_ALGS:
        raise ValueError("JWKS/public key requires an RS* or ES* algorithm")
    if not asymmetric and algorithm not in _SYMMETRIC_ALGS:
        raise ValueError("HS* requires a shared secret and no JWKS/public key")
    if production:
        if not cfg.get("issuer") or not cfg.get("audience"):
            raise ValueError("production requires JWT issuer and audience")
        staging_hs256 = (
            allow_hs256_staging()
            and not asymmetric
            and algorithm == "HS256"
            and bool(secret)
        )
        if not asymmetric and not staging_hs256:
            raise ValueError(
                "production requires JWKS/public key; staging HS256 additionally "
                "requires JWT_ALLOW_HS256_STAGING=1 and a tenant-scoped HS256 secret"
            )
    return True


def verify_end_user(tenant_id: str, token: str):
    """Verify an end-user JWT against the TENANT's jwt config (tenant_config section
    'jwt': secret encrypted at rest, algorithms, issuer, audience, role_claim), falling
    back to the env JWT_SECRET when the tenant has none. When a tenant secret IS set it
    takes precedence — tokens signed with another tenant's (or the global) secret fail.

    Returns one of:
      - {present, verified: False, error}                          -> unusable token
      - {present, verified: True, user_id, claims, role_claim}     -> verified identity;
        role resolution (mappings / exact name) happens in consumer._effective.
    """
    try:
        import jwt  # optional: pip install pyjwt
    except Exception:
        return {"present": True, "verified": False, "error": "PYJWT_MISSING"}
    cfg = store.get_jwt_config(tenant_id) or {}
    issuer = (cfg.get("issuer") or "").strip()
    audience = cfg.get("audience") or ""
    jwks_url = (cfg.get("jwks_url") or "").strip()
    public_key = (cfg.get("public_key") or "").strip()
    secret = cfg.get("secret") or (None if _production() else os.getenv("JWT_SECRET"))
    raw_algs = cfg.get("algorithms") or (
        ["RS256"] if (jwks_url or public_key) else ["HS256"]
    )
    algorithms = [str(a).upper() for a in raw_algs]

    if _production():
        if not issuer or not audience:
            return {"present": True, "verified": False, "error": "JWT_ISSUER_AUDIENCE_REQUIRED"}
        asymmetric = bool(jwks_url or public_key)
        staging_hs256 = (
            allow_hs256_staging()
            and not asymmetric
            and bool(secret)
            and algorithms == ["HS256"]
        )
        valid_asymmetric = (
            asymmetric
            and bool(algorithms)
            and all(a in _ASYMMETRIC_ALGS for a in algorithms)
        )
        if not (valid_asymmetric or staging_hs256):
            return {"present": True, "verified": False, "error": "JWT_ASYMMETRIC_REQUIRED"}
    elif not (jwks_url or public_key or secret):
        return {"present": True, "verified": False, "error": "JWT_VERIFY_UNAVAILABLE"}

    allowed = _ASYMMETRIC_ALGS if (jwks_url or public_key) else _SYMMETRIC_ALGS
    if not algorithms or any(a not in allowed for a in algorithms):
        return {"present": True, "verified": False, "error": "JWT_ALGORITHM_INVALID"}

    kwargs = {
        "options": {
            "require": ["exp", "iat"] if _production() else [],
            "verify_aud": bool(audience),
            "verify_iss": bool(issuer),
        },
    }
    if audience:
        kwargs["audience"] = audience
    if issuer:
        kwargs["issuer"] = issuer
    try:
        key = _jwks_signing_key(jwks_url, token) if jwks_url else (public_key or secret)
        data = jwt.decode(token, key, algorithms=algorithms, leeway=_JWT_LEEWAY, **kwargs)
    except (ValueError, OSError):
        return {"present": True, "verified": False, "error": "JWT_KEY_UNAVAILABLE"}
    except Exception:
        return {"present": True, "verified": False, "error": "JWT_INVALID"}
    return {"present": True, "verified": True, "claims": data,
            "user_id": data.get("sub") or data.get("user_id"),
            "role_claim": cfg.get("role_claim") or "role"}
