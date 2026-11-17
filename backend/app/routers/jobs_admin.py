"""Control-plane async job endpoints.

The console polls these short requests instead of holding one request through the
company gateway while a database, embedding model, or LLM does long-running work.
"""
from fastapi import APIRouter, Depends, HTTPException

from .. import jobs, store
from ..console_auth import console_guard, current_principal, ensure_tenant_access
from ..context import get_request_id
from ..models import AsyncTestConnIn, DataSourceIn, DSConnIn, DocConfigIn

router = APIRouter(
    prefix="/admin",
    tags=["persistent background jobs"],
    dependencies=[Depends(console_guard)],
)


def _err(status: int, code: str, message: str):
    raise HTTPException(status, {"code": code, "message": message})


def _created(job: dict) -> dict:
    return {
        "job_id": job["id"],
        "status": job["status"],
        "status_url": f"/admin/jobs/{job['id']}",
    }


def _actor(principal: dict) -> str:
    return principal.get("user_id") or principal.get("email") or "master-key"


@router.get("/jobs/{job_id}")
def get_job(job_id: str, after: int = 0, p: dict = Depends(current_principal)):
    row = jobs.get(job_id)
    if not row:
        _err(404, "JOB_NOT_FOUND", "Job tidak ditemukan.")
    ensure_tenant_access(p, row["tenant_id"])
    out = jobs._public(row)
    out["events"] = jobs.events(job_id, after=after)
    return out


@router.delete("/jobs/{job_id}")
def cancel_job(job_id: str, p: dict = Depends(current_principal)):
    row = jobs.get(job_id)
    if not row:
        _err(404, "JOB_NOT_FOUND", "Job tidak ditemukan.")
    ensure_tenant_access(p, row["tenant_id"])
    jobs.request_cancel(job_id)
    return {"ok": True, "job_id": job_id}


@router.post("/jobs/datasource-test", status_code=202)
def datasource_test_job(b: AsyncTestConnIn, p: dict = Depends(current_principal)):
    ensure_tenant_access(p, b.tenant_id)
    return _created(jobs.enqueue(
        "datasource_test",
        b.tenant_id,
        {"type": b.type, "conn": b.conn},
        actor_id=_actor(p),
        request_id=get_request_id(),
    ))


@router.post("/jobs/datasources", status_code=202)
def datasource_create_job(b: DataSourceIn, p: dict = Depends(current_principal)):
    ensure_tenant_access(p, b.tenant_id)
    return _created(jobs.enqueue(
        "datasource_create",
        b.tenant_id,
        {"type": b.type, "name": b.name, "conn": b.conn},
        actor_id=_actor(p),
        request_id=get_request_id(),
    ))


def _datasource(did: str, p: dict) -> dict:
    ds = store.get_datasource(did)
    if not ds:
        _err(404, "NOT_FOUND", "Datasource tidak ditemukan.")
    ensure_tenant_access(p, ds["tenant_id"])
    return ds


@router.post("/jobs/datasources/{did}/connection", status_code=202)
def datasource_update_job(
    did: str, b: DSConnIn, p: dict = Depends(current_principal)
):
    ds = _datasource(did, p)
    return _created(jobs.enqueue(
        "datasource_update",
        ds["tenant_id"],
        {"datasource_id": did, "conn": b.conn},
        actor_id=_actor(p),
        request_id=get_request_id(),
    ))


@router.post("/jobs/datasources/{did}/rescan", status_code=202)
def datasource_rescan_job(did: str, p: dict = Depends(current_principal)):
    ds = _datasource(did, p)
    return _created(jobs.enqueue(
        "datasource_rescan",
        ds["tenant_id"],
        {"datasource_id": did},
        actor_id=_actor(p),
        request_id=get_request_id(),
    ))


@router.post("/jobs/datasources/{did}/auto-enrich", status_code=202)
def datasource_enrich_job(
    did: str, use_samples: bool = True, p: dict = Depends(current_principal)
):
    ds = _datasource(did, p)
    return _created(jobs.enqueue(
        "datasource_auto_enrich",
        ds["tenant_id"],
        {"datasource_id": did, "use_samples": use_samples},
        actor_id=_actor(p),
        request_id=get_request_id(),
    ))


@router.post("/jobs/tenants/{tid}/businesses/{bid}/datasources/{did}/auto-enrich",
             status_code=202)
def business_enrich_job(tid: str, bid: str, did: str, use_samples: bool = True,
                        p: dict = Depends(current_principal)):
    """Draft the curation for one (business, datasource) pair. Writes the BUSINESS's
    knowledge only — the datasource's own MDL is left alone."""
    business = store.get_business(bid)
    if not business or business["tenant_id"] != tid:
        _err(404, "NOT_FOUND", "Business tidak ditemukan untuk tenant ini.")
    ds = _datasource(did, p)
    if ds["tenant_id"] != tid:
        _err(404, "NOT_FOUND", "Datasource tidak ditemukan untuk tenant ini.")
    return _created(jobs.enqueue(
        "business_auto_enrich",
        tid,
        {"business_id": bid, "datasource_id": did, "use_samples": use_samples},
        actor_id=_actor(p),
        request_id=get_request_id(),
    ))


@router.post("/jobs/documents/{doc_id}/reingest", status_code=202)
def document_reingest_job(
    doc_id: str, b: DocConfigIn, p: dict = Depends(current_principal)
):
    doc = store.get_document(doc_id)
    if not doc:
        _err(404, "NOT_FOUND", "Dokumen tidak ditemukan.")
    ensure_tenant_access(p, doc["tenant_id"])
    raw = b.model_dump() if hasattr(b, "model_dump") else b.dict()
    config = {k: v for k, v in raw.items() if v is not None}
    return _created(jobs.enqueue(
        "document_reingest",
        doc["tenant_id"],
        {"document_id": doc_id, "config": config},
        actor_id=_actor(p),
        request_id=get_request_id(),
    ))


@router.post("/jobs/tenants/{tenant_id}/reindex-docs", status_code=202)
def documents_reindex_job(tenant_id: str, p: dict = Depends(current_principal)):
    ensure_tenant_access(p, tenant_id)
    return _created(jobs.enqueue(
        "documents_reindex",
        tenant_id,
        {},
        actor_id=_actor(p),
        request_id=get_request_id(),
    ))
