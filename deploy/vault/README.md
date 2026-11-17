# Poros × Vault — secrets backend

Poros stores every secret (tenant DB passwords, LLM API keys, per-tenant JWT
secrets) through ONE seam: `backend/app/secrets_util.py`. Two backends, chosen
by env `SECRETS_BACKEND`:

| Backend | What is stored in SQLite | Where the secret lives |
|---|---|---|
| `fernet` (default) | `enc:<ciphertext>` inline | in SQLite, encrypted with a key derived from `ADMIN_SECRET_KEY` |
| `vault` | `vault:v2:<version>:<path>` — a version-pinned reference only | in Vault/OpenBao KV v2; nothing secret stays local |

`decrypt()` **dual-reads** both formats regardless of the active backend, so you
can switch at any time: existing `enc:` rows keep working, new/updated secrets
go to Vault. Switching back to `fernet` also keeps `vault:` refs readable as
long as `VAULT_ADDR` + auth stay configured.

## Dev quickstart (5 minutes)

```bash
cd deploy/vault
docker compose up -d          # Vault dev mode on :8200 (root token: tenantos-dev-root)
```

Then set the backend env before starting Poros:

```bash
# Windows (run_backend / same shell as uvicorn):
set SECRETS_BACKEND=vault
set VAULT_ADDR=http://localhost:8200
set VAULT_TOKEN=tenantos-dev-root
```

Verify: create/update a datasource in the UI → in `admin.db` the
`datasources.conn` value now starts with `vault:` instead of `enc:`, and the
actual credential sits in Vault (`secret/tenantos/<id>`).

**Dev-mode warning:** Vault dev mode is in-memory — restarting the container
wipes all stored secrets, and any `vault:` refs created against it become
unreadable. Use it to develop, not to keep real credentials.

## Persistent local Vault (`persistent/`) — use this before migrating real secrets

Single-node raft storage on a docker named volume (`persistent_vault-data`) —
**data survives restarts**. Unlike dev mode it must be initialized once and
unsealed after every start; both are automated:

```
run_vault.bat            (repo root) = compose up + init-once/unseal + write env file
run_backend_vault.bat    starts the backend; automatically uses persistent/vault.env.bat
```

Run `run_vault.bat` again after every reboot/container restart (Vault comes
back SEALED by design; the script unseals it). Files it manages:

| File | Contents | Care |
|---|---|---|
| `persistent/.vault-init.json` | unseal key + root token | GUARD IT — whoever holds it opens the Vault. Gitignored. |
| `persistent/vault.env.bat` | `SECRETS_BACKEND/VAULT_ADDR/VAULT_TOKEN` | consumed automatically by `run_backend_vault.bat` |

Implementation gotchas (why the files look the way they do):
- raft path must be `/vault/file` — the image entrypoint chowns only that dir;
  a volume at `/vault/data` gets root ownership → `permission denied` crash-loop.
- compose `command:` must be just `server` — the entrypoint already appends
  `-config=/vault/config`; passing `-config=<file>` too loads the config twice
  → duplicate listener → `bind: address already in use`.
- `sys/init` can take >5s; the client timeout is 90s because a lost init
  response = keys lost forever (volume wipe required).
- after unseal a raft node needs a few seconds to become ACTIVE; mount/policy
  calls before that fail 500 `local node not active` (the script waits).

Root token in a local file = local convenience only; production uses AppRole +
TLS (below). Dev-mode and persistent both bind :8200 — run only one at a time.

## Environment reference

| Env | Default | Meaning |
|---|---|---|
| `SECRETS_BACKEND` | `fernet` | `fernet` or `vault` |
| `VAULT_ADDR` | — | e.g. `http://localhost:8200` (required for vault) |
| `VAULT_TOKEN` | — | static token auth (dev only; production rejects it) |
| `VAULT_ROLE_ID` / `VAULT_SECRET_ID` | — | AppRole auth (production; used when `VAULT_TOKEN` unset, auto re-login on lease expiry) |
| `VAULT_KV_MOUNT` | `secret` | KV v2 mount name. Changing it orphans existing `vault:` refs |
| `VAULT_BASE_PATH` | `tenantos` | path prefix for new secrets |
| `VAULT_CACHE_TTL` | `300` | seconds a fetched secret is reused in-memory (spares one Vault round-trip per query) |
| `VAULT_TIMEOUT_SEC` | `5` | HTTP timeout per Vault call |
| `VAULT_CACERT` | — | CA bundle path for private TLS |
| `VAULT_NAMESPACE` | — | Vault Enterprise namespace header |

Startup behavior (`app/security.py`): with `SECRETS_BACKEND=vault` the backend
pings Vault at boot — **dev** logs a warning if unreachable, **APP_ENV=production
refuses to start** (same fail-hard rule as the Fernet dev-key guard). In vault
mode the Fernet-specific `ADMIN_SECRET_KEY` checks are skipped. Original
documents still require a stable `DOCUMENT_ENCRYPTION_KEY` (recommended) or an
`ADMIN_SECRET_KEY` fallback of at least 32 characters; keep `ADMIN_SECRET_KEY`
set as well while legacy `enc:` rows still exist so they can be decrypted.

## Production sketch (not dev mode!)

1. **Storage + TLS** — run Vault with raft storage and TLS, e.g. `vault-config.hcl`:
   ```hcl
   storage "raft" { path = "/vault/data"  node_id = "vault-1" }
   listener "tcp" {
     address       = "0.0.0.0:8200"
     tls_cert_file = "/vault/tls/vault.crt"
     tls_key_file  = "/vault/tls/vault.key"
   }
   api_addr = "https://vault.internal:8200"
   ui = true
   ```
2. **Init + unseal** — `vault operator init` (store the unseal keys OFFLINE,
   split among people), `vault operator unseal` after every restart, or
   configure auto-unseal via a cloud KMS.
3. **Audit log** — `vault audit enable file file_path=/vault/logs/audit.log`
   (this is the "who read which secret when" trail).
4. **KV v2 + policy + AppRole** for the Poros backend:
   ```bash
   vault secrets enable -path=secret kv-v2        # dev mode has this already
   vault policy write tenantos tenantos-policy.hcl
   vault auth enable approle
   vault write auth/approle/role/tenantos token_policies=tenantos \
         token_ttl=1h token_max_ttl=4h
   vault read  auth/approle/role/tenantos/role-id          # -> VAULT_ROLE_ID
   vault write -f auth/approle/role/tenantos/secret-id     # -> VAULT_SECRET_ID
   ```
5. **Poros env** — `SECRETS_BACKEND=vault`, `VAULT_ADDR=https://...`,
   `VAULT_ROLE_ID`, `VAULT_SECRET_ID` (never the root token), `APP_ENV=production`.
6. **Ops** — back up the raft data dir, monitor `sys/health`, and treat
   `VAULT_SECRET_ID` as the one secret-zero: env-injected with tight
   permissions, rotated periodically.

## Migration of existing secrets

Old `enc:` and unversioned `vault:<path>` values remain dual-readable. Migrate
online to version-pinned KV v2 refs with:

```bat
call deploy\vault\persistent\vault.env.bat
backend\.venv\Scripts\python.exe backend\scripts\migrate_secrets_to_vault.py
```

The first pass retains old Vault entries so in-flight workers remain safe. Verify
all backend instances, restart them, then optionally remove replaced entries:

```bat
backend\.venv\Scripts\python.exe backend\scripts\migrate_secrets_to_vault.py --purge-old
```
