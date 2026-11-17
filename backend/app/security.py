"""Startup security checks — fail HARD on insecure defaults in a real deployment.

Called once from main.py at import. Which mode applies comes from `runtime_config`, not
from a local string comparison: absent or unrecognised APP_ENV is treated as HARDENED, so
forgetting the variable can no longer disarm the checks silently. `APP_ENV=dev`/`local`/
`test` gives the old warnings-only behaviour and must be stated explicitly.

  * dev / local / test -> warnings only; local experience unchanged.
  * anything else      -> RuntimeError with actionable messages when dev-grade secrets
                          are still in place (default ADMIN_KEY, unset ADMIN_SECRET_KEY,
                          missing `cryptography`), or when SECRETS_BACKEND=vault is
                          configured but Vault is unreachable/unauthenticated.

`security_report()` returns the same findings WITHOUT raising, so an operator can see what
a hardened start would reject while still running relaxed — use it before flipping APP_ENV.
"""
import os
from urllib.parse import urlparse

from . import secrets_util
from .auth import DEV_ADMIN_KEY, allow_hs256_staging
from .runtime_config import app_env, production

_LEGACY_JWT_SECRETS = {
    "tenantos-dev-jwt-secret-change-me-please-32",
    "test-secret",
}


def _truthy(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def startup_check():
    strict = production()               # raises on an unrecognised APP_ENV
    warnings = []
    if secrets_util.backend() == "vault":
        vp = secrets_util.vault_check()
        if vp and strict:
            raise RuntimeError("Refusing to start: SECRETS_BACKEND=vault is not usable:\n  - "
                               + "\n  - ".join(vp) + "\nFix the Vault config/connectivity and restart.")
        warnings += [f"SECRETS_BACKEND=vault: {p} — storing/reading Vault secrets will fail until fixed"
                     for p in vp]
    warnings += _relaxed_warnings() if not strict else []

    if strict:
        problems = _hardened_problems()
        if problems:
            raise RuntimeError(
                f"Refusing to start: APP_ENV={app_env() or '(unset)'} requires hardened "
                "configuration, but:\n  - "
                + "\n  - ".join(problems)
                + "\nSet the environment variables above and restart, or set APP_ENV=dev "
                  "deliberately for a local run."
            )

    for w in warnings:
        print(f"[security] WARNING: {w}")


def _relaxed_warnings() -> list:
    jwt_secret = (os.getenv("JWT_SECRET") or "").strip()
    if not jwt_secret:
        return ["JWT_SECRET is unset — /v1 consumer calls (strict Pattern B) will fail JWT "
                "verification for tenants that have no per-tenant jwt config; set JWT_SECRET "
                "or configure each tenant's config/jwt"]
    return ["tenants WITHOUT a per-tenant jwt config all share the env JWT_SECRET, so their "
            "tokens are not tenant-bound (a token minted for one tenant verifies for another "
            "if role names collide). For multi-tenant production set a per-tenant secret "
            "and/or audience/issuer via POST /admin/tenants/{id}/config/jwt"]


_MIN_SECRET_LEN = 32

# Secrets whose only requirement is "long enough". Kept as DATA rather than a wall of
# near-identical `if len(...) < 32` branches: adding the next one is a tuple, and the
# checks stay readable as a list of what production needs.
# (env names tried in order, message when none of them is long enough)
_LENGTH_CHECKS = (
    (("API_KEY_PEPPER",),
     "API_KEY_PEPPER is unset or shorter than 32 characters — set one stable "
     "secret before issuing or migrating consumer API keys"),
    (("DOCUMENT_ENCRYPTION_KEY", "ADMIN_SECRET_KEY"),
     "DOCUMENT_ENCRYPTION_KEY (or its ADMIN_SECRET_KEY fallback) is unset "
     "or shorter than 32 characters; uploaded originals require a stable key"),
    (("ADMIN_AUDIT_CHAIN_KEY",),
     "ADMIN_AUDIT_CHAIN_KEY is unset or shorter than 32 characters"),
    (("ADMIN_AUDIT_SINK_HMAC_KEY",),
     "ADMIN_AUDIT_SINK_HMAC_KEY is unset or shorter than 32 characters"),
)


def _env_first(*names) -> str:
    """First non-blank value among `names`, "" if none — the documented fallback chains."""
    for name in names:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return ""


def _secret_problems() -> list:
    problems = []
    admin_key = (os.getenv("ADMIN_KEY") or "").strip()
    if not admin_key or admin_key == DEV_ADMIN_KEY or len(admin_key) < _MIN_SECRET_LEN:
        problems.append("ADMIN_KEY is unset, the legacy dev default, or shorter than 32 characters "
                        "— set a strong unique value (it protects the entire /admin control plane)")
    problems += [message for names, message in _LENGTH_CHECKS
                 if len(_env_first(*names)) < _MIN_SECRET_LEN]
    if (os.getenv("JWT_SECRET") or "").strip() in _LEGACY_JWT_SECRETS:
        problems.append("JWT_SECRET is a known development value — rotate it, or remove the global "
                        "fallback and configure a strong JWT secret/issuer/audience per tenant")
    return problems


def _deployment_problems() -> list:
    problems = []
    try:
        session_hours = int(os.getenv("CONSOLE_SESSION_HOURS", "12"))
        if session_hours < 1 or session_hours > 24:
            problems.append("CONSOLE_SESSION_HOURS must be between 1 and 24 in production")
    except ValueError:
        problems.append("CONSOLE_SESSION_HOURS must be an integer between 1 and 24")
    if _truthy("APPS_GATEWAY_TRUST_HEADER") and not _truthy("APPS_GATEWAY_NETWORK_ISOLATED"):
        problems.append("APPS_GATEWAY_TRUST_HEADER is enabled without "
                        "APPS_GATEWAY_NETWORK_ISOLATED=1 — isolate the backend so only Apps Gateway "
                        "can reach it before trusting X-User-Id")
    if not _truthy("ADMIN_DB_RLS"):
        problems.append("ADMIN_DB_RLS=1 is required in production for PostgreSQL tenant isolation")
    sink_url = (os.getenv("ADMIN_AUDIT_SINK_URL") or "").strip()
    if not sink_url or urlparse(sink_url).scheme != "https":
        problems.append("ADMIN_AUDIT_SINK_URL must be an HTTPS off-host audit collector")
    return problems


def _hardened_problems() -> list:
    """Everything a hardened start refuses. Pure — never raises, never mutates.

    Split out of startup_check() so `security_report()` can show an operator exactly what
    would block, from a still-running relaxed instance, BEFORE they flip APP_ENV.
    """
    return (_secret_problems() + _deployment_problems()
            + secrets_util.production_problems())


def security_report() -> dict:
    """What mode this instance is in, and what a hardened start would refuse.

    The point is to make flipping APP_ENV a checked move rather than a gamble: a relaxed
    instance can report the exact list that would block it, so the gaps get closed BEFORE
    the switch instead of being discovered as a container that will not boot.

    Reports only WHICH checks fail, never a secret value.
    """
    env = app_env()
    try:
        strict = production()
        recognised = True
    except RuntimeError:
        strict, recognised = True, False
    problems = _hardened_problems()
    return {
        "app_env": env or None,
        "app_env_recognised": recognised,
        "hardened": strict,
        "hardened_by_default": not env,      # nothing set => hardened, not relaxed
        "would_block_hardened_start": problems,
        "ready_for_hardened_start": not problems,
        "warnings": [] if strict else _relaxed_warnings(),
        "secrets_backend": secrets_util.backend(),
    }


def post_store_check():
    """Production checks that require initialized PostgreSQL metadata."""
    if not production():
        return
    from . import store

    problems = []
    c = store._c()
    try:
        active_tenants = {
            row["tenant_id"]
            for row in c.execute(
                "SELECT DISTINCT tenant_id FROM api_keys WHERE active=1"
            ).fetchall()
        }
        for tenant_id in active_tenants:
            row = c.execute(
                "SELECT data FROM tenant_config WHERE tenant_id=? AND section='jwt'",
                (tenant_id,),
            ).fetchone()
            try:
                cfg = __import__("json").loads(row["data"]) if row else {}
            except Exception:
                cfg = {}
            algorithms = {str(x).upper() for x in (cfg.get("algorithms") or [])}
            valid_asymmetric = (
                (cfg.get("jwks_url") or cfg.get("public_key"))
                and cfg.get("issuer")
                and cfg.get("audience")
                and algorithms
                and algorithms <= {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}
            )
            valid_staging_hs256 = (
                allow_hs256_staging()
                and bool(cfg.get("secret"))
                and bool(cfg.get("issuer"))
                and bool(cfg.get("audience"))
                and algorithms == {"HS256"}
            )
            if not (valid_asymmetric or valid_staging_hs256):
                problems.append(
                    f"tenant {tenant_id} has an active API key but no production-grade "
                    "JWT config (JWKS/public key + issuer + audience; or the explicit "
                    "staging-only HS256 exception)"
                )

        if secrets_util.backend() == "vault":
            refs = [
                row["conn"] for row in c.execute("SELECT conn FROM datasources").fetchall()
            ]
            refs += [
                row["api_key"] for row in c.execute(
                    "SELECT api_key FROM llm_providers WHERE api_key IS NOT NULL AND api_key<>''"
                ).fetchall()
            ]
            for row in c.execute(
                "SELECT data FROM tenant_config WHERE section='jwt'"
            ).fetchall():
                try:
                    refs.append(__import__("json").loads(row["data"]).get("secret") or "")
                except Exception:
                    pass
            if any(ref and not secrets_util.is_versioned_vault_ref(ref) for ref in refs):
                problems.append(
                    "unversioned/legacy secret references remain; run "
                    "backend/scripts/migrate_secrets_to_vault.py before production"
                )
    finally:
        c.close()
    if problems:
        raise RuntimeError(
            "Refusing to start: production metadata is not P1-ready:\n  - "
            + "\n  - ".join(problems)
        )
