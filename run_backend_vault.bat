@echo off
rem === Backend dengan secrets backend = VAULT ===
rem Prioritas: Vault PERSISTEN (setup via run_vault.bat -> deploy\vault\persistent\vault.env.bat).
rem Fallback : Vault dev-mode (deploy\vault, token tenantos-dev-root; in-memory, restart = data hilang).
set "SECRETS_BACKEND=vault"
set "VAULT_ADDR=http://localhost:8200"
set "VAULT_TOKEN=tenantos-dev-root"
if exist "%~dp0deploy\vault\persistent\vault.env.bat" (
  call "%~dp0deploy\vault\persistent\vault.env.bat"
  echo [vault-mode] pakai Vault PERSISTEN
) else (
  echo [vault-mode] pakai Vault dev-mode ^(in-memory^)
)
echo [vault-mode] SECRETS_BACKEND=vault  VAULT_ADDR=%VAULT_ADDR%
call "%~dp0run_backend.bat"
