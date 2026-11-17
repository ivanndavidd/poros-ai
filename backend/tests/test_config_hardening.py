"""Findings 10, 19 and 23 — the three that were about configuration, not logic.

  10: /admin/test-connection took a connection config with no tenant and returned the
      raw driver exception, which turns "test this datasource" into an outbound port
      probe with a readable oracle.
  19: every hardening switch hung on `APP_ENV == "production"`, evaluated independently
      in nine modules, each defaulting to PERMISSIVE.
  23: nothing pinned the dependency that IS the governance boundary.

Offline: no live database or warehouse is contacted.
"""
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import runtime_config, security                      # noqa: E402
from app.main import app                                      # noqa: E402
# aliased: a module-level name starting with `Test` is collected as a test class
from app.models import TestConnIn as ConnTestInput             # noqa: E402

client = TestClient(app)
AK = {"X-Admin-Key": "admin-dev-key"}


# ---- finding 10: the connection tester is scoped, and stays quiet on failure ----
def test_test_connection_requires_a_tenant():
    """Without tenant_id there is nothing to authorize against, so the field is required
    rather than optional-and-ignored."""
    with pytest.raises(Exception):
        ConnTestInput(type="postgres", conn={})
    r = client.post("/admin/test-connection", headers=AK,
                    json={"type": "postgres", "conn": {"host": "127.0.0.1"}})
    assert r.status_code == 422, r.text


def test_test_connection_failure_does_not_echo_the_driver_error():
    """The old handler interpolated the exception, so the response distinguished
    "connection refused" from "host unreachable" from "auth failed" — an oracle for
    probing the internal network. Now it answers the same way every time and puts the
    detail behind a request id in the backend log."""
    # No credential field: port 1 refuses the connection before auth is ever reached, so
    # the test needs nothing that looks like a password (Sonar python:S2068).
    r = client.post("/admin/test-connection", headers=AK,
                    json={"tenant_id": "t_missing", "type": "postgres",
                          "conn": {"host": "127.0.0.1", "port": 1, "database": "x",
                                   "user": "x"}})
    assert r.status_code in (400, 403, 404), r.text
    body = r.text.lower()
    for leak in ("traceback", "psycopg2", "connection refused", "could not connect",
                 "operationalerror"):
        assert leak not in body, f"response leaks {leak!r}: {r.text[:300]}"


# ---- finding 19: absent or misspelled APP_ENV means HARDENED ----
def test_unset_app_env_is_hardened_not_permissive(monkeypatch):
    """The whole finding in one assertion: forgetting the variable used to disable
    dev-secret checks, egress blocking, doc hiding, CORS limits and Postgres RLS."""
    monkeypatch.delenv("APP_ENV", raising=False)
    assert runtime_config.production() is True


@pytest.mark.parametrize("value", ["dev", "local", "test", "staging", "DEV", " dev "])
def test_only_the_named_relaxed_envs_relax(monkeypatch, value):
    monkeypatch.setenv("APP_ENV", value)
    assert runtime_config.production() is False


def test_staging_is_relaxed_on_purpose(monkeypatch):
    """Regression test for a real outage, so nobody "tidies" staging back into the
    hardened set without doing the provisioning first.

    Hardening staging demanded ADMIN_AUDIT_SINK_URL (an off-host HTTPS collector) and
    ADMIN_DB_RLS=1. The staging deployment had neither, so a long-valid APP_ENV=staging
    stopped booting — the value never changed, its meaning did. Moving staging back into
    HARDENED_ENVS is a legitimate future change; it just has to come AFTER
    /admin/security-status reports ready_for_hardened_start on that environment.
    """
    monkeypatch.setenv("APP_ENV", "staging")
    assert runtime_config.production() is False
    assert "staging" not in runtime_config.HARDENED_ENVS


@pytest.mark.parametrize("value", ["production", "prod", "PRODUCTION"])
def test_deployment_envs_are_hardened(monkeypatch, value):
    monkeypatch.setenv("APP_ENV", value)
    assert runtime_config.production() is True


@pytest.mark.parametrize("typo", ["prodction", "produciton", "development", "qa"])
def test_a_typo_fails_loudly_instead_of_disarming(monkeypatch, typo):
    """`APP_ENV=prodction` used to answer "not production, carry on"."""
    monkeypatch.setenv("APP_ENV", typo)
    with pytest.raises(RuntimeError, match="not recognised"):
        runtime_config.production()


def test_startup_check_refuses_a_hardened_start_on_dev_secrets(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ADMIN_KEY", "admin-dev-key")
    with pytest.raises(RuntimeError, match="hardened configuration"):
        security.startup_check()


def test_security_report_shows_the_gaps_without_raising(monkeypatch):
    """The operator-facing half: a RELAXED instance must be able to list exactly what
    would block a hardened start, so flipping APP_ENV is checked rather than attempted."""
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("ADMIN_KEY", "admin-dev-key")
    report = security.security_report()
    assert report["hardened"] is False
    assert report["app_env"] == "dev"
    assert report["ready_for_hardened_start"] is False
    assert any("ADMIN_KEY" in p for p in report["would_block_hardened_start"])
    # names the failing check, never the value
    assert "admin-dev-key" not in str(report)


def test_security_report_survives_an_unrecognised_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prodction")
    report = security.security_report()
    assert report["app_env_recognised"] is False and report["hardened"] is True


def test_security_status_endpoint_is_admin_only():
    assert client.get("/admin/security-status").status_code in (401, 403)
    r = client.get("/admin/security-status", headers=AK)
    assert r.status_code == 200 and "would_block_hardened_start" in r.json()


# ---- finding 23: the parser that enforces governance cannot float ----
_REQ = Path(__file__).resolve().parents[1] / "requirements.txt"
_REQ_DEV = Path(__file__).resolve().parents[1] / "requirements-dev.txt"


def _requirements(path):
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            yield line


@pytest.mark.parametrize("path", [_REQ, _REQ_DEV], ids=["requirements", "requirements-dev"])
def test_every_dependency_is_pinned(path):
    """A floating range means `pip install` on a new day can change behaviour under the
    security controls without a single line of the repo changing."""
    unpinned = [r for r in _requirements(path) if "==" not in r]
    assert not unpinned, f"unpinned in {path.name}: {unpinned}"


def test_sqlglot_pin_matches_what_the_suite_actually_verified():
    """sqlglot IS the governance boundary: govern() decides what a query means by reading
    its AST, and the read-only bypass fix depends on which args land on an exp.Select.
    A pin that has drifted from the installed version means the adversarial suite proved
    something about a parser nobody is running."""
    import sqlglot
    pinned = next(r for r in _requirements(_REQ) if r.lower().startswith("sqlglot"))
    version = re.sub(r"^sqlglot\s*==\s*", "", pinned, flags=re.I).strip()
    assert version == sqlglot.__version__, (
        f"requirements.txt pins sqlglot=={version} but {sqlglot.__version__} is installed; "
        "re-run tests/test_govern_adversarial.py before moving the pin")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
