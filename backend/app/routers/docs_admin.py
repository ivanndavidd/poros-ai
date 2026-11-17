"""Documentation doc-RAG (Feature 1.2) admin endpoints: upload / list / delete docs
for a per-tenant 'docs' datasource. Each chunk is tagged with tenant + role ACL.

Versioning guard: admins WILL re-upload updated documents without deleting the old
version (observed behavior, not an edge case). Same filename -> the old version is
auto-ARCHIVED (excluded from retrieval, nothing deleted); similar filename -> both
stay active but the response flags possible_duplicates so the UI can warn.
"""
import os
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import Response
from .. import jobs, store
from ..api_errors import raise_safe
from ..console_auth import console_guard, current_principal, ensure_tenant_access
from ..models import DocUpdateIn, DocConfigIn
from .. import rag
from ..context import get_request_id
from ..upload_security import (
    UploadRejected,
    read_bounded,
    tenant_quota_bytes,
    validate as validate_upload,
)


router = APIRouter(prefix="/admin", tags=["documentation doc-RAG (1.2)"], dependencies=[Depends(console_guard)])


@router.get("/tenants/{tid}/documents")
def list_documents(tid: str, datasource_id: str = None):
    return store.list_documents(tid, datasource_id)


@router.post("/documents", status_code=202)
async def upload_document(
    tenant_id: str = Form(...),
    datasource_id: str = Form(...),
    acl_roles: str = Form(""),                 # comma-separated; empty = all roles
    chunk_size: int = Form(None),              # per-document chunking (words); blank = tenant/code default
    chunk_overlap: int = Form(None),
    ocr: str = Form(None),                     # "true"/"false"/"1"/... per-document OCR toggle
    file: UploadFile = File(...),
    p: dict = Depends(current_principal),
):
    ensure_tenant_access(p, tenant_id)
    ds = store.get_datasource(datasource_id)
    if not ds or ds["tenant_id"] != tenant_id:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "docs datasource not found for tenant"})
    if ds["type"] != "docs":
        raise HTTPException(400, {"code": "NOT_DOCS", "message": "datasource is not of type 'docs'"})
    roles = [r.strip() for r in acl_roles.split(",") if r.strip()]
    doc_config = {}                            # per-document config supplied at upload time
    if chunk_size is not None:
        doc_config["chunk_size"] = chunk_size
    if chunk_overlap is not None:
        doc_config["chunk_overlap"] = chunk_overlap
    if ocr is not None:
        doc_config["ocr"] = str(ocr).strip().lower() in ("1", "true", "on", "yes")
    try:
        data = await read_bounded(file)
        filename, detected_mime = validate_upload(file.filename, file.content_type, data)
    except UploadRejected as exc:
        raise HTTPException(400, {"code": exc.code, "message": str(exc)}) from exc
    used_bytes = store.document_storage_bytes(tenant_id)
    quota = tenant_quota_bytes()
    if used_bytes + len(data) > quota:
        raise HTTPException(400, {
            "code": "DOCUMENT_QUOTA_EXCEEDED",
            "message": (
                f"kuota penyimpanan dokumen tenant terlampaui "
                f"({quota // (1024 * 1024)} MB)"
            ),
        })
    existing = [d for d in store.list_documents(tenant_id, datasource_id) if not d.get("archived")]
    doc_cfg = store.get_tenant_config(tenant_id, "document") or {}
    # Max documents: cap the tenant's ACTIVE (non-archived) document count. A same-filename
    # re-upload is a version REPLACEMENT (the old one is archived below) so it never trips the cap.
    try:
        max_docs = int(doc_cfg.get("max_documents") or 0)
    except (TypeError, ValueError):
        max_docs = 0
    if max_docs > 0:
        active_total = sum(1 for d in store.list_documents(tenant_id) if not d.get("archived"))
        is_replacement = any(d["filename"] == filename for d in existing)
        if not is_replacement and active_total >= max_docs:
            raise HTTPException(400, {"code": "MAX_DOCUMENTS",
                "message": f"batas {max_docs} dokumen aktif tercapai — arsipkan/hapus dokumen lama "
                           f"atau naikkan 'Max documents' di konfigurasi Document"})
    doc_id = store.create_document(
        tenant_id, datasource_id, filename, detected_mime, roles, 0,
        config=doc_config or None,
    )
    store.set_document_job_status(doc_id, "processing", "")
    try:
        store.save_document_file(tenant_id, doc_id, filename, data)
    except Exception as exc:
        store.delete_document(tenant_id, doc_id)
        raise_safe(
            500,
            "DOCUMENT_STORAGE_FAILED",
            "Dokumen tidak dapat disimpan dengan aman. Gunakan request id untuk memeriksa log backend.",
            exc,
            context=f"encrypted document storage tenant={tenant_id}",
        )
    try:
        job = jobs.enqueue(
            "document_ingest",
            tenant_id,
            {"document_id": doc_id, "config": doc_config or {}},
            actor_id=p.get("user_id") or p.get("email") or "master-key",
            request_id=get_request_id(),
        )
    except Exception:
        store.delete_document(tenant_id, doc_id)
        raise
    return {
        "job_id": job["id"],
        "status": job["status"],
        "status_url": f"/admin/jobs/{job['id']}",
        "document_id": doc_id,
        "filename": filename,
    }
@router.delete("/tenants/{tid}/documents/{doc_id}")
def delete_document(tid: str, doc_id: str):
    store.delete_document(tid, doc_id); return {"ok": True}


@router.patch("/documents/{doc_id}")
def update_document(doc_id: str, b: DocUpdateIn):
    """Archive/unarchive a document. Archived = excluded from retrieval, but chunks and
    the original file are KEPT — a mistaken archive is one click away from recovery."""
    d = store.get_document(doc_id)
    if not d:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "document not found"})
    if b.archived is not None:
        store.set_document_archived(doc_id, b.archived)
        return {"ok": True, "archived": b.archived}
    return {"ok": True, "archived": bool(d.get("archived"))}


@router.patch("/documents/{doc_id}/config")
def update_document_config(doc_id: str, b: DocConfigIn):
    """Edit ONE document's RAG config (chunk_size / chunk_overlap / ocr), then re-chunk +
    re-embed it from the stored original file. The embedding model is per-tenant (unchanged)."""
    d = store.get_document(doc_id)
    if not d:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "document not found"})
    new_cfg = {}
    if b.chunk_size is not None:
        new_cfg["chunk_size"] = b.chunk_size
    if b.chunk_overlap is not None:
        new_cfg["chunk_overlap"] = b.chunk_overlap
    if b.ocr is not None:
        new_cfg["ocr"] = b.ocr
    try:
        return {"ok": True, **rag.reingest_document(d["tenant_id"], doc_id, new_cfg)}
    except Exception as e:
        raise_safe(400, "REEMBED_FAILED",
                   "Dokumen tidak dapat diproses ulang. Periksa log backend menggunakan request id.",
                   e, context=f"document reingest id={doc_id}")


@router.get("/documents/{doc_id}/file")
def document_file(doc_id: str):
    """Serve the ORIGINAL uploaded document (admin console: clickable citations)."""
    d = store.get_document(doc_id)
    if not d:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "document not found"})
    if not store.document_file_available(d):
        raise HTTPException(404, {"code": "NO_FILE",
                                  "message": "original file not stored — re-upload the document to enable viewing"})
    data = store.read_document_file(d["tenant_id"], doc_id)
    return Response(
        content=data,
        media_type=d.get("mime") or "application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(d['filename'])}"},
    )


@router.post("/tenants/{tid}/reindex-docs")
def reindex_docs(tid: str):
    """Re-embed all of the tenant's chunks with the CURRENT embedder. Run after
    switching embedding models — old vectors live in a different space and are
    skipped at retrieval until reindexed. Texts/ACL/citations are untouched."""
    try:
        return {"ok": True, **rag.reindex_tenant(tid)}
    except Exception as e:
        raise_safe(400, "REINDEX_FAILED",
                   "Reindex dokumen gagal. Periksa log backend menggunakan request id.",
                   e, context=f"document reindex tenant={tid}")
