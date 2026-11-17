# Least-privilege Vault policy for the Poros backend.
# (Policy name + paths keep the original "tenantos" id — live vault: refs depend on it.)
# Scope: KV v2 mount "secret" (VAULT_KV_MOUNT), base path "tenantos" (VAULT_BASE_PATH).
# The backend writes one entry per secret (random id), reads it back on use,
# and deletes it when the owning tenant/datasource/provider row is removed.

path "secret/data/tenantos/*" {
  capabilities = ["create", "update", "read"]
}

path "secret/metadata/tenantos/*" {
  capabilities = ["delete", "list"]
}
