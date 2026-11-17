# AI Distribution Administrator — Project Brief

> A multi-tenant platform that provisions **governed** analytics + RAG per tenant and **distributes**
> it to consumer apps via API keys. Every tenant later builds its own chatbot/consumer on this API.
> This brief is the **requirement spec** (the full target). Status tags show what already exists in the
> MVP vs what to build: ✅ done · 🟡 partial · ⬜ to build. Companion: `CLAUDE.md`, `README.md`.

## 0. North-star principles (apply to every feature)
- **Numbers only from validated SQL** — never guessed by an LLM.
- **Three data-source types per tenant: Database + Documentation + Memory** — co-equal, all first-class.
- **Multi-LLM**: a gatekeeper model checks/triages every prompt; a router picks the answer model.
- **RBAC from the data source**: access scoped per tenant + role (rows, columns, tables, docs, tools).
- **One uniform governed API** so every consumer app gets the same features.
- **Reuse Wren (Apache-2.0)** for the engine/governance; never rebuild it; never copy Wren Classic (AGPL).

---

## FEATURE 1 — Tenant Creation: Data Source (Database + Documentation + Memory) + Model Setup + RBAC

### 1.1 Database connection + auto-scan schema → MDL  ✅ (core done) / 🟡 (PG,BQ untested)
- Connect multiple DB types per tenant: **BigQuery, Postgres, MySQL, SQL Server, DuckDB, …**
  (schemas differ per tenant).
- **Automation scan**: on connect, introspect ALL tables/columns from the dataset layer and build an
  **MDL/semantic model** (a pipeline like `wren-ai-service`): tables, columns, types, **PII guess**.
- Admin **review/enrich** step (edit allowlist, mark PII, add descriptions) before publish.
- Implemented: `backend/app/connectors/*` (DuckDB done; Postgres/BigQuery ready), `schema_scan.py`,
  `POST /admin/datasources` (auto-scan), `POST /admin/datasources/{id}/rescan`.
- To do: review/enrich UI; verify Postgres/BigQuery live; richer MDL (relationships, metrics).

### 1.2 Documentation (SOP / Playbook) injection — doc-RAG per tenant  ⬜ to build
- Upload SOP/Playbook docs (PDF/DOCX/MD) per tenant → parse → chunk → embed → **per-tenant vector store**
  (pgvector / Qdrant / Vertex AI Search). Every chunk tagged `tenant_id` + ACL.
- Retrieval with metadata filter (tenant + role ACL) + rerank; answers **cite sources**.
- New: `backend/app/rag/` (ingest, vector store client, retriever), `POST /admin/datasources` of type
  `docs`, consumer `POST /v1/retrieve`. Wren does NOT do general doc-QA — this is custom + a vector DB.

### 1.3 Memory based on the tenant (and per end-user)  ⬜ to build
- Persistent memory namespaced by **(tenant, user)**: store + recall conversation context / preferences;
  used by `/v1/chat`. Never crosses tenants. Options: Mem0 / Vertex AI Memory Bank / custom (pgvector).
- New: `backend/app/memory/` (store, recall, write); wire into chat.

### 1.4 Model Setup (per tenant) + Multi-LLM (gatekeeper + router)  🟡 partial
- **Model Setup**: per-tenant config of which models/providers are allowed + defaults.
- **Gatekeeper model** (cheap/local): screens every prompt (prompt-injection, PII, intent + complexity).
- **Router**: picks the answer model by **complexity ∩ tenant policy/budget** (cheap→strong).
- **Model gateway = LiteLLM**: per-tenant virtual keys, budgets, **token/cost metering**, fallback.
- Implemented: `nl2sql.py` (rule-based, LiteLLM-ready). To do: LiteLLM gateway integration, gatekeeper,
  router, per-tenant model config table + admin UI.

### 1.5 RBAC via app key + role  ✅ done (recommended model — answers your original idea)
- Your original idea was many app keys scoped per data source/table. **Recommended instead** (easier,
  safer): **one API key = identity of a consumer app (per tenant) + a role**; permissions live
  **server-side as policy** keyed by tenant+role; data-level enforcement = **RLAC/CLAC at query time**.
  (Target) add **JWT** so the consumer passes the end-user's identity/role per request; the platform
  enforces — never trust the consumer to pick the right data.
- Implemented: `auth.py` (X-API-Key → tenant+role), `governance/engine.py` (Wren policy allowlist +
  CLAC PII block + RLAC row filter per role). To do: JWT end-user identity; richer policy editor.

---

## FEATURE 2 — RAG AI System / Agent / Skills management (the global method)
A single implementation that **all consumer apps inherit** through `/v1/*`.

### 2.1 Unified RAG over the 3 sources  🟡 (DB done; docs/memory pending)
- One pipeline retrieves across **Database (SQL), Documentation (vector), Memory** and produces a
  grounded, cited answer — identical behaviour for every tenant/consumer.

### 2.2 Agent orchestration  ⬜ to build
- Flow: **gatekeeper** (safety/PII/intent+complexity) → **router** (model) → **planner** → **tools**
  → **evidence check / retry (corrective)** → **verifier** (citations/PII/faithfulness) → answer.
- New: `backend/app/agent/` (orchestrator, gatekeeper, router, verifier).

### 2.3 Skills / tools registry (per-role permissions)  🟡 (run_sql done)
- Registry of tools the agent may call, each declaring required role/scope: `run_sql` ✅,
  `search_docs` ⬜, `recall_memory` ⬜, `generate_report` ⬜, plugins/skills ⬜. The agent is exposed
  ONLY the tools the principal's role allows. New: `backend/app/skills/registry.py`.

---

## FEATURE 3 — Monitoring tenant usage (model)  🟡 partial
- Per tenant: requests, rows, **tokens + cost per model**, by-status, recent activity, budget vs spend.
- Implemented: `store.usage_log` + `GET /admin/tenants/{id}/usage` (requests/rows/status/recent).
- To do: token/cost from the LiteLLM gateway; per-tenant budgets + alerts; charts in admin UI.

## FEATURE 4 — Consumer chatbot example  ✅ (extend later)
- Reference chatbot using only an API key (frontend `pages/Chatbot.jsx`). Extend to show doc citations
  + memory once 1.2/1.3 land. Ship a copyable template + (later) a JS/Python SDK.

## FEATURE 5 — API docs  ✅ (SDK ⬜)
- OpenAPI/Swagger auto at `/docs`. To do: publish a client SDK (JS/Python) + usage guide so each tenant
  can build a consumer quickly.

---

## Architecture
- **Control plane** (admin app): tenant / datasource(DB+Docs+Memory) / role / key lifecycle, schema scan,
  model setup, policy editor, usage.
- **Data/runtime plane** (consumer API `/v1/*`): gatekeeper → router → agent → tools(SQL/Docs/Memory) →
  **govern** (Wren policy + RLAC/CLAC) → verifier → respond; metered + logged.
- **Consumer apps**: call `/v1/*` with an API key.
- **Reuse (don't rebuild)**: Wren NEW `core/sdk/skills` (Apache-2.0) — connectors, MDL, RLAC/CLAC,
  generate-mdl; LiteLLM (model gateway/metering); pgvector/Qdrant (docs); Mem0/Memory Bank (memory).
- **Build**: control plane, the unified `/v1` API + SDK + chatbot, the RBAC/policy layer, agent+skills.

## Tech stack
Backend: Python 3.10+, FastAPI, pydantic v2, sqlglot, duckdb (+psycopg2 / google-cloud-bigquery optional),
vendored Wren `policy.py` (Apache-2.0). Metadata: SQLite (MVP) → Postgres (prod). Add: LiteLLM,
pgvector/Qdrant, embeddings, OIDC/JWT, Secret Manager. Frontend: React 18 + Vite. GCP-native lean.

## Current implementation status (starting point for Claude Code)
Done in repo: 1.1 (DB connect+scan→MDL), 1.5 (RBAC app key+role+RLAC/CLAC reuse Wren), 2.3-run_sql,
Feature 3 (basic usage), Feature 4 (chatbot), Feature 5 (OpenAPI). To build next, in order:
**1.4 multi-LLM/LiteLLM + Model Setup → 1.2 Documentation doc-RAG → 1.3 Memory → 2.2 agent (gatekeeper+
router+verifier) → 2.3 skills (search_docs, recall_memory, generate_report) → 3 token/cost metering →
1.5 JWT end-user → security (Secret Manager, BigQuery RAP) → productionize (Postgres, rate-limit, tests, deploy).**

## Constraints (do not violate)
- Every consumer query passes through `governance/engine.govern()`; never query a tenant DB directly.
- Reuse Wren NEW (Apache-2.0) only; **never copy/run Wren Classic `legacy/v1` (AGPL-3.0)** in this product.
- Read-only (SELECT); **PII never sent to an LLM** (CLAC blocks per role); tenant isolation enforced
  server-side (carry `tenant_id` everywhere; never trust the consumer for data-level RBAC).
- Consumer API stays stable + versioned under `/v1`. Three data sources are first-class — don't drop Docs/Memory.

## Conventions for the coding agent
- New DB type → `connectors/<db>.py` (`run`, `introspect`) + register in `factory.py`.
- New tool/skill → register in `skills/registry.py` with required role; expose only if allowed.
- New consumer capability → under `/v1/*`, behind `require_api_key`, log usage, go through `govern()`.
- Real NL→SQL / models → wire `nl2sql` + agent to **LiteLLM** (per-tenant key+budget+metering).
- Python: type hints, pydantic v2, small modules. React: functional components, no extra UI deps.

## Definition of done (per feature)
F1: connect DB/Docs/Memory per tenant → auto MDL / indexed docs / scoped memory; admin can edit policy.
F2: one pipeline answers from DB+Docs+Memory with citations; agent routes models; only allowed tools exposed.
F3: per-tenant requests/tokens/cost visible; budgets enforced.
F4: a separate consumer app, with only an API key, gets governed answers (incl. doc citations) via `/v1/*`.
F5: OpenAPI complete + SDK; example chatbot runs against a tenant key.

## References
- Wren (Apache-2.0): https://github.com/Canner/WrenAI  · governance reused at `backend/app/governance/policy.py`.
- Wren Classic (AGPL-3.0, reference only, do NOT copy): branch `legacy/v1`.
- LiteLLM (model gateway): https://docs.litellm.ai/docs/proxy
