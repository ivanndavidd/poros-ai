# Poros — AI Distribution Administrator

Control plane **multi-tenant** untuk mendistribusikan analitik + RAG yang **ter-governance** ke banyak
aplikasi konsumen lewat satu API. Admin membuat tenant, menyambungkan sumber data (database + dokumen +
memory), mengatur role & kebijakan akses, lalu menerbitkan API key — aplikasi konsumen tinggal memanggil
`/v1` dan **setiap query otomatis dijaga**: allowlist tabel, row-level & column-level access per role,
read-only, PII tidak pernah sampai ke LLM.

**North star:** angka hanya boleh berasal dari SQL yang tervalidasi; akses selalu ter-scope per tenant +
role; satu API seragam untuk semua konsumen. Governance **me-reuse engine Wren** (`policy.py`,
Apache-2.0) — tidak dibangun ulang.

Status: v0.2.0 — kelima fitur inti lengkap, diverifikasi **135 test otomatis** (e2e + adversarial +
secrets + auth + ingest + agent-loop + memory + gatekeeper + vectorstore + mdl-enrich + ocr). Metadata store = PostgreSQL.

---

## Fitur inti

| # | Fitur | Isi |
|---|---|---|
| 1 | **Tenant & data source** | 3 sumber first-class per tenant: **Database** (DuckDB/Postgres/MySQL/SQL Server/BigQuery — auto-scan schema → MDL + kurasi metrics/reference values/grain), **Documentation** (doc-RAG), **Memory** (per tenant+user). BigQuery mendukung **keyless auth** (ADC/WIF + service-account impersonation — tanpa JSON key) |
| 2 | **Pipeline agentic ter-governance** | Satu pipeline untuk semua konsumen: gatekeeper (anti prompt-injection) → router (pilih model per kompleksitas∩budget) → agent + tools (SQL/docs/memory) → `govern()` → verifier. **Loop-engineered**: SQL gagal diperbaiki sendiri (tool-loop), verifier hanya melapor (angka yang tidak cocok dengan data disebut di `unverified_figures`, jawaban tidak ditulis ulang), retrieval lemah memicu query-rewrite |
| 3 | **Monitoring** | Request, token, biaya, budget per tenant/model + audit trail per langkah (trace id, SQL, jawaban) |
| 4 | **Chatbot contoh** | Governed chat dengan trace lengkap: SQL tervalidasi, tabel hasil, chart/artifact interaktif, sitasi dokumen **yang bisa diklik** (pratinjau + download) |
| 5 | **API & SDK** | OpenAPI (`/docs`), menu API Docs in-app, SDK JS & Python (`sdk/`) |

### Keamanan (ringkas)
- **Strict Pattern B**: `X-API-Key` = identitas aplikasi; **JWT terverifikasi** (`Authorization: Bearer`)
  = identitas + role end-user. Role claim configurable, **role mapping** untuk id role eksternal
  (mis. `SPV-01` → `analyst`), deny-by-default, unmapped role direkam & bisa di-map sekali klik.
- **`govern()` = satu-satunya pintu SQL**: allowlist tabel (± per-role), RLAC (row filter),
  CLAC (kolom PII diblok), read-only (non-SELECT ditolak), aman terhadap UNION/CTE/comment tricks
  (26 test adversarial).
- **PII tidak pernah dikirim ke LLM** — di-mask sebelum masuk prompt untuk SEMUA role; API/chatbot
  tetap menampilkan nilai asli sesuai hak akses.
- **Secrets** (password DB, API key LLM, JWT secret) tersimpan lewat seam `SECRETS_BACKEND`:
  `fernet` (default, enkripsi lokal) atau **`vault`** (HashiCorp Vault/OpenBao KV v2 — database hanya
  menyimpan referensi `vault:…`). Dual-read: pindah backend tidak merusak data lama.
- Rate-limit per API key, rotasi key, startup guard produksi (`APP_ENV=production` menolak
  default dev), admin key dibandingkan constant-time.

---

## Arsitektur

```
 Consumer apps (web/mobile/internal)        Poros Console (React)
        │  X-API-Key + Bearer JWT                 │  X-Admin-Key
        ▼                                         ▼
 ┌─────────────────────────────── FastAPI ───────────────────────────────┐
 │  /v1/query · /v1/chat · /v1/chat/stream · /v1/retrieve                │
 │  /v1/documents/{id}/file            /admin/* (control plane)         │
 │                                                                       │
 │  gatekeeper → router → agent(tool-loop) → ~~~~~~~~~ → verifier (flags)│
 │                          │ run_sql        govern()  ▲                 │
 │                          │ search_docs   (Wren policy + RLAC + CLAC   │
 │                          │ recall_memory  + read-only)                │
 └──────────┬──────────────┴────────────┬──────────────┬────────────────┘
            ▼                           ▼              ▼
   DB tenant (BigQuery/MySQL/…)   Vector store     Memory store
   keyless ADC/WIF utk BigQuery   (Postgres, seam  (per tenant+user)
                                   pgvector/Qdrant)
   Secrets → Fernet │ Vault KV v2          Metadata → PostgreSQL (ADMIN_DB_URL)
```

## Struktur repo

```
backend/app/
  main.py  store.py(PostgreSQL)  auth.py  schema_scan.py  nl2sql.py  security.py
  secrets_util.py      seam SECRETS_BACKEND=fernet|vault (dual-read enc:/vault:)
  governance/          policy.py (REUSE Wren, Apache-2.0) + engine.py govern()  ← choke point
  connectors/          duckdb/postgres/mysql/sqlserver/bigquery + factory (form spec-driven)
  llm/                 gateway (LiteLLM) + gatekeeper + router
  rag/                 ingest (PDF/DOCX/PPTX/MD/TXT) + embeddings (multilingual ST) +
                       retriever (cosine → cross-encoder rerank) + reindex
  memory/  agent/      orchestrator (verify, flags only) + sql_agent (tool-loop) + verifier
  skills/              registry per-role: run_sql/search_docs/recall_memory/generate_report
  routers/             admin, models_admin, docs_admin, memory_admin, consumer(/v1)
  tests/               88 test (conftest mengisolasi DB/file ke tmp)
frontend/src/          React+Vite — console Poros (tenants, monitoring, chatbot, config modal)
sdk/                   js/client.js · python/client.py
deploy/vault/          Vault dev-mode + persistent (raft) + policy + README ops
docs/                  dokumentasi tambahan (consumer-api.md)
```

---

## Menjalankan (dev)

Prasyarat: Python 3.10+, Node 18+, **PostgreSQL** (metadata store — wajib), (opsional) Docker untuk Vault.

### Backend
```bash
# 1) siapkan database Postgres (sekali)
createdb poros            # atau: psql -U postgres -c "CREATE DATABASE poros"

cd backend
python -m venv .venv && .venv\Scripts\activate     # Windows; Linux: source .venv/bin/activate
pip install -r requirements.txt                     # ± beberapa menit — menarik torch utk embeddings lokal
copy .env.example .env                              # Windows (Linux/Mac: cp) — sesuaikan bila perlu
python scripts/secure_local_env.py                  # isi secret lokal acak; nilainya tidak dicetak
python -m app.seed                                  # tenant contoh "Acme Corp" (aman diulang, skip bila sudah ada)
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```
Konfigurasi dibaca dari file **`.env`** (dimuat otomatis saat start — lihat `app/__init__.py`);
salin dari `.env.example`. Semua env di tabel bawah bisa ditaruh di situ. Variabel yang di-set
manual di shell tetap menang atas isi `.env`. Metadata (tenant/role/key/dokumen/memory) disimpan
di Postgres — set `ADMIN_DB_URL` atau `DATABASE_URL` secara eksplisit; Poros tidak
menyediakan password database bawaan.
Launcher membuat `ADMIN_KEY` unik di `.env` (tidak ada default bersama). OpenAPI dev:
http://localhost:8000/docs

> Butuh Postgres cepat? `docker compose -f deploy/pgvector/docker-compose.yml up -d`
> memberi Postgres+pgvector di port 5433 yang bisa dipakai untuk metadata **dan** vektor sekaligus
> (set `ADMIN_DB_URL` + `VECTOR_BACKEND=pgvector` ke instance yang sama).

### Frontend
```bash
cd frontend && npm install && npm run dev           # http://localhost:5173
```
Backend bukan di localhost? set `VITE_API_BASE`.

### Container produksi

Root `Dockerfile` adalah multi-stage build: Node mengompilasi React, lalu satu
proses FastAPI menyajikan `frontend/dist` dan API pada port `8000`. Pada build
production tanpa `VITE_API_BASE`, frontend memakai same-origin; untuk split
deployment berikan `--build-arg VITE_API_BASE=https://...`.

```bash
docker build -t poros:local .
docker run --rm -p 8000:8000 --env-file /path/to/production.env poros:local
```

Healthcheck: `GET /dangerously-skip-login/health`. Baseline Kubernetes, Secret
contract, persistent document volume, dan contoh dua ingress tersedia di
`deploy/production/README.md`.

### Launcher Windows (opsional, satu klik)
| File | Fungsi |
|---|---|
| `run_backend.bat` | venv + secret-lokal-unik + seed-jika-perlu + uvicorn `127.0.0.1:8000` (mode Fernet) |
| `run_vault.bat` | start Vault **persisten** (Docker) + init-sekali/auto-unseal |
| `run_backend_vault.bat` | backend dengan `SECRETS_BACKEND=vault` (pakai env Vault persisten) |
| `run_frontend.bat` | npm dev server |

Urutan harian mode Vault setelah reboot: `run_vault.bat` **dulu** (unseal), baru `run_backend_vault.bat`.

---

## Konfigurasi (env)

| Env | Default | Fungsi |
|---|---|---|
| `ADMIN_KEY` | dibuat acak oleh launcher | Kunci console admin (`X-Admin-Key`) |
| `ADMIN_SECRET_KEY` | dibuat acak oleh launcher | Kunci enkripsi Fernet (mode fernet). Harus stabil |
| `API_KEY_PEPPER` | dibuat acak oleh launcher | Pepper HMAC API key. Harus stabil; rotasi memutus key consumer lama |
| `APP_ENV` | `dev` | `production` = menolak start dengan default dev / Vault mati |
| `JWT_SECRET` | — | Fallback HS untuk local/dev saja; production wajib JWKS/public key per tenant |
| `JWT_LEEWAY_SEC` | `120` | Toleransi selisih jam konsumen (iat/exp) |
| `JWT_ALLOW_HS256_STAGING` | `0` | Opt-in khusus staging: izinkan HS256 per tenant dengan secret + issuer + audience; jangan aktifkan di production riil |
| `ADMIN_DB_RLS` | `0` | Wajib `1` di production; FORCE RLS tenant pada metadata PostgreSQL |
| `CONSOLE_SESSION_HOURS` | `12` | TTL cookie HttpOnly console; production maksimum 24 jam |
| `LOGIN_IP_MAX_FAILURES` | `20` | Batas gagal login gabungan per IP; akun individual tetap terkunci pada 5 kegagalan |
| `BUDGET_RESERVE_USD_PER_CHAT` | `0.25` | Reservasi atomik per chat untuk hard monthly-budget ceiling |
| `DEEP_RESEARCH_MAX_ROUNDS` / `DEEP_RESEARCH_MAX_SQL` | `24` / `18` | Circuit breaker agen Deep Research adaptif; bukan template/jumlah langkah wajib |
| `DEEP_RESEARCH_MAX_CATALOG` / `DEEP_RESEARCH_MAX_DOCS` / `DEEP_RESEARCH_MAX_WEB` | `14` / `6` / `4` | Batas pemanggilan tool per chat Deep |
| `DEEP_RESEARCH_MAX_DISCOVERY` / `DEEP_RESEARCH_MAX_PROFILE` / `DEEP_RESEARCH_MAX_SEMANTIC` | `6` / `8` / `8` | Batas live metadata discovery, profiling sample non-PII, dan verifikasi semantic per chat |
| `DEEP_RESEARCH_MAX_HYPOTHESES` | `16` | Jumlah maksimum hipotesis material dalam ledger per chat |
| `DEEP_RESEARCH_CRITIC_ROUNDS` / `DEEP_RESEARCH_MAX_SECONDS` | `2` / `600` | Maksimum penolakan critic dan durasi satu riset |
| `DISCOVERY_CACHE_TTL_SEC` / `PROFILE_MAX_SAMPLE_ROWS` / `PROFILE_MAX_COLUMNS` | `180` / `300` / `24` | Cache metadata live dan batas aman profiler backend-owned |
| `WEB_RESEARCH_ENABLED` / `WEB_RESEARCH_PROVIDER` | `auto` / `auto` | Validasi eksternal Deep Analysis; otomatis memakai key provider OpenAI tenant dengan tool `web_search` |
| `OPENAI_WEB_SEARCH_MODEL` | model Primary tenant | Override model Responses API untuk Web Search; kosong = Primary → Simple/Fallback |
| `GEMINI_SEARCH_API_KEY` / `GEMINI_SEARCH_MODEL` | — / `gemini-2.5-flash` | Fallback opsional untuk tenant tanpa provider OpenAI |
| `ADMIN_AUDIT_CHAIN_KEY` / `ADMIN_AUDIT_SINK_URL` / `ADMIN_AUDIT_SINK_HMAC_KEY` | — | Hash-chain audit perubahan admin + pengiriman bertanda tangan ke collector off-host |
| `ADMIN_AUDIT_RETRY_SEC` | `60` | Interval worker untuk mengirim ulang audit outbox yang belum sampai |
| `JOB_WORKERS` / `JOB_LEASE_SEC` / `JOB_RETENTION_HOURS` | `2` / `300` / `168` | Worker PostgreSQL + recovery lease + retensi hasil untuk operasi yang melampaui timeout gateway |
| `DOCUMENT_ENCRYPTION_KEY` | turunan `ADMIN_SECRET_KEY` | Key AES-GCM per-tenant untuk file dokumen asli; minimal 32 karakter dan harus stabil |
| `DOCUMENT_MAX_UPLOAD_MB` / `DOCUMENT_TENANT_QUOTA_MB` | `25` / `250` | Batas satu file dan total dokumen asli per tenant |
| `LLM_API_BASE_ALLOWLIST` | kosong | Endpoint model custom yang diizinkan di production; URL berarti scheme+host+port harus persis |
| `LLM_ALLOW_PRIVATE_BASE` / `LLM_ALLOW_INSECURE_BASE` | `0` / `0` | Opt-in endpoint private atau HTTP; localhost tetap otomatis boleh hanya saat development |
| `SECRETS_BACKEND` | `fernet` | `vault` = simpan secret di Vault/OpenBao KV v2 |
| `VAULT_ADDR` / `VAULT_TOKEN` / `VAULT_ROLE_ID`+`VAULT_SECRET_ID` | — | Alamat + auth Vault (`VAULT_TOKEN` dev; AppRole wajib produksi) |
| `VAULT_KV_MOUNT` / `VAULT_BASE_PATH` / `VAULT_CACHE_TTL` | `secret` / `tenantos` / `300` | Layout + cache Vault |
| `VECTOR_BACKEND` | `sqlite` | `pgvector` = pencarian dokumen via indeks HNSW; jika tidak, scan cosine biasa atas tabel `doc_chunks` di Postgres |
| `VECTOR_PG_DSN` / `VECTOR_PG_DIM` / `VECTOR_PG_TABLE` | — / `384` / `doc_chunks_vec` | Koneksi + dimensi vektor + nama tabel mirror (lihat `deploy/pgvector/README.md`) |
| `GOOGLE_APPLICATION_CREDENTIALS` | — | Identitas dasar BigQuery keyless (WIF credential-config / ADC) |
| `ST_EMBED_MODEL` | `paraphrase-multilingual-MiniLM-L12-v2` | Model embedding lokal (`ST_EMBED_DISABLE=1` = embedder offline) |
| `RAG_RERANK` / `RAG_RERANK_MODEL` / `RAG_RERANK_POOL` / `RAG_RERANK_MAXLEN` | `1` / mmarco / `8` / `256` | Cross-encoder rerank (dua tahap) |
| `OCR_LANG` / `TESSERACT_CMD` / `OCR_MIN_IMAGE_PX` / `OCR_MAX_IMAGES` | `ind+eng` / — / `80` / `60` | OCR gambar di PPT/PDF (opt-in via toggle Document "OCR extraction"). Butuh **Tesseract** terpasang + traineddata bahasa; tanpa itu OCR dilewati diam-diam |
| `DOCS_FILES_DIR` | `backend/app/files` | Lokasi file dokumen asli (sitasi klik-able) |
| `ADMIN_DB_URL` / `DATABASE_URL` | wajib | DSN metadata store; `ADMIN_DB_URL` menang, `DATABASE_URL` adalah alias platform |
| `ADMIN_DB_SCHEMA` | `public` | Schema Postgres (dipakai test untuk isolasi per-test) |
| `ADMIN_SAMPLE_DB` | `backend/app/sample_acme.duckdb` | Lokasi DB contoh (DuckDB) yang dibuat `seed` |
| `RATE_LIMIT_PER_MIN` | `120` | Default rate-limit per API key (bisa per-tenant) |
| `CORS_ORIGINS` | localhost:5173 (dev), kosong (prod) | Origin frontend yang boleh memanggil backend |
| `EXPOSE_API_DOCS` | aktif (dev), mati (prod) | Opt-in eksplisit untuk `/docs` dan `/openapi.json` di produksi |
| `POROS_BACKEND_HOST` | `127.0.0.1` (launcher) | Bind host; gunakan alamat jaringan hanya di lingkungan yang sudah terisolasi |
| `PORT` | `8000` (container) | Port uvicorn pada image produksi; Service/probe harus menunjuk port yang sama |
| `GOOGLE_SSO_CLIENT_ID` / `GOOGLE_SSO_CLIENT_SECRET` | — | Aktifkan Google SSO standalone; buat OAuth Web client gratis di Google Auth Platform |
| `GOOGLE_SSO_REDIRECT_URI` / `GOOGLE_SSO_ALLOWED_DOMAINS` | localhost callback / kosong | Callback harus sama persis; domain kosong mengizinkan semua akun Google terverifikasi |
| `SSO_CLOCK_SKEW_SEC` | `120` | Toleransi terbatas untuk selisih waktu host terhadap Google/Microsoft saat memvalidasi `iat`/`exp` (maksimum 300 detik) |

---

## Alur pemakaian

### Admin (console)
1. **Buat tenant** → modal Config (6 tab: Database · Document · Memory · Role · Model · App Key).
2. **Database**: pilih tipe → isi form (BigQuery: keyless — project/dataset/location + impersonate SA)
   → *Test* → *Connect & scan* → kurasi MDL (allowlist tabel, PII, deskripsi, metrics, contoh query).
   Koneksi bisa dirotasi belakangan (ikon pensil — validate-first, MDL aman).
3. **Document**: buat source tipe Documentation → upload PDF/DOCX/PPTX/MD/TXT → otomatis di-chunk +
   embed. Upload ulang nama sama = versi lama **diarsipkan otomatis** (tak dipakai AI, reversibel).
4. **Role**: definisikan role + RLAC/CLAC + skill allowlist; **Role Mapping** untuk id role sistem
   eksternal; unmapped role muncul di sini untuk di-map sekali klik.
5. **Model**: daftarkan provider (OpenAI/Anthropic/Gemini/DeepSeek/…) + assign model per purpose.
   Catatan: model reasoning tanpa function-calling (mis. `deepseek-reasoner`) otomatis memakai jalur
   single-shot; model ber-tools mendapat agentic loop.
6. **App Key**: terbitkan API key per environment + kelola **JWKS/public key JWT** tenant. Secret app
   key hanya ditampilkan saat create/recreate; setelah itu database hanya menyimpan hash.
7. **Monitoring**: rollup lintas-tenant + detail per tenant dan per end-user consumer
   (token, request, biaya; filter All/30d/7d).

### Konsumen (aplikasi)
Setiap request `/v1` wajib membawa dua header:
```
X-API-Key: sk_…                         ← identitas aplikasi (dari tab App Key)
Authorization: Bearer <JWT end-user>    ← asymmetric JWT dari IdP tenant; claim role menentukan akses
```
Endpoint: `POST /v1/query` (SQL mentah ter-governance) · `POST /v1/chat` (agent penuh) ·
`POST /v1/chat_stream` (SSE) · `POST /v1/retrieve` (doc-RAG) · `GET /v1/documents/{id}/file`
(buka dokumen sumber, ACL ditegakkan). Detail + contoh kode: **[docs/consumer-api.md](docs/consumer-api.md)**
atau menu **API Docs** di console. SDK siap pakai di `sdk/`.

---

## Testing

```bash
cd backend && python -m pytest tests/ -q
```
135 test: 28 e2e smoke · 26 adversarial governance (UNION/CTE/comment/injection/star-expansion) ·
17 secrets-backend (fake Vault) · 6 BigQuery keyless auth · 4 ingest format · 7 agent-loop · 10 memory-v2 ·
6 gatekeeper · 5 orchestrator-viz · 13 vectorstore (fake pgvector) · 8 mdl-enrich (AI auto-fill MDL) ·
5 ocr-ingest (Tesseract, offline). Butuh Postgres: `tests/conftest.py`
mereset schema `poros_test` tiap run + mengisolasi file/embedder ke tmp — tidak pernah menyentuh data asli.

## Menuju produksi

1. `APP_ENV=production`, `ADMIN_DB_RLS=1`, dedicated non-superuser DB role, dan setiap tenant
   aktif memakai asymmetric JWT (JWKS/public key + issuer + audience).
2. `SECRETS_BACKEND=vault` + Vault ber-TLS + **AppRole** (bukan static/root token), lalu jalankan
   migrator agar semua ref berformat `vault:v2:<version>:…` — panduan: `deploy/vault/README.md`.
3. BigQuery: identitas dasar via WIF/ADC + `impersonate_service_account` per tenant (zero stored secret).
4. Backup tiga serangkai: **Postgres** (`pg_dump`) + `backend/app/files/` + volume Vault (dan simpan `.vault-init.json` offline).
5. Isi collector audit off-host (`ADMIN_AUDIT_*`), batasi `CORS_ORIGINS`, biarkan API docs
   nonaktif, dan jangan expose port backend langsung;
   TLS berakhir di Apps Gateway/reverse proxy. **Metadata store sudah Postgres** (env
   `ADMIN_DB_URL`; SQLite dihapus total). **Vector store: pgvector
   sudah didukung** (`VECTOR_BACKEND=pgvector` — panduan `deploy/pgvector/README.md`); sisa roadmap:
   backend Qdrant (seam sama di `rag/retriever.py`), konsumsi penuh knob Document config, connection pooling
   Postgres (kini 1 koneksi per operasi).
6. Isi `LLM_API_BASE_ALLOWLIST`, simpan `DOCUMENT_ENCRYPTION_KEY` di secret manager,
   dan pantau `/admin/audit-delivery-status`; endpoint model custom/private tidak dibuka
   tanpa kebutuhan operasional yang jelas.

## Lisensi & atribusi

- `backend/app/governance/policy.py` di-vendor dari **Canner/WrenAI** — **Apache-2.0** (header lisensi
  utuh di file). Jangan mem-port kode dari Wren Classic `legacy/v1` (AGPL-3.0).
- Lisensi repo ini: tentukan sebelum dipublikasikan (belum ada file `LICENSE`).
