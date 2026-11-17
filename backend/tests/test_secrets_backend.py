"""Secrets backend seam: Fernet (default, unchanged) + Vault (KV v2) dual-read.

Run:  cd backend && python -m pytest tests/test_secrets_backend.py -q

Vault is FAKED at the single HTTP seam (secrets_util._http), so these tests need
no running Vault. NOTE: only `app.secrets_util` is imported at module scope —
`app.store` reads ADMIN_DB_PATH at import, and test_smoke.py must stay free to
set that env before ITS import; store/security are imported lazily inside tests
and pointed at per-test tmp DBs.
"""
import os, re, sys, json
import psycopg2

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)

from app import secrets_util as sec  # noqa: E402  (reads env at CALL time only — safe to import early)


class FakeVault:
    """In-memory stand-in for the Vault HTTP API (KV v2 + AppRole + health)."""
    def __init__(self):
        self.kv = {}                      # path -> stored data dict
        self.reads = 0
        self.logins = 0
        self.valid_tokens = {"root-token"}
        self.approle = ("rid-1", "sid-1")
        self.down = False

    def http(self, method, url, body=None, token=None):
        if self.down:
            raise RuntimeError(f"Vault unreachable at {url}: connection refused")
        path = url.split("/v1/", 1)[1]
        if path == "sys/health":
            return 200, {"initialized": True, "sealed": False}
        if path == "auth/approle/login":
            self.logins += 1
            if (body or {}).get("role_id") == self.approle[0] and (body or {}).get("secret_id") == self.approle[1]:
                t = f"approle-token-{self.logins}"
                self.valid_tokens.add(t)
                return 200, {"auth": {"client_token": t, "lease_duration": 300}}
            return 400, {"errors": ["invalid role or secret id"]}
        if path == "auth/token/lookup-self":
            return (200, {"data": {}}) if token in self.valid_tokens else (403, {"errors": ["permission denied"]})
        if token not in self.valid_tokens:
            return 403, {"errors": ["permission denied"]}
        if path.startswith("secret/data/"):
            key = path[len("secret/data/"):].split("?", 1)[0]
            if method == "POST":
                self.kv[key] = (body or {}).get("data", {})
                return 200, {"data": {"version": 1}}
            if method == "GET":
                self.reads += 1
                if key in self.kv:
                    return 200, {"data": {"data": self.kv[key], "metadata": {"version": 1}}}
                return 404, {"errors": []}
        if path.startswith("secret/metadata/") and method == "DELETE":
            self.kv.pop(path[len("secret/metadata/"):], None)
            return 204, {}
        return 404, {"errors": [f"no handler: {method} {path}"]}


def _clear_state():
    sec._vault_cache.clear()
    sec._vault_token.update({"token": None, "expires": 0.0})


@pytest.fixture
def vault(monkeypatch):
    """Active vault backend against a FakeVault, static token auth."""
    fv = FakeVault()
    monkeypatch.setenv("SECRETS_BACKEND", "vault")
    monkeypatch.setenv("VAULT_ADDR", "http://fake-vault:8200")
    monkeypatch.setenv("VAULT_TOKEN", "root-token")
    monkeypatch.delenv("VAULT_ROLE_ID", raising=False)
    monkeypatch.delenv("VAULT_SECRET_ID", raising=False)
    monkeypatch.delenv("VAULT_CACHE_TTL", raising=False)
    monkeypatch.setattr(sec, "_http", fv.http)
    _clear_state()
    yield fv
    _clear_state()


def _store(monkeypatch, tmp_path):
    """Isolated store for the test: its own Postgres SCHEMA (the Postgres equivalent of
    the old per-test admin.db file), dropped + recreated clean each run."""
    from app import store
    schema = "sec_" + re.sub(r"\W", "_", os.path.basename(str(tmp_path)))[:40]
    admin = psycopg2.connect(store.DB_URL); admin.autocommit = True
    admin.cursor().execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE; CREATE SCHEMA "{schema}"')
    admin.close()
    monkeypatch.setenv("ADMIN_DB_SCHEMA", schema)
    store.init()
    return store


# ---- backend selection + round-trips ----
def test_default_backend_fernet_unchanged(monkeypatch):
    monkeypatch.delenv("SECRETS_BACKEND", raising=False)
    assert sec.backend() == "fernet"
    tok = sec.encrypt("s3cret")
    assert tok.startswith("enc:") and "s3cret" not in tok
    assert sec.decrypt(tok) == "s3cret"


def test_vault_encrypt_returns_ref_and_roundtrip(vault):
    tok = sec.encrypt("db-password-1")
    assert tok.startswith("vault:") and "db-password-1" not in tok
    assert tok.startswith("vault:v2:1:")
    assert sec.decrypt(tok) == "db-password-1"
    assert len(vault.kv) == 1
    assert list(vault.kv.values())[0] == {"v": "db-password-1"}   # secret lives in Vault, not SQLite


def test_vault_json_roundtrip(vault):
    conn = {"host": "10.0.0.5", "user": "svc", "password": "pw!"}
    tok = sec.encrypt_json(conn)
    assert tok.startswith("vault:")
    assert sec.decrypt_json(tok) == conn


# ---- dual-read in both directions ----
def test_dual_read_legacy_enc_while_vault_active(vault, monkeypatch):
    monkeypatch.setenv("SECRETS_BACKEND", "fernet")
    legacy = sec.encrypt("old-secret")
    assert legacy.startswith("enc:")
    monkeypatch.setenv("SECRETS_BACKEND", "vault")
    assert sec.decrypt(legacy) == "old-secret"     # old rows keep working after the switch


def test_dual_read_vault_ref_while_fernet_active(vault, monkeypatch):
    tok = sec.encrypt("vaulted")
    monkeypatch.setenv("SECRETS_BACKEND", "fernet")   # rollback scenario: refs stay readable
    _clear_state()
    assert sec.decrypt(tok) == "vaulted"


# ---- cache ----
def test_cache_spares_reads_and_ttl_expiry(vault, monkeypatch):
    tok = sec.encrypt("cached-value")
    assert sec.decrypt(tok) == "cached-value"
    assert sec.decrypt(tok) == "cached-value"
    assert vault.reads == 0                        # write pre-warmed the cache
    sec._vault_cache.clear()
    assert sec.decrypt(tok) == "cached-value"
    assert sec.decrypt(tok) == "cached-value"
    assert vault.reads == 1                        # one cold read, then cached again
    monkeypatch.setenv("VAULT_CACHE_TTL", "0")     # TTL applies when an entry is STORED
    sec._vault_cache.clear()
    assert sec.decrypt(tok) == "cached-value"      # cold read, stored already-expired
    assert sec.decrypt(tok) == "cached-value"      # expired -> reads again
    assert vault.reads == 3


# ---- purge + failure modes ----
def test_purge_deletes_and_never_raises(vault):
    tok = sec.encrypt("to-be-deleted")
    sec.purge(tok)
    assert vault.kv == {}
    with pytest.raises(RuntimeError):
        sec.decrypt(tok)
    sec.purge(tok)                                 # already gone: no raise
    vault.down = True
    sec.purge(tok)                                 # Vault down: still no raise
    sec.purge("enc:whatever")                      # non-vault tokens: no-op
    sec.purge(None)


def test_encrypt_fails_loud_when_vault_down(vault):
    vault.down = True
    with pytest.raises(RuntimeError):              # never a silent fallback to fernet/plaintext
        sec.encrypt("x")
    vault.down = False
    tok = sec.encrypt("x")
    vault.down = True
    sec._vault_cache.clear()
    with pytest.raises(RuntimeError):
        sec.decrypt(tok)


def test_missing_vault_addr_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    with pytest.raises(RuntimeError) as e:
        sec.decrypt("vault:tenantos/deadbeef")
    assert "VAULT_ADDR" in str(e.value)


# ---- AppRole ----
def test_approle_login_and_relogin_on_stale_token(vault, monkeypatch):
    monkeypatch.delenv("VAULT_TOKEN")
    monkeypatch.setenv("VAULT_ROLE_ID", "rid-1")
    monkeypatch.setenv("VAULT_SECRET_ID", "sid-1")
    tok = sec.encrypt("via-approle")
    assert vault.logins == 1
    assert sec.decrypt(tok) == "via-approle"
    assert vault.logins == 1                       # token reused within its lease
    # server-side revocation -> next op gets 403, re-logins ONCE and succeeds
    vault.valid_tokens = {t for t in vault.valid_tokens if not t.startswith("approle")}
    sec._vault_cache.clear()
    assert sec.decrypt(tok) == "via-approle"
    assert vault.logins == 2


# ---- startup / production guards ----
def test_production_problems_backend_aware(monkeypatch):
    monkeypatch.setenv("SECRETS_BACKEND", "vault")
    for k in ("VAULT_ADDR", "VAULT_TOKEN", "VAULT_ROLE_ID", "VAULT_SECRET_ID", "ADMIN_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)
    probs = " | ".join(sec.production_problems())
    assert "VAULT_ADDR" in probs and "auth" in probs
    assert "ADMIN_SECRET_KEY" not in probs         # fernet checks don't apply in vault mode
    monkeypatch.setenv("VAULT_ADDR", "http://v:8200")
    monkeypatch.setenv("VAULT_TOKEN", "tenantos-dev-root")
    assert any("dev-mode root token" in p for p in sec.production_problems())
    monkeypatch.setenv("VAULT_TOKEN", "s.real-token")
    assert sec.production_problems() == []


def test_vault_check_health_and_auth(vault, monkeypatch):
    assert sec.vault_check() == []
    monkeypatch.setenv("VAULT_TOKEN", "wrong-token")
    assert any("rejected" in p for p in sec.vault_check())
    monkeypatch.setenv("VAULT_TOKEN", "root-token")
    vault.down = True
    assert any("reach" in p for p in sec.vault_check())


def test_startup_check_vault_dev_warns_production_raises(vault, monkeypatch):
    from app import security
    vault.down = True
    monkeypatch.setenv("APP_ENV", "dev")
    security.startup_check()                       # dev: warning only, must not raise
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(RuntimeError) as e:
        security.startup_check()
    assert "vault" in str(e.value).lower()
    # healthy Vault + strong admin key -> production passes WITHOUT ADMIN_SECRET_KEY (vault mode)
    vault.down = False
    monkeypatch.setenv("ADMIN_KEY", "a-strong-admin-key-1234567890-extra")
    monkeypatch.setenv("JWT_SECRET", "a-strong-jwt-secret-12345678901234567890")
    monkeypatch.setenv("API_KEY_PEPPER", "a-strong-api-key-pepper-123456789012345")
    monkeypatch.setenv("ADMIN_DB_RLS", "1")
    monkeypatch.setenv("ADMIN_AUDIT_CHAIN_KEY", "audit-chain-key-at-least-32-characters")
    monkeypatch.setenv("ADMIN_AUDIT_SINK_HMAC_KEY", "audit-sink-key-at-least-32-characters")
    monkeypatch.setenv("ADMIN_AUDIT_SINK_URL", "https://audit.example.test/events")
    monkeypatch.setenv("DOCUMENT_ENCRYPTION_KEY", "document-key-at-least-32-characters")
    monkeypatch.delenv("VAULT_TOKEN")
    monkeypatch.setenv("VAULT_ROLE_ID", "rid-1")
    monkeypatch.setenv("VAULT_SECRET_ID", "sid-1")
    monkeypatch.delenv("ADMIN_SECRET_KEY", raising=False)
    security.startup_check()


# ---- store integration (SQLite holds only vault: refs; delete/rotate purges) ----
def test_store_datasource_vault_roundtrip_and_purge(vault, monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    did = store.create_datasource("t_v1", "duckdb", "db", {"path": "x.duckdb", "password": "pw"}, {"tables": {}})
    c = store._c(); raw = c.execute("SELECT conn FROM datasources WHERE id=?", (did,)).fetchone()["conn"]; c.close()
    assert raw.startswith("vault:") and "pw" not in raw
    assert store.get_datasource(did)["conn"]["password"] == "pw"
    store.delete_datasource(did)
    assert vault.kv == {}


def test_store_jwt_rotation_purges_old_entry(vault, monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    store.set_jwt_config("t_v1", {"secret": "first-secret"})
    assert len(vault.kv) == 1
    store.set_jwt_config("t_v1", {"secret": "second-secret"})
    assert len(vault.kv) == 1                      # rotated: old entry purged
    assert store.get_jwt_config("t_v1")["secret"] == "second-secret"
    store.set_jwt_config("t_v1", {"secret": "", "role_claim": "roles"})   # blank keeps existing
    assert store.get_jwt_config("t_v1")["secret"] == "second-secret"
    assert store.get_jwt_config("t_v1")["role_claim"] == "roles"
    assert len(vault.kv) == 1


def test_store_provider_replacement_purges_old(vault, monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    store.upsert_provider("t_v1", "openai", "OpenAI", "sk-old")
    store.upsert_provider("t_v1", "openai", "OpenAI", "sk-new")
    assert len(vault.kv) == 1
    assert store.get_provider("t_v1", "openai")["api_key"] == "sk-new"


def test_store_delete_tenant_purges_all(vault, monkeypatch, tmp_path):
    store = _store(monkeypatch, tmp_path)
    store.create_datasource("t_v2", "duckdb", "db", {"path": "x", "password": "pw"}, {})
    store.upsert_provider("t_v2", "openai", "OpenAI", "sk-1")
    store.set_jwt_config("t_v2", {"secret": "jwt-s"})
    assert len(vault.kv) == 3
    store.delete_tenant("t_v2")
    assert vault.kv == {}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
