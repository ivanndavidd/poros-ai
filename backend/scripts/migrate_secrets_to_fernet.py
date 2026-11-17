"""One-shot REVERSE migration: rewrite every 'vault:' secret back to local Fernet ('enc:').

Use this to LEAVE Vault. It reads each `vault:` ref from Vault (dual-read) and stores it
inline in SQLite as Fernet `enc:...`, so afterwards the backend runs with NO Vault at all
(just `run_backend.bat` — no Docker/Vault). Mirror of migrate_secrets_to_vault.py.

Idempotent (enc:/obf:/plaintext/empty are skipped) and SAFE: admin.db is backed up first,
and every item is verified round-trip (decrypt(new) == plain) BEFORE its row is updated —
any mismatch aborts before any write.

Run (Windows, from repo root) with Vault reachable + UNSEALED:
  call deploy\\vault\\persistent\\vault.env.bat   :: sets VAULT_ADDR + VAULT_TOKEN + SECRETS_BACKEND=vault
  set SECRETS_BACKEND=fernet                       :: override: encrypt() must write enc:, NOT vault:
  backend\\.venv\\Scripts\\python.exe backend\\scripts\\migrate_secrets_to_fernet.py

IMPORTANT: do NOT set ADMIN_SECRET_KEY differently from what run_backend.bat uses (it leaves it
unset -> dev-default key). The Fernet key must match at write time and at every later run, or the
enc: rows won't decrypt.

Covers: datasources.conn, llm_providers.api_key, tenant_config[jwt].secret.
"""
import json, os, shutil, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))          # backend/ on sys.path
from app import store, secrets_util as sec          # noqa: E402


def main():
    if sec.backend() == "vault":
        print("ABORT: SECRETS_BACKEND masih 'vault'. Set 'fernet' supaya encrypt() menulis enc: "
              "(tetap sertakan VAULT_ADDR + VAULT_TOKEN agar vault: bisa DIBACA). Lihat docstring.")
        return 1
    if not os.getenv("VAULT_ADDR"):
        print("ABORT: VAULT_ADDR belum di-set — dibutuhkan untuk MEMBACA vault: ref. Lihat docstring.")
        return 1
    problems = sec.vault_check()
    if problems:
        print("ABORT: Vault belum siap (harus hidup + UNSEALED untuk baca secret):", "; ".join(problems))
        return 1

    backup = store.DB + time.strftime(".bak-%Y%m%d-%H%M%S")
    shutil.copy2(store.DB, backup)
    print(f"backup admin.db -> {backup}")

    migrated, skipped = [], 0

    def to_fernet(tok):
        """vault: token -> enc: token, with round-trip assertion; None = skip."""
        nonlocal skipped
        if not tok or not isinstance(tok, str) or not tok.startswith("vault:"):
            skipped += 1
            return None
        plain = sec.decrypt(tok)            # reads from Vault (dual-read)
        new = sec.encrypt(plain)            # SECRETS_BACKEND=fernet -> "enc:..."
        assert new.startswith("enc:"), (
            f"expected enc: but got {new[:8]!r} — pastikan SECRETS_BACKEND=fernet & cryptography terpasang")
        assert sec.decrypt(new) == plain, "round-trip mismatch — aborting before any write"
        return new

    c = store._c()
    for r in c.execute("SELECT id, tenant_id, name, conn FROM datasources").fetchall():
        new = to_fernet(r["conn"])
        if new:
            c.execute("UPDATE datasources SET conn=? WHERE id=?", (new, r["id"]))
            migrated.append(f"datasource {r['name']} ({r['tenant_id']})")
    for r in c.execute("SELECT id, tenant_id, provider, api_key FROM llm_providers").fetchall():
        new = to_fernet(r["api_key"])
        if new:
            c.execute("UPDATE llm_providers SET api_key=? WHERE id=?", (new, r["id"]))
            migrated.append(f"llm api_key {r['provider']} ({r['tenant_id']})")
    for r in c.execute("SELECT tenant_id, data FROM tenant_config WHERE section='jwt'").fetchall():
        cfg = json.loads(r["data"])
        new = to_fernet(cfg.get("secret") or "")
        if new:
            cfg["secret"] = new
            c.execute("UPDATE tenant_config SET data=? WHERE tenant_id=? AND section='jwt'",
                      (json.dumps(cfg), r["tenant_id"]))
            migrated.append(f"jwt secret ({r['tenant_id']})")
    c.commit(); c.close()

    print(f"\nmigrated {len(migrated)} secret(s) vault:->enc:, skipped {skipped} (sudah enc:/kosong):")
    for m in migrated:
        print("  +", m)

    # verify: no vault: refs must remain anywhere
    c = store._c()
    left = []
    for r in c.execute("SELECT name, conn FROM datasources").fetchall():
        if isinstance(r["conn"], str) and r["conn"].startswith("vault:"):
            left.append(f"datasource {r['name']}")
    for r in c.execute("SELECT provider, api_key FROM llm_providers").fetchall():
        if isinstance(r["api_key"], str) and r["api_key"].startswith("vault:"):
            left.append(f"llm {r['provider']}")
    for r in c.execute("SELECT tenant_id, data FROM tenant_config WHERE section='jwt'").fetchall():
        try:
            if str(json.loads(r["data"]).get("secret", "")).startswith("vault:"):
                left.append(f"jwt {r['tenant_id']}")
        except Exception:
            pass
    c.close()
    print(f"\nsisa vault: ref = {len(left)} (harus 0){': ' + ', '.join(left) if left else ''}")

    print("\npost-check (info): connect + introspect tiap SQL datasource — read-only, MDL tidak disentuh")
    from app.connectors.factory import make_connector, SQL_TYPES
    ok = bad = 0
    for tid in [t["id"] for t in store.list_tenants()]:
        for ds in store.list_datasources(tid):
            if ds["type"] not in SQL_TYPES:
                continue
            try:
                n = len(make_connector(ds["type"], ds["conn"]).introspect())
                print(f"  OK   {ds['name']:<28} {ds['type']:<9} {n} tabel")
                ok += 1
            except Exception as e:
                print(f"  WARN {ds['name']:<28} {ds['type']:<9} {e}")
                bad += 1

    print(f"\nselesai. migrasi vault:->enc: = {len(migrated)}, sisa vault: = {len(left)} (harus 0), "
          f"introspect OK/WARN = {ok}/{bad} (WARN = koneksi DB, bukan kegagalan migrasi).")
    print(f"Sesudah ini: cukup run_backend.bat (tanpa Vault). Rollback: copy '{backup}' -> admin.db")
    return 0 if not left else 2


if __name__ == "__main__":
    sys.exit(main())
