# AGENTS.md — AI Distribution Administrator

Guidance for Codex in this repo. Full requirement spec: `PROJECT_BRIEF.md`. Run: `README.md`.

## What this is
A multi-tenant **control plane** that provisions **governed** analytics + RAG per tenant and distributes
it to consumer apps via API keys. Built around 5 features:
1. **Tenant Creation — Data Source = Database + Documentation + Memory** (all 3 first-class) + **Model
   Setup (multi-LLM)** + **RBAC** (API key + role).
2. **RAG / Agent / Skills management** — one global pipeline all consumers inherit (gatekeeper → router
   → agent → tools over DB+Docs+Memory → govern → verify).
3. **Monitoring** tenant usage (requests, tokens, cost).
4. **Consumer chatbot example.**
5. **API docs** (OpenAPI) + SDK.

North star: numbers only from validated SQL; access scoped per tenant+role; one uniform governed API;
reuse Wren (Apache-2.0), never rebuild it, never copy Wren Classic (AGPL).

## Repo structure
```
backend/app/
  main.py  store.py(PostgreSQL via psycopg2; env ADMIN_DB_URL)  auth.py(admin+API key+JWT)  schema_scan.py(scan->MDL)  nl2sql.py  secrets_util.py(secrets at rest: Fernet default | Vault seam)
  business_scope.py(resolve+narrow the Tenant>Role>Business slice; enforced in govern())
  business_confirm.py(ask WHICH business when several fit — never inferred by a model)
  entity_confirm.py(ask WHICH VALUE when a term only resembles one) + confirm.py(shared)
  governance/ policy.py(REUSED Wren, Apache-2.0) + engine.py(govern: policy + CLAC + RLAC)  <-- choke point
  connectors/ duckdb,postgres,mysql,sqlserver,bigquery + factory.py(CONNECTOR_SPECS drive typed forms)
  llm/ gateway.py(LiteLLM) gatekeeper.py router.py        <-- multi-LLM (1.4)
  rag/ embeddings.py ingest.py retriever.py               <-- doc-RAG (1.2); vector store seam = vectorstore.py (sqlite default | pgvector)
  memory/ recall.py conversation.py capture.py            <-- memory 1.3 + v2 (auto-capture, conversation)
  agent/ orchestrator.py verifier.py                      <-- pipeline (2.2)
  skills/ registry.py (run_sql/search_docs/recall_memory/generate_report, per-role)  <-- (2.3)
  routers/ admin.py models_admin.py docs_admin.py memory_admin.py  consumer.py(/v1/query,/chat,/retrieve)
  seed.py(demo tenant "Acme Corp" + role-mapping demo "SPV-01")   tests/ test_smoke.py(26 e2e) + test_govern_adversarial.py(26) + test_secrets_backend.py(17) + test_bigquery_auth.py(6)
deploy/vault/ docker-compose.yml(dev-mode Vault) + tenantos-policy.hcl + README.md(quickstart/prod/env reference)
  + persistent/(raft-on-volume compose + setup_vault.py: init-once->.vault-init.json + auto-unseal + KV v2 + policy
    -> vault.env.bat). Root launchers: run_vault.bat (start+unseal), run_backend_vault.bat (prefers persistent env).
frontend/src/ React+Vite — product brand "Poros" (d/h TenantOS; IBM Plex + Material Symbols, light/dark):
  App.jsx(login+sidebar+theme+routing) tos.jsx(Icon/toast) api.js
  views/{Tenants(cards),KnowledgeBase(Tenant>Role>Business>DB tree),Monitoring(KPI/donut/trend),
         Chatbot(governed chat),ConfigModal(6 tabs)}.jsx  +  views/kb/{ui,CurationEditor}.jsx
  ConfigModal tabs = Database·Document·Memory·Role·Model·App Key (per-tenant config, wired);
    the Database tab is CONNECTION-ONLY — curation lives in the Knowledge Base menu, per business
  (legacy pages/ + components/ui.jsx kept but unused)
sdk/ js/client.js  python/client.py  (consumer SDKs; optional `business` + `ApiError.businesses`)
New TenantOS endpoints: GET /admin/monitoring (cross-tenant rollup); GET/POST /admin/tenants/{id}/config/{document|memory};
  PATCH /admin/tenants/{id} (descr/icon/status). Per-role authorized_tables enforced in govern() (ROLE_TABLE_DENIED).
  PATCH /admin/datasources/{id}/conn = validate-first connection rotation (introspect must pass or old conn kept; MDL/docs
  preserved; old secret's Vault entry purged) — UI: pencil "Edit connection" on each DSCard (masked *** fields dropped,
  blank secret = keyless).
```

## Run / test
```
cd backend && pip install -r requirements.txt && python -m app.seed && uvicorn app.main:app --reload
cd frontend && npm install && npm run dev
```
Admin key default `admin-dev-key` (env `ADMIN_KEY`). Paths overridable: `ADMIN_DB_PATH`/`ADMIN_SAMPLE_DB`.

## Status
All 5 features implemented. Tests: `cd backend && pytest` (needs a local PostgreSQL; see `tests/conftest.py`).
This copy carries no tenant or client data: seed a demo with `python -m app.seed` and connect your own local
databases (DuckDB/Postgres/MySQL/...) from the console.

## Hard rules (do not violate)
- Every consumer query MUST pass through `governance/engine.govern()`.
- Reuse Wren NEW (Apache-2.0) only; **do NOT copy/run Wren Classic `legacy/v1` (AGPL-3.0)**.
- Read-only (SELECT); **PII never sent to an LLM** (CLAC); tenant isolation server-side (carry `tenant_id`).
- Keep all 3 data sources (Database + Documentation + Memory) first-class. API stable + versioned `/v1`.

## How to extend
- DB type → `connectors/<db>.py` + `factory.py`. Tool/skill → `skills/registry.py` (per-role). New
  consumer capability → `/v1/*` behind `require_api_key`, log usage, via `govern()`. Models → LiteLLM.
