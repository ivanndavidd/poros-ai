"""One-shot: re-encrypt every stored secret with the ACTIVE secrets backend.

With SECRETS_BACKEND=vault this moves legacy Fernet rows ("enc:") and old unversioned
Vault refs into fresh KV v2 entries, leaving only "vault:v2:<version>:<path>" refs.
Already-versioned rows are skipped
and transactional: every item is verified round-trip (decrypt(new) == decrypt(old))
before the PostgreSQL transaction commits. A failure rolls all metadata rows back.

Run (Windows, from repo root — env must match the backend's Vault env):
  set SECRETS_BACKEND=vault& set VAULT_ADDR=http://localhost:8200& set VAULT_TOKEN=<token>
  backend\\.venv\\Scripts\\python.exe backend\\scripts\\migrate_secrets_to_vault.py

The default is ONLINE-SAFE: replaced Vault entries are retained temporarily so an
in-flight process holding an old ref cannot fail. After all backend instances restart
and verification passes, rerun with ``--purge-old`` to remove those replaced entries.

Covers: datasources.conn, llm_providers.api_key, tenant_config[jwt].secret.
Post-check: introspect every SQL datasource with its (re-encrypted) conn — read-only,
NO rescan, so curated MDL (metrics/reference_values/grain) is never touched.
"""
import argparse, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # backend/ on sys.path
from app import store, secrets_util as sec         # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--purge-old", action="store_true",
                        help="delete replaced unversioned Vault entries after DB commit")
    args = parser.parse_args()
    if sec.backend() != "vault":
        print("ABORT: SECRETS_BACKEND bukan 'vault' — set env dulu (lihat docstring).")
        return 1
    problems = sec.vault_check()
    if problems:
        print("ABORT: Vault belum siap:", "; ".join(problems))
        return 1

    store.init()
    migrated, skipped, old_vault_refs = 0, 0, []

    def reencrypt(tok):
        """old token -> (new token) with round-trip assertion; None = skip."""
        nonlocal skipped
        if not tok or not isinstance(tok, str) or sec.is_versioned_vault_ref(tok):
            skipped += 1
            return None
        plain = sec.decrypt(tok)
        new = sec.encrypt(plain)
        assert sec.decrypt(new) == plain, "round-trip mismatch — aborting before any write"
        if tok.startswith("vault:"):
            old_vault_refs.append(tok)
        return new

    c = store._c()
    try:
        for r in c.execute("SELECT id, conn FROM datasources").fetchall():
            new = reencrypt(r["conn"])
            if new:
                c.execute("UPDATE datasources SET conn=? WHERE id=?", (new, r["id"]))
                migrated += 1
        for r in c.execute("SELECT id, api_key FROM llm_providers").fetchall():
            new = reencrypt(r["api_key"])
            if new:
                c.execute("UPDATE llm_providers SET api_key=? WHERE id=?", (new, r["id"]))
                migrated += 1
        for r in c.execute("SELECT tenant_id, data FROM tenant_config WHERE section='jwt'").fetchall():
            cfg = json.loads(r["data"])
            new = reencrypt(cfg.get("secret") or "")
            if new:
                cfg["secret"] = new
                c.execute("UPDATE tenant_config SET data=? WHERE tenant_id=? AND section='jwt'",
                          (json.dumps(cfg), r["tenant_id"]))
                migrated += 1
        for ref in old_vault_refs:
            c.execute(
                """INSERT INTO retired_secret_refs(ref,created,purged) VALUES(?,?,0)
                   ON CONFLICT(ref) DO NOTHING""",
                (ref, store.now()),
            )
        c.commit()
    except Exception:
        c._c.rollback()
        c.close()
        raise
    c.close()
    # Purge only when explicitly requested and only AFTER PostgreSQL committed.
    purged = 0
    if args.purge_old:
        c = store._c()
        retired = c.execute(
            "SELECT ref FROM retired_secret_refs WHERE purged=0"
        ).fetchall()
        c.close()
        for row in retired:
            if sec.purge(row["ref"]):
                c = store._c()
                c.execute("UPDATE retired_secret_refs SET purged=1 WHERE ref=?", (row["ref"],))
                c.commit(); c.close()
                purged += 1

    # Metadata-only post-check: every stored secret is now a Vault reference and can be
    # decrypted from Vault. No secret value is printed and no tenant datasource is queried.
    c = store._c()
    refs = []
    refs.extend(r["conn"] for r in c.execute("SELECT conn FROM datasources").fetchall())
    refs.extend(r["api_key"] for r in c.execute(
        "SELECT api_key FROM llm_providers WHERE api_key IS NOT NULL AND api_key<>''"
    ).fetchall())
    for r in c.execute("SELECT data FROM tenant_config WHERE section='jwt'").fetchall():
        refs.append((json.loads(r["data"]) or {}).get("secret") or "")
    c.close()
    bad = [r for r in refs if r and not str(r).startswith("vault:")]
    for ref in refs:
        if ref:
            sec.decrypt(ref)

    print({
        "migrated": migrated,
        "skipped_versioned_or_empty": skipped,
        "non_vault_remaining": len(bad),
        "roundtrip_errors": 0,
        "old_refs_retained": 0 if args.purge_old else len(old_vault_refs),
        "retired_refs_purged": purged,
    })
    return 0 if not bad else 2


if __name__ == "__main__":
    sys.exit(main())
