@echo off
rem === Vault PERSISTEN lokal: start container + init (sekali) / unseal (tiap start) ===
rem Data tersimpan di docker volume - TAHAN restart. Jalankan ini setiap habis reboot/restart Vault.
cd /d %~dp0deploy\vault\persistent
docker compose up -d
set "PY=%~dp0backend\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" setup_vault.py
echo.
echo Selesai. Jalankan backend dengan: run_backend_vault.bat
pause
