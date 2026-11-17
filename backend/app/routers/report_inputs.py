"""Consumer API for report inputs: exports a person attaches for their MBR reports.

The files arrive in chunks (a 100 MB zip passes any proxy body limit that way), are kept
encrypted for a few hours after the upload (the person may generate the report again with
them meanwhile), then purged. Only the app key and end user who created an input can add
to it, close it, delete it or name it in a report request.
"""
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .. import store
from ..auth import end_user_identity, require_api_key
from ..reports import inputs
from .consumer import _effective, rate_limited

router = APIRouter(prefix="/v1", tags=["consumer API (per API key + role)"])


def _fail(exc: inputs.InputError):
    raise HTTPException(exc.status, {"code": exc.code, "message": exc.message})


@router.post("/reports/inputs", status_code=201)
def create_input(p=Depends(rate_limited), eu=Depends(end_user_identity)):
    pr, user_id = _effective(p, eu)
    if not inputs.make_room(pr["tenant_id"], pr["key"], user_id):
        _fail(inputs.InputError(429, "TOO_MANY_INPUTS",
                                "Too many attached file sets are waiting; generate a report with "
                                "them or remove them first."))
    row = store.create_report_input(pr["tenant_id"], pr["key"], user_id, inputs.TTL_SEC)
    return {"input_id": row["id"], "expires_at": inputs.iso(row["expires"]), **inputs.limits()}


def _store_chunk(p, eu, input_id, file_no, chunk_no, name, body: bytes) -> dict:
    pr, user_id = _effective(p, eu)
    try:
        row = inputs.owned(pr["tenant_id"], input_id, pr["key"], user_id)
        inputs.check_chunk(row, file_no, chunk_no, name, len(body))
    except inputs.InputError as exc:
        _fail(exc)
    store.add_report_input_part(pr["tenant_id"], input_id, file_no, chunk_no, name, body)
    return {"ok": True, "file_no": file_no, "chunk_no": chunk_no, "size": len(body)}


# Not rate limited per minute like a question: a 100 MB zip is 25 chunks. The input's own
# byte limits and the live-input cap bound what one caller can store.
@router.put("/reports/inputs/{input_id}/files/{file_no}/chunks/{chunk_no}")
async def put_chunk(input_id: str, file_no: int, chunk_no: int, request: Request,
                    p=Depends(require_api_key), eu=Depends(end_user_identity)):
    try:
        name = inputs.clean_name(unquote(request.headers.get("x-file-name") or ""))
        declared = int(request.headers.get("content-length") or 0)
    except inputs.InputError as exc:
        _fail(exc)
    except ValueError:
        _fail(inputs.InputError(400, "BAD_CHUNK", "Content-Length is not a number."))
    if declared > inputs.CHUNK_BYTES:
        _fail(inputs.InputError(413, "INPUT_TOO_LARGE", "A chunk is too large."))
    body = bytearray()
    async for piece in request.stream():
        body += piece
        if len(body) > inputs.CHUNK_BYTES:
            _fail(inputs.InputError(413, "INPUT_TOO_LARGE", "A chunk is too large."))
    return await run_in_threadpool(_store_chunk, p, eu, input_id, file_no, chunk_no, name,
                                   bytes(body))


@router.post("/reports/inputs/{input_id}/close")
def close_input(input_id: str, p=Depends(rate_limited), eu=Depends(end_user_identity)):
    """No more chunks; answers what the attached names look like (the full read happens
    in the report job)."""
    pr, user_id = _effective(p, eu)
    try:
        row = inputs.owned(pr["tenant_id"], input_id, pr["key"], user_id)
    except inputs.InputError as exc:
        _fail(exc)
    if row["status"] == "open":
        if not row["parts"]:
            _fail(inputs.InputError(400, "NO_FILES", "No files were attached."))
        try:
            inputs.check_complete(row)
        except inputs.InputError as exc:
            _fail(exc)
        store.close_report_input(pr["tenant_id"], input_id, inputs.manifest(pr["tenant_id"], row))
        row = store.get_report_input(pr["tenant_id"], input_id)
    return inputs.public(row)


@router.delete("/reports/inputs/{input_id}")
def delete_input(input_id: str, p=Depends(require_api_key), eu=Depends(end_user_identity)):
    pr, user_id = _effective(p, eu)
    try:
        inputs.owned(pr["tenant_id"], input_id, pr["key"], user_id)
    except inputs.InputError as exc:
        _fail(exc)
    store.delete_report_input(pr["tenant_id"], input_id)
    return {"ok": True}
