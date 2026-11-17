"""Print only aggregate P0 migration state; never print credentials or hashes."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # backend/ on sys.path

from app import store  # noqa: E402


store.init()
c = store._c()
total = c.execute("SELECT COUNT(*) n FROM api_keys").fetchone()["n"]
unhashed = c.execute(
    "SELECT COUNT(*) n FROM api_keys WHERE key_hash IS NULL OR key_hash=''"
).fetchone()["n"]
plaintext_usage = c.execute(
    "SELECT COUNT(*) n FROM usage_log WHERE left(api_key,3)='sk_'"
).fetchone()["n"]
legacy_fernet = (
    c.execute("SELECT COUNT(*) n FROM datasources WHERE left(conn,4)='enc:'").fetchone()["n"]
    + c.execute("SELECT COUNT(*) n FROM llm_providers WHERE left(api_key,4)='enc:'").fetchone()["n"]
    + c.execute(
        "SELECT COUNT(*) n FROM tenant_config WHERE section='jwt' AND data LIKE '%enc:%'"
    ).fetchone()["n"]
)
vault_total = (
    c.execute("SELECT COUNT(*) n FROM datasources WHERE left(conn,6)='vault:'").fetchone()["n"]
    + c.execute("SELECT COUNT(*) n FROM llm_providers WHERE left(api_key,6)='vault:'").fetchone()["n"]
    + c.execute(
        "SELECT COUNT(*) n FROM tenant_config WHERE section='jwt' AND data LIKE '%vault:%'"
    ).fetchone()["n"]
)
vault_versioned = (
    c.execute("SELECT COUNT(*) n FROM datasources WHERE left(conn,9)='vault:v2:'").fetchone()["n"]
    + c.execute("SELECT COUNT(*) n FROM llm_providers WHERE left(api_key,9)='vault:v2:'").fetchone()["n"]
    + c.execute(
        "SELECT COUNT(*) n FROM tenant_config WHERE section='jwt' AND data LIKE '%vault:v2:%'"
    ).fetchone()["n"]
)
c.close()

print({
    "api_keys_total": total,
    "unhashed_remaining": unhashed,
    "plaintext_usage_remaining": plaintext_usage,
    "legacy_fernet_secret_refs": legacy_fernet,
    "vault_refs_total": vault_total,
    "vault_refs_versioned": vault_versioned,
    "vault_refs_unversioned": vault_total - vault_versioned,
})
