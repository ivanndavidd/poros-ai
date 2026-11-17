"""Small, side-effect-free runtime exposure policy helpers.

Single source of truth for "is this a hardened deployment?". Nine modules used to carry
their own copy of `(os.getenv("APP_ENV") or "dev") == "production"`, and every copy
defaulted to PERMISSIVE — so an unset variable, or `prod` instead of `production`,
silently turned off dev-default secret checks, private-network egress blocking, API-doc
hiding, CORS restrictions and Postgres row-level security, all at once and with no signal.

The default is now inverted: absent or unrecognised means HARDENED. A relaxed local
environment must say so out loud (`APP_ENV=dev`, which backend/.env.example sets and
tests/conftest.py sets), and a typo fails at startup instead of quietly disarming.
"""
import os

# "staging" is RELAXED, and that is a deliberate walk-back, not an oversight. The security
# review's remediation said only dev/local/test would relax, which put staging under the
# full production gate — including an off-host HTTPS audit collector and ADMIN_DB_RLS=1,
# neither of which the staging environment has. The result was a CrashLoopBackOff on a
# deployment whose APP_ENV=staging had been valid and running for months: the value did not
# change, its meaning did. Restoring it here keeps the part of the fix that mattered (an
# UNSET or misspelled APP_ENV no longer silently disarms everything) without holding a
# pre-production environment to a requirement nobody had provisioned for it.
#
# To harden staging later, do it in this order and it cannot take the site down: put the
# secrets in place, read GET /admin/security-status on the running instance until
# would_block_hardened_start is empty, and only then move "staging" to HARDENED_ENVS.
PERMISSIVE_ENVS = frozenset({"dev", "local", "test", "staging"})
HARDENED_ENVS = frozenset({"production", "prod"})


def app_env() -> str:
    """The configured APP_ENV, lowercased. "" when unset."""
    return (os.getenv("APP_ENV") or "").strip().lower()


def production() -> bool:
    """True when hardened checks apply. Raises on a value that is neither list.

    Rejecting an unknown value is deliberate: `APP_ENV=prodction` is a configuration bug,
    and the old code answered it with "not production, carry on".
    """
    env = app_env()
    if not env:
        return True                      # unset => hardened (see module docstring)
    if env in PERMISSIVE_ENVS:
        return False
    if env in HARDENED_ENVS:
        return True
    raise RuntimeError(
        f"Refusing to start: APP_ENV={env!r} is not recognised. Use one of "
        f"{sorted(HARDENED_ENVS)} for a deployment, or {sorted(PERMISSIVE_ENVS)} locally.")


def docs_enabled() -> bool:
    if not production():
        return True
    return (os.getenv("EXPOSE_API_DOCS") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def cors_origins() -> list[str]:
    raw = os.getenv("CORS_ORIGINS")
    origins = ([x.strip() for x in raw.split(",") if x.strip()]
               if raw is not None
               else ([] if production()
                     else ["http://localhost:5173", "http://127.0.0.1:5173"]))
    if production() and "*" in origins:
        raise RuntimeError("Refusing to start: production CORS_ORIGINS may not contain '*'")
    return origins
