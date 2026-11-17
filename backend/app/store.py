"""Multi-tenant metadata store (PostgreSQL).

Tables: tenants, datasources, roles, api_keys, usage_log,
        llm_providers, llm_models, tenant_budgets, documents, doc_chunks, memories,
        feedback.

Connection secrets (datasources.conn) and LLM keys (llm_providers.api_key) are
stored encrypted via secrets_util; helpers below decrypt on read.

DB backend is PostgreSQL (env ADMIN_DB_URL, or platform-standard DATABASE_URL as an
alias, both libpq DSNs). The rest of the module is
written against a tiny sqlite3-style adapter (_Conn) so the ~100 existing statements
did not have to be rewritten: it exposes connection-level .execute()/.executemany()/
.executescript() returning a cursor, dict rows (r['col']), and .commit()/.close();
it rewrites '?' placeholders to psycopg2's '%s' and coerces bool params to 0/1
(the schema has no boolean columns). One connection per operation, taken from and
returned to a per-schema pool (see '---- connection pool ----' below) — .close() means
"give it back", so every caller must still pair _c() with close().
"""
import hashlib, hmac, json, logging, os, secrets, threading, time
from datetime import datetime, timedelta, timezone
from .runtime_config import production          # one source of truth for hardened-mode
import urllib.error
import urllib.request
import psycopg2
from psycopg2.extras import RealDictCursor
from . import secrets_util as sec
from . import vectorstore as vs   # pgvector mirror seam (no-op unless VECTOR_BACKEND=pgvector)
from .context import get_tenant_db_scope

_log = logging.getLogger("poros.store")

def _database_url():
    """Resolve the metadata DSN.

    ADMIN_DB_URL is Poros' explicit setting and wins when both are present.
    DATABASE_URL lets standard application platforms inject their managed Postgres
    binding without a custom translation layer.
    """
    dsn = (
        os.environ.get("ADMIN_DB_URL")
        or os.environ.get("DATABASE_URL")
    )
    if not (dsn or "").strip():
        raise RuntimeError(
            "Metadata database is not configured: set ADMIN_DB_URL or DATABASE_URL. "
            "Poros intentionally has no built-in database password."
        )
    return dsn.strip()


DB_URL = _database_url()
# original uploaded documents live on disk (citations link to them); DB keeps only the path
FILES_DIR = os.environ.get("DOCS_FILES_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "files")

# Original uploaded documents (clickable citations): FILES_BACKEND=db keeps the encrypted
# envelope in Postgres (table document_files) so pods stay STATELESS — the ReadWriteOnce
# PVC behind FILES_DIR is what pins the deploy to one replica. Default "disk" keeps the
# legacy behavior byte-identical. READS ARE REF-DRIVEN whatever the backend (a "db:<id>"
# file_path reads the table, a filesystem path reads disk), so flipping the env — or
# rolling it back — never strands rows written the other way.
_DB_FILE_REF = "db:"


def _files_backend() -> str:
    value = (os.getenv("FILES_BACKEND") or "disk").strip().lower()
    return value if value in ("disk", "db") else "disk"


def _p(params):
    """psycopg2 params: None when empty (so a literal % in the SQL is not treated as a
    placeholder), and bool -> int (no boolean columns exist; 0/1 is what the code means)."""
    if not params:
        return None
    return tuple(int(x) if isinstance(x, bool) else x for x in params)


# ---- connection pool ----
# Every store operation takes a connection and gives it back. Connecting is cheap on
# Linux (~1-3ms) but expensive on Windows (~80ms — each connect spawns a Postgres backend
# process), which the dev test-loop pays thousands of times over, so connections are
# recycled instead of closed.
#
# The pool is keyed by resolved schema. ADMIN_DB_SCHEMA drives search_path, and the suite
# switches it per xdist worker and per test (tests/conftest.py, test_secrets_backend.py,
# test_security_p1.py) — one shared pool would hand back a connection still pointing at
# the previous namespace. Idle connections are held for the current schema only: the
# setting is process-wide, so a switch means the old namespace is done with (a test makes
# a schema per case) and its connections would otherwise pile up until Postgres refused
# new ones.
def _pool_max_idle():
    """Idle connections kept per schema. ADMIN_DB_POOL_SIZE=0 turns pooling off."""
    try:
        return max(0, int(os.environ.get("ADMIN_DB_POOL_SIZE") or 8))
    except ValueError:
        return 8


_POOL_MAX_IDLE = _pool_max_idle()
_pools = {}                     # schema -> [idle psycopg2 connections]
_pool_lock = threading.Lock()
_pool_pid = os.getpid()


def _schema():
    return os.environ.get("ADMIN_DB_SCHEMA") or "public"


def _drop(conn):
    try:
        conn.close()
    except Exception:           # this connection is being discarded either way
        pass


def _pool_take(schema):
    """An idle connection for `schema`, or None — evicting any held for another schema."""
    global _pool_pid
    evicted = []
    with _pool_lock:
        if os.getpid() != _pool_pid:       # inherited by a fork: those sockets are the parent's
            _pools.clear(); _pool_pid = os.getpid()
        if schema not in _pools:
            evicted = [c for conns in _pools.values() for c in conns]
            _pools.clear(); _pools[schema] = []
        conns = _pools[schema]
        conn = conns.pop() if conns else None
    for c in evicted:
        _drop(c)
    return conn


def _prepare(conn, schema):
    """Point a checked-out connection at its schema and the caller's tenant scope.

    Applied on every checkout, not once per connection: a plain SET is transactional in
    Postgres, so a caller-side rollback would otherwise silently leave the connection on
    the wrong search_path. autocommit keeps these settings out of the caller's own
    transaction for the same reason."""
    conn.rollback()             # no-op when idle; clears whatever the previous user left open
    conn.autocommit = True
    try:
        sql = "SELECT set_config('poros.tenant_id', %s, false)"
        if schema != "public":
            sql = f'SET search_path TO "{schema}", public; ' + sql
        cur = conn.cursor(); cur.execute(sql, (get_tenant_db_scope(),)); cur.close()
    finally:
        conn.autocommit = False


def _checkout(schema):
    while True:
        conn = _pool_take(schema)
        if conn is None:
            break
        if conn.closed:
            continue                       # the server hung up while it sat idle
        try:
            _prepare(conn, schema)
            return conn
        except psycopg2.Error:             # stale connection — discard it, try the next one
            _drop(conn)
    conn = psycopg2.connect(DB_URL)
    _prepare(conn, schema)                 # failing on a brand-new connection is a real outage
    return conn


def _release(schema, conn):
    """Hand a connection back to the pool.

    Rolled back first: a connection returned after a failed statement stays in the aborted
    state where every later statement errors out, and one returned mid-transaction would
    keep holding its snapshot and its locks while idle."""
    if _POOL_MAX_IDLE <= 0 or conn.closed:
        _drop(conn); return
    try:
        conn.rollback()
    except Exception:                      # unusable — do not put it back
        _drop(conn); return
    keep = False
    with _pool_lock:
        conns = _pools.get(schema)
        if conns is not None and len(conns) < _POOL_MAX_IDLE and os.getpid() == _pool_pid:
            conns.append(conn); keep = True
    if not keep:
        _drop(conn)


class _Conn:
    """sqlite3-connection-shaped adapter over a psycopg2 connection (see module docstring).

    Honors ADMIN_DB_SCHEMA (default 'public') via search_path — lets the test-suite give
    each isolated test its own namespace on one Postgres (the old 'separate admin.db file')."""
    def __init__(self):
        self._schema = _schema()
        self._c = _checkout(self._schema)
    def execute(self, sql, params=()):
        cur = self._c.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql.replace("?", "%s"), _p(params))
        return cur                              # iterable + .fetchone()/.fetchall()/.rowcount
    def executemany(self, sql, seq):
        cur = self._c.cursor()
        cur.executemany(sql.replace("?", "%s"), list(seq))
        return cur
    def executescript(self, sql):
        cur = self._c.cursor()
        cur.execute(sql)                        # psycopg2 runs multiple ';'-separated statements
        return cur
    def commit(self):
        self._c.commit()
    def close(self):
        """Return the connection to the pool (a real close only when pooling is off).
        Idempotent — a double close must not hand the same connection out twice."""
        conn, self._c = self._c, None
        if conn is not None:
            _release(self._schema, conn)


def _c():
    return _Conn()
def _id(p): return f"{p}_{secrets.token_hex(4)}"
# Every stored timestamp is UTC and SAYS SO. It used to be `time.strftime(...)` with no
# argument, which is the SERVER's local time with no marker — so an audit entry written at
# 10:47 in Jakarta read "03:47" in the console, and nothing in the string revealed which
# zone it meant. Two problems, only one of them visible: the display was 7 hours out, and
# the value silently depended on the container's TZ, so setting TZ on the pod would have
# started mixing local and UTC rows in one column with no way to tell them apart.
#
# The trailing Z is what makes it self-describing, and the frontend now converts to the
# viewer's own clock. Sorting and the `ts >= ?` range filters are unaffected: the suffix
# comes after every field that differs, so pre-Z rows still order and compare correctly
# against post-Z ones.
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def now(): return datetime.now(timezone.utc).strftime(_TS_FMT)


def ts_after(seconds: float) -> str:
    """A UTC timestamp `seconds` from now, in the same format as now(). Negative = past.

    Shared so expiry writers and cutoff readers cannot drift into different formats — a
    session compared as `expires >= now()` must be written by the same clock and shape.
    """
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime(_TS_FMT)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants(id TEXT PRIMARY KEY, name TEXT, created TEXT, descr TEXT, icon TEXT, status TEXT);
CREATE TABLE IF NOT EXISTS tenant_config(tenant_id TEXT, section TEXT, data TEXT, updated TEXT, PRIMARY KEY(tenant_id, section));
CREATE TABLE IF NOT EXISTS datasources(id TEXT PRIMARY KEY, tenant_id TEXT, type TEXT, name TEXT, conn TEXT, mdl TEXT);
CREATE TABLE IF NOT EXISTS roles(id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT, policy TEXT);
CREATE TABLE IF NOT EXISTS api_keys(key TEXT PRIMARY KEY, tenant_id TEXT, role TEXT, label TEXT, created TEXT,
    active INTEGER, key_hash TEXT, key_prefix TEXT);
CREATE TABLE IF NOT EXISTS usage_log(id SERIAL PRIMARY KEY, tenant_id TEXT, api_key TEXT, role TEXT,
    action TEXT, rows INTEGER, status TEXT, ts TEXT, model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, cost REAL);
CREATE TABLE IF NOT EXISTS llm_providers(id TEXT PRIMARY KEY, tenant_id TEXT, provider TEXT, label TEXT,
    api_key TEXT, api_base TEXT, extra TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS llm_models(id TEXT PRIMARY KEY, tenant_id TEXT, purpose TEXT, provider TEXT, model TEXT,
    params TEXT, monthly_budget_usd REAL, fallback_model TEXT, enabled INTEGER, created TEXT);
CREATE TABLE IF NOT EXISTS tenant_budgets(tenant_id TEXT PRIMARY KEY, monthly_usd REAL, created TEXT);
CREATE TABLE IF NOT EXISTS budget_reservations(id TEXT PRIMARY KEY, tenant_id TEXT, amount REAL,
    created TEXT, expires TEXT);
CREATE TABLE IF NOT EXISTS tenant_limits(tenant_id TEXT PRIMARY KEY, per_min INTEGER, created TEXT);
CREATE TABLE IF NOT EXISTS rate_limit_buckets(api_key TEXT PRIMARY KEY, tenant_id TEXT,
    tokens DOUBLE PRECISION, updated DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS console_login_attempts(subject_hash TEXT PRIMARY KEY, failures INTEGER,
    locked_until DOUBLE PRECISION, updated DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY, tenant_id TEXT, datasource_id TEXT, filename TEXT,
    mime TEXT, acl_roles TEXT, n_chunks INTEGER, created TEXT);
CREATE TABLE IF NOT EXISTS doc_chunks(id TEXT PRIMARY KEY, tenant_id TEXT, document_id TEXT, chunk_index INTEGER,
    text TEXT, embedding TEXT, acl_roles TEXT);
CREATE TABLE IF NOT EXISTS document_files(document_id TEXT PRIMARY KEY, tenant_id TEXT,
    data BYTEA);
CREATE TABLE IF NOT EXISTS report_files(job_id TEXT, tenant_id TEXT, name TEXT, mime TEXT,
    size INTEGER, sha256 TEXT, created TEXT, data BYTEA, PRIMARY KEY(job_id, name));
CREATE TABLE IF NOT EXISTS analysis_cache(tenant_id TEXT, key TEXT, kind TEXT, stamp REAL,
    data BYTEA, PRIMARY KEY(tenant_id, key));
CREATE TABLE IF NOT EXISTS report_inputs(id TEXT PRIMARY KEY, tenant_id TEXT, app_key_id TEXT,
    actor_id TEXT, status TEXT, created DOUBLE PRECISION, expires DOUBLE PRECISION,
    manifest TEXT);
CREATE TABLE IF NOT EXISTS report_input_parts(input_id TEXT, tenant_id TEXT, file_no INTEGER,
    chunk_no INTEGER, name TEXT, size INTEGER, data BYTEA, PRIMARY KEY(input_id, file_no, chunk_no));
CREATE TABLE IF NOT EXISTS memories(id TEXT PRIMARY KEY, tenant_id TEXT, user_id TEXT, kind TEXT, text TEXT,
    embedding TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS audit_log(id SERIAL PRIMARY KEY, tenant_id TEXT, trace_id TEXT, ts TEXT,
    role TEXT, user_id TEXT, event TEXT, model TEXT, status TEXT, code TEXT, cost REAL, detail TEXT);
CREATE TABLE IF NOT EXISTS admin_audit_log(id SERIAL PRIMARY KEY, event_id TEXT UNIQUE, ts TEXT,
    request_id TEXT, actor_user_id TEXT, actor_email TEXT, actor_role TEXT, auth_via TEXT,
    method TEXT, path TEXT, tenant_id TEXT, status INTEGER, source_ip TEXT,
    user_agent_hash TEXT, previous_hash TEXT, event_hash TEXT);
CREATE TABLE IF NOT EXISTS admin_audit_outbox(event_id TEXT PRIMARY KEY, payload TEXT,
    attempts INTEGER, delivered INTEGER, updated TEXT);
CREATE TABLE IF NOT EXISTS retired_secret_refs(ref TEXT PRIMARY KEY, created TEXT, purged INTEGER);
CREATE TABLE IF NOT EXISTS feedback(id TEXT PRIMARY KEY, tenant_id TEXT, trace_id TEXT, user_id TEXT,
    role TEXT, rating TEXT, reason TEXT, comment TEXT, question TEXT, answer TEXT,
    created TEXT, updated TEXT, UNIQUE(tenant_id, trace_id, user_id));
CREATE TABLE IF NOT EXISTS role_mappings(id TEXT PRIMARY KEY, tenant_id TEXT, external_id TEXT, role_name TEXT,
    created TEXT, UNIQUE(tenant_id, external_id));
CREATE TABLE IF NOT EXISTS unmapped_roles(tenant_id TEXT, external_id TEXT, count INTEGER, last_seen TEXT,
    PRIMARY KEY(tenant_id, external_id));
CREATE TABLE IF NOT EXISTS console_users(id TEXT PRIMARY KEY, email TEXT UNIQUE, name TEXT, role TEXT,
    password_hash TEXT, provider TEXT, active INTEGER, created TEXT, last_login TEXT);
CREATE TABLE IF NOT EXISTS console_sessions(token_hash TEXT PRIMARY KEY, user_id TEXT, created TEXT, expires TEXT);
CREATE TABLE IF NOT EXISTS tenant_groups(id TEXT PRIMARY KEY, name TEXT UNIQUE, descr TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS tenant_group_tenants(group_id TEXT, tenant_id TEXT,
    PRIMARY KEY(group_id, tenant_id));
CREATE TABLE IF NOT EXISTS tenant_group_users(group_id TEXT, user_id TEXT,
    PRIMARY KEY(group_id, user_id));
CREATE TABLE IF NOT EXISTS console_chat_sessions(id TEXT, tenant_id TEXT, owner_id TEXT,
    title TEXT, payload TEXT, created TEXT, updated TEXT,
    PRIMARY KEY(id,tenant_id,owner_id));
CREATE TABLE IF NOT EXISTS operation_jobs(
    id TEXT PRIMARY KEY, tenant_id TEXT, kind TEXT, status TEXT, progress INTEGER,
    phase TEXT, payload TEXT, result TEXT, error_code TEXT, error_message TEXT,
    actor_id TEXT, app_key_id TEXT, request_id TEXT, cancel_requested INTEGER,
    lease_owner TEXT, lease_until DOUBLE PRECISION, created TEXT, updated TEXT,
    started TEXT, finished TEXT);
CREATE TABLE IF NOT EXISTS operation_job_events(
    seq BIGSERIAL PRIMARY KEY, job_id TEXT, tenant_id TEXT, event TEXT, created TEXT);

-- Knowledge scope: Tenant > Role > Business > Datasource. A business is a named slice of
-- the tenant's data (Acme: 1P / 3P / GDV) and MAY span datasources -- 3P reads L2+L3 while
-- 1P reads L2 only, so the slice is (business, datasource) pairs, not a datasource group.
-- `datasources.mdl` keeps the TECHNICAL layer (scanned schema, pii_columns, connection
-- allowlist); the CURATION layer (tables/descriptions/metrics/concepts/examples) lives here
-- so two businesses can describe the same table differently.
CREATE TABLE IF NOT EXISTS businesses(id TEXT PRIMARY KEY, tenant_id TEXT, name TEXT, descr TEXT,
    status TEXT, created TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS business_scopes(tenant_id TEXT, business_id TEXT, datasource_id TEXT,
    knowledge TEXT, updated TEXT, PRIMARY KEY(business_id, datasource_id));
CREATE TABLE IF NOT EXISTS role_businesses(tenant_id TEXT, role_name TEXT, business_id TEXT,
    PRIMARY KEY(tenant_id, role_name, business_id));
-- One turn that ended in a confirmation QUESTION, waiting for its structured choice.
-- The id is the opaque token the client replays (design doc §5.3 allows a server-side
-- nonce in place of an AEAD token); `options` is the SERVER's copy of the menu including
-- the physical identity of each meaning, which the client payload never carries.
CREATE TABLE IF NOT EXISTS pending_confirmations(id TEXT PRIMARY KEY, tenant_id TEXT,
    kind TEXT, role TEXT, user_id TEXT, business_id TEXT, question TEXT, options TEXT,
    created TEXT, expires TEXT);
CREATE INDEX IF NOT EXISTS ix_pending_confirm_tenant ON pending_confirmations(tenant_id, expires);
CREATE INDEX IF NOT EXISTS ix_chunks_tenant ON doc_chunks(tenant_id);
CREATE INDEX IF NOT EXISTS ix_mem_tenant_user ON memories(tenant_id, user_id);
CREATE INDEX IF NOT EXISTS ix_usage_tenant ON usage_log(tenant_id);
CREATE INDEX IF NOT EXISTS ix_rate_limit_tenant ON rate_limit_buckets(tenant_id);
CREATE INDEX IF NOT EXISTS ix_budget_reservations_tenant ON budget_reservations(tenant_id, expires);
CREATE INDEX IF NOT EXISTS ix_audit_tenant ON audit_log(tenant_id, trace_id);
CREATE INDEX IF NOT EXISTS ix_admin_audit_ts ON admin_audit_log(ts);
CREATE INDEX IF NOT EXISTS ix_feedback_tenant ON feedback(tenant_id, updated);
CREATE UNIQUE INDEX IF NOT EXISTS ix_tenant_groups_name_lower ON tenant_groups(lower(name));
CREATE INDEX IF NOT EXISTS ix_tenant_group_tenant ON tenant_group_tenants(tenant_id);
CREATE INDEX IF NOT EXISTS ix_tenant_group_user ON tenant_group_users(user_id);
CREATE INDEX IF NOT EXISTS ix_operation_jobs_queue ON operation_jobs(status,created);
CREATE INDEX IF NOT EXISTS ix_operation_job_events_job ON operation_job_events(job_id,seq);
CREATE UNIQUE INDEX IF NOT EXISTS ix_businesses_tenant_name ON businesses(tenant_id, lower(name));
CREATE INDEX IF NOT EXISTS ix_business_scopes_ds ON business_scopes(datasource_id);
CREATE INDEX IF NOT EXISTS ix_role_businesses_business ON role_businesses(business_id);
"""

# columns added after the original MVP — migrate older admin.db files in place.
_MIGRATIONS = {"usage_log": {"model": "TEXT", "prompt_tokens": "INTEGER",
                             "completion_tokens": "INTEGER", "cost": "REAL",
                             "user_id": "TEXT", "trace_id": "TEXT"},
               "tenants": {"descr": "TEXT", "icon": "TEXT", "status": "TEXT",
                           "owner_id": "TEXT"},   # console user who created it; NULL = platform-owned

               "api_keys": {"env": "TEXT", "rate_limit": "INTEGER", "expiry": "TEXT",
                            "key_hash": "TEXT", "key_prefix": "TEXT"},
               "documents": {"embedder": "TEXT",    # which embedding model made the chunks' vectors
                             "file_path": "TEXT",   # original file on disk (clickable citations)
                             "archived": "INTEGER",  # 1 = stale version: kept, but EXCLUDED from retrieval
                             "config": "TEXT",      # per-document RAG config JSON {chunk_size,chunk_overlap,ocr}
                             "file_size": "BIGINT",
                             "job_status": "TEXT",  # processing | ready | failed
                             "job_error": "TEXT"},
               "memories": {"source": "TEXT"},      # 'admin' (manual) | 'auto' (AI captured it itself)
               # what a job is about in words a list shows ({"brand", "period"} for a report);
               # NOT purged with the payload, so a cancelled or failed one still has a name
               "operation_jobs": {"summary": "TEXT"},
               # the consumer app's conversation id of a chat turn (Audit Log by session)
               "audit_log": {"session_id": "TEXT"},
               # 0 = the business stays usable when named (the MBR report, an app's pin)
               # but is left out of the chat's "which business?" menu; NULL reads as offered
               "businesses": {"in_chat": "INTEGER"},

               # 0 = paused: config, MDL curation and documents are KEPT, but the source is
               # invisible to every answering path. NULL (pre-migration rows) reads as
               # enabled, so COALESCE(enabled,1) is the only correct test.
               "datasources": {"enabled": "INTEGER"}}


def _api_key_pepper() -> bytes:
    pepper = (os.getenv("API_KEY_PEPPER") or "").strip()
    if len(pepper) < 32:
        raise RuntimeError("API_KEY_PEPPER must be a stable secret of at least 32 characters")
    return pepper.encode()


def _api_key_digest(raw_key: str) -> str:
    return hmac.new(_api_key_pepper(), (raw_key or "").encode(), hashlib.sha256).hexdigest()


def _api_key_prefix(raw_key: str) -> str:
    raw = str(raw_key or "")
    return raw[:10] + ("…" if len(raw) > 10 else "")


def _migrate_api_keys(c):
    """One-way in-place migration from plaintext keys to HMAC digests.

    The existing secret remains valid for consumer apps, but is replaced in ``api_keys``
    by an opaque internal id. Matching usage rows are rewritten to that id as well.
    Any orphaned plaintext-looking usage value is irreversibly scrubbed.
    """
    rows = c.execute(
        "SELECT key FROM api_keys WHERE key_hash IS NULL OR key_hash=''"
    ).fetchall()
    for row in rows:
        raw = row["key"]
        key_id = _id("key")
        c.execute(
            "UPDATE api_keys SET key=?, key_hash=?, key_prefix=? WHERE key=?",
            (key_id, _api_key_digest(raw), _api_key_prefix(raw), raw),
        )
        c.execute("UPDATE usage_log SET api_key=? WHERE api_key=?", (key_id, raw))

    leftovers = c.execute(
        "SELECT DISTINCT api_key FROM usage_log WHERE api_key LIKE 'sk\\_%' ESCAPE '\\'"
    ).fetchall()
    for row in leftovers:
        raw = row["api_key"]
        redacted = "legacy_" + hashlib.sha256(raw.encode()).hexdigest()[:16]
        c.execute("UPDATE usage_log SET api_key=? WHERE api_key=?", (redacted, raw))


# The Jev (TypeSafe System One) integration was removed on 2026-10-14; its per-tenant
# settings, including an encrypted API key, lived in tenant_config under this section.
_RETIRED_SECTION = "systemone"


def _drop_retired_config(c) -> list:
    """Delete the retired integration's tenant settings and return their stored key
    tokens, purged once the deletion is committed. Idempotent: nothing left = no-op."""
    tokens = []
    for row in c.execute("SELECT data FROM tenant_config WHERE section=?",
                         (_RETIRED_SECTION,)).fetchall():
        try:
            tokens.append(json.loads(row["data"] or "{}").get("api_key") or "")
        except Exception:
            pass
    c.execute("DELETE FROM tenant_config WHERE section=?", (_RETIRED_SECTION,))
    return [t for t in tokens if t]


def init():
    c = _c()
    schema = os.environ.get("ADMIN_DB_SCHEMA") or "public"
    c.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')   # search_path already points here
    c.executescript(_SCHEMA)
    # Postgres ADD COLUMN IF NOT EXISTS is idempotent — no PRAGMA table_info probe needed.
    for table, cols in _MIGRATIONS.items():
        for col, typ in cols.items():
            c.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {typ}")
    _migrate_api_keys(c)
    retired_keys = _drop_retired_config(c)
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_api_keys_hash ON api_keys(key_hash)")
    # These indexes depend on columns introduced by migrations above.
    c.execute(
        "CREATE INDEX IF NOT EXISTS ix_usage_user_ts "
        "ON usage_log(tenant_id,user_id,ts)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS ix_audit_user_trace "
        "ON audit_log(tenant_id,event,user_id,trace_id,ts)"
    )
    c.execute(
        "CREATE INDEX IF NOT EXISTS ix_audit_session "
        "ON audit_log(tenant_id,session_id)"
    )
    if (os.getenv("ADMIN_DB_RLS") or "").strip().lower() in {"1", "true", "yes", "on"}:
        _configure_rls(c)
    c.commit(); c.close()
    for token in retired_keys:
        sec.purge(token)
    migrate_document_files_encryption()


_RLS_TABLES = {
    "tenants": "id",
    "tenant_config": "tenant_id",
    "datasources": "tenant_id",
    "roles": "tenant_id",
    "api_keys": "tenant_id",
    "usage_log": "tenant_id",
    "llm_providers": "tenant_id",
    "llm_models": "tenant_id",
    "tenant_budgets": "tenant_id",
    "budget_reservations": "tenant_id",
    "tenant_limits": "tenant_id",
    "rate_limit_buckets": "tenant_id",
    "documents": "tenant_id",
    "doc_chunks": "tenant_id",
    "document_files": "tenant_id",
    "report_files": "tenant_id",
    "analysis_cache": "tenant_id",
    "report_inputs": "tenant_id",
    "report_input_parts": "tenant_id",
    "memories": "tenant_id",
    "audit_log": "tenant_id",
    "feedback": "tenant_id",
    "role_mappings": "tenant_id",
    "unmapped_roles": "tenant_id",
    "tenant_group_tenants": "tenant_id",
    "businesses": "tenant_id",
    "business_scopes": "tenant_id",
    "role_businesses": "tenant_id",
    "pending_confirmations": "tenant_id",
    "console_chat_sessions": "tenant_id",
    "operation_jobs": "tenant_id",
    "operation_job_events": "tenant_id",
}


def _configure_rls(c):
    """Install FORCE RLS policies. `*` is reserved for control-plane/admin operations;
    consumer request code sets the exact tenant id before any tenant-scoped store call.
    """
    for table, column in _RLS_TABLES.items():
        policy = f"poros_tenant_isolation_{table}"
        predicate = (
            "(current_setting('poros.tenant_id', true) = '*' "
            f"OR {column} = current_setting('poros.tenant_id', true))"
        )
        c.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        c.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        c.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
        c.execute(
            f"CREATE POLICY {policy} ON {table} USING ({predicate}) WITH CHECK ({predicate})"
        )
    if production():
        role = c.execute(
            """SELECT rolname,rolsuper,rolbypassrls FROM pg_roles
               WHERE rolname=current_user"""
        ).fetchone()
        if role and (role["rolsuper"] or role["rolbypassrls"]):
            raise RuntimeError(
                "ADMIN_DB_RLS=1 is ineffective because ADMIN_DB_URL uses a superuser/"
                "BYPASSRLS role; use a dedicated non-superuser application role"
            )


# ---- tenants ----
def _row_tenant(r):
    d = dict(r)
    d["descr"] = d.get("descr") or ""
    d["icon"] = d.get("icon") or "hub"
    d["status"] = d.get("status") or "active"
    return d
def create_tenant(name, descr="", icon="hub", status="active", owner_id=None):
    tid = _id("t"); c = _c()
    c.execute("INSERT INTO tenants(id,name,created,descr,icon,status,owner_id) VALUES(?,?,?,?,?,?,?)",
              (tid, name, now(), descr, icon, status, owner_id)); c.commit(); c.close(); return tid
def update_tenant(tid, name=None, descr=None, icon=None, status=None):
    fields, vals = [], []
    for k, v in (("name", name), ("descr", descr), ("icon", icon), ("status", status)):
        if v is not None:
            fields.append(f"{k}=?"); vals.append(v)
    if not fields:
        return
    vals.append(tid); c = _c(); c.execute(f"UPDATE tenants SET {','.join(fields)} WHERE id=?", vals); c.commit(); c.close()
def list_tenants(owner_id=None):
    """All tenants, or those visible to one console user.

    A tenant manager sees the union of directly-owned tenants and tenants assigned
    through one or more tenant groups. ``owner_id`` keeps its historic name so older
    callers remain source-compatible; it now represents the console user whose access
    is being resolved. ``owner_email`` is joined for the admin console.
    """
    c = _c()
    sql = ("SELECT t.*, u.email AS owner_email FROM tenants t "
           "LEFT JOIN console_users u ON u.id = t.owner_id")
    if owner_id is None:
        rows = c.execute(sql + " ORDER BY t.created").fetchall()
    else:
        rows = c.execute(
            sql + """ WHERE t.owner_id=?
                      OR EXISTS (
                          SELECT 1
                          FROM tenant_group_tenants tgt
                          JOIN tenant_group_users tgu ON tgu.group_id=tgt.group_id
                          WHERE tgt.tenant_id=t.id AND tgu.user_id=?
                      )
                      ORDER BY t.created""",
            (owner_id, owner_id),
        ).fetchall()
    c.close(); return [_row_tenant(x) for x in rows]


def console_user_can_access_tenant(user_id, tenant_id):
    if not user_id or not tenant_id:
        return False
    c = _c()
    row = c.execute(
        """SELECT 1
           FROM tenants t
           WHERE t.id=?
             AND (
                 t.owner_id=?
                 OR EXISTS (
                     SELECT 1
                     FROM tenant_group_tenants tgt
                     JOIN tenant_group_users tgu ON tgu.group_id=tgt.group_id
                     WHERE tgt.tenant_id=t.id AND tgu.user_id=?
                 )
             )
           LIMIT 1""",
        (tenant_id, user_id, user_id),
    ).fetchone()
    c.close()
    return bool(row)


def set_tenant_owner(tid, owner_id):
    c = _c(); c.execute("UPDATE tenants SET owner_id=? WHERE id=?", (owner_id, tid)); c.commit(); c.close()
def get_tenant(tid):
    c = _c(); r = c.execute("SELECT * FROM tenants WHERE id=?", (tid,)).fetchone(); c.close(); return _row_tenant(r) if r else None
def get_tenant_config(tid, section):
    c = _c(); r = c.execute("SELECT data FROM tenant_config WHERE tenant_id=? AND section=?", (tid, section)).fetchone(); c.close()
    return json.loads(r["data"]) if r else None
def set_tenant_config(tid, section, data):
    c = _c(); c.execute("INSERT INTO tenant_config VALUES(?,?,?,?) ON CONFLICT(tenant_id,section) DO UPDATE SET data=excluded.data, updated=excluded.updated",
                        (tid, section, json.dumps(data), now())); c.commit(); c.close()
def delete_tenant(tid):
    c = _c()
    # collect secret tokens BEFORE deleting rows so their Vault entries can be purged (no-op for enc:)
    tokens = [r["conn"] for r in c.execute("SELECT conn FROM datasources WHERE tenant_id=?", (tid,))]
    tokens += [r["api_key"] for r in c.execute("SELECT api_key FROM llm_providers WHERE tenant_id=?", (tid,)) if r["api_key"]]
    # tenant_config rows holding an encrypted secret: the JWT secret (and the key of the
    # retired Jev integration, should a row predate its cleanup in init())
    for section, field in (("jwt", "secret"), (_RETIRED_SECTION, "api_key")):
        row = c.execute("SELECT data FROM tenant_config WHERE tenant_id=? AND section=?",
                        (tid, section)).fetchone()
        if row:
            try:
                tokens.append(json.loads(row["data"]).get(field) or "")
            except Exception:
                pass
    files = [r["file_path"] for r in c.execute(
        "SELECT file_path FROM documents WHERE tenant_id=? AND file_path IS NOT NULL", (tid,))]
    tokens += [r["payload"] for r in c.execute(
        "SELECT payload FROM console_chat_sessions WHERE tenant_id=?", (tid,)) if r["payload"]]
    tokens += [r["payload"] for r in c.execute(
        "SELECT payload FROM operation_jobs WHERE tenant_id=?", (tid,)) if r["payload"]]
    tokens += [r["result"] for r in c.execute(
        "SELECT result FROM operation_jobs WHERE tenant_id=?", (tid,)) if r["result"]]
    for t in ("tenants", "datasources", "roles", "memories", "documents", "doc_chunks",
              "document_files", "report_files", "analysis_cache", "report_inputs",
              "report_input_parts", "llm_providers",
              "llm_models", "feedback", "businesses", "business_scopes", "role_businesses",
              "pending_confirmations",
              "tenant_budgets", "budget_reservations", "tenant_limits", "rate_limit_buckets",
              "role_mappings", "unmapped_roles", "tenant_config", "console_chat_sessions",
              "operation_job_events", "operation_jobs"):
        col = "id" if t == "tenants" else "tenant_id"
        c.execute(f"DELETE FROM {t} WHERE {col}=?", (tid,))
    c.execute("DELETE FROM tenant_group_tenants WHERE tenant_id=?", (tid,))
    c.execute("UPDATE api_keys SET active=0 WHERE tenant_id=?", (tid,))
    c.commit(); c.close()
    _rm_files(files)
    vs.delete_tenant(tid)   # drop the tenant's chunks from the pgvector mirror
    for t in tokens:
        sec.purge(t)


# ---- datasources (conn secrets encrypted) ----
def create_datasource(tenant_id, type, name, conn, mdl):
    did = _id("ds"); c = _c()
    c.execute("INSERT INTO datasources VALUES(?,?,?,?,?,?)",
              (did, tenant_id, type, name, sec.encrypt_json(conn), json.dumps(mdl)))
    c.commit(); c.close(); return did
def _row_ds(x):
    row = dict(x)
    enabled = row.get("enabled")
    return {**row, "conn": sec.decrypt_json(x["conn"]), "mdl": json.loads(x["mdl"]),
            "enabled": True if enabled is None else bool(enabled)}
def list_datasources(tenant_id, include_disabled=False):
    """Datasources for a tenant. DISABLED ONES ARE EXCLUDED BY DEFAULT.

    The default is the safe one on purpose: every answering path (orchestrator routing,
    cross-datasource planning, /v1/query) calls this, so a paused source disappears from
    all of them without each caller having to remember. Only the admin console passes
    include_disabled=True — it must still show the card so it can be switched back on.
    """
    c = _c()
    sql = "SELECT * FROM datasources WHERE tenant_id=?"
    if not include_disabled:
        sql += " AND COALESCE(enabled,1)=1"
    rows = c.execute(sql, (tenant_id,)).fetchall(); c.close()
    return [_row_ds(x) for x in rows]
def disabled_datasource_ids(tenant_id):
    """Ids of paused datasources. Read live, per retrieval, rather than mirrored into
    pgvector: mirror writes are best-effort by design (a PG hiccup drifts them), and
    "this source is switched off" must not depend on a write that is allowed to fail."""
    c = _c()
    rows = c.execute("SELECT id FROM datasources WHERE tenant_id=? AND COALESCE(enabled,1)=0",
                     (tenant_id,)).fetchall()
    c.close()
    return [x["id"] for x in rows]
def set_datasource_enabled(did, enabled: bool):
    """Pause/resume a datasource. Nothing is deleted — this is reversible by design, so an
    admin can take a broken or costly source out of service without losing its curation."""
    c = _c(); c.execute("UPDATE datasources SET enabled=? WHERE id=?",
                        (1 if enabled else 0, did)); c.commit(); c.close()
def get_datasource(did):
    c = _c(); x = c.execute("SELECT * FROM datasources WHERE id=?", (did,)).fetchone(); c.close()
    return _row_ds(x) if x else None
def update_mdl(did, mdl):
    c = _c(); c.execute("UPDATE datasources SET mdl=? WHERE id=?", (json.dumps(mdl), did)); c.commit(); c.close()
def update_datasource_conn(did, conn):
    """Replace ONLY the connection config (re-encrypted). MDL, documents and the id
    stay untouched — delete+recreate would lose the admin's curation. The replaced
    secret's Vault entry is purged (no-op for enc:)."""
    c = _c()
    row = c.execute("SELECT conn FROM datasources WHERE id=?", (did,)).fetchone()
    if not row:
        c.close(); return False
    enc = sec.encrypt_json(conn)
    c.execute("UPDATE datasources SET conn=? WHERE id=?", (enc, did))
    c.commit(); c.close()
    if row["conn"] and row["conn"] != enc:
        sec.purge(row["conn"])
    return True
def delete_datasource(did):
    c = _c()
    row = c.execute("SELECT conn FROM datasources WHERE id=?", (did,)).fetchone()
    files = [r["file_path"] for r in c.execute(
        "SELECT file_path FROM documents WHERE datasource_id=? AND file_path IS NOT NULL", (did,))]
    doc_ids = [r["id"] for r in c.execute("SELECT id FROM documents WHERE datasource_id=?", (did,))]
    c.execute("DELETE FROM datasources WHERE id=?", (did,))
    # businesses must not keep pointing at a connection that no longer exists
    c.execute("DELETE FROM business_scopes WHERE datasource_id=?", (did,))
    c.execute("DELETE FROM doc_chunks WHERE document_id IN (SELECT id FROM documents WHERE datasource_id=?)", (did,))
    c.execute("DELETE FROM document_files WHERE document_id IN (SELECT id FROM documents WHERE datasource_id=?)", (did,))
    c.execute("DELETE FROM documents WHERE datasource_id=?", (did,))
    c.commit(); c.close()
    _rm_files(files)
    vs.delete_documents(doc_ids)   # drop the datasource's chunks from the pgvector mirror
    if row:
        sec.purge(row["conn"])


# ---- console users (login panel: role 'admin' sees every tenant, 'tenant_manager'
# ----                only the tenants they own) + revocable opaque sessions ----
def _row_console_user(r, with_hash=False):
    d = dict(r)
    d["active"] = bool(d.get("active"))
    if not with_hash:
        d.pop("password_hash", None)
    return d
def create_console_user(email, name, role, password_hash="", provider="local"):
    uid = _id("u"); c = _c()
    c.execute("INSERT INTO console_users(id,email,name,role,password_hash,provider,active,created) VALUES(?,?,?,?,?,?,1,?)",
              (uid, (email or "").strip().lower(), name, role, password_hash, provider, now()))
    c.commit(); c.close(); return uid
def get_console_user(uid, with_hash=False):
    c = _c(); r = c.execute("SELECT * FROM console_users WHERE id=?", (uid,)).fetchone(); c.close()
    return _row_console_user(r, with_hash) if r else None
def get_console_user_by_email(email, with_hash=False):
    c = _c(); r = c.execute("SELECT * FROM console_users WHERE email=?", ((email or "").strip().lower(),)).fetchone(); c.close()
    return _row_console_user(r, with_hash) if r else None
def list_console_users():
    c = _c(); rows = c.execute("SELECT * FROM console_users ORDER BY created").fetchall(); c.close()
    return [_row_console_user(x) for x in rows]
def update_console_user(uid, name=None, role=None, active=None, password_hash=None):
    fields, vals = [], []
    for k, v in (("name", name), ("role", role), ("active", active), ("password_hash", password_hash)):
        if v is not None:
            fields.append(f"{k}=?"); vals.append(v)
    if not fields:
        return
    vals.append(uid); c = _c()
    c.execute(f"UPDATE console_users SET {','.join(fields)} WHERE id=?", vals)
    if role is not None and role != "tenant_manager":
        # Group assignment is meaningful only for tenant managers. Clearing it avoids
        # stale access silently reappearing if an admin is demoted at a later date.
        c.execute("DELETE FROM tenant_group_users WHERE user_id=?", (uid,))
    if active is not None and not active:
        c.execute("DELETE FROM console_sessions WHERE user_id=?", (uid,))   # deactivation revokes logins NOW
    c.commit(); c.close()
def delete_console_user(uid):
    c = _c()
    c.execute("DELETE FROM console_sessions WHERE user_id=?", (uid,))
    c.execute("DELETE FROM tenant_group_users WHERE user_id=?", (uid,))
    c.execute("DELETE FROM console_users WHERE id=?", (uid,))
    # their tenants stay (admin still sees them, owner shows as unassigned)
    c.execute("UPDATE tenants SET owner_id=NULL WHERE owner_id=?", (uid,))
    c.commit(); c.close()


# ---- tenant groups (console authorization: users <-> groups <-> tenants) ----
def create_tenant_group(name, descr=""):
    gid = _id("tg"); c = _c()
    c.execute(
        "INSERT INTO tenant_groups(id,name,descr,created) VALUES(?,?,?,?)",
        (gid, (name or "").strip(), (descr or "").strip(), now()),
    )
    c.commit(); c.close()
    return gid


def get_tenant_group(gid):
    c = _c()
    row = c.execute("SELECT * FROM tenant_groups WHERE id=?", (gid,)).fetchone()
    c.close()
    return dict(row) if row else None


def get_tenant_group_by_name(name):
    c = _c()
    row = c.execute(
        "SELECT * FROM tenant_groups WHERE lower(name)=lower(?)",
        ((name or "").strip(),),
    ).fetchone()
    c.close()
    return dict(row) if row else None


def list_tenant_groups():
    c = _c()
    groups = [dict(x) for x in c.execute(
        "SELECT * FROM tenant_groups ORDER BY lower(name), created"
    ).fetchall()]
    tenants = [dict(x) for x in c.execute(
        """SELECT tgt.group_id, t.id, t.name, t.status
           FROM tenant_group_tenants tgt
           JOIN tenants t ON t.id=tgt.tenant_id
           ORDER BY lower(t.name), t.created"""
    ).fetchall()]
    users = [dict(x) for x in c.execute(
        """SELECT tgu.group_id, u.id, u.email, u.name, u.role, u.active
           FROM tenant_group_users tgu
           JOIN console_users u ON u.id=tgu.user_id
           ORDER BY lower(u.email), u.created"""
    ).fetchall()]
    c.close()
    tenant_map, user_map = {}, {}
    for item in tenants:
        tenant_map.setdefault(item.pop("group_id"), []).append(item)
    for item in users:
        item["active"] = bool(item.get("active"))
        user_map.setdefault(item.pop("group_id"), []).append(item)
    for group in groups:
        group["tenants"] = tenant_map.get(group["id"], [])
        group["users"] = user_map.get(group["id"], [])
        group["tenant_ids"] = [x["id"] for x in group["tenants"]]
        group["user_ids"] = [x["id"] for x in group["users"]]
    return groups


def update_tenant_group(gid, name=None, descr=None):
    fields, vals = [], []
    for key, value in (("name", name), ("descr", descr)):
        if value is not None:
            fields.append(f"{key}=?")
            vals.append((value or "").strip())
    if not fields:
        return
    vals.append(gid)
    c = _c()
    c.execute(f"UPDATE tenant_groups SET {','.join(fields)} WHERE id=?", vals)
    c.commit(); c.close()


def replace_tenant_group_members(gid, tenant_ids=None, user_ids=None):
    """Atomically replace either side of a group's membership.

    ``None`` preserves that side; an empty list removes every assignment on it.
    Validation is performed by the admin router before this store operation.
    """
    c = _c()
    if tenant_ids is not None:
        c.execute("DELETE FROM tenant_group_tenants WHERE group_id=?", (gid,))
        c.executemany(
            "INSERT INTO tenant_group_tenants(group_id,tenant_id) VALUES(?,?)",
            [(gid, tid) for tid in dict.fromkeys(tenant_ids)],
        )
    if user_ids is not None:
        c.execute("DELETE FROM tenant_group_users WHERE group_id=?", (gid,))
        c.executemany(
            "INSERT INTO tenant_group_users(group_id,user_id) VALUES(?,?)",
            [(gid, uid) for uid in dict.fromkeys(user_ids)],
        )
    c.commit(); c.close()


def delete_tenant_group(gid):
    c = _c()
    c.execute("DELETE FROM tenant_group_tenants WHERE group_id=?", (gid,))
    c.execute("DELETE FROM tenant_group_users WHERE group_id=?", (gid,))
    c.execute("DELETE FROM tenant_groups WHERE id=?", (gid,))
    c.commit(); c.close()
def touch_console_login(uid):
    c = _c(); c.execute("UPDATE console_users SET last_login=? WHERE id=?", (now(), uid)); c.commit(); c.close()

def _tok_hash(token):
    # only the hash is stored — a leaked DB dump can't be replayed as live sessions
    return hashlib.sha256((token or "").encode()).hexdigest()
def create_console_session(user_id, ttl_hours=12):
    token = "st_" + secrets.token_hex(24); c = _c()
    c.execute("DELETE FROM console_sessions WHERE expires < ?", (now(),))   # opportunistic GC
    c.execute("INSERT INTO console_sessions VALUES(?,?,?,?)",
              (_tok_hash(token), user_id, now(), ts_after(ttl_hours * 3600)))
    c.commit(); c.close(); return token
def get_console_session(token):
    """The console user for a live session token, or None (expired / revoked / deactivated)."""
    c = _c()
    r = c.execute("""SELECT u.* FROM console_sessions s JOIN console_users u ON u.id = s.user_id
                     WHERE s.token_hash=? AND s.expires >= ? AND u.active=1""",
                  (_tok_hash(token), now())).fetchone()
    c.close(); return _row_console_user(r) if r else None
def delete_console_session(token):
    c = _c(); c.execute("DELETE FROM console_sessions WHERE token_hash=?", (_tok_hash(token),)); c.commit(); c.close()


def list_console_chat_sessions(tenant_id, owner_id):
    c = _c()
    rows = c.execute(
        """SELECT id,title,payload,created,updated FROM console_chat_sessions
           WHERE tenant_id=? AND owner_id=? ORDER BY updated DESC""",
        (tenant_id, owner_id),
    ).fetchall()
    c.close()
    out = []
    for row in rows:
        payload = sec.decrypt_json(row["payload"])
        out.append({
            "id": row["id"],
            "title": row["title"] or "",
            "created": payload.get("created") or row["created"],
            "updated": payload.get("updated") or row["updated"],
            "messages": payload.get("messages") or [],
            # the business this conversation was answered in, once its owner picked one
            "business": payload.get("business"),
        })
    return out


def upsert_console_chat_session(tenant_id, owner_id, session):
    session_id = session["id"]
    payload = sec.encrypt_json({
        "created": session.get("created"),
        "updated": session.get("updated"),
        "messages": session.get("messages") or [],
        "business": session.get("business"),
    })
    c = _c()
    old = c.execute(
        """SELECT payload FROM console_chat_sessions
           WHERE id=? AND tenant_id=? AND owner_id=?""",
        (session_id, tenant_id, owner_id),
    ).fetchone()
    c.execute(
        """INSERT INTO console_chat_sessions(id,tenant_id,owner_id,title,payload,created,updated)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(id,tenant_id,owner_id) DO UPDATE SET
             title=excluded.title,payload=excluded.payload,updated=excluded.updated""",
        (session_id, tenant_id, owner_id, session.get("title") or "", payload,
         str(session.get("created") or now()), str(session.get("updated") or now())),
    )
    c.commit(); c.close()
    if old and old["payload"] and old["payload"] != payload:
        sec.purge(old["payload"])


def delete_console_chat_session(tenant_id, owner_id, session_id):
    c = _c()
    old = c.execute(
        """SELECT payload FROM console_chat_sessions
           WHERE id=? AND tenant_id=? AND owner_id=?""",
        (session_id, tenant_id, owner_id),
    ).fetchone()
    c.execute(
        """DELETE FROM console_chat_sessions
           WHERE id=? AND tenant_id=? AND owner_id=?""",
        (session_id, tenant_id, owner_id),
    )
    c.commit(); c.close()
    if old and old["payload"]:
        sec.purge(old["payload"])


# ---- roles ----
def create_role(tenant_id, name, policy):
    rid = _id("r"); c = _c(); c.execute("INSERT INTO roles VALUES(?,?,?,?)", (rid, tenant_id, name, json.dumps(policy))); c.commit(); c.close(); return rid
def update_role(tenant_id, name, policy):
    c = _c(); c.execute("UPDATE roles SET policy=? WHERE tenant_id=? AND name=?", (json.dumps(policy), tenant_id, name)); c.commit(); c.close()
def delete_role(tenant_id, name):
    c = _c()
    c.execute("DELETE FROM roles WHERE tenant_id=? AND name=?", (tenant_id, name))
    # a deleted role must not leave assignments a later same-named role would inherit
    c.execute("DELETE FROM role_businesses WHERE tenant_id=? AND role_name=?", (tenant_id, name))
    c.commit(); c.close()
def list_roles(tenant_id):
    c = _c(); rows = c.execute("SELECT * FROM roles WHERE tenant_id=?", (tenant_id,)).fetchall(); c.close()
    return [{**dict(x), "policy": json.loads(x["policy"])} for x in rows]
def get_role(tenant_id, name):
    c = _c(); x = c.execute("SELECT * FROM roles WHERE tenant_id=? AND name=?", (tenant_id, name)).fetchone(); c.close()
    return {**dict(x), "policy": json.loads(x["policy"])} if x else None


# ---- businesses (Tenant > Role > Business > Datasource) ----
# A business is a named slice of a tenant's data (Acme: 1P / 3P / GDV). It MAY span
# datasources -- 3P reads L2+L3 while 1P reads L2 only -- so a scope is a (business,
# datasource) PAIR, not a datasource group.
#
# Curation lives here rather than on datasources.mdl so two businesses can describe the
# SAME physical table differently. Two things deliberately stay on the datasource: the
# scanned schema (technical truth, one per connection) and pii_columns (a hard rule; one
# shared list per table must not be able to drift between businesses).
KNOWLEDGE_KEYS = ("allowed_tables", "descriptions", "metrics", "business_concepts",
                  "routing_aliases", "reference_values", "examples")


def blank_knowledge() -> dict:
    """An empty curation set, shaped exactly like the MDL curation keys it replaces."""
    return {"allowed_tables": [], "descriptions": {}, "metrics": [], "business_concepts": [],
            "routing_aliases": [], "reference_values": {}, "examples": []}


def clean_knowledge(k) -> dict:
    """Coerce caller-supplied curation into the blank shape, dropping unknown keys.

    This JSON is read by the answering path, so it must not become a place where arbitrary
    API fields accumulate; a wrong-typed value falls back to the empty container rather
    than reaching govern() as a surprise.
    """
    src = k if isinstance(k, dict) else {}
    base = blank_knowledge()
    for key in KNOWLEDGE_KEYS:
        val = src.get(key)
        if isinstance(val, type(base[key])):
            base[key] = val
    return base


def _row_business(r) -> dict:
    d = dict(r)
    d["status"] = d.get("status") or "active"
    d["in_chat"] = d.get("in_chat") is None or bool(d["in_chat"])
    return d


def create_business(tenant_id, name, descr="", status="active") -> str:
    bid = _id("b")
    c = _c()
    c.execute("INSERT INTO businesses(id,tenant_id,name,descr,status,created,updated) VALUES(?,?,?,?,?,?,?)",
              (bid, tenant_id, (name or "").strip(), (descr or "").strip(),
               (status or "active"), now(), now()))
    c.commit(); c.close()
    return bid


def get_business(bid):
    c = _c()
    row = c.execute("SELECT * FROM businesses WHERE id=?", (bid,)).fetchone()
    c.close()
    return _row_business(row) if row else None


# ---- pending confirmations (a QUESTION turn waiting for its structured choice) ----
def create_pending_confirmation(tenant_id, kind, role, user_id, business_id, question,
                                options, ttl_seconds=600) -> str:
    """Persist one confirmation the server just asked; returns the opaque token.

    The token is a random server-side nonce (design doc §5.3's alternative to AEAD):
    nothing about the question, options or scope is decodable from it, and replaying it
    only works against this row's own tenant/role/user/business binding, before `expires`.
    """
    cid = "pc_" + secrets.token_hex(16)
    c = _c()
    # opportunistic hygiene: this tenant's expired questions are dead weight, never data
    c.execute("DELETE FROM pending_confirmations WHERE tenant_id=? AND expires < ?",
              (tenant_id, now()))
    c.execute("""INSERT INTO pending_confirmations(id,tenant_id,kind,role,user_id,
                 business_id,question,options,created,expires) VALUES(?,?,?,?,?,?,?,?,?,?)""",
              (cid, tenant_id, kind, role or "", user_id or "", business_id or "",
               question or "", json.dumps(options or []), now(), ts_after(ttl_seconds)))
    c.commit(); c.close()
    return cid


def get_pending_confirmation(cid):
    """The pending question this token refers to, or None when unknown/expired."""
    c = _c()
    row = c.execute("SELECT * FROM pending_confirmations WHERE id=?",
                    (str(cid or ""),)).fetchone()
    c.close()
    if not row:
        return None
    out = dict(row)
    if out.get("expires", "") < now():
        return None
    try:
        out["options"] = json.loads(out.get("options") or "[]")
    except Exception:
        out["options"] = []
    return out


def get_business_by_name(tenant_id, name):
    c = _c()
    row = c.execute("SELECT * FROM businesses WHERE tenant_id=? AND lower(name)=lower(?)",
                    (tenant_id, (name or "").strip())).fetchone()
    c.close()
    return _row_business(row) if row else None


def list_businesses(tenant_id, include_paused=True):
    """Businesses of a tenant, each with its per-datasource knowledge and the roles allowed
    to use it. Explicitly ordered: Postgres gives no implicit row order, and the console
    tree must not reshuffle between reads."""
    c = _c()
    rows = c.execute("SELECT * FROM businesses WHERE tenant_id=? ORDER BY lower(name), created",
                     (tenant_id,)).fetchall()
    items = [_row_business(x) for x in rows]
    if not include_paused:
        items = [b for b in items if b["status"] == "active"]
    ids = [b["id"] for b in items]
    scopes, roles = {}, {}
    if ids:
        marks = ",".join(["?"] * len(ids))
        for r in c.execute("SELECT * FROM business_scopes WHERE business_id IN (%s)"
                           " ORDER BY datasource_id" % marks, ids).fetchall():
            scopes.setdefault(r["business_id"], {})[r["datasource_id"]] = (
                clean_knowledge(json.loads(r["knowledge"] or "{}")))
        for r in c.execute("SELECT * FROM role_businesses WHERE business_id IN (%s)"
                           " ORDER BY role_name" % marks, ids).fetchall():
            roles.setdefault(r["business_id"], []).append(r["role_name"])
    c.close()
    for b in items:
        b["scopes"] = scopes.get(b["id"], {})
        b["datasource_ids"] = sorted(b["scopes"])
        b["roles"] = roles.get(b["id"], [])
    return items


def update_business(bid, name=None, descr=None, status=None, in_chat=None):
    fields, vals = [], []
    for key, value in (("name", name), ("descr", descr), ("status", status)):
        if value is not None:
            fields.append(key + "=?")
            vals.append((value or "").strip())
    if in_chat is not None:
        fields.append("in_chat=?")
        vals.append(1 if in_chat else 0)
    if not fields:
        return
    fields.append("updated=?")
    vals += [now(), bid]
    c = _c()
    c.execute("UPDATE businesses SET %s WHERE id=?" % ",".join(fields), vals)
    c.commit(); c.close()


def delete_business(bid):
    """Drop a business and everything scoped to it. Its curation is NOT recoverable, so the
    console confirms first -- the reversible alternative is status='paused'."""
    c = _c()
    c.execute("DELETE FROM business_scopes WHERE business_id=?", (bid,))
    c.execute("DELETE FROM role_businesses WHERE business_id=?", (bid,))
    c.execute("DELETE FROM businesses WHERE id=?", (bid,))
    c.commit(); c.close()


# ---- (business, datasource) knowledge ----
def get_business_knowledge(bid, datasource_id):
    """The curation a business applies to one datasource, or None when that datasource is not
    part of the business. None and 'present but empty' mean different things: not-part-of
    means the answering path must not reach the datasource at all."""
    c = _c()
    row = c.execute("SELECT knowledge FROM business_scopes WHERE business_id=? AND datasource_id=?",
                    (bid, datasource_id)).fetchone()
    c.close()
    return clean_knowledge(json.loads(row["knowledge"] or "{}")) if row else None


def set_business_knowledge(bid, datasource_id, knowledge):
    """Attach a datasource to a business, or replace the curation it applies. Idempotent.

    The tenant is DERIVED from the two ids rather than passed in, and a mismatch is refused:
    scoping one tenant's datasource into another tenant's business would be a cross-tenant
    leak that no amount of router-level checking downstream could undo.
    """
    c = _c()
    biz = c.execute("SELECT tenant_id FROM businesses WHERE id=?", (bid,)).fetchone()
    ds = c.execute("SELECT tenant_id FROM datasources WHERE id=?", (datasource_id,)).fetchone()
    if not biz or not ds:
        c.close()
        raise ValueError("business or datasource does not exist")
    if biz["tenant_id"] != ds["tenant_id"]:
        c.close()
        raise ValueError("business and datasource belong to different tenants")
    c.execute("""INSERT INTO business_scopes(tenant_id,business_id,datasource_id,knowledge,updated)
                 VALUES(?,?,?,?,?)
                 ON CONFLICT(business_id,datasource_id)
                 DO UPDATE SET knowledge=excluded.knowledge, updated=excluded.updated""",
              (biz["tenant_id"], bid, datasource_id,
               json.dumps(clean_knowledge(knowledge)), now()))
    c.commit(); c.close()


def delete_business_scope(bid, datasource_id):
    """Remove a datasource from a business. The datasource and its schema are untouched."""
    c = _c()
    c.execute("DELETE FROM business_scopes WHERE business_id=? AND datasource_id=?",
              (bid, datasource_id))
    c.commit(); c.close()


def list_datasource_businesses(datasource_id):
    """Which businesses reference a datasource -- the reverse lookup the Database tab needs
    before a delete, and the one rescan needs to prune dropped tables everywhere."""
    c = _c()
    rows = c.execute("""SELECT b.* FROM businesses b JOIN business_scopes s ON s.business_id=b.id
                        WHERE s.datasource_id=? ORDER BY lower(b.name)""",
                     (datasource_id,)).fetchall()
    c.close()
    return [_row_business(x) for x in rows]


def prune_business_tables(datasource_id, valid_tables):
    """Drop tables that no longer exist in a datasource from every business scoping it.

    Called after a rescan. Without it curation outlives the table it describes: 84 Acme
    tables once vanished from a scan while their allowlist entries stayed, so questions
    routed to names govern() then refused. Returns {business_id: [removed tables]}.
    """
    keep = {str(t).lower() for t in (valid_tables or [])}
    removed = {}
    c = _c()
    rows = c.execute("SELECT business_id, knowledge FROM business_scopes WHERE datasource_id=?",
                     (datasource_id,)).fetchall()
    for r in rows:
        k = clean_knowledge(json.loads(r["knowledge"] or "{}"))
        gone = [t for t in k["allowed_tables"] if str(t).lower() not in keep]
        if not gone:
            continue
        k["allowed_tables"] = [t for t in k["allowed_tables"] if str(t).lower() in keep]
        # descriptions are keyed by "table" AND "table.column": both die with the table
        k["descriptions"] = {key: v for key, v in k["descriptions"].items()
                             if str(key).split(".")[0].lower() in keep}
        c.execute("UPDATE business_scopes SET knowledge=?, updated=? "
                  "WHERE business_id=? AND datasource_id=?",
                  (json.dumps(k), now(), r["business_id"], datasource_id))
        removed[r["business_id"]] = gone
    c.commit(); c.close()
    return removed


# ---- role -> businesses (which slices a role may use) ----
def list_role_businesses(tenant_id, role_name):
    c = _c()
    rows = c.execute("""SELECT b.* FROM businesses b JOIN role_businesses rb ON rb.business_id=b.id
                        WHERE rb.tenant_id=? AND rb.role_name=? ORDER BY lower(b.name)""",
                     (tenant_id, role_name)).fetchall()
    c.close()
    return [_row_business(x) for x in rows]


def set_role_businesses(tenant_id, role_name, business_ids):
    """Replace the set of businesses a role may use. An empty list assigns none."""
    c = _c()
    c.execute("DELETE FROM role_businesses WHERE tenant_id=? AND role_name=?",
              (tenant_id, role_name))
    rows = [(tenant_id, role_name, bid) for bid in dict.fromkeys(business_ids or [])]
    if rows:
        c.executemany("INSERT INTO role_businesses(tenant_id,role_name,business_id) VALUES(?,?,?)",
                      rows)
    c.commit(); c.close()


# ---- role mappings (external RBAC alias -> tenant role, per tenant) ----
def upsert_role_mapping(tenant_id, external_id, role_name):
    """Map a client system's role/group id (opaque string) onto one of OUR tenant roles.
    Also clears the id from unmapped_roles — it is mapped now."""
    c = _c()
    c.execute("""INSERT INTO role_mappings(id,tenant_id,external_id,role_name,created) VALUES(?,?,?,?,?)
                 ON CONFLICT(tenant_id,external_id) DO UPDATE SET role_name=excluded.role_name""",
              (_id("map"), tenant_id, external_id, role_name, now()))
    c.execute("DELETE FROM unmapped_roles WHERE tenant_id=? AND external_id=?", (tenant_id, external_id))
    c.commit(); c.close()
def list_role_mappings(tenant_id):
    c = _c(); rows = c.execute("SELECT * FROM role_mappings WHERE tenant_id=? ORDER BY created", (tenant_id,)).fetchall(); c.close()
    return [dict(x) for x in rows]
def get_role_mapping(tenant_id, external_id):
    c = _c(); x = c.execute("SELECT role_name FROM role_mappings WHERE tenant_id=? AND external_id=?",
                            (tenant_id, external_id)).fetchone(); c.close()
    return x["role_name"] if x else None
def delete_role_mapping(tenant_id, external_id):
    c = _c(); c.execute("DELETE FROM role_mappings WHERE tenant_id=? AND external_id=?", (tenant_id, external_id)); c.commit(); c.close()


# ---- unmapped external roles (seen on JWTs but resolving to no tenant role) ----
def record_unmapped_role(tenant_id, external_id):
    """Remember a JWT role value that failed to resolve, so admins can map it later.
    Fail-safe: visibility must never break the (already-403) request path."""
    try:
        c = _c()
        c.execute("""INSERT INTO unmapped_roles(tenant_id,external_id,count,last_seen) VALUES(?,?,1,?)
                     ON CONFLICT(tenant_id,external_id) DO UPDATE SET count=unmapped_roles.count+1, last_seen=excluded.last_seen""",
                  (tenant_id, str(external_id)[:200], now()))
        c.commit(); c.close()
    except Exception:
        pass
def list_unmapped_roles(tenant_id):
    c = _c(); rows = c.execute("SELECT * FROM unmapped_roles WHERE tenant_id=? ORDER BY last_seen DESC", (tenant_id,)).fetchall(); c.close()
    return [dict(x) for x in rows]


# ---- per-tenant JWT verification config (secret encrypted at rest) ----
JWT_CONFIG_DEFAULTS = {
    "secret": "",                    # legacy/dev HS* only; rejected in production
    "jwks_url": "",                  # preferred production mode (IdP key rotation)
    "public_key": "",                # PEM fallback when an IdP has no JWKS endpoint
    "algorithms": [],
    "issuer": "",
    "audience": "",
    "role_claim": "role",
}

def set_jwt_config(tenant_id, data):
    """Persist the tenant's JWT config. The secret is encrypted via secrets_util; a blank
    secret on update keeps the existing one (same pattern as provider api_key upsert)."""
    cfg = {**JWT_CONFIG_DEFAULTS, **{k: v for k, v in (data or {}).items() if k in JWT_CONFIG_DEFAULTS}}
    old = get_tenant_config(tenant_id, "jwt") or {}
    if cfg["secret"]:
        cfg["secret"] = sec.encrypt(cfg["secret"])       # encrypt new FIRST: if it raises, the old one stays
    else:
        cfg["secret"] = old.get("secret") or ""
    set_tenant_config(tenant_id, "jwt", cfg)
    if old.get("secret") and old["secret"] != cfg["secret"]:
        sec.purge(old["secret"])                         # rotated -> drop the replaced Vault entry

def get_jwt_config(tenant_id):
    """Tenant JWT config with the secret DECRYPTED (for verification); None when unset."""
    cfg = get_tenant_config(tenant_id, "jwt")
    if not cfg:
        return None
    out = {**JWT_CONFIG_DEFAULTS, **cfg}
    out["secret"] = sec.decrypt(out["secret"]) if out.get("secret") else ""
    return out


# ---- api keys (per-key env / rate_limit / expiry; last_used derived from usage) ----
def create_api_key(tenant_id, role="", label="", env="Production", rate_limit=None, expiry=None):
    # `role` is legacy/unused under strict Pattern B (the end-user role arrives per request
    # via a verified JWT). Kept nullable only so old admin.db rows still read back.
    raw = "sk_" + secrets.token_hex(16)
    key_id = _id("key")
    c = _c()
    c.execute("""INSERT INTO api_keys(
                    key,tenant_id,role,label,created,active,env,rate_limit,expiry,key_hash,key_prefix
                 ) VALUES(?,?,?,?,?,1,?,?,?,?,?)""",
              (key_id, tenant_id, role, label, now(), env, rate_limit, expiry,
               _api_key_digest(raw), _api_key_prefix(raw)))
    c.commit(); c.close()
    return raw


def _row_api_key(row):
    if not row:
        return None
    out = dict(row)
    out["active"] = bool(out.get("active"))
    out["key_prefix"] = out.get("key_prefix") or "legacy"
    out.pop("key_hash", None)
    return out


def _last_used(c, key):
    x = c.execute("SELECT MAX(ts) m FROM usage_log WHERE api_key=?", (key,)).fetchone()
    return x["m"] if x and x["m"] else None


def list_api_keys(tenant_id):
    c = _c()
    rows = [_row_api_key(x) for x in c.execute(
        "SELECT * FROM api_keys WHERE tenant_id=? ORDER BY created", (tenant_id,)
    ).fetchall()]
    for r in rows:
        r["last_used"] = _last_used(c, r["key"])
    c.close(); return rows


def get_api_key(key):
    """Resolve a presented consumer secret. The plaintext is never stored or returned."""
    if not key:
        return None
    digest = _api_key_digest(str(key))
    c = _c()
    x = c.execute("SELECT * FROM api_keys WHERE key_hash=?", (digest,)).fetchone()
    c.close()
    return _row_api_key(x)


def get_api_key_by_id(key_id):
    """Resolve an opaque admin-side key id (safe for URLs and audit trails)."""
    c = _c()
    x = c.execute("SELECT * FROM api_keys WHERE key=?", (key_id,)).fetchone()
    c.close()
    return _row_api_key(x)


def update_api_key(key, env=None, rate_limit=None, expiry=None, label=None):
    fields, vals = [], []
    for col, v in (("env", env), ("rate_limit", rate_limit), ("expiry", expiry), ("label", label)):
        if v is not None:
            fields.append(f"{col}=?"); vals.append(v)
    if not fields:
        return
    vals.append(key); c = _c(); c.execute(f"UPDATE api_keys SET {','.join(fields)} WHERE key=?", vals); c.commit(); c.close()
def revoke_api_key(key):
    c = _c(); c.execute("UPDATE api_keys SET active=0 WHERE key=?", (key,)); c.commit(); c.close()
def rotate_api_key(old_key):
    """Issue a new key with the same tenant/role/label/env/limit/expiry and revoke the old one."""
    k = get_api_key_by_id(old_key)
    if not k:
        return None
    new_key = create_api_key(k["tenant_id"], k["role"], k["label"], k.get("env") or "Production", k.get("rate_limit"), k.get("expiry"))
    revoke_api_key(old_key)
    return new_key


# ---- LLM providers (api_key encrypted) ----
def upsert_provider(tenant_id, provider, label, api_key, api_base="", extra=None):
    c = _c()
    row = c.execute("SELECT id, api_key FROM llm_providers WHERE tenant_id=? AND provider=?", (tenant_id, provider)).fetchone()
    if api_key:
        enc = sec.encrypt(api_key)
    elif row:
        enc = row["api_key"]          # keep existing key when a blank value is sent on update
    else:
        enc = ""
    if row:
        c.execute("UPDATE llm_providers SET label=?, api_key=?, api_base=?, extra=? WHERE id=?",
                  (label, enc, api_base, json.dumps(extra or {}), row["id"]))
        pid = row["id"]
    else:
        pid = _id("prov")
        c.execute("INSERT INTO llm_providers VALUES(?,?,?,?,?,?,?,?)",
                  (pid, tenant_id, provider, label, enc, api_base, json.dumps(extra or {}), now()))
    c.commit(); c.close()
    if api_key and row and row["api_key"] and row["api_key"] != enc:
        sec.purge(row["api_key"])                        # key replaced -> drop the old Vault entry
    return pid
def list_providers(tenant_id, reveal=False):
    c = _c(); rows = c.execute("SELECT * FROM llm_providers WHERE tenant_id=?", (tenant_id,)).fetchall(); c.close()
    out = []
    for x in rows:
        d = dict(x); raw = sec.decrypt(d["api_key"]) if d["api_key"] else ""
        d["api_key"] = raw if reveal else sec.mask(raw)
        d["extra"] = json.loads(d["extra"] or "{}")
        out.append(d)
    return out
def get_provider(tenant_id, provider):
    c = _c(); x = c.execute("SELECT * FROM llm_providers WHERE tenant_id=? AND provider=?", (tenant_id, provider)).fetchone(); c.close()
    if not x: return None
    d = dict(x); d["api_key"] = sec.decrypt(d["api_key"]) if d["api_key"] else ""; d["extra"] = json.loads(d["extra"] or "{}"); return d
def delete_provider(tenant_id, provider):
    c = _c()
    row = c.execute("SELECT api_key FROM llm_providers WHERE tenant_id=? AND provider=?", (tenant_id, provider)).fetchone()
    c.execute("DELETE FROM llm_providers WHERE tenant_id=? AND provider=?", (tenant_id, provider))
    c.commit(); c.close()
    if row and row["api_key"]:
        sec.purge(row["api_key"])


# ---- LLM model assignments (per purpose: gatekeeper/answer_simple/answer_complex/embedding) ----
def upsert_model(tenant_id, purpose, provider, model, params=None, monthly_budget_usd=0.0, fallback_model="", enabled=True):
    # Filter here too, not only in ModelIn: this blob reaches the LLM SDK verbatim, and a
    # row written before the API-level allowlist existed must not smuggle `api_base` or
    # `mock_response` back in on the next read.
    from .models import ALLOWED_MODEL_PARAMS
    params = {k: v for k, v in (params or {}).items() if k in ALLOWED_MODEL_PARAMS}
    c = _c()
    row = c.execute("SELECT id FROM llm_models WHERE tenant_id=? AND purpose=?", (tenant_id, purpose)).fetchone()
    if row:
        c.execute("UPDATE llm_models SET provider=?,model=?,params=?,monthly_budget_usd=?,fallback_model=?,enabled=? WHERE id=?",
                  (provider, model, json.dumps(params or {}), monthly_budget_usd, fallback_model, 1 if enabled else 0, row["id"]))
        mid = row["id"]
    else:
        mid = _id("lm")
        c.execute("INSERT INTO llm_models VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (mid, tenant_id, purpose, provider, model, json.dumps(params or {}), monthly_budget_usd, fallback_model, 1 if enabled else 0, now()))
    c.commit(); c.close(); return mid
def list_models(tenant_id):
    c = _c(); rows = c.execute("SELECT * FROM llm_models WHERE tenant_id=?", (tenant_id,)).fetchall(); c.close()
    return [{**dict(x), "params": json.loads(x["params"] or "{}"), "enabled": bool(x["enabled"])} for x in rows]
def get_model(tenant_id, purpose):
    c = _c(); x = c.execute("SELECT * FROM llm_models WHERE tenant_id=? AND purpose=?", (tenant_id, purpose)).fetchone(); c.close()
    return {**dict(x), "params": json.loads(x["params"] or "{}"), "enabled": bool(x["enabled"])} if x else None
def delete_model(tenant_id, purpose):
    c = _c(); c.execute("DELETE FROM llm_models WHERE tenant_id=? AND purpose=?", (tenant_id, purpose)); c.commit(); c.close()


# ---- budgets ----
def set_budget(tenant_id, monthly_usd):
    c = _c(); c.execute("INSERT INTO tenant_budgets VALUES(?,?,?) ON CONFLICT(tenant_id) DO UPDATE SET monthly_usd=excluded.monthly_usd",
                        (tenant_id, monthly_usd, now())); c.commit(); c.close()
def get_budget(tenant_id):
    c = _c(); x = c.execute("SELECT monthly_usd FROM tenant_budgets WHERE tenant_id=?", (tenant_id,)).fetchone(); c.close()
    return x["monthly_usd"] if x else None


class BudgetExceeded(Exception):
    def __init__(self, limit, spent, pending, requested):
        self.limit = float(limit or 0)
        self.spent = float(spent or 0)
        self.pending = float(pending or 0)
        self.requested = float(requested or 0)
        super().__init__(
            f"monthly LLM budget exhausted (limit ${self.limit:.4f}, "
            f"spent ${self.spent:.4f}, pending ${self.pending:.4f})"
        )


def _month_start():
    return datetime.now(timezone.utc).strftime("%Y-%m-01T00:00:00Z")


def reserve_budget(tenant_id, amount=None, ttl_sec=900):
    """Atomically reserve headroom before a chat starts.

    A missing/zero budget means unlimited (backward-compatible). PostgreSQL row locking
    serializes concurrent checks, so ten simultaneous requests cannot each observe the
    same remaining balance and overspend it.
    """
    requested = max(0.000001, float(
        amount if amount is not None else os.getenv("BUDGET_RESERVE_USD_PER_CHAT", "0.25")
    ))
    c = _c()
    try:
        budget = c.execute(
            "SELECT monthly_usd FROM tenant_budgets WHERE tenant_id=? FOR UPDATE",
            (tenant_id,),
        ).fetchone()
        if not budget or float(budget["monthly_usd"] or 0) <= 0:
            c.commit()
            return None
        c.execute("DELETE FROM budget_reservations WHERE expires < ?", (now(),))
        spent_row = c.execute(
            """SELECT COALESCE(SUM(cost),0) cost FROM usage_log
               WHERE tenant_id=? AND ts>=?""",
            (tenant_id, _month_start()),
        ).fetchone()
        pending_row = c.execute(
            """SELECT COALESCE(SUM(amount),0) amount FROM budget_reservations
               WHERE tenant_id=? AND expires>=?""",
            (tenant_id, now()),
        ).fetchone()
        limit = float(budget["monthly_usd"])
        spent = float(spent_row["cost"] or 0)
        pending = float(pending_row["amount"] or 0)
        if spent + pending + requested > limit:
            c._c.rollback()
            raise BudgetExceeded(limit, spent, pending, requested)
        reservation_id = _id("bres")
        expires = ts_after(max(60, int(ttl_sec)))
        c.execute(
            "INSERT INTO budget_reservations(id,tenant_id,amount,created,expires) VALUES(?,?,?,?,?)",
            (reservation_id, tenant_id, requested, now(), expires),
        )
        c.commit()
        return {"id": reservation_id, "amount": requested, "limit": limit,
                "spent": spent, "pending": pending}
    finally:
        c.close()


def release_budget(reservation_id):
    if not reservation_id:
        return
    c = _c()
    c.execute("DELETE FROM budget_reservations WHERE id=?", (reservation_id,))
    c.commit(); c.close()


def budget_reservation_active(reservation_id, tenant_id):
    if not reservation_id:
        return False
    c = _c()
    row = c.execute(
        """SELECT 1 ok FROM budget_reservations
           WHERE id=? AND tenant_id=? AND expires>=?""",
        (reservation_id, tenant_id, now()),
    ).fetchone()
    c.close()
    return bool(row)


# ---- rate limits ----
def set_rate_limit(tenant_id, per_min):
    c = _c(); c.execute("INSERT INTO tenant_limits VALUES(?,?,?) ON CONFLICT(tenant_id) DO UPDATE SET per_min=excluded.per_min",
                        (tenant_id, int(per_min), now())); c.commit(); c.close()
def get_rate_limit(tenant_id):
    c = _c(); x = c.execute("SELECT per_min FROM tenant_limits WHERE tenant_id=?", (tenant_id,)).fetchone(); c.close()
    if x and x["per_min"]:
        return int(x["per_min"])
    return int(os.environ.get("RATE_LIMIT_PER_MIN", "120"))


def consume_rate_limit(tenant_id, api_key, limit, now_ts=None):
    """Atomically consume one token from a Postgres-backed 60-second bucket."""
    import math
    now_ts = float(now_ts if now_ts is not None else time.time())
    limit = max(1, int(limit))
    c = _c()
    c.execute(
        """INSERT INTO rate_limit_buckets(api_key,tenant_id,tokens,updated)
           VALUES(?,?,?,?) ON CONFLICT(api_key) DO NOTHING""",
        (api_key, tenant_id, float(limit), now_ts),
    )
    row = c.execute(
        "SELECT tokens,updated FROM rate_limit_buckets WHERE api_key=? FOR UPDATE",
        (api_key,),
    ).fetchone()
    elapsed = max(0.0, now_ts - float(row["updated"] or now_ts))
    tokens = min(float(limit), float(row["tokens"] or 0.0) + elapsed * limit / 60.0)
    if tokens >= 1.0:
        tokens -= 1.0
        allowed, retry = True, 0
    else:
        allowed = False
        retry = max(1, int(math.ceil((1.0 - tokens) * 60.0 / limit)))
    c.execute(
        """UPDATE rate_limit_buckets
           SET tenant_id=?,tokens=?,updated=? WHERE api_key=?""",
        (tenant_id, tokens, now_ts, api_key),
    )
    c.commit(); c.close()
    return allowed, retry, limit, max(0, int(tokens))


def reset_rate_limit(api_key=None):
    c = _c()
    if api_key:
        c.execute("DELETE FROM rate_limit_buckets WHERE api_key=?", (api_key,))
    else:
        c.execute("DELETE FROM rate_limit_buckets")
    c.commit(); c.close()


def console_login_retry_after(subject_hash, now_ts=None):
    now_ts = float(now_ts if now_ts is not None else time.time())
    c = _c()
    row = c.execute(
        "SELECT locked_until FROM console_login_attempts WHERE subject_hash=?",
        (subject_hash,),
    ).fetchone()
    c.close()
    return max(0, int(float(row["locked_until"] or 0) - now_ts + 0.999)) if row else 0


def note_console_login_attempt(subject_hash, ok, max_failures=5, lock_seconds=60,
                               now_ts=None):
    now_ts = float(now_ts if now_ts is not None else time.time())
    c = _c()
    if ok:
        c.execute("DELETE FROM console_login_attempts WHERE subject_hash=?", (subject_hash,))
        c.commit(); c.close()
        return
    c.execute(
        """INSERT INTO console_login_attempts(subject_hash,failures,locked_until,updated)
           VALUES(?,0,0,?) ON CONFLICT(subject_hash) DO NOTHING""",
        (subject_hash, now_ts),
    )
    row = c.execute(
        """SELECT failures,locked_until,updated FROM console_login_attempts
           WHERE subject_hash=? FOR UPDATE""",
        (subject_hash,),
    ).fetchone()
    failures = int(row["failures"] or 0) + 1
    locked_until = float(row["locked_until"] or 0)
    if now_ts - float(row["updated"] or now_ts) > max(1, int(lock_seconds)):
        failures = 1
    if failures >= max(1, int(max_failures)):
        failures = 0
        locked_until = now_ts + max(1, int(lock_seconds))
    c.execute(
        """UPDATE console_login_attempts
           SET failures=?,locked_until=?,updated=? WHERE subject_hash=?""",
        (failures, locked_until, now_ts, subject_hash),
    )
    c.commit(); c.close()


# ---- documents + chunks (doc-RAG) ----
def _row_document(x):
    """Parse a documents row: acl_roles + per-document config JSON -> dicts."""
    d = {**dict(x), "acl_roles": json.loads(x["acl_roles"] or "[]")}
    try:
        d["config"] = json.loads(d.get("config") or "null")
    except (TypeError, ValueError):
        d["config"] = None
    d["job_status"] = d.get("job_status") or ("ready" if int(d.get("n_chunks") or 0) else "")
    d["job_error"] = d.get("job_error") or ""
    return d
def create_document(tenant_id, datasource_id, filename, mime, acl_roles, n_chunks, config=None):
    did = _id("doc"); c = _c()
    # explicit column list: positional VALUES breaks the moment a migration adds a column
    c.execute("INSERT INTO documents(id,tenant_id,datasource_id,filename,mime,acl_roles,n_chunks,created,config) "
              "VALUES(?,?,?,?,?,?,?,?,?)",
              (did, tenant_id, datasource_id, filename, mime, json.dumps(acl_roles or []), n_chunks, now(),
               json.dumps(config) if config is not None else None))
    c.commit(); c.close(); return did
def list_documents(tenant_id, datasource_id=None):
    c = _c()
    if datasource_id:
        rows = c.execute("SELECT * FROM documents WHERE tenant_id=? AND datasource_id=? ORDER BY created DESC", (tenant_id, datasource_id)).fetchall()
    else:
        rows = c.execute("SELECT * FROM documents WHERE tenant_id=? ORDER BY created DESC", (tenant_id,)).fetchall()
    c.close(); return [_row_document(x) for x in rows]
def get_document(doc_id):
    c = _c(); x = c.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone(); c.close()
    return _row_document(x) if x else None
def set_document_config(document_id, config: dict):
    c = _c(); c.execute("UPDATE documents SET config=? WHERE id=?", (json.dumps(config or {}), document_id)); c.commit(); c.close()
def set_document_job_status(document_id, status: str, error: str = ""):
    c = _c()
    c.execute(
        "UPDATE documents SET job_status=?,job_error=? WHERE id=?",
        (status or "", error or "", document_id),
    )
    c.commit(); c.close()
def replace_document_chunks(tenant_id, document_id, chunks, acl_roles):
    """Swap a document's chunks in place (used when re-embedding after a config edit):
    drop the old chunks + their pgvector mirror rows, then re-add with fresh vectors."""
    c = _c()
    c.execute("DELETE FROM doc_chunks WHERE document_id=? AND tenant_id=?", (document_id, tenant_id))
    c.execute("UPDATE documents SET n_chunks=? WHERE id=?", (len(chunks), document_id))
    c.commit(); c.close()
    vs.delete_documents([document_id])          # clear stale mirror rows (add_chunks re-inserts)
    add_chunks(tenant_id, document_id, chunks, acl_roles)
def save_document_file(tenant_id, doc_id, filename, data: bytes) -> str:
    """Keep the original encrypted (same AES-GCM envelope either way); downloads decrypt
    after authz. FILES_BACKEND=db writes the envelope to Postgres and records the ref
    "db:<doc_id>" in file_path; disk (default) keeps the legacy path behavior."""
    from . import document_crypto
    if _files_backend() == "db":
        ref = _DB_FILE_REF + doc_id
        blob = document_crypto.encrypt(data, tenant_id)
        c = _c()
        c.execute("DELETE FROM document_files WHERE document_id=? AND tenant_id=?",
                  (doc_id, tenant_id))
        c.execute("INSERT INTO document_files VALUES(?,?,?)", (doc_id, tenant_id, blob))
        c.execute("UPDATE documents SET file_path=?, file_size=? WHERE id=? AND tenant_id=?",
                  (ref, len(data), doc_id, tenant_id))
        c.commit(); c.close()
        return ref
    ext = os.path.splitext(filename or "")[1][:16]
    d = os.path.join(FILES_DIR, tenant_id)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{doc_id}{ext}")
    document_crypto.write(path, data, tenant_id)
    c = _c()
    c.execute("UPDATE documents SET file_path=?, file_size=? WHERE id=? AND tenant_id=?",
              (path, len(data), doc_id, tenant_id))
    c.commit(); c.close()
    return path
def save_report_file(tenant_id, job_id, name, mime, data: bytes) -> dict:
    """A generated report file, kept in the same per-tenant AES-GCM envelope as uploaded
    originals, always in Postgres: reports are produced by job workers on any replica and
    downloaded through any other, so the pod filesystem is never an option here."""
    import hashlib
    from . import document_crypto
    digest = hashlib.sha256(data).hexdigest()
    blob = document_crypto.encrypt(data, tenant_id)
    c = _c()
    try:
        c.execute("DELETE FROM report_files WHERE job_id=? AND name=? AND tenant_id=?",
                  (job_id, name, tenant_id))
        c.execute("INSERT INTO report_files(job_id, tenant_id, name, mime, size, sha256, created, data) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  (job_id, tenant_id, name, mime, len(data), digest, now(), blob))
        c.commit()
    finally:
        c.close()
    return {"name": name, "mime": mime, "size": len(data), "sha256": digest}
# ---- analysis cache (brand analysis: the same question gets the same analysis) ----
ANALYSIS_CACHE_KEEP_SEC = 7 * 24 * 3600


def get_analysis_cache(tenant_id, key, max_age_sec) -> dict | None:
    """A cached analysis younger than ``max_age_sec`` ({"stamp", "payload"}), or None.
    Kept in the same per-tenant AES-GCM envelope as uploaded originals: it holds the
    brand's aggregates and product names, the same class of data as a chat answer."""
    from . import document_crypto
    if max_age_sec <= 0:
        return None
    c = _c()
    try:
        row = c.execute("SELECT stamp, data FROM analysis_cache WHERE tenant_id=? AND key=?",
                        (tenant_id, key)).fetchone()
    finally:
        c.close()
    if not row or row["data"] is None or time.time() - float(row["stamp"] or 0) > max_age_sec:
        return None
    try:
        payload = json.loads(document_crypto.decrypt(bytes(row["data"]), tenant_id))
    except Exception:
        return None
    return {"stamp": float(row["stamp"]), "payload": payload}


def put_analysis_cache(tenant_id, key, kind, payload: dict) -> float:
    from . import document_crypto
    stamp = time.time()
    blob = document_crypto.encrypt(json.dumps(payload, default=str).encode("utf-8"), tenant_id)
    c = _c()
    try:
        c.execute("DELETE FROM analysis_cache WHERE tenant_id=? AND (key=? OR stamp<?)",
                  (tenant_id, key, stamp - ANALYSIS_CACHE_KEEP_SEC))
        c.execute("INSERT INTO analysis_cache(tenant_id, key, kind, stamp, data) "
                  "VALUES(?,?,?,?,?)", (tenant_id, key, kind, stamp, blob))
        c.commit()
    finally:
        c.close()
    return stamp


def list_report_files(tenant_id, job_id) -> list:
    c = _c()
    try:
        rows = c.execute("SELECT name, mime, size, sha256, created FROM report_files "
                         "WHERE job_id=? AND tenant_id=? ORDER BY name", (job_id, tenant_id)).fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]
def read_report_file(tenant_id, job_id, name):
    """(mime, bytes) or None."""
    from . import document_crypto
    c = _c()
    try:
        row = c.execute("SELECT mime, data FROM report_files WHERE job_id=? AND name=? AND tenant_id=?",
                        (job_id, name, tenant_id)).fetchone()
    finally:
        c.close()
    if not row or row["data"] is None:
        return None
    return row["mime"], document_crypto.decrypt(bytes(row["data"]), tenant_id)


# ---- report inputs: files a person attaches for their reports, kept a few hours ----
# The raw exports carry buyer data, so they live only for the upload's TTL (a person may
# generate the report again with them meanwhile): in Postgres (any replica takes the chunks,
# any replica runs the job), in the per-tenant AES-GCM envelope, purged once expired.
def create_report_input(tenant_id, app_key_id, actor_id, ttl_sec: float) -> dict:
    import secrets
    stamp = time.time()
    row = {"id": "inp_" + secrets.token_hex(16), "tenant_id": tenant_id,
           "app_key_id": app_key_id or "", "actor_id": actor_id or "", "status": "open",
           "created": stamp, "expires": stamp + ttl_sec, "manifest": None}
    purge_expired_report_inputs(tenant_id)
    c = _c()
    try:
        c.execute("INSERT INTO report_inputs(id, tenant_id, app_key_id, actor_id, status, created, "
                  "expires, manifest) VALUES(?,?,?,?,?,?,?,?)",
                  tuple(row[k] for k in ("id", "tenant_id", "app_key_id", "actor_id", "status",
                                         "created", "expires", "manifest")))
        c.commit()
    finally:
        c.close()
    return row


def get_report_input(tenant_id, input_id) -> dict | None:
    """The input with its parts' sizes; None when unknown or expired."""
    c = _c()
    try:
        row = c.execute("SELECT * FROM report_inputs WHERE id=? AND tenant_id=?",
                        (input_id, tenant_id)).fetchone()
        parts = c.execute("SELECT file_no, chunk_no, name, size FROM report_input_parts "
                          "WHERE input_id=? AND tenant_id=? ORDER BY file_no, chunk_no",
                          (input_id, tenant_id)).fetchall() if row else []
    finally:
        c.close()
    if not row or float(row["expires"] or 0) < time.time():
        return None
    out = dict(row)
    out["manifest"] = json.loads(out["manifest"]) if out.get("manifest") else None
    out["parts"] = [dict(p) for p in parts]
    return out


def add_report_input_part(tenant_id, input_id, file_no: int, chunk_no: int, name: str,
                          data: bytes) -> None:
    from . import document_crypto
    blob = document_crypto.encrypt(data, tenant_id)
    c = _c()
    try:
        c.execute("DELETE FROM report_input_parts WHERE input_id=? AND file_no=? AND chunk_no=? "
                  "AND tenant_id=?", (input_id, file_no, chunk_no, tenant_id))
        c.execute("INSERT INTO report_input_parts(input_id, tenant_id, file_no, chunk_no, name, "
                  "size, data) VALUES(?,?,?,?,?,?,?)",
                  (input_id, tenant_id, file_no, chunk_no, name, len(data), blob))
        c.commit()
    finally:
        c.close()


def read_report_input_chunk(tenant_id, input_id, file_no: int, chunk_no: int) -> bytes:
    from . import document_crypto
    c = _c()
    try:
        row = c.execute("SELECT data FROM report_input_parts WHERE input_id=? AND file_no=? "
                        "AND chunk_no=? AND tenant_id=?",
                        (input_id, file_no, chunk_no, tenant_id)).fetchone()
    finally:
        c.close()
    return document_crypto.decrypt(bytes(row["data"]), tenant_id) if row else b""


def list_live_report_inputs(tenant_id, app_key_id, actor_id) -> list:
    """This caller's unexpired inputs, oldest first: [{id, status, created}]."""
    c = _c()
    try:
        rows = c.execute("SELECT id, status, created FROM report_inputs WHERE tenant_id=? AND "
                         "app_key_id=? AND actor_id=? AND expires>=? ORDER BY created",
                         (tenant_id, app_key_id or "", actor_id or "", time.time())).fetchall()
    finally:
        c.close()
    return [dict(r) for r in rows]


def use_report_input(tenant_id, input_id, keep_until: float) -> None:
    """A report took the input: mark it used and keep it at least until ``keep_until``, so
    a report queued near the end of the TTL still finds its files."""
    c = _c()
    try:
        c.execute("UPDATE report_inputs SET status='used', expires=GREATEST(expires, ?) "
                  "WHERE id=? AND tenant_id=?", (keep_until, input_id, tenant_id))
        c.commit()
    finally:
        c.close()


def read_report_input_files(tenant_id, input_id) -> list:
    """[(name, bytes)] per file in attach order, chunks joined in order."""
    from . import document_crypto
    c = _c()
    try:
        rows = c.execute("SELECT file_no, name, data FROM report_input_parts "
                         "WHERE input_id=? AND tenant_id=? ORDER BY file_no, chunk_no",
                         (input_id, tenant_id)).fetchall()
    finally:
        c.close()
    files: dict = {}
    for r in rows:
        _, chunks = files.setdefault(r["file_no"], (r["name"], []))
        chunks.append(document_crypto.decrypt(bytes(r["data"]), tenant_id))
    return [(name, b"".join(chunks)) for _, (name, chunks) in sorted(files.items())]


def close_report_input(tenant_id, input_id, manifest: dict) -> None:
    c = _c()
    try:
        c.execute("UPDATE report_inputs SET status='closed', manifest=? WHERE id=? AND tenant_id=?",
                  (json.dumps(manifest, default=str), input_id, tenant_id))
        c.commit()
    finally:
        c.close()


def delete_report_input(tenant_id, input_id) -> None:
    c = _c()
    try:
        c.execute("DELETE FROM report_input_parts WHERE input_id=? AND tenant_id=?",
                  (input_id, tenant_id))
        c.execute("DELETE FROM report_inputs WHERE id=? AND tenant_id=?", (input_id, tenant_id))
        c.commit()
    finally:
        c.close()


def purge_expired_report_inputs(tenant_id) -> int:
    """Drop this tenant's inputs past their expiry (never used, or a report that died)."""
    c = _c()
    try:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM report_inputs WHERE tenant_id=? AND expires<?",
            (tenant_id, time.time())).fetchall()]
        for input_id in ids:
            c.execute("DELETE FROM report_input_parts WHERE input_id=? AND tenant_id=?",
                      (input_id, tenant_id))
            c.execute("DELETE FROM report_inputs WHERE id=? AND tenant_id=?", (input_id, tenant_id))
        c.commit()
    finally:
        c.close()
    return len(ids)
def document_storage_bytes(tenant_id):
    c = _c()
    row = c.execute(
        "SELECT COALESCE(SUM(file_size),0) bytes FROM documents WHERE tenant_id=?",
        (tenant_id,),
    ).fetchone()
    c.close()
    return int(row["bytes"] or 0)
def document_file_available(doc) -> bool:
    """Whether this documents row has a readable original, for BOTH ref kinds — a
    "db:" ref checks the document_files row, a path checks the filesystem. Callers
    used to test os.path.exists(file_path) directly, which wrongly reports NO_FILE
    for db-backed originals."""
    ref = (doc or {}).get("file_path")
    if not ref:
        return False
    if ref.startswith(_DB_FILE_REF):
        c = _c()
        row = c.execute(
            "SELECT document_id FROM document_files WHERE document_id=? AND tenant_id=?",
            (doc.get("id"), doc.get("tenant_id")),
        ).fetchone()
        c.close()
        return bool(row)
    return os.path.exists(ref)
def read_document_file(tenant_id, document_id):
    from . import document_crypto
    c = _c()
    row = c.execute(
        "SELECT file_path FROM documents WHERE id=? AND tenant_id=?",
        (document_id, tenant_id),
    ).fetchone()
    ref = row["file_path"] if row else None
    if ref and ref.startswith(_DB_FILE_REF):
        blob = c.execute(
            "SELECT data FROM document_files WHERE document_id=? AND tenant_id=?",
            (document_id, tenant_id),
        ).fetchone()
        c.close()
        if not blob or blob["data"] is None:
            raise FileNotFoundError("document original is not stored")
        return document_crypto.decrypt(bytes(blob["data"]), tenant_id)
    c.close()
    if not ref or not os.path.exists(ref):
        raise FileNotFoundError("document original is not stored")
    with open(ref, "rb") as source:
        return document_crypto.decrypt(source.read(), tenant_id)
def migrate_document_files_encryption():
    """Convert legacy plaintext originals to encrypted envelopes in place."""
    from . import document_crypto
    c = _c()
    rows = c.execute(
        """SELECT id,tenant_id,file_path,file_size FROM documents
           WHERE file_path IS NOT NULL AND file_path<>''"""
    ).fetchall()
    for row in rows:
        path = row["file_path"]
        if not path or not os.path.exists(path):
            continue
        with open(path, "rb") as source:
            data = source.read()
        plaintext = (
            document_crypto.decrypt(data, row["tenant_id"])
            if document_crypto.encrypted(data)
            else data
        )
        if not document_crypto.encrypted(data):
            document_crypto.write(path, plaintext, row["tenant_id"])
        if row["file_size"] is None:
            c.execute(
                "UPDATE documents SET file_size=? WHERE id=? AND tenant_id=?",
                (len(plaintext), row["id"], row["tenant_id"]),
            )
    c.commit()
    c.close()
def _rm_files(paths):
    for p in paths:
        # "db:" refs are rows, not paths — their callers delete document_files by SQL
        if p and not p.startswith(_DB_FILE_REF):
            try:
                os.remove(p)
            except OSError:
                pass
def delete_document(tenant_id, document_id):
    c = _c()
    row = c.execute("SELECT file_path FROM documents WHERE id=? AND tenant_id=?", (document_id, tenant_id)).fetchone()
    c.execute("DELETE FROM doc_chunks WHERE document_id=? AND tenant_id=?", (document_id, tenant_id))
    c.execute("DELETE FROM document_files WHERE document_id=? AND tenant_id=?", (document_id, tenant_id))
    c.execute("DELETE FROM documents WHERE id=? AND tenant_id=?", (document_id, tenant_id)); c.commit(); c.close()
    _rm_files([row["file_path"]] if row else [])
    vs.delete_documents([document_id])   # drop from the pgvector mirror too
def add_chunks(tenant_id, document_id, chunks, acl_roles):
    """chunks: list of (index, text, embedding[list[float]])."""
    c = _c()
    meta = c.execute("SELECT datasource_id, filename FROM documents WHERE id=?", (document_id,)).fetchone()
    ds_id = meta["datasource_id"] if meta else None
    fname = meta["filename"] if meta else None
    rows = []
    for idx, text, emb in chunks:
        cid = _id("ch")
        c.execute("INSERT INTO doc_chunks VALUES(?,?,?,?,?,?,?)",
                  (cid, tenant_id, document_id, idx, text, json.dumps(emb), json.dumps(acl_roles or [])))
        rows.append({"id": cid, "tenant_id": tenant_id, "document_id": document_id, "datasource_id": ds_id,
                     "chunk_index": idx, "text": text, "embedding": emb,
                     "acl_roles": acl_roles or [], "filename": fname, "archived": False})
    c.commit(); c.close()
    vs.upsert_chunks(rows)   # mirror into pgvector (no-op in sqlite mode)
def update_chunk_embeddings(pairs):
    """pairs: list of (chunk_id, embedding) — used by reindex after an embedder switch."""
    c = _c()
    c.executemany("UPDATE doc_chunks SET embedding=? WHERE id=?",
                  [(json.dumps(v), cid) for cid, v in pairs])
    c.commit(); c.close()
    vs.update_embeddings(pairs)   # keep the pgvector mirror in step
def set_document_embedder(document_id, name):
    c = _c(); c.execute("UPDATE documents SET embedder=? WHERE id=?", (name, document_id)); c.commit(); c.close()
def set_document_archived(document_id, archived: bool):
    """Archived = stale version: chunks + original file stay (recoverable, downloadable)
    but retrieval never sees it — the safe answer to 'admin uploaded v2 without deleting v1'."""
    c = _c(); c.execute("UPDATE documents SET archived=? WHERE id=?", (1 if archived else 0, document_id)); c.commit(); c.close()
    vs.set_archived(document_id, archived)   # mirror the archived flag into pgvector
def set_all_documents_embedder(tenant_id, name):
    c = _c(); c.execute("UPDATE documents SET embedder=? WHERE tenant_id=?", (name, tenant_id)); c.commit(); c.close()
def iter_chunks(tenant_id):
    c = _c()
    # A disabled datasource must stop feeding ANSWERS too, not just SQL: its documents are
    # excluded here the same way an archived version is. LEFT JOIN so a document with no
    # datasource stays retrievable.
    rows = c.execute("""SELECT dc.id, dc.document_id, dc.chunk_index, dc.text, dc.embedding, dc.acl_roles, d.filename
                        FROM doc_chunks dc JOIN documents d ON d.id=dc.document_id
                        LEFT JOIN datasources ds ON ds.id=d.datasource_id
                        WHERE dc.tenant_id=? AND COALESCE(d.archived,0)=0
                          AND COALESCE(ds.enabled,1)=1""", (tenant_id,)).fetchall()
    c.close()
    return [{"id": x["id"], "document_id": x["document_id"], "chunk_index": x["chunk_index"], "text": x["text"],
             "embedding": json.loads(x["embedding"]), "acl_roles": json.loads(x["acl_roles"] or "[]"),
             "filename": x["filename"]} for x in rows]
def iter_chunks_full(tenant_id):
    """Every chunk for the tenant INCLUDING archived ones, with datasource_id +
    filename + archived — the full row shape the pgvector mirror needs (rebuild)."""
    c = _c()
    rows = c.execute("""SELECT dc.id, dc.tenant_id, dc.document_id, dc.chunk_index, dc.text, dc.embedding,
                               dc.acl_roles, d.datasource_id, d.filename, COALESCE(d.archived,0) AS archived
                        FROM doc_chunks dc JOIN documents d ON d.id=dc.document_id
                        WHERE dc.tenant_id=?""", (tenant_id,)).fetchall()
    c.close()
    return [{"id": x["id"], "tenant_id": x["tenant_id"], "document_id": x["document_id"],
             "datasource_id": x["datasource_id"], "chunk_index": x["chunk_index"], "text": x["text"],
             "embedding": json.loads(x["embedding"]), "acl_roles": json.loads(x["acl_roles"] or "[]"),
             "filename": x["filename"], "archived": bool(x["archived"])} for x in rows]


# ---- memories (per tenant,user) ----
def add_memory(tenant_id, user_id, kind, text, embedding, source="admin"):
    # named columns: positional INSERT breaks the moment a migration adds a column (see create_document)
    mid = _id("mem"); c = _c()
    c.execute("INSERT INTO memories(id,tenant_id,user_id,kind,text,embedding,created,source) VALUES(?,?,?,?,?,?,?,?)",
              (mid, tenant_id, user_id, kind, text, json.dumps(embedding), now(), source))
    c.commit(); c.close(); return mid
def list_memories(tenant_id, user_id=None):
    c = _c()
    if user_id:
        rows = c.execute("SELECT * FROM memories WHERE tenant_id=? AND user_id=? ORDER BY created DESC", (tenant_id, user_id)).fetchall()
    else:
        rows = c.execute("SELECT * FROM memories WHERE tenant_id=? ORDER BY created DESC", (tenant_id,)).fetchall()
    c.close()
    return [{"id": x["id"], "tenant_id": x["tenant_id"], "user_id": x["user_id"], "kind": x["kind"],
             "text": x["text"], "embedding": json.loads(x["embedding"]), "created": x["created"],
             "source": x["source"] or "admin"} for x in rows]
def delete_memory(tenant_id, mem_id):
    c = _c(); c.execute("DELETE FROM memories WHERE tenant_id=? AND id=?", (tenant_id, mem_id)); c.commit(); c.close()
def delete_memories(tenant_id, user_id):
    """Bulk 'forget this user' (GDPR-style). Returns how many rows were removed."""
    c = _c(); cur = c.execute("DELETE FROM memories WHERE tenant_id=? AND user_id=?", (tenant_id, user_id))
    n = cur.rowcount; c.commit(); c.close(); return n


# ---- usage ----
# A "request" is something a caller asked for. The retired Jev integration wrote its own
# usage rows (action systemone_*), up to five per chat turn; they stay in the history for
# their tokens and cost but were never requests, so the count keeps leaving them out.
_REQUESTS = "COUNT(*) FILTER (WHERE substr(COALESCE(action,''),1,10) <> 'systemone_')"
def log_usage(tenant_id, api_key, role, action, rows, status,
              model=None, prompt_tokens=0, completion_tokens=0, cost=0.0,
              user_id="", trace_id=""):
    # Defense in depth: callers should pass the opaque internal key id returned by
    # require_api_key. If a plaintext secret is accidentally passed, resolve it to the
    # id (or irreversibly redact an unknown one) before writing the audit/metering row.
    key_ref = api_key
    if str(api_key or "").startswith("sk_"):
        found = get_api_key(str(api_key))
        key_ref = found["key"] if found else (
            "unknown_" + hashlib.sha256(str(api_key).encode()).hexdigest()[:16]
        )
    c = _c()
    c.execute("""INSERT INTO usage_log(
                     tenant_id,api_key,role,action,rows,status,ts,model,
                     prompt_tokens,completion_tokens,cost,user_id,trace_id
                 ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (tenant_id, key_ref, role, action, rows, status, now(), model,
               prompt_tokens, completion_tokens, cost,
               str(user_id or "")[:200], str(trace_id or "")[:128]))
    c.commit(); c.close()
def log_audit(tenant_id, trace_id, event, role="", user_id="", model="", status="", code="",
              cost=0.0, detail=None, session_id=None):
    """Append one governed-query audit record (per-step trail: question, each SQL, answer).
    ``session_id`` defaults to the running chat turn's (context.chat_session_var).
    Fail-safe: auditing must NEVER break a query, so all errors are swallowed."""
    import json as _json
    from .context import get_chat_session
    try:
        session = session_id if session_id is not None else get_chat_session()
        c = _c()
        c.execute("""INSERT INTO audit_log(tenant_id,trace_id,ts,role,user_id,event,model,status,
                                           code,cost,detail,session_id)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (tenant_id, trace_id, now(), role, user_id, event, model, status, code, cost,
                   _json.dumps(detail, default=str) if detail is not None else None,
                   session or None))
        c.commit(); c.close()
    except Exception:
        pass


_audit_flush_lock = threading.Lock()


def _admin_audit_chain_key():
    return (
        os.getenv("ADMIN_AUDIT_CHAIN_KEY")
        or os.getenv("ADMIN_SECRET_KEY")
        or "dev-only-admin-audit-chain"
    ).encode()


def log_admin_audit(request_id, principal, method, path, status, tenant_id="",
                    source_ip="", user_agent=""):
    """Append a tamper-evident control-plane mutation event and queue off-host delivery.

    No request body is stored, so passwords, API keys, and provider credentials cannot
    leak into the audit stream. Rows are never updated/deleted by application code.
    """
    event_id = _id("aa")
    ts = now()
    principal = principal or {}
    event = {
        "event_id": event_id,
        "ts": ts,
        "request_id": str(request_id or "")[:128],
        "actor_user_id": str(principal.get("user_id") or "")[:200],
        "actor_email": str(principal.get("email") or "")[:320],
        "actor_role": str(principal.get("role") or "")[:80],
        "auth_via": str(principal.get("via") or "anonymous")[:40],
        "method": str(method or "")[:12],
        "path": str(path or "")[:500],
        "tenant_id": str(tenant_id or "")[:200],
        "status": int(status or 0),
        "source_ip": str(source_ip or "")[:100],
        "user_agent_hash": hashlib.sha256(str(user_agent or "").encode()).hexdigest(),
    }
    c = _c()
    try:
        c.execute("SELECT pg_advisory_xact_lock(hashtext('poros_admin_audit_chain'))")
        previous = c.execute(
            "SELECT event_hash FROM admin_audit_log ORDER BY id DESC LIMIT 1 FOR UPDATE"
        ).fetchone()
        previous_hash = previous["event_hash"] if previous else ""
        canonical = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        event_hash = hmac.new(
            _admin_audit_chain_key(),
            (previous_hash + "\n" + canonical).encode(),
            hashlib.sha256,
        ).hexdigest()
        c.execute(
            """INSERT INTO admin_audit_log(
                   event_id,ts,request_id,actor_user_id,actor_email,actor_role,auth_via,
                   method,path,tenant_id,status,source_ip,user_agent_hash,previous_hash,event_hash
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event_id, ts, event["request_id"], event["actor_user_id"],
                event["actor_email"], event["actor_role"], event["auth_via"],
                event["method"], event["path"], event["tenant_id"], event["status"],
                event["source_ip"], event["user_agent_hash"], previous_hash, event_hash,
            ),
        )
        if os.getenv("ADMIN_AUDIT_SINK_URL"):
            payload = {**event, "previous_hash": previous_hash, "event_hash": event_hash}
            c.execute(
                """INSERT INTO admin_audit_outbox(event_id,payload,attempts,delivered,updated)
                   VALUES(?,?,0,0,?)""",
                (event_id, json.dumps(payload, sort_keys=True), ts),
            )
        c.commit()
    finally:
        c.close()
    if os.getenv("ADMIN_AUDIT_SINK_URL"):
        threading.Thread(
            target=flush_admin_audit_outbox,
            daemon=True,
            name="admin-audit-outbox",
        ).start()
    return event_id


def flush_admin_audit_outbox(limit=20):
    """Best-effort signed JSON delivery. Failed rows remain for the next mutation/retry."""
    sink = (os.getenv("ADMIN_AUDIT_SINK_URL") or "").strip()
    if not sink or not _audit_flush_lock.acquire(blocking=False):
        return
    try:
        c = _c()
        rows = c.execute(
            """SELECT event_id,payload,attempts FROM admin_audit_outbox
               WHERE delivered=0 ORDER BY updated LIMIT ?""",
            (max(1, min(int(limit), 100)),),
        ).fetchall()
        c.close()
        signing_key = (
            os.getenv("ADMIN_AUDIT_SINK_HMAC_KEY")
            or os.getenv("ADMIN_AUDIT_CHAIN_KEY")
            or ""
        ).encode()
        for row in rows:
            body = row["payload"].encode()
            signature = hmac.new(signing_key, body, hashlib.sha256).hexdigest()
            req = urllib.request.Request(
                sink,
                data=body,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "X-Poros-Signature": "sha256=" + signature,
                },
            )
            delivered = 0
            try:
                with urllib.request.urlopen(
                    req, timeout=float(os.getenv("ADMIN_AUDIT_SINK_TIMEOUT_SEC", "5"))
                ) as response:
                    delivered = 1 if 200 <= response.status < 300 else 0
            except Exception as exc:
                _log.warning(
                    "admin_audit_delivery_failed event_id=%s attempt=%s error=%s",
                    row["event_id"], int(row["attempts"] or 0) + 1,
                    type(exc).__name__,
                )
                delivered = 0
            c = _c()
            c.execute(
                """UPDATE admin_audit_outbox SET attempts=?,delivered=?,updated=?
                   WHERE event_id=?""",
                (int(row["attempts"] or 0) + 1, delivered, now(), row["event_id"]),
            )
            c.commit(); c.close()
    finally:
        _audit_flush_lock.release()


def admin_audit_delivery_status():
    c = _c()
    row = c.execute(
        """SELECT COUNT(*) FILTER (WHERE delivered=0) pending,
                  COALESCE(MAX(attempts) FILTER (WHERE delivered=0),0) max_attempts,
                  MIN(updated) FILTER (WHERE delivered=0) oldest_pending,
                  COUNT(*) FILTER (WHERE delivered=1) delivered
           FROM admin_audit_outbox"""
    ).fetchone()
    c.close()
    return {
        "configured": bool((os.getenv("ADMIN_AUDIT_SINK_URL") or "").strip()),
        "pending": int(row["pending"] or 0),
        "delivered": int(row["delivered"] or 0),
        "max_attempts": int(row["max_attempts"] or 0),
        "oldest_pending": row["oldest_pending"],
    }


def list_admin_audit(limit=200):
    c = _c()
    rows = c.execute(
        """SELECT event_id,ts,request_id,actor_user_id,actor_email,actor_role,auth_via,
                  method,path,tenant_id,status,source_ip,user_agent_hash,previous_hash,event_hash
           FROM admin_audit_log ORDER BY id DESC LIMIT ?""",
        (max(1, min(int(limit), 1000)),),
    ).fetchall()
    c.close()
    return [dict(row) for row in rows]


def audit_session_of(tenant_id, trace_id) -> str:
    """The app session id a chat trace was asked in ("" = none recorded)."""
    c = _c()
    row = c.execute("""SELECT session_id FROM audit_log WHERE tenant_id=? AND trace_id=?
                         AND event='question' ORDER BY id LIMIT 1""",
                    (tenant_id, trace_id)).fetchone()
    c.close()
    return (row or {}).get("session_id") or ""


# what a reviewer reads of a turn's answer event (the rest stays in the raw events)
_TURN_FIELDS = ("plan", "confidence", "missing_requirements", "unverified_figures",
                "proposed_figures", "analysis_mode", "datasources_used", "assumptions")


def _decoded(row) -> dict:
    d = dict(row)
    try:
        d["detail"] = json.loads(d["detail"]) if d.get("detail") else None
    except Exception:
        pass
    return d


def _turn_event(turn, ev):
    """Fold one audit event into the turn read so far."""
    detail = ev.get("detail") if isinstance(ev.get("detail"), dict) else {}
    turn["cost"] += float(ev.get("cost") or 0)
    if ev["event"] == "question" and not turn["question"]:
        turn["question"] = str(detail.get("text") or "")
    elif ev["event"] == "answer":
        turn.update(answer=str(detail.get("answer") or ""), status=ev.get("status") or "",
                    model=ev.get("model") or "", answered=ev["ts"])
        turn.update({k: detail.get(k) for k in _TURN_FIELDS if detail.get(k) is not None})
    elif ev["event"] == "blocked":
        turn.update(status="blocked", answer="Blocked by the gatekeeper: "
                    + ", ".join(str(r) for r in detail.get("reasons") or []))
    elif ev["event"] == "cancelled" and not turn["answer"]:
        turn["status"] = "cancelled"


def _turn(trace_id, events, feedback) -> dict:
    """One question and its answer, read from the trace's audit events."""
    turn = {"trace_id": trace_id, "ts": events[0]["ts"], "question": "", "answer": "",
            "status": "", "model": "", "cost": 0.0, "events": events,
            "feedback": feedback}
    for ev in events:
        _turn_event(turn, ev)
    turn["cost"] = round(turn["cost"], 6)
    turn["queries"] = sum(1 for ev in events if ev["event"] == "query")
    turn["failed_queries"] = sum(1 for ev in events
                                 if ev["event"] == "query" and ev.get("status") == "error")
    return turn


_SESSION_KEY = "COALESCE(NULLIF(session_id,''), trace_id)"


def _conversation_keys(c, tenant_id, limit, user_id) -> list:
    """(session key, user) of the newest sessions."""
    by_user = " AND user_id=?" if user_id else ""
    params = (tenant_id, *([user_id] if user_id else []), limit)
    return c.execute(f"""SELECT {_SESSION_KEY} skey, COALESCE(user_id,'') uid, MAX(id) last_id
                         FROM audit_log WHERE tenant_id=? AND event='question'
                           AND COALESCE(trace_id,'')<>''{by_user}
                         GROUP BY 1, 2 ORDER BY last_id DESC LIMIT ?""", params).fetchall()


def _conversation_events(c, tenant_id, keys) -> list:
    """Every audit event of those sessions, oldest first, details decoded."""
    wanted = {(k["skey"], k["uid"]) for k in keys}
    skeys = sorted({k["skey"] for k in keys})
    rows = c.execute(f"""SELECT id,trace_id,session_id,ts,role,user_id,event,model,status,
                                code,cost,detail, {_SESSION_KEY} skey
                         FROM audit_log WHERE tenant_id=?
                           AND {_SESSION_KEY} IN ({",".join("?" * len(skeys))})
                         ORDER BY id""", (tenant_id, *skeys)).fetchall()
    return [_decoded(r) for r in rows if (r["skey"], r.get("user_id") or "") in wanted]


def _conversation_feedback(c, tenant_id, rows) -> dict:
    """trace id -> the feedback given on it."""
    traces = sorted({r["trace_id"] for r in rows})
    if not traces:
        return {}
    fb_rows = c.execute(f"""SELECT trace_id,user_id,rating,reason,comment,updated
                            FROM feedback WHERE tenant_id=?
                              AND trace_id IN ({",".join("?" * len(traces))})""",
                        (tenant_id, *traces)).fetchall()
    feedback = {}
    for fb in fb_rows:
        feedback.setdefault(fb["trace_id"], []).append(dict(fb))
    return feedback


def _group_sessions(rows) -> dict:
    """(session key, user) -> the session with its events grouped per trace."""
    sessions = {}
    for r in rows:
        uid = r.get("user_id") or ""
        s = sessions.setdefault((r["skey"], uid), {
            "key": r["skey"], "session_id": r.get("session_id") or "",
            "user_id": uid, "roles": [], "traces": {}})
        if r.get("session_id"):
            s["session_id"] = r["session_id"]
        if r.get("role") and r["role"] not in s["roles"]:
            s["roles"].append(r["role"])
        r.pop("skey", None)
        r.pop("id", None)
        s["traces"].setdefault(r["trace_id"], []).append(r)
    return sessions


def _conversation(s, feedback):
    """One session as the reviewer reads it (None = no session or no question in it)."""
    if not s:
        return None
    turns = [_turn(t, evs, feedback.get(t, [])) for t, evs in s.pop("traces").items()
             if any(e["event"] == "question" for e in evs)]
    if not turns:
        return None
    ratings = [f["rating"] for t in turns for f in t["feedback"]]
    return {**s, "turns": turns, "started": turns[0]["ts"],
            "latest": max(e["ts"] for t in turns for e in t["events"]),
            "cost": round(sum(t["cost"] for t in turns), 6),
            "feedback": {"helpful": ratings.count("helpful"),
                         "unhelpful": ratings.count("unhelpful")}}


def list_conversations(tenant_id, limit=40, user_id=""):
    """Chat sessions, newest first, as a reviewer reads them: each session's turns in
    order (question, answer, status, feedback) with every audit event of the turn kept
    for the detail view. A turn whose app sent no session id is its own session."""
    limit = max(1, min(int(limit or 40), 200))
    c = _c()
    try:
        keys = _conversation_keys(c, tenant_id, limit, user_id)
        if not keys:
            return []
        rows = _conversation_events(c, tenant_id, keys)
        feedback = _conversation_feedback(c, tenant_id, rows)
    finally:
        c.close()
    sessions = _group_sessions(rows)
    out = []
    for k in keys:
        conversation = _conversation(sessions.get((k["skey"], k["uid"])), feedback)
        if conversation:
            out.append(conversation)
    return out


def list_audit(tenant_id, limit=100):
    import json as _json
    c = _c()
    rows = c.execute("""SELECT trace_id,session_id,ts,role,user_id,event,model,status,code,cost,detail
                        FROM audit_log WHERE tenant_id=? ORDER BY id DESC LIMIT ?""", (tenant_id, limit)).fetchall()
    c.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["detail"] = _json.loads(d["detail"]) if d.get("detail") else None
        except Exception:
            pass
        out.append(d)
    return out


def list_audit_events(tenant_id, event, limit=500):
    """Latest audit rows of ONE event kind for a tenant, newest first, detail decoded."""
    import json as _json
    c = _c()
    rows = c.execute("""SELECT trace_id,ts,role,user_id,event,model,status,code,cost,detail
                        FROM audit_log WHERE tenant_id=? AND event=? ORDER BY id DESC LIMIT ?""",
                     (tenant_id, event, limit)).fetchall()
    c.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["detail"] = _json.loads(d["detail"]) if d.get("detail") else None
        except Exception:
            pass
        out.append(d)
    return out


def audit_question_texts(tenant_id, trace_ids):
    """{trace_id: question text} from the 'question' audit events of these traces."""
    import json as _json
    ids = [t for t in dict.fromkeys(trace_ids or []) if t]
    if not ids:
        return {}
    c = _c()
    rows = c.execute(f"""SELECT trace_id,detail FROM audit_log
                         WHERE tenant_id=? AND event='question'
                           AND trace_id IN ({",".join("?" * len(ids))}) ORDER BY id""",
                     (tenant_id, *ids)).fetchall()
    c.close()
    out = {}
    for row in rows:
        try:
            detail = _json.loads(row["detail"]) if row.get("detail") else {}
        except Exception:
            detail = {}
        out.setdefault(row["trace_id"], str(detail.get("text") or ""))
    return out


# ---- end-user answer feedback ----
def get_audit_exchange(tenant_id, trace_id):
    """Trusted question/answer snapshot for one chat trace in this tenant."""
    import json as _json
    c = _c()
    rows = c.execute("""SELECT event,detail FROM audit_log
                        WHERE tenant_id=? AND trace_id=? AND event IN ('question','answer','blocked')
                        ORDER BY id""", (tenant_id, trace_id)).fetchall()
    c.close()
    question, answer, blocked = "", "", False
    for row in rows:
        try:
            detail = _json.loads(row["detail"]) if row.get("detail") else {}
        except Exception:
            detail = {}
        if row["event"] == "question" and not question:
            question = str(detail.get("text") or "")
        elif row["event"] == "answer":
            answer = str(detail.get("answer") or "")
        elif row["event"] == "blocked":
            blocked = True
    if blocked and not answer:
        answer = "This request was blocked by the gatekeeper (possible prompt-injection or unsafe content)."
    return {"question": question, "answer": answer} if question and answer else None


def upsert_feedback(tenant_id, trace_id, user_id, role, rating, reason="", comment=""):
    """Create or update one user's feedback for one answer trace."""
    target = get_audit_exchange(tenant_id, trace_id)
    if not target:
        return None
    fid, ts = _id("fb"), now()
    c = _c()
    c.execute("""INSERT INTO feedback(
                    id,tenant_id,trace_id,user_id,role,rating,reason,comment,question,answer,created,updated
                 ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(tenant_id,trace_id,user_id) DO UPDATE SET
                    role=excluded.role, rating=excluded.rating, reason=excluded.reason,
                    comment=excluded.comment, question=excluded.question,
                    answer=excluded.answer, updated=excluded.updated""",
              (fid, tenant_id, trace_id, user_id, role, rating, reason, comment,
               target["question"], target["answer"], ts, ts))
    row = c.execute("""SELECT id,tenant_id,trace_id,user_id,role,rating,reason,comment,
                              question,answer,created,updated
                       FROM feedback WHERE tenant_id=? AND trace_id=? AND user_id=?""",
                    (tenant_id, trace_id, user_id)).fetchone()
    c.commit(); c.close()
    return dict(row)


def list_feedback(tenant_id, limit=200):
    c = _c()
    rows = c.execute("""SELECT id,tenant_id,trace_id,user_id,role,rating,reason,comment,
                              question,answer,created,updated
                       FROM feedback WHERE tenant_id=? ORDER BY updated DESC LIMIT ?""",
                    (tenant_id, limit)).fetchall()
    c.close()
    return [dict(row) for row in rows]


def usage_summary(tenant_id):
    c = _c()
    total = c.execute(f"""SELECT {_REQUESTS} n, COALESCE(SUM("rows"),0) r, COALESCE(SUM(prompt_tokens),0) pt,
                         COALESCE(SUM(completion_tokens),0) ct, COALESCE(SUM(cost),0) cost FROM usage_log WHERE tenant_id=?""", (tenant_id,)).fetchone()
    by_status = [dict(x) for x in c.execute(f"SELECT status, {_REQUESTS} n FROM usage_log WHERE tenant_id=? GROUP BY status", (tenant_id,)).fetchall()]
    by_model = [dict(x) for x in c.execute("""SELECT COALESCE(model,'(none)') model, COUNT(*) n,
                         COALESCE(SUM(prompt_tokens+completion_tokens),0) tokens, COALESCE(SUM(cost),0) cost
                         FROM usage_log WHERE tenant_id=? GROUP BY model ORDER BY cost DESC""", (tenant_id,)).fetchall()]
    by_day = [dict(x) for x in c.execute(f"""SELECT substr(ts,1,10) AS "day", {_REQUESTS} requests,
                         COALESCE(SUM(prompt_tokens+completion_tokens),0) tokens, COALESCE(SUM(cost),0) cost
                         FROM usage_log WHERE tenant_id=? GROUP BY substr(ts,1,10) ORDER BY "day" DESC LIMIT 14""", (tenant_id,)).fetchall()]
    recent = [dict(x) for x in c.execute("""SELECT role,action,"rows",status,ts,model,prompt_tokens,completion_tokens,cost
                         FROM usage_log WHERE tenant_id=? ORDER BY id DESC LIMIT 20""", (tenant_id,)).fetchall()]
    month = c.execute(
        """SELECT COALESCE(SUM(cost),0) cost FROM usage_log
           WHERE tenant_id=? AND ts>=?""",
        (tenant_id, _month_start()),
    ).fetchone()
    c.close()
    budget = get_budget(tenant_id)
    return {"requests": total["n"], "rows_returned": total["r"],
            "prompt_tokens": total["pt"], "completion_tokens": total["ct"],
            "total_tokens": total["pt"] + total["ct"], "cost": round(total["cost"], 6),
            "budget_usd": budget, "budget_spent_usd": round(float(month["cost"] or 0), 6),
            "by_status": by_status, "by_model": by_model,
            "by_day": list(reversed(by_day)), "recent": recent}


def _display_model(tid):
    """Model name to show on tenant cards: the Model-tab Primary (design 'models' config,
    same source the gateway uses) first, then any configured design model, then the legacy
    per-purpose model, else '—'."""
    cfg = get_tenant_config(tid, "models") or {}
    dm = cfg.get("models", []) if isinstance(cfg, dict) else []
    pick = next((m for m in dm if m.get("role") == "Primary" and m.get("name")), None) \
        or next((m for m in dm if m.get("name")), None)
    if pick:
        return pick["name"]
    pm = get_model(tid, "answer_complex") or get_model(tid, "answer_simple") or get_model(tid, "gatekeeper")
    return pm["model"] if pm else "—"


def _monitoring_range_start(range_key: str):
    range_key = str(range_key or "all").strip().lower()
    days = {"7d": 7, "30d": 30}.get(range_key)
    if not days:
        return None
    return ts_after(-days * 86400)


def monitoring_summary(owner_id=None, range_key="all"):
    """Cross-tenant rollup for the Monitoring dashboard.

    With ``owner_id``, everything is restricted to tenants visible to that console
    user: directly-owned tenants plus tenant-group assignments. End-user token usage
    comes from one final ``answer`` audit row per trace, preserving attribution from
    before usage_log gained its user_id column.
    """
    c = _c()
    if owner_id is None:
        tenants = [_row_tenant(x) for x in c.execute("SELECT * FROM tenants ORDER BY created").fetchall()]
    else:
        tenants = list_tenants(owner_id=owner_id)
    ids = tuple(t["id"] for t in tenants)
    if owner_id is not None and not ids:
        c.close()
        return {"total_tenants": 0, "active_tenants": 0, "total_tokens": 0, "total_requests": 0,
                "total_cost": 0.0, "by_model": [], "per_tenant": [], "per_user": [],
                "by_day": [], "range": range_key}
    conditions, params = [], []
    if owner_id is not None:
        conditions.append(f"tenant_id IN ({','.join('?' * len(ids))})")
        params.extend(ids)
    start = _monitoring_range_start(range_key)
    if start:
        conditions.append("ts>=?")
        params.append(start)
    flt = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    prm = tuple(params)
    tot = c.execute(f"SELECT {_REQUESTS} reqs, COALESCE(SUM(prompt_tokens+completion_tokens),0) toks, COALESCE(SUM(cost),0) cost FROM usage_log" + flt, prm).fetchone()
    by_model = [dict(x) for x in c.execute(f"""SELECT COALESCE(model,'(rule-based)') model, COUNT(*) n,
        COALESCE(SUM(prompt_tokens+completion_tokens),0) tokens, COALESCE(SUM(cost),0) cost
        FROM usage_log{flt} GROUP BY model ORDER BY tokens DESC""", prm).fetchall()]
    per_rows = {x["tenant_id"]: dict(x) for x in c.execute(f"""SELECT tenant_id, {_REQUESTS} requests,
        COALESCE(SUM(prompt_tokens+completion_tokens),0) tokens, COALESCE(SUM(cost),0) cost
        FROM usage_log{flt} GROUP BY tenant_id""", prm).fetchall()}
    by_day = [dict(x) for x in c.execute(f"""SELECT substr(ts,1,10) AS "day", {_REQUESTS} requests,
        COALESCE(SUM(prompt_tokens+completion_tokens),0) tokens FROM usage_log{flt}
        GROUP BY substr(ts,1,10) ORDER BY "day" DESC LIMIT ?""",
        (*prm, 30 if range_key == "30d" else 14)).fetchall()]

    user_conditions = ["COALESCE(user_id,'')<>''"]
    user_params = []
    if owner_id is not None:
        user_conditions.append(f"tenant_id IN ({','.join('?' * len(ids))})")
        user_params.extend(ids)
    if start:
        user_conditions.append("ts>=?")
        user_params.append(start)
    user_where = " WHERE " + " AND ".join(user_conditions)
    current_user_rows = [dict(x) for x in c.execute(
        f"""SELECT tenant_id, user_id, {_REQUESTS} requests,
                   COALESCE(SUM(prompt_tokens+completion_tokens),0) tokens,
                   COALESCE(SUM(cost),0) cost,
                   STRING_AGG(DISTINCT NULLIF(role,''), ', ') roles
            FROM usage_log{user_where}
            GROUP BY tenant_id, user_id""",
        tuple(user_params),
    ).fetchall()]

    # Older usage_log rows predate user_id/trace_id. Reconstruct only those chat
    # answers from audit_log. The NOT EXISTS prevents a current row from being
    # counted twice once direct user attribution is available.
    audit_conditions = ["a.event='answer'", "COALESCE(a.user_id,'')<>''",
                        """NOT EXISTS (
                              SELECT 1 FROM usage_log u
                              WHERE u.tenant_id=a.tenant_id
                                AND u.trace_id=a.trace_id
                                AND COALESCE(u.user_id,'')<>''
                           )"""]
    audit_params = []
    if owner_id is not None:
        audit_conditions.append(f"a.tenant_id IN ({','.join('?' * len(ids))})")
        audit_params.extend(ids)
    if start:
        audit_conditions.append("a.ts>=?")
        audit_params.append(start)
    legacy_user_rows = [dict(x) for x in c.execute(
        f"""SELECT a.tenant_id, a.user_id, COUNT(*) requests,
                   COALESCE(SUM(
                     COALESCE((a.detail::jsonb->>'prompt_tokens')::BIGINT,0)
                     + COALESCE((a.detail::jsonb->>'completion_tokens')::BIGINT,0)
                   ),0) tokens,
                   COALESCE(SUM(a.cost),0) cost,
                   STRING_AGG(DISTINCT NULLIF(a.role,''), ', ') roles
            FROM audit_log a
            WHERE {' AND '.join(audit_conditions)}
            GROUP BY a.tenant_id, a.user_id""",
        tuple(audit_params),
    ).fetchall()]
    c.close()
    per_tenant = []
    for t in tenants:
        u = per_rows.get(t["id"], {})
        per_tenant.append({"id": t["id"], "name": t["name"], "status": t["status"], "icon": t["icon"],
                           "model": _display_model(t["id"]),
                           "tokens": u.get("tokens", 0), "requests": u.get("requests", 0), "cost": round(u.get("cost", 0.0), 6)})
    per_tenant.sort(key=lambda x: x["tokens"], reverse=True)
    tenant_names = {t["id"]: t["name"] for t in tenants}
    combined_users = {}
    for row in current_user_rows + legacy_user_rows:
        key = (row["tenant_id"], row["user_id"])
        dst = combined_users.setdefault(key, {
            "tenant_id": row["tenant_id"],
            "tenant_name": tenant_names.get(row["tenant_id"], row["tenant_id"]),
            "user_id": row["user_id"],
            "roles": set(),
            "tokens": 0,
            "requests": 0,
            "cost": 0.0,
        })
        dst["tokens"] += int(row.get("tokens") or 0)
        dst["requests"] += int(row.get("requests") or 0)
        dst["cost"] += float(row.get("cost") or 0)
        dst["roles"].update(x.strip() for x in str(row.get("roles") or "").split(",") if x.strip())
    per_user = []
    for row in combined_users.values():
        row["roles"] = ", ".join(sorted(row["roles"])) or "role tidak tercatat"
        row["cost"] = round(row["cost"], 6)
        per_user.append(row)
    per_user.sort(key=lambda row: (row["tokens"], row["requests"]), reverse=True)
    return {"total_tenants": len(tenants),
            "active_tenants": sum(1 for t in tenants if t["status"] == "active"),
            "total_tokens": tot["toks"], "total_requests": tot["reqs"], "total_cost": round(tot["cost"], 6),
            "by_model": by_model, "per_tenant": per_tenant, "per_user": per_user,
            "by_day": list(reversed(by_day)), "range": range_key}
