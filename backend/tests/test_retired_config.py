"""The Jev (TypeSafe System One) integration was removed; its stored settings go with it.

Each tenant that had switched Jev on kept a ``tenant_config`` row in section
``systemone``, including an encrypted API key. Nothing reads that section any more, so
``store.init()`` deletes those rows on startup and purges the stored key; a second
startup finds nothing and changes nothing.
"""
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store                        # noqa: E402
from app.main import app                     # noqa: E402

client = TestClient(app)
AK = {"X-Admin-Key": "admin-dev-key"}


def test_startup_deletes_retired_jev_settings_and_purges_the_key(monkeypatch):
    tid = client.post("/admin/tenants", headers=AK, json={"name": "Retired Jev Co"}).json()["id"]
    try:
        store.set_tenant_config(tid, "systemone", {"enabled": True, "checks": ["pii"],
                                                   "api_key": "vault:tenantos/jev/x",
                                                   "api_key_last4": "abcd"})
        store.set_tenant_config(tid, "memory", {"persistent": True})
        purged = []
        monkeypatch.setattr(store.sec, "purge", purged.append)

        store.init()

        assert store.get_tenant_config(tid, "systemone") is None
        assert store.get_tenant_config(tid, "memory") == {"persistent": True}   # untouched
        assert purged == ["vault:tenantos/jev/x"]

        store.init()                                                            # idempotent
        assert purged == ["vault:tenantos/jev/x"]
    finally:
        store.delete_tenant(tid)


def test_the_jev_endpoints_are_gone():
    """No status, test or observations endpoint is left to call."""
    tid = client.post("/admin/tenants", headers=AK, json={"name": "No Jev Co"}).json()["id"]
    try:
        assert client.get(f"/admin/tenants/{tid}/systemone/observations",
                          headers=AK).status_code == 404
        assert client.post(f"/admin/tenants/{tid}/systemone/test",
                           headers=AK).status_code in (404, 405)
        assert client.get("/admin/systemone/status", headers=AK).status_code == 404
    finally:
        store.delete_tenant(tid)
