# pgvector vector store (opt-in)

By default Poros stores document embeddings in the SQLite `admin.db` and searches them
with a Python cosine scan — self-contained, zero services, fine for demos but O(n) per
query. For larger corpora, switch the retrieval backend to **Postgres + pgvector**
(HNSW index, cosine `<=>`) without changing any application code.

**SQLite stays the source of truth.** Postgres holds a *searchable mirror* of the vectors
that is written through on ingest / reindex / archive / delete and can be rebuilt from
SQLite at any time. If Postgres is down, ingest still works (the mirror just drifts until
`rebuild`) and retrieval automatically falls back to the SQLite scan.

## 1. Start Postgres + pgvector

```bash
set PGVECTOR_PASSWORD=<local-only-password>
docker compose -f deploy/pgvector/docker-compose.yml up -d
```

The `pgvector/pgvector:pg16` image is stock Postgres with the extension bundled. Data is
persisted in the `poros_pgvector` volume. Host port is **5433** (to avoid clashing with a
local Postgres on 5432).

## 2. Point the backend at it

| Env | Example | Notes |
|---|---|---|
| `VECTOR_BACKEND` | `pgvector` | Anything else (or unset) = SQLite (default) |
| `VECTOR_PG_DSN` | supplied through local `.env` / secret manager | libpq DSN; falls back to `PG*` env if unset |
| `VECTOR_PG_DIM` | `384` | **Must match your embedder's dimension.** Default multilingual sentence-transformers = 384; the offline hashing embedder = 256; OpenAI `text-embedding-3-small` = 1536 |
| `VECTOR_PG_TABLE` | `doc_chunks_vec` | Mirror table name |

```bash
# Windows (cmd)
set PGVECTOR_PASSWORD=<local-only-password>
set PGPASSWORD=%PGVECTOR_PASSWORD%
set VECTOR_BACKEND=pgvector
set VECTOR_PG_DSN=postgresql://poros@localhost:5433/poros_vec
set VECTOR_PG_DIM=384
```

The `vector` extension, the mirror table, and the HNSW cosine index are created
automatically on the first write (`vectorstore._ensure_schema`).

## 3. Populate the mirror from existing documents

After switching (and any time you suspect drift), rebuild once:

```bash
curl -X POST http://localhost:8000/admin/vector/rebuild -H "X-Admin-Key: admin-dev-key"
# one tenant only:
curl -X POST "http://localhost:8000/admin/vector/rebuild?tenant_id=t_xxx" -H "X-Admin-Key: admin-dev-key"
```

Check status any time:

```bash
curl http://localhost:8000/admin/vector/status -H "X-Admin-Key: admin-dev-key"
# {"backend":"pgvector","pgvector_enabled":true,"table":"doc_chunks_vec","dim":384}
```

## 4. Verify (live round-trip)

```bash
cd backend
set PGVECTOR_PASSWORD=<local-only-password>
set PGPASSWORD=%PGVECTOR_PASSWORD%
set VECTOR_BACKEND=pgvector
set VECTOR_PG_DSN=postgresql://poros@localhost:5433/poros_vec
set VECTOR_PG_DIM=4
python ../deploy/pgvector/smoke.py
# OK — pgvector live round-trip passed: ['smoke_a'] score 0.994
```

## Changing embedder / dimension

Switching embedding models changes the vector dimension. After a switch:

1. `POST /admin/tenants/{id}/reindex-docs` — re-embeds every chunk in SQLite (source of truth).
2. Update `VECTOR_PG_DIM` to the new dimension.
3. `POST /admin/vector/rebuild` — recreate the mirror at the new dimension.

(The mirror table's `vector(N)` column is fixed-dimension; a mismatch drops the old rows on
rebuild. Vectors whose length ≠ `VECTOR_PG_DIM` are skipped on write with a warning.)

## Notes / limits

- **Doc chunks only.** Memory recall still uses the SQLite cosine path (few vectors per
  user — little to gain). The seam is structured so memory can move here later.
- **Connection-per-query** for now (no pool). Fine for typical admin/consumer load; add
  pooling (e.g. pgbouncer) for heavy concurrency.
- The per-tenant Document-config `vector_store` knob in the console is still informational —
  the active backend is chosen globally by `VECTOR_BACKEND` (like `SECRETS_BACKEND`).
- Production: use a managed Postgres with pgvector (Cloud SQL / RDS / Supabase), TLS in the
  DSN (`?sslmode=require`), and least-privilege credentials.
