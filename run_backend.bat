@echo off
cd /d %~dp0backend
echo === Poros (AI Distribution Administrator) - BACKEND ===
if not exist .venv ( echo Membuat virtualenv... & python -m venv .venv )
call .venv\Scripts\activate
rem Konfigurasi (ADMIN_DB_URL, JWT_SECRET, ADMIN_KEY, dll.) dibaca dari file .env
rem yang dimuat otomatis saat start (lihat app\__init__.py). Dibuat sekali dari
rem .env.example. Variabel yang di-set manual di shell tetap menang atas isi .env.
if not exist ".env" if exist ".env.example" (
  echo Membuat .env dari .env.example ^(sesuaikan kredensial Postgres bila perlu^)...
  copy /y ".env.example" ".env" >nul
)
rem Isi secret kosong / default lama dengan nilai acak unik. Nilainya tidak pernah
rem dicetak. Secret custom yang sudah ada tetap dipertahankan.
python scripts\secure_local_env.py
rem Metadata store = PostgreSQL (SQLite sudah dihapus). Pastikan Postgres nyala +
rem database "poros" sudah dibuat: createdb poros
echo Memasang dependensi...
pip install -q -r requirements.txt
rem Seed tenant contoh + API keys HANYA ke store KOSONG. Kalau Postgres sudah berisi
rem tenant apa pun (mis. "Acme Assistant" milikmu), seed otomatis dilewati dan datamu
rem TIDAK disentuh - aman dipanggil tiap start (lihat app/seed.py).
echo Cek/seed tenant contoh (hanya kalau store kosong)...
python -m app.seed
echo.
if not defined POROS_BACKEND_HOST set "POROS_BACKEND_HOST=127.0.0.1"
echo Backend listening di %POROS_BACKEND_HOST%:8000.
echo   Lokal / OpenAPI : http://localhost:8000/docs
echo Biarkan jendela ini terbuka. Tutup untuk stop.
uvicorn app.main:app --host %POROS_BACKEND_HOST% --port 8000 --reload
pause
