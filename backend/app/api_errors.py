"""Safe API error helpers.

Internal exception text is useful in server logs, but it must not be returned to a
consumer or admin browser: connector/provider errors commonly contain hostnames,
SQL fragments, filesystem paths, or credential-adjacent configuration.
"""
from __future__ import annotations

import logging

from fastapi import HTTPException

from .context import get_request_id


_log = logging.getLogger("poros.api")


def log_internal(code: str, exc: Exception, *, context: str = "") -> str:
    request_id = get_request_id() or "-"
    _log.exception(
        "request_failed rid=%s code=%s context=%s",
        request_id,
        code,
        context or "-",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return request_id


def safe_detail(code: str, message: str) -> dict:
    return {
        "code": code,
        "message": message,
        "request_id": get_request_id() or "-",
    }


def raise_safe(status: int, code: str, message: str, exc: Exception,
               *, context: str = ""):
    log_internal(code, exc, context=context)
    raise HTTPException(status, safe_detail(code, message)) from exc
