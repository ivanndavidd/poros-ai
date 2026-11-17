import hmac
import logging
import os
import re
import sys
import threading
import uuid
from http.cookies import SimpleCookie
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from . import (business_scope, jobs, llm, runtime_config, security, semantic_resolver,
               store, version)
from .console_auth import apps_gateway_enabled
from .auth import _admin_key
from .console_auth import SESSION_COOKIE
from .context import (get_request_id, set_budget_reservation, set_request_id,
                      set_tenant_db_scope)
from .api_errors import log_internal, safe_detail
from .routers import (
    admin, console, consumer, docs_admin, jobs_admin, memory_admin, models_admin,
    report_inputs, sql_console,
)
from .routers.console import _google_sso_cfg, _sso_cfg

security.startup_check()   # fail hard on dev-grade secrets when APP_ENV=production

# Heavy lazy imports are paid HERE in a background thread at startup — and after every
# --reload restart — so no user request ever waits for them:
#   * litellm import ~5s (Model tab / first chat)
#   * sentence-transformers embedding + cross-encoder rerank models ~10-30s CPU
#     (first doc question would otherwise hang on "Menelusuri dokumen…")
def _warm_models():
    llm.available()
    try:
        from .rag.embeddings import _sentence_transformers
        from .rag.retriever import _reranker
        _sentence_transformers()       # no-op when ST_EMBED_DISABLE=1 / lib missing
        _reranker()                    # no-op when RAG_RERANK=0
    except Exception:
        pass


# Skip warm-up under the test suite: the models are disabled/mocked there, so the thread
# does no useful work, and a daemon thread importing litellm/torch concurrently with the
# suite's shared-Postgres schema setup makes the suite flaky. ("pytest" in sys.modules is
# the canonical "running under pytest" signal — true at collection time, never in prod.)
if "pytest" not in sys.modules:
    threading.Thread(target=_warm_models, daemon=True, name="model-warmup").start()


def _audit_outbox_worker():
    try:
        interval = max(15, min(int(os.getenv("ADMIN_AUDIT_RETRY_SEC", "60")), 3600))
    except ValueError:
        interval = 60
    while True:
        try:
            store.flush_admin_audit_outbox(limit=100)
        except Exception:
            logging.getLogger("poros.audit").exception("periodic audit outbox flush failed")
        threading.Event().wait(interval)

_docs_enabled = runtime_config.docs_enabled()

app = FastAPI(
    title="Poros — AI Distribution Administrator",
    version="0.2.0",
    description="Multi-tenant control plane: governed analytics + RAG distributed to consumer apps via API keys. "
                "Auto-scan schema → MDL, multi-LLM gateway (gatekeeper + router), doc-RAG + memory, "
                "governance reuses Wren policy.py (Apache-2.0) + RLAC/CLAC. Consumer API is per API key + role.",
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

_cors_origins = runtime_config.cors_origins()
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        # PUT was missing, which broke every cross-origin PUT with a 400 on the
        # PREFLIGHT -- so the role-grants editor (PUT roles/{name}/businesses) had
        # never worked from a split origin, only from the same-origin production
        # bundle where CORS does not apply. A method list has to match the routes.
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Accept", "Authorization", "Content-Type", "Idempotency-Key", "X-Admin-Key",
            "X-API-Key", "X-Request-Id", "X-User-Id",
        ],
        expose_headers=["X-Request-Id"],
    )


class _CorrelationIdMiddleware:
    """Pure-ASGI (NOT BaseHTTPMiddleware): the request-id ContextVar set here must PROPAGATE
    into the endpoint and everything it calls synchronously — BaseHTTPMiddleware runs the app
    in a separate task, which would break that. Honors the Apps Gateway `X-Request-Id` (or mints
    one when standalone), publishes it via `context.request_id_var` (so the LLM gateway can tag
    downstream calls), and echoes it back so a request can be followed end to end."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        rid = ""
        for k, v in scope.get("headers") or []:
            if k == b"x-request-id":
                rid = v.decode("latin-1").strip()
                break
        if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", rid):
            rid = ""
        rid = rid or ("req-" + uuid.uuid4().hex[:16])
        set_request_id(rid)
        set_tenant_db_scope("*")
        set_budget_reservation("")

        async def _send(message):
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                existing = {key.lower() for key, _value in headers}
                for key, value in (
                    (b"x-request-id", rid.encode("ascii")),
                    (b"x-content-type-options", b"nosniff"),
                    # same-origin, NOT no-referrer: the console runs behind Apps Gateway,
                    # whose CSRF/origin checks on state-changing requests read Referer.
                    # no-referrer stripped it from every console request, so POST/PATCH/
                    # DELETE were rejected at the edge while GET kept working. same-origin
                    # still sends nothing cross-origin, so no referrer leaks outward.
                    (b"referrer-policy", b"same-origin"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                ):
                    if key not in existing:
                        headers.append((key, value))
            await send(message)

        await self.app(scope, receive, _send)


app.add_middleware(_CorrelationIdMiddleware)


@app.exception_handler(business_scope.BusinessError)
async def _business_scope_error(_request: Request, exc: business_scope.BusinessError):
    """A question that cannot be placed in a business is refused, never widened.

    The choices ride along so a client can present them — which is what phase 4's
    confirmation question is built on. 409, not 403: nothing was forbidden, the request is
    simply incomplete until a slice is named.
    """
    return JSONResponse(
        status_code=409,
        content={"detail": {"code": exc.code, "message": exc.message,
                            "businesses": exc.choices}},
    )


@app.exception_handler(semantic_resolver.SemanticError)
async def _semantic_error(_request: Request, exc: semantic_resolver.SemanticError):
    """One KPI phrase, more than one curated meaning (or broken replica authority):
    refused with the choices, never settled by datasource order. Same 409 family as the
    business handler above — `concepts` instead of `businesses`, and the payload is the
    contract a future metric-confirmation question will consume (design doc phase 2)."""
    return JSONResponse(
        status_code=409,
        content={"detail": {"code": exc.code, "message": exc.message,
                            "concepts": exc.options}},
    )


@app.exception_handler(Exception)
async def _unhandled_exception(_request: Request, exc: Exception):
    """Keep stack traces, paths and provider/connector details in server logs only."""
    log_internal("INTERNAL_ERROR", exc, context="unhandled request")
    return JSONResponse(
        status_code=500,
        content={"detail": safe_detail(
            "INTERNAL_ERROR",
            "Terjadi kesalahan internal. Gunakan request id saat menghubungi operator.",
        )},
    )


def _scope_headers(scope):
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in (scope.get("headers") or [])
    }


def _audit_actor(headers):
    """Resolve an actor before a mutation without retaining any credential."""
    if apps_gateway_enabled():
        email = (headers.get("x-user-id") or "").strip().lower()
        user = store.get_console_user_by_email(email) if "@" in email else None
        if user:
            return {"user_id": user["id"], "email": user["email"], "role": user["role"],
                    "via": "gateway"}
    token = ""
    cookie = SimpleCookie()
    try:
        cookie.load(headers.get("cookie") or "")
        token = cookie[SESSION_COOKIE].value if SESSION_COOKIE in cookie else ""
    except Exception:
        token = ""
    authorization = headers.get("authorization") or ""
    if authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip() or token
    if token:
        user = store.get_console_session(token)
        if user:
            return {"user_id": user["id"], "email": user["email"], "role": user["role"],
                    "via": "session"}
    candidate = headers.get("x-admin-key") or (
        token if authorization.lower().startswith("bearer ") else ""
    )
    key = _admin_key()
    if candidate and key and hmac.compare_digest(candidate.encode(), key.encode()):
        return {"user_id": "", "email": "", "role": "admin", "via": "master_key"}
    return {"user_id": "", "email": "", "role": "", "via": "anonymous"}


class _AdminAuditMiddleware:
    """Append-only audit for control-plane mutations; bodies/secrets are never logged."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        method = (scope.get("method") or "").upper()
        path = scope.get("path") or ""
        audited = method in {"POST", "PATCH", "DELETE"} and (
            path.startswith("/admin/") or path.startswith("/auth/users")
            or path in {"/auth/login", "/auth/logout"}
        )
        if not audited:
            return await self.app(scope, receive, send)
        headers = _scope_headers(scope)
        set_tenant_db_scope("*")
        actor = _audit_actor(headers)
        status = 500

        async def _send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message.get("status") or 500)
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            try:
                match = re.search(r"/admin/tenants/([^/]+)", path)
                client = scope.get("client") or ("", 0)
                store.log_admin_audit(
                    get_request_id(),
                    actor,
                    method,
                    path,
                    status,
                    tenant_id=match.group(1) if match else "",
                    source_ip=client[0] or "",
                    user_agent=headers.get("user-agent") or "",
                )
            except Exception as exc:
                print(f"[security] WARNING: admin audit write failed: {exc}")


app.add_middleware(_AdminAuditMiddleware)

store.init()
security.post_store_check()


@app.on_event("startup")
def _start_background_workers():
    # STARTUP hook, NOT module import: under `uvicorn --reload` the reloader parent
    # also imports app.main, and import-time workers gave it its own job threads —
    # frozen on the code from when the server was first started. Jobs were then
    # claimed alternately by the fresh worker and that stale parent (live repro
    # 2026-07-30: claims flip-flopped between the two pids), so half the runs
    # executed hours-old code. Only the process that actually serves HTTP may work
    # the queue.
    if "pytest" in sys.modules:
        return
    jobs.start_workers()
    if os.getenv("ADMIN_AUDIT_SINK_URL"):
        threading.Thread(
            target=_audit_outbox_worker,
            daemon=True,
            name="admin-audit-retry",
        ).start()


@app.on_event("shutdown")
def _release_background_jobs():
    # SIGTERM (a rollout, or the cluster autoscaler draining a node) kills the daemon job
    # threads mid-run. Hand their jobs back to the queue so another replica restarts them
    # now, instead of a report sitting "running 55%" until the lease runs out (live
    # 2026-10-13: an MBR evicted at 08:48:40 UTC would have waited an hour).
    if "pytest" in sys.modules:
        return
    try:
        jobs.release_running()
    except Exception:
        logging.getLogger("poros.jobs").exception("could not release running jobs on shutdown")


app.include_router(console.router)
app.include_router(console.users_router)   # /admin/users mirror — the gateway never forwards /auth/**
app.include_router(admin.router)
app.include_router(models_admin.router)
app.include_router(docs_admin.router)
app.include_router(jobs_admin.router)
app.include_router(memory_admin.router)
app.include_router(sql_console.router)
app.include_router(report_inputs.router)
app.include_router(consumer.router)

# ---- Apps Gateway public surface ----
# Behind the gateway every path needs login EXCEPT those under /dangerously-skip-login/**
# (the only anonymous prefix). Health probes and the console's mode-discovery must live
# here so they work before/without a logged-in user.
@app.get("/dangerously-skip-login/health", tags=["meta"])
def public_health():
    # version+built let an operator confirm a rebuild picked up their commit
    # WITHOUT logging in — this is the gateway's anonymous prefix.
    return {"status": "ok", **version.build_info()}


@app.get("/dangerously-skip-login/console-config", tags=["meta"])
def console_config(response: Response):
    """Lets the console UI pick its boot path without a credential: when `gateway` is true
    it skips the login form and reads identity from the gateway (X-User-Id); otherwise it
    shows the built-in login with whichever standalone SSO providers are configured."""
    # Deployment/login mode can change after an operator edits .env and restarts the
    # backend. Never let a browser reuse the old "SSO disabled" response.
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    microsoft = bool(_sso_cfg())
    return {
        "gateway": apps_gateway_enabled(),
        # Keep the old field as a Microsoft alias for backward-compatible frontends.
        "sso_configured": microsoft,
        "microsoft_sso_configured": microsoft,
        "google_sso_configured": bool(_google_sso_cfg()),
        # The SPA logs these at boot so "which build am I on?" is one F12 away.
        **version.build_info(),
    }


# Production image: the Vite bundle is copied into /app/frontend/dist and served by
# this same FastAPI process. Local development remains unchanged because frontend/dist
# may be absent and Vite still runs separately on :5173.
def _mount_frontend(application, directory) -> bool:
    """Mount a compiled Vite console when present; return whether it was mounted."""
    directory = Path(directory)
    if not (directory / "index.html").is_file():
        return False
    # Mounted last so API, admin, gateway-public, docs, and reserved application routes
    # retain priority. html=True serves index.html at "/" without inventing a second
    # runtime or weakening the Apps Gateway public-path policy.
    application.mount(
        "/",
        StaticFiles(directory=str(directory), html=True),
        name="poros-console",
    )
    return True


_frontend_dist = Path(
    os.getenv("FRONTEND_DIST_DIR")
    or Path(__file__).resolve().parents[2] / "frontend" / "dist"
)
_mount_frontend(app, _frontend_dist)
