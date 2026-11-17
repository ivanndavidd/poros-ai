"""Shared test env — MUST run before any app-module import freezes store.DB_URL.

pytest imports conftest.py before collecting any test module, so this guarantees the
whole suite always runs against a throwaway Postgres database (poros_test), reset to an
empty schema each run — regardless of which test file imports `app.*` first. (Without
this, a test module that imports app.store at module scope BEFORE the env is set would
freeze store.DB_URL to the default `poros` database and the suite would mutate real data.)

Requires a reachable Postgres (the app is Postgres-only now). Override the target with
ADMIN_DB_URL, e.g. postgresql://user:pass@host:5432/poros_test.
"""
import os, sys, tempfile

# Keep the suite hermetic: never let a developer's local .env (loaded by app/__init__.py)
# leak into tests. Set BEFORE any `app.*` import so the package __init__ skips load_dotenv.
os.environ["POROS_SKIP_DOTENV"] = "1"

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

_TMP = tempfile.mkdtemp(prefix="adatest_")
# metadata store = a throwaway Postgres database; sample data (DuckDB) + doc files -> tmp
os.environ.setdefault("ADMIN_DB_URL", "postgresql://postgres:postgres@localhost:5432/poros_test")

# Running under `pytest -n`: every worker is a separate process that inherits the
# CONTROLLER's environment, so plain setdefault() would hand all of them the same
# Postgres schema, the same DuckDB sample file and the same documents directory —
# they would truncate each other's tables and fight over the DuckDB file lock.
# Give each worker its own of all three. Serial runs are left exactly as they were
# (schema `public`, no ADMIN_DB_SCHEMA set).
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "")
if _WORKER:
    os.environ["ADMIN_DB_SCHEMA"] = f"w_{_WORKER}"
    os.environ["ADMIN_SAMPLE_DB"] = os.path.join(_TMP, "sample.duckdb")
    os.environ["DOCS_FILES_DIR"] = os.path.join(_TMP, "files")

os.environ.setdefault("ADMIN_SAMPLE_DB", os.path.join(_TMP, "sample.duckdb"))
os.environ["APP_ENV"] = "dev"
os.environ["SECRETS_BACKEND"] = "fernet"
os.environ["ADMIN_KEY"] = "admin-dev-key"
os.environ["ADMIN_SECRET_KEY"] = "test-only-fernet-key-at-least-32-characters"
os.environ["JWT_SECRET"] = "test-secret"
os.environ["API_KEY_PEPPER"] = "test-only-api-key-pepper-at-least-32-bytes"
os.environ["APPS_GATEWAY_TRUST_HEADER"] = "0"
# force the offline embedder + no reranker: deterministic, no model downloads in CI
os.environ.setdefault("ST_EMBED_DISABLE", "1")
# uploaded-document originals also go to tmp — never the real backend/app/files
os.environ.setdefault("DOCS_FILES_DIR", os.path.join(_TMP, "files"))

# Start every run from a clean public schema (mirrors the fresh tmp admin.db the old
# SQLite suite used). Done BEFORE any app import so store.init() then recreates tables.
import psycopg2  # noqa: E402

_SCHEMA = os.environ.get("ADMIN_DB_SCHEMA") or "public"
_c = psycopg2.connect(os.environ["ADMIN_DB_URL"]); _c.autocommit = True
_c.cursor().execute(f'DROP SCHEMA IF EXISTS "{_SCHEMA}" CASCADE; CREATE SCHEMA "{_SCHEMA}"')
_c.close()
