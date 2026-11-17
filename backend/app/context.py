"""Per-request correlation id (the Apps Gateway `X-Request-Id`, or one minted locally).

Kept in a ContextVar so code deep in the stack — notably the LLM gateway — can tag its
downstream calls with the id WITHOUT threading it through every function signature. Set by
`main._CorrelationIdMiddleware` (pure-ASGI, so the value propagates into the endpoint and
everything it calls synchronously) and copied into background threads (memory capture) via
`contextvars.copy_context()`.
"""
import contextvars
import re

request_id_var = contextvars.ContextVar("request_id", default="")
tenant_db_scope_var = contextvars.ContextVar("tenant_db_scope", default="*")
budget_reservation_var = contextvars.ContextVar("budget_reservation", default="")
cancel_event_var = contextvars.ContextVar("cancel_event", default=None)
cancel_trace_var = contextvars.ContextVar("cancel_trace", default="")
cancel_usage_var = contextvars.ContextVar("cancel_usage", default=None)
# the consumer app's own conversation id for the running chat turn ("" = none sent): every
# audit row of the turn carries it, so a reviewer reads a session as one conversation
chat_session_var = contextvars.ContextVar("chat_session", default="")

_SESSION_ID = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")


def clean_session_id(value) -> str:
    """An app's session id as stored, or "" when it is absent or not a plain id (it is
    display grouping only, never trusted for access, so a bad one is dropped, not refused)."""
    value = str(value or "").strip()
    return value if _SESSION_ID.match(value) else ""


def set_chat_session(session_id: str) -> None:
    chat_session_var.set(clean_session_id(session_id))


def get_chat_session() -> str:
    try:
        return chat_session_var.get() or ""
    except Exception:
        return ""


class PipelineCancelled(BaseException):
    """Cooperative stop signal for a running chat pipeline.

    This intentionally derives from ``BaseException`` so the many best-effort
    ``except Exception`` fallbacks in the analytics pipeline cannot accidentally
    swallow a user's explicit Stop request and continue spending tokens.
    """

    def __init__(self, trace_id: str = "", usage: dict = None):
        super().__init__("chat generation cancelled")
        self.trace_id = trace_id or ""
        self.usage = usage or {
            "model": None,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cost": 0.0,
        }


def set_request_id(rid: str) -> None:
    request_id_var.set(rid or "")


def get_request_id() -> str:
    try:
        return request_id_var.get() or ""
    except Exception:
        return ""


def set_tenant_db_scope(tenant_id: str) -> None:
    tenant_db_scope_var.set(tenant_id or "*")


def get_tenant_db_scope() -> str:
    try:
        return tenant_db_scope_var.get() or "*"
    except Exception:
        return "*"


def set_budget_reservation(reservation_id: str) -> None:
    budget_reservation_var.set(reservation_id or "")


def get_budget_reservation() -> str:
    try:
        return budget_reservation_var.get() or ""
    except Exception:
        return ""


def set_cancellation(event=None, trace_id: str = "", usage=None) -> None:
    """Bind one stream's shared cancellation event to the current context."""
    cancel_event_var.set(event)
    cancel_trace_var.set(trace_id or "")
    cancel_usage_var.set(usage)


def set_cancellation_usage(usage) -> None:
    """Attach the live ``Ctx.usage`` list once the orchestrator creates it."""
    cancel_usage_var.set(usage)


def cancellation_requested() -> bool:
    try:
        event = cancel_event_var.get()
        return bool(event and event.is_set())
    except Exception:
        return False


def _cancelled_usage() -> dict:
    total = {
        "model": None,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost": 0.0,
    }
    try:
        items = cancel_usage_var.get() or []
    except Exception:
        items = []
    for item in items:
        total["model"] = item.get("model") or total["model"]
        total["prompt_tokens"] += int(item.get("prompt_tokens", 0) or 0)
        total["completion_tokens"] += int(item.get("completion_tokens", 0) or 0)
        total["cost"] += float(item.get("cost", 0.0) or 0.0)
    total["cost"] = round(total["cost"], 6)
    return total


def check_cancelled() -> None:
    """Raise at a safe boundary when the client has stopped the stream."""
    if cancellation_requested():
        try:
            trace_id = cancel_trace_var.get() or ""
        except Exception:
            trace_id = ""
        raise PipelineCancelled(trace_id, _cancelled_usage())
