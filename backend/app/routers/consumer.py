import contextvars
import os
import time
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from .. import jobs, store, rag, ratelimit
from ..api_errors import log_internal, raise_safe
from ..auth import require_api_key, end_user_identity, verify_end_user
from ..models import QueryIn, ChatIn, FeedbackIn, RetrieveIn, MbrReportIn
from ..connectors.factory import make_connector, SQL_TYPES
from ..governance.engine import govern
from ..governance._compat import WrenError
from ..agent import run_agent
from ..skills.registry import cost_advice
from .. import business_scope
from ..context import (
    PipelineCancelled,
    get_request_id,
    set_budget_reservation,
    set_cancellation,
    set_tenant_db_scope,
)

router = APIRouter(prefix="/v1", tags=["consumer API (per API key + role)"])


def _err(status, code, message):
    raise HTTPException(status, {"code": code, "message": message})


def rate_limited(p=Depends(require_api_key)):
    """Require a valid API key AND enforce the per-tenant rate limit."""
    ok, retry, limit, remaining = ratelimit.check(p["tenant_id"], p["key"])
    if not ok:
        raise HTTPException(429, {"code": "RATE_LIMITED",
                                  "message": f"rate limit of {limit}/min exceeded; retry in {retry}s",
                                  "retry_after": retry})
    return p


_JWT_ERR_MSG = {
    "PYJWT_MISSING": "JWT verification unavailable: install pyjwt on the server",
    "JWT_VERIFY_UNAVAILABLE": "JWT verification unavailable: configure this tenant's JWT verifier",
    "JWT_ASYMMETRIC_REQUIRED": "Production requires tenant JWT verification with JWKS/public key and an asymmetric algorithm; staging HS256 must be explicitly enabled and configured per tenant",
    "JWT_ISSUER_AUDIENCE_REQUIRED": "Production requires both JWT issuer and audience for this tenant",
    "JWT_ALGORITHM_INVALID": "JWT algorithm is not allowed for the configured key type",
    "JWT_KEY_UNAVAILABLE": "JWT signing key/JWKS endpoint is unavailable or unsafe",
    "JWT_INVALID": "End-user JWT signature is invalid or has expired",
}


def _resolve_role(tenant_id: str, candidates: list):
    """Resolve external role values -> a defined tenant role. Deny-by-default:
    per value, try the tenant's role_mappings alias FIRST, then an exact role-name
    match (backward compatible); the first value that resolves wins.

    A mapping is AUTHORITATIVE for its external id: if its target role was deleted,
    that value is DENIED — it must NOT fall through to an exact-name match, or a
    deliberately down-mapped privileged name (e.g. IdP group 'admin' mapped to a
    restricted role) would silently re-escalate the moment the target role is removed
    (review-confirmed escalation)."""
    for v in candidates:
        mapped = store.get_role_mapping(tenant_id, v)
        if mapped:
            if store.get_role(tenant_id, mapped):
                return mapped
            continue                       # broken mapping: deny this value, never fall through
        if store.get_role(tenant_id, v):
            return v
    return None


def _effective(p, eu):
    """Effective governance principal — STRICT Pattern B.

    The API key (X-API-Key) only authenticates the *consumer app*: it carries the tenant,
    metering and rate-limit, but NO role. Every call MUST also carry an end-user JWT
    (`Authorization: Bearer <token>`), VERIFIED against the tenant's JWT config. A
    global HS secret is a development-only compatibility fallback; production requires
    tenant-scoped asymmetric JWKS/public-key verification with issuer + audience.
    The configured role_claim (default 'role') may hold a
    string OR an array (e.g. 'groups'); each value resolves via role_mappings first,
    then exact role-name match. No JWT / bad JWT / unresolvable role is rejected —
    there is no key-role fallback; unresolved values are recorded for admin visibility.
    Returns (principal_with_role, end_user_id)."""
    # RLS defense-in-depth for every tenant-scoped metadata lookup after the API key
    # has identified the tenant. Control-plane requests use the default "*" scope.
    set_tenant_db_scope(p["tenant_id"])
    if not eu or not eu.get("present"):
        _err(401, "JWT_REQUIRED",
             "End-user JWT required: send 'Authorization: Bearer <token>' (with a 'role' claim) "
             "alongside X-API-Key. The role identifies the end-user and selects their data access.")
    v = verify_end_user(p["tenant_id"], eu.get("token") or "")
    if not v.get("verified"):
        code = v.get("error") or "JWT_INVALID"
        _err(401, code, _JWT_ERR_MSG.get(code, "Invalid end-user JWT"))
    claim = v["role_claim"]
    raw = (v.get("claims") or {}).get(claim)
    if raw is None or raw == "" or raw == []:
        _err(403, "JWT_ROLE_MISSING", f"End-user JWT has no '{claim}' claim")
    # normalize each value EXACTLY like the admin write path (strip + 200-char cap) so
    # a mapping created from the unmapped list always matches the live claim value;
    # scan up to 50 values (enterprise IdPs send long group arrays).
    vals = raw if isinstance(raw, (list, tuple)) else [raw]
    candidates = [str(x).strip()[:200] for x in vals if x is not None and str(x).strip()][:50]
    if not candidates:
        _err(403, "JWT_ROLE_MISSING", f"End-user JWT claim '{claim}' has no usable value")
    role = _resolve_role(p["tenant_id"], candidates)
    if not role:
        # visibility: admin can map these later. Skip values that DO have a (broken)
        # mapping row — they're already visible in the mappings list; double-listing
        # the same id as both mapped and unmapped reads as contradictory state.
        for cand in candidates[:10]:
            if not store.get_role_mapping(p["tenant_id"], cand):
                store.record_unmapped_role(p["tenant_id"], cand)
        _err(403, "ROLE_NOT_FOUND",
             f"JWT role value(s) {candidates[:10]} resolve to no defined role for this tenant "
             "(no role mapping and no exact role-name match)")
    return {**p, "role": role}, v.get("user_id") or ""


def _pick_sql_ds(tenant_id: str, datasource_id: str | None, business: dict | None = None):
    """Select the SQL datasource to query: by id if given, else the first SQL-type source.

    With a business active this sees only that business's datasources, already scoped — so
    a raw-SQL caller cannot reach a table outside the slice by naming its datasource."""
    all_ds = business_scope.visible_datasources(tenant_id, business)
    sql_ds = [d for d in all_ds if d["type"] in SQL_TYPES]
    if datasource_id:
        match = [d for d in all_ds if d["id"] == datasource_id]
        if not match:
            _err(400, "DATASOURCE_NOT_FOUND", f"datasource '{datasource_id}' not found for this tenant")
        if match[0]["type"] not in SQL_TYPES:
            _err(400, "NOT_SQL_DATASOURCE", "selected datasource is not a SQL database")
        return match[0]
    if not sql_ds:
        _err(400, "NO_DATASOURCE", "tenant has no SQL datasource")
    return sql_ds[0]


def _ctx(p, datasource_id=None, business=None):
    biz = business_scope.resolve(p["tenant_id"], p["role"], business)
    ds = _pick_sql_ds(p["tenant_id"], datasource_id, biz)
    role = store.get_role(p["tenant_id"], p["role"])
    if not role:
        _err(400, "ROLE_NOT_FOUND", f"role '{p['role']}' does not exist")
    return ds, role


def _run(p, sql, action, datasource_id=None, model=None, ptoks=0, ctoks=0, cost=0.0,
         user_id="", trace_id="", business=None):
    ds, role = _ctx(p, datasource_id, business)
    conn = make_connector(ds["type"], ds["conn"])
    try:
        safe = govern(sql, conn.dialect, ds["mdl"], role["policy"],
                      namespace=getattr(conn, "namespace", lambda: None)(),
                      tenant_id=p["tenant_id"])
        # Cost guard: estimate scanned bytes WITHOUT running (BigQuery; None where the
        # engine cannot estimate). The agent path has always done this
        # (skills/registry.execute_governed) — without it this raw-SQL endpoint was the
        # LOOSER of the two, so an API key could bill unbounded scans the agent refuses.
        est = conn.dry_run(safe)
        cap = getattr(conn, "max_bytes_billed", None)
        if est is not None and cap and est > cap:
            raise WrenError("COST_LIMIT",
                            f"query would scan ~{est:,} bytes, over the {cap:,}-byte limit. "
                            + cost_advice(safe, conn.dialect, ds["mdl"]))
        cols, rows = conn.run(safe)
    except WrenError as e:
        store.log_usage(p["tenant_id"], p["key"], p["role"], action, 0, e.code,
                        model, ptoks, ctoks, cost, user_id=user_id, trace_id=trace_id)
        raise HTTPException(403, {"code": e.code, "message": e.message})
    except HTTPException:
        raise
    except Exception as e:
        store.log_usage(p["tenant_id"], p["key"], p["role"], action, 0, "ERROR",
                        model, ptoks, ctoks, cost, user_id=user_id, trace_id=trace_id)
        raise_safe(400, "QUERY_EXECUTION_FAILED",
                   "Query tidak dapat dijalankan. Gunakan request id untuk memeriksa log backend.",
                   e, context=f"consumer query tenant={p['tenant_id']}")
    store.log_usage(p["tenant_id"], p["key"], p["role"], action, len(rows), "OK",
                    model, ptoks, ctoks, cost, user_id=user_id, trace_id=trace_id)
    return {"role": p["role"], "datasource_id": ds["id"], "safe_sql": safe, "columns": cols, "rows": rows}


def _reserve_chat_budget(tenant_id):
    try:
        return store.reserve_budget(tenant_id)
    except store.BudgetExceeded as exc:
        raise HTTPException(429, {
            "code": "BUDGET_EXCEEDED",
            "message": "Monthly AI budget is exhausted; ask the tenant admin to increase it or wait for next month",
            "budget_usd": exc.limit,
            "spent_usd": round(exc.spent, 6),
            "pending_usd": round(exc.pending, 6),
            "requested_reservation_usd": round(exc.requested, 6),
        })


@router.post("/query")
def query(b: QueryIn, p=Depends(rate_limited), eu=Depends(end_user_identity)):
    pr, user_id = _effective(p, eu)
    return _run(pr, b.sql, "query", b.datasource_id, user_id=user_id, business=b.business)


@router.post("/chat")
def chat(b: ChatIn, p=Depends(rate_limited), eu=Depends(end_user_identity)):
    """Unified agent pipeline: gatekeeper -> router -> tools (DB+Docs+Memory) -> verify."""
    pr, user_id = _effective(p, eu)
    reservation = _reserve_chat_budget(pr["tenant_id"])
    set_budget_reservation(reservation["id"] if reservation else "")
    # end-user identity comes ONLY from the verified JWT (`sub`). The body `user_id` is
    # NOT trusted: honoring it would let any app read another user's per-user memory
    # whenever a token lacks `sub` (review-confirmed isolation break). No sub -> "" ->
    # memory recall is simply skipped.
    try:
        out = run_agent(
            pr,
            b.message,
            b.datasource_id,
            user_id,
            history=b.history,
            analysis_mode=b.analysis_mode,
            business=b.business,
            confirmation=b.confirmation,
            session_id=b.session_id,
        )
        u = out.get("usage", {})
        sql = out.get("sql") or {}
        rows = len(sql.get("rows", [])) if sql.get("ok") else 0
        status = "BLOCKED" if out.get("blocked") else "OK"
        store.log_usage(pr["tenant_id"], pr["key"], pr["role"], "chat", rows, status,
                        u.get("model"), u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
                        u.get("cost", 0.0), user_id=user_id, trace_id=out.get("trace_id", ""))
        return out
    except Exception:
        # A provider may have billed work before returning an error. Charge the
        # conservative reservation so repeated failures cannot bypass the ceiling.
        if reservation:
            store.log_usage(pr["tenant_id"], pr["key"], pr["role"], "chat", 0, "ERROR",
                            cost=reservation["amount"], user_id=user_id)
        raise
    finally:
        store.release_budget(reservation["id"] if reservation else None)
        set_budget_reservation("")


@router.post("/chat/jobs", status_code=202)
def create_chat_job(
    b: ChatIn, p=Depends(rate_limited), eu=Depends(end_user_identity)
):
    """Queue governed chat/deep research and return before the edge timeout."""
    pr, user_id = _effective(p, eu)
    job = jobs.enqueue(
        "chat",
        pr["tenant_id"],
        {
            "message": b.message,
            "datasource_id": b.datasource_id,
            "history": b.history,
            "analysis_mode": b.analysis_mode,
            "business": b.business,
            "confirmation": b.confirmation,
            "session_id": b.session_id,
            "role": pr["role"],
        },
        actor_id=user_id,
        app_key_id=pr["key"],
        request_id=get_request_id(),
    )
    return {
        "job_id": job["id"],
        "status": job["status"],
        "status_url": f"/v1/chat/jobs/{job['id']}",
    }


def _consumer_job(job_id: str, p: dict, eu: dict):
    pr, user_id = _effective(p, eu)
    row = jobs.get(job_id)
    if (
        not row
        or row["kind"] != "chat"
        or row["tenant_id"] != pr["tenant_id"]
        or row.get("app_key_id") != pr["key"]
        or (row.get("actor_id") or "") != (user_id or "")
    ):
        _err(404, "JOB_NOT_FOUND", "Chat job tidak ditemukan.")
    return row


@router.get("/chat/jobs/{job_id}")
def get_chat_job(
    job_id: str,
    after: int = 0,
    p=Depends(require_api_key),
    eu=Depends(end_user_identity),
):
    row = _consumer_job(job_id, p, eu)
    out = jobs._public(row)
    out["events"] = jobs.events(job_id, after=after)
    return out


@router.delete("/chat/jobs/{job_id}")
def cancel_chat_job(
    job_id: str,
    p=Depends(require_api_key),
    eu=Depends(end_user_identity),
):
    _consumer_job(job_id, p, eu)
    jobs.request_cancel(job_id)
    return {"ok": True, "job_id": job_id}


# ---- generated reports (Monthly Business Review) ----
@router.post("/reports/mbr", status_code=202)
def create_mbr_report(b: MbrReportIn, p=Depends(rate_limited), eu=Depends(end_user_identity)):
    """Queue an MBR package for one brand and month. Inputs and the business slice are
    checked NOW, so a typo answers in this response instead of as a failed job later."""
    from ..reports.mbr_config import request_targets
    from ..reports.sqlkit import Month, ReportInputError, clean_brand
    pr, user_id = _effective(p, eu)
    try:
        month = Month.parse(b.period)
        brand = clean_brand(b.brand)
        targets = {k: v["nmv"] for k, v in request_targets(b.targets, month).items()}
    except ReportInputError as exc:
        _err(400, exc.code, exc.message)
    business_scope.resolve(pr["tenant_id"], pr["role"], b.business)   # 409/403 via handler
    if b.inputs:
        _claim_inputs(pr, user_id, b.inputs)
    job = jobs.enqueue(
        "mbr_report", pr["tenant_id"],
        {"brand": brand, "period": b.period, "business": b.business, "role": pr["role"],
         "targets": targets, "inputs": b.inputs},
        actor_id=user_id, app_key_id=pr["key"], request_id=get_request_id(),
        summary={"brand": brand, "period": b.period},
    )
    return {"report_id": job["id"], "status": job["status"],
            "status_url": f"/v1/reports/{job['id']}"}


def _claim_inputs(pr: dict, user_id: str, input_id: str) -> None:
    """The attached files must be closed and the caller's own; a set already used may feed
    another report until it expires (generate again after a fix, or for another month)."""
    from ..reports import inputs
    try:
        row = inputs.owned(pr["tenant_id"], input_id, pr["key"], user_id)
    except inputs.InputError as exc:
        _err(exc.status, exc.code, exc.message)
    if row["status"] == "open":
        _err(409, "INPUT_NOT_CLOSED", "Finish attaching the files before asking for the report.")
    store.use_report_input(pr["tenant_id"], input_id, time.time() + inputs.RUN_GRACE_SEC)


@router.get("/reports/mbr/brands")
def mbr_brands(business: str | None = None, p=Depends(require_api_key),
               eu=Depends(end_user_identity)):
    """Brands the caller can build an MBR for, read from the order history in their
    business slice (governed, cached for hours), for a picker instead of a text box.
    Not rate limited like a question: a consumer app may fetch it on every page load."""
    from ..reports import mbr_engine
    from ..reports.sqlkit import ReportInputError
    pr, _user_id = _effective(p, eu)
    try:
        return mbr_engine.brand_choices(pr["tenant_id"], pr["role"], business)
    except ReportInputError as exc:
        _err(400, exc.code, exc.message)


def _consumer_report(job_id: str, p: dict, eu: dict):
    pr, user_id = _effective(p, eu)
    row = jobs.get(job_id)
    if (
        not row
        or row["kind"] != "mbr_report"
        or row["tenant_id"] != pr["tenant_id"]
        or row.get("app_key_id") != pr["key"]
        or (row.get("actor_id") or "") != (user_id or "")
    ):
        _err(404, "REPORT_NOT_FOUND", "Report not found.")
    return row, pr


@router.get("/reports")
def list_reports(limit: int = 20, p=Depends(require_api_key), eu=Depends(end_user_identity)):
    """The caller's own recent reports (same app key and end user), newest first, so a
    consumer app can show a history without keeping its own copy of the report ids."""
    pr, user_id = _effective(p, eu)
    items = []
    for job in jobs.list_for_actor("mbr_report", pr["tenant_id"], pr["key"], user_id, limit):
        params = job.pop("params", {}) or {}
        result = job.pop("result", None) or {}
        summary = job.pop("summary", {}) or {}
        job["brand"] = params.get("brand") or result.get("brand") or summary.get("brand")
        job["period"] = params.get("period") or result.get("period") or summary.get("period")
        job["business"] = params.get("business") or (result.get("business") or {}).get("name")
        job["delivery_status"] = result.get("delivery_status")
        job["files"] = (store.list_report_files(pr["tenant_id"], job["id"])
                        if job["status"] == "succeeded" else [])
        items.append(job)
    return {"reports": items}


@router.get("/reports/{job_id}")
def get_report(job_id: str, after: int = 0, p=Depends(require_api_key),
               eu=Depends(end_user_identity)):
    row, pr = _consumer_report(job_id, p, eu)
    out = jobs._public(row)
    out["events"] = jobs.events(job_id, after=after)
    out["files"] = store.list_report_files(pr["tenant_id"], job_id)
    return out


@router.delete("/reports/{job_id}")
def cancel_report(job_id: str, p=Depends(require_api_key), eu=Depends(end_user_identity)):
    """Stop the caller's own report that is still queued or running. A queued one ends at
    once; a running one stops at its next query; one whose worker died ends when the
    lease runs out (jobs._claim)."""
    row, _pr = _consumer_report(job_id, p, eu)
    if row["status"] not in ("queued", "running"):
        _err(409, "REPORT_NOT_ACTIVE", "This report has already finished.")
    jobs.request_cancel(job_id)
    return {"ok": True, "report_id": job_id}


@router.get("/reports/{job_id}/files/{name}")
def get_report_file(job_id: str, name: str, p=Depends(rate_limited),
                    eu=Depends(end_user_identity)):
    _, pr = _consumer_report(job_id, p, eu)
    found = store.read_report_file(pr["tenant_id"], job_id, name)
    if not found:
        _err(404, "NO_FILE", "This report has no file with that name.")
    mime, data = found
    return Response(content=data, media_type=mime,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


@router.post("/chat_stream")
async def chat_stream(request: Request, b: ChatIn, p=Depends(rate_limited),
                      eu=Depends(end_user_identity)):
    """Stream the governed pipeline and cooperatively stop when its client disconnects."""
    import asyncio
    import json as _json
    import queue as _queue
    import threading
    import uuid

    pr, user_id = _effective(p, eu)
    reservation = _reserve_chat_budget(pr["tenant_id"])
    q = _queue.Queue()
    cancel_event = threading.Event()
    trace_id = uuid.uuid4().hex

    def work():
        set_tenant_db_scope(pr["tenant_id"])
        set_budget_reservation(reservation["id"] if reservation else "")
        set_cancellation(cancel_event, trace_id)
        try:
            out = run_agent(
                pr,
                b.message,
                b.datasource_id,
                user_id,
                emit=q.put,
                history=b.history,
                analysis_mode=b.analysis_mode,
                trace_id=trace_id,
                business=b.business,
                confirmation=b.confirmation,
                session_id=b.session_id,
            )
            u = out.get("usage", {})
            sql = out.get("sql") or {}
            rows = len(sql.get("rows", [])) if sql.get("ok") else 0
            status = "BLOCKED" if out.get("blocked") else "OK"
            store.log_usage(
                pr["tenant_id"], pr["key"], pr["role"], "chat", rows, status,
                u.get("model"), u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
                u.get("cost", 0.0), user_id=user_id, trace_id=out.get("trace_id", ""),
            )
            q.put({"type": "done", "result": out})
        except PipelineCancelled as stopped:
            # A model call already in flight may still be billed, but no subsequent
            # model/SQL/web step and no final answer audit is allowed after Stop.
            u = stopped.usage or {}
            store.log_usage(
                pr["tenant_id"], pr["key"], pr["role"], "chat", 0, "CANCELLED",
                u.get("model"), u.get("prompt_tokens", 0), u.get("completion_tokens", 0),
                u.get("cost", 0.0), user_id=user_id, trace_id=trace_id,
            )
            store.log_audit(
                pr["tenant_id"], trace_id, "cancelled",
                role=pr["role"], user_id=user_id, model=u.get("model"),
                status="cancelled", cost=u.get("cost", 0.0),
                detail={
                    "reason": "client_disconnected",
                    "prompt_tokens": u.get("prompt_tokens", 0),
                    "completion_tokens": u.get("completion_tokens", 0),
                },
            )
            q.put({"type": "cancelled", "trace_id": trace_id})
        except Exception as e:                       # noqa: BLE001
            if reservation:
                store.log_usage(
                    pr["tenant_id"], pr["key"], pr["role"], "chat", 0, "ERROR",
                    cost=reservation["amount"], user_id=user_id,
                )
            request_id = log_internal(
                "CHAT_FAILED", e, context=f"stream chat tenant={pr['tenant_id']}"
            )
            q.put({
                "type": "error",
                "code": "CHAT_FAILED",
                "error": "Poros gagal menyelesaikan respons. Periksa backend menggunakan request id.",
                "request_id": request_id,
            })
        finally:
            store.release_budget(reservation["id"] if reservation else None)
            set_budget_reservation("")
            set_cancellation()
            q.put(None)

    ctx = contextvars.copy_context()
    threading.Thread(target=lambda: ctx.run(work), daemon=True).start()

    async def gen():
        try:
            while True:
                # AbortController closes the fetch. Navigating inside the SPA keeps
                # this connection alive because the Chatbot view remains mounted.
                if await request.is_disconnected():
                    cancel_event.set()
                    break
                try:
                    ev = q.get_nowait()
                except _queue.Empty:
                    await asyncio.sleep(0.08)
                    continue
                if ev is None:
                    break
                yield f"data: {_json.dumps(ev, default=str)}\n\n"
        finally:
            cancel_event.set()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


_FEEDBACK_RATINGS = {"helpful", "unhelpful"}
_FEEDBACK_REASONS = {"", "incorrect", "incomplete", "irrelevant", "unclear", "outdated", "other"}


@router.post("/feedback")
def feedback(b: FeedbackIn, p=Depends(rate_limited), eu=Depends(end_user_identity)):
    """Attach end-user feedback to a trusted question/answer trace.

    The API never accepts question/answer text from the client. It resolves both from
    the tenant-scoped audit trail, preventing cross-tenant and forged feedback targets.
    Re-submitting by the same user updates the existing feedback.
    """
    pr, user_id = _effective(p, eu)
    if not user_id:
        _err(403, "FEEDBACK_USER_REQUIRED", "End-user JWT must contain a non-empty 'sub' claim")
    trace_id = (b.trace_id or "").strip()[:128]
    rating = (b.rating or "").strip().lower()
    reason = (b.reason or "").strip().lower()
    comment = (b.comment or "").strip()[:2000]
    if not trace_id:
        _err(400, "FEEDBACK_TRACE_REQUIRED", "trace_id is required")
    if rating not in _FEEDBACK_RATINGS:
        _err(400, "FEEDBACK_RATING_INVALID", "rating must be 'helpful' or 'unhelpful'")
    if reason not in _FEEDBACK_REASONS:
        _err(400, "FEEDBACK_REASON_INVALID",
             "reason must be incorrect, incomplete, irrelevant, unclear, outdated, other, or empty")
    saved = store.upsert_feedback(pr["tenant_id"], trace_id, user_id, pr["role"],
                                  rating, reason, comment)
    if not saved:
        _err(404, "FEEDBACK_TARGET_NOT_FOUND",
             "No completed chat response with this trace_id exists for the API key's tenant")
    store.log_audit(pr["tenant_id"], trace_id, "feedback", role=pr["role"], user_id=user_id,
                    status=rating, detail={"feedback_id": saved["id"], "rating": rating,
                                           "reason": reason, "comment": comment},
                    session_id=store.audit_session_of(pr["tenant_id"], trace_id))
    return {"ok": True, "id": saved["id"], "trace_id": trace_id,
            "rating": rating, "reason": reason, "comment": comment,
            "updated_at": saved["updated"]}


@router.post("/retrieve")
def retrieve(b: RetrieveIn, p=Depends(rate_limited), eu=Depends(end_user_identity)):
    """Doc-RAG retrieval scoped to the caller's tenant + (effective) role ACL; returns citations."""
    pr, user_id = _effective(p, eu)
    hits = rag.retrieve(pr["tenant_id"], b.query, pr["role"], b.top_k)
    store.log_usage(pr["tenant_id"], pr["key"], pr["role"], "retrieve", len(hits), "OK",
                    user_id=user_id)
    return {"role": pr["role"], "query": b.query, "results": hits,
            "citations": [h["citation"] for h in hits]}


@router.get("/documents/{doc_id}/file")
def document_file(doc_id: str, p=Depends(rate_limited), eu=Depends(end_user_identity)):
    """Serve the ORIGINAL uploaded document so consumer apps can make citations clickable.
    Guarded like everything else: same tenant + the effective role must pass the
    document's ACL (empty ACL = all roles of the tenant)."""
    pr, user_id = _effective(p, eu)
    d = store.get_document(doc_id)
    if not d or d["tenant_id"] != pr["tenant_id"]:
        _err(404, "NOT_FOUND", "document not found")
    if d["acl_roles"] and pr["role"] not in d["acl_roles"]:
        _err(403, "DOC_ACL_DENIED", "role is not allowed to open this document")
    if not store.document_file_available(d):
        _err(404, "NO_FILE", "original file not stored — re-upload the document to enable viewing")
    store.log_usage(pr["tenant_id"], pr["key"], pr["role"], "document_file", 0, "OK",
                    user_id=user_id)
    data = store.read_document_file(pr["tenant_id"], doc_id)
    return Response(
        content=data,
        media_type=d.get("mime") or "application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(d['filename'])}"},
    )
