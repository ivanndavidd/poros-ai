"""Persistent background jobs for operations that may outlive an HTTP gateway request.

Every public request only validates/enqueues and returns a job id. Workers claim jobs
with ``FOR UPDATE SKIP LOCKED``, so multiple Poros replicas can safely share the same
PostgreSQL queue. Payloads are encrypted with the configured secrets backend because
datasource jobs may contain credentials. Results and progress events never contain
credentials.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from typing import Callable

from . import secrets_util as sec
from . import store
from .context import (
    PipelineCancelled,
    set_budget_reservation,
    set_cancellation,
    set_request_id,
    set_tenant_db_scope,
)

_log = logging.getLogger("poros.jobs")
_handlers: dict[str, Callable] = {}
_started = False
_start_lock = threading.Lock()
_local_cancel: dict[str, threading.Event] = {}
_cancel_lock = threading.Lock()
_cleanup_lock = threading.Lock()
_last_cleanup = 0.0

# Two lanes, one queue. A person is waiting on interactive kinds in a chat window;
# batch kinds (ingest, reindex, rescan, enrich) run for minutes with nobody watching.
# Reserved interactive workers never touch batch work, so a reindex can never park a
# question behind it — the live "queued, waiting its turn" that raising JOB_WORKERS
# alone could not fix (a wider FIFO is still one FIFO).
INTERACTIVE_KINDS = frozenset({"chat"})
LANE_ANY = "any"                   # plain FIFO across every kind (single-worker fallback)
LANE_INTERACTIVE = "interactive"   # interactive kinds only
LANE_BATCH = "batch"               # batch kinds first; interactive only when none is queued
_lanes: list[str] = []             # lanes of the workers this process started
# Set when the process shuts down: workers stop claiming, the heartbeat stops, and the
# jobs handed back to the queue (`_released`) are never finished by this process.
_stopping = threading.Event()
_released: set[str] = set()


class JobFailure(Exception):
    """A safe, expected job failure that may be shown to the caller."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def register(kind: str, handler: Callable) -> None:
    _handlers[kind] = handler


def _public(row, *, include_result=True) -> dict | None:
    if not row:
        return None
    out = {
        "id": row["id"],
        "tenant_id": row["tenant_id"],
        "kind": row["kind"],
        "status": row["status"],
        "progress": int(row.get("progress") or 0),
        # a cancel is honoured at the job's next step (or, when its worker died, when the
        # lease runs out): say so meanwhile instead of looking like nothing happened
        "phase": ("cancelling" if row.get("cancel_requested") and row["status"] == "running"
                  else row.get("phase") or ""),
        "cancel_requested": bool(row.get("cancel_requested")),
        "error": (
            {
                "code": row.get("error_code") or "JOB_FAILED",
                "message": row.get("error_message") or "Proses gagal.",
                "request_id": row.get("request_id") or "-",
            }
            if row.get("status") == "failed"
            else None
        ),
        "created": row.get("created"),
        "updated": row.get("updated"),
        "started": row.get("started"),
        "finished": row.get("finished"),
    }
    if include_result and row.get("status") == "succeeded":
        try:
            out["result"] = sec.decrypt_json(row["result"]) if row.get("result") else None
        except Exception:
            _log.exception("cannot decrypt background job result job=%s", row.get("id"))
            out["result"] = None
    return out


def enqueue(kind: str, tenant_id: str, payload: dict, *, actor_id: str = "",
            app_key_id: str = "", request_id: str = "", summary: dict | None = None) -> dict:
    """Persist an encrypted job and return its public representation.

    ``summary`` = a few non-sensitive words a list shows (a report's brand and month). It
    is stored in the clear and outlives the payload, which is purged when the job ends."""
    if kind not in _handlers:
        from . import job_handlers
        job_handlers.register_all()
    if kind not in _handlers:
        raise RuntimeError(f"background job handler is not registered: {kind}")
    job_id = "job_" + uuid.uuid4().hex
    ts = store.now()
    encrypted = sec.encrypt_json(payload or {})
    c = store._c()
    try:
        c.execute(
            """INSERT INTO operation_jobs(
                 id,tenant_id,kind,status,progress,phase,payload,result,
                 error_code,error_message,actor_id,app_key_id,request_id,
                 cancel_requested,lease_owner,lease_until,created,updated,started,finished,
                 summary
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job_id, tenant_id, kind, "queued", 0, "queued", encrypted, None,
                None, None, actor_id or "", app_key_id or "", request_id or "",
                0, None, None, ts, ts, None, None,
                json.dumps(summary) if summary else None,
            ),
        )
        c.commit()
    except Exception:
        sec.purge(encrypted)
        raise
    finally:
        c.close()
    return {
        "id": job_id,
        "tenant_id": tenant_id,
        "kind": kind,
        "status": "queued",
        "progress": 0,
        "phase": "queued",
        "error": None,
        "created": ts,
        "updated": ts,
        "started": None,
        "finished": None,
    }


def get(job_id: str) -> dict | None:
    c = store._c()
    row = c.execute("SELECT * FROM operation_jobs WHERE id=?", (job_id,)).fetchone()
    c.close()
    return dict(row) if row else None


def public(job_id: str, *, include_result=True) -> dict | None:
    return _public(get(job_id), include_result=include_result)


def list_for_actor(kind: str, tenant_id: str, app_key_id: str, actor_id: str,
                   limit: int = 20) -> list:
    """Newest jobs of one kind that THIS app key + end user started, each with its
    decrypted payload under ``params`` while it is still queued or running (finishing
    purges the payload; a succeeded job carries its own ``result``). Someone else's
    jobs never appear."""
    c = store._c()
    try:
        rows = c.execute(
            """SELECT * FROM operation_jobs WHERE kind=? AND tenant_id=? AND app_key_id=?
               AND actor_id=? ORDER BY created DESC LIMIT ?""",
            (kind, tenant_id, app_key_id or "", actor_id or "", max(1, min(int(limit), 100))),
        ).fetchall()
    finally:
        c.close()
    out = []
    for row in rows:
        row = dict(row)
        item = _public(row)
        params = {}
        try:                     # the payload is purged when a job finishes
            params = sec.decrypt_json(row["payload"]) if row.get("payload") else {}
        except Exception:
            _log.exception("cannot decrypt background job payload job=%s", row.get("id"))
        item["params"] = params or {}
        try:
            item["summary"] = json.loads(row["summary"]) if row.get("summary") else {}
        except (TypeError, ValueError):
            item["summary"] = {}
        out.append(item)
    return out


def events(job_id: str, after: int = 0, limit: int = 200) -> list:
    c = store._c()
    rows = c.execute(
        """SELECT seq,event,created FROM operation_job_events
           WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?""",
        (job_id, max(0, int(after or 0)), max(1, min(int(limit or 200), 500))),
    ).fetchall()
    c.close()
    out = []
    for row in rows:
        try:
            event = json.loads(row["event"])
        except (TypeError, ValueError):
            continue
        event["seq"] = int(row["seq"])
        out.append(event)
    return out


def emit(job_id: str, event: dict) -> None:
    """Append one bounded progress event and refresh the worker lease."""
    safe = dict(event or {})
    safe.pop("result", None)  # final result lives once in operation_jobs
    c = store._c()
    c.execute(
        "INSERT INTO operation_job_events(job_id,tenant_id,event,created) "
        "SELECT id,tenant_id,?,? FROM operation_jobs WHERE id=?",
        (json.dumps(safe, default=str), store.now(), job_id),
    )
    c.execute(
        "UPDATE operation_jobs SET updated=?,lease_until=? WHERE id=? AND status='running'",
        (store.now(), time.time() + _lease_seconds(), job_id),
    )
    # Keep polling payloads bounded even for very long research loops.
    c.execute(
        """DELETE FROM operation_job_events WHERE job_id=? AND seq NOT IN
           (SELECT seq FROM operation_job_events WHERE job_id=? ORDER BY seq DESC LIMIT 500)""",
        (job_id, job_id),
    )
    c.commit()
    c.close()


def progress(job_id: str, value: int, phase: str, detail: str = "") -> None:
    value = max(0, min(int(value), 99))
    c = store._c()
    c.execute(
        """UPDATE operation_jobs SET progress=?,phase=?,updated=?,lease_until=?
           WHERE id=? AND status='running'""",
        (value, phase or "", store.now(), time.time() + _lease_seconds(), job_id),
    )
    c.commit()
    c.close()
    emit(job_id, {"type": "status", "progress": value, "phase": phase, "text": detail or phase})


def request_cancel(job_id: str) -> bool:
    c = store._c()
    before = c.execute(
        "SELECT status,payload FROM operation_jobs WHERE id=? FOR UPDATE", (job_id,)
    ).fetchone()
    cur = c.execute(
        """UPDATE operation_jobs SET cancel_requested=1,
                  status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,
                  phase=CASE WHEN status='queued' THEN 'cancelled' ELSE phase END,
                  payload=CASE WHEN status='queued' THEN NULL ELSE payload END,
                  finished=CASE WHEN status='queued' THEN ? ELSE finished END,
                  updated=?
           WHERE id=? AND status IN ('queued','running')""",
        (store.now(), store.now(), job_id),
    )
    changed = cur.rowcount > 0
    c.commit()
    c.close()
    if changed and before and before["status"] == "queued" and before["payload"]:
        sec.purge(before["payload"])
    with _cancel_lock:
        event = _local_cancel.get(job_id)
        if event:
            event.set()
    return changed


def _cancel_requested(job_id: str) -> bool:
    c = store._c()
    row = c.execute(
        "SELECT cancel_requested,status FROM operation_jobs WHERE id=?", (job_id,)
    ).fetchone()
    c.close()
    return bool(row and (row["cancel_requested"] or row["status"] == "cancelled"))


class _PersistentCancelEvent:
    """Event facade used by the existing cooperative cancellation ContextVar."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.local = threading.Event()
        self._last_check = 0.0
        self._cached = False

    def set(self):
        self.local.set()

    def is_set(self):
        if self.local.is_set():
            return True
        now = time.monotonic()
        if now - self._last_check >= 0.75:
            self._last_check = now
            self._cached = _cancel_requested(self.job_id)
        return self._cached


def _lease_seconds() -> int:
    """How long a running job stays claimed without a sign of life. The heartbeat renews
    it for every job this process runs, so it only bounds how long a job whose pod died
    hard (OOM, node loss) waits before another worker picks it up."""
    try:
        return max(60, int(os.getenv("JOB_LEASE_SEC", "300")))
    except ValueError:
        return 300


def _heartbeat_every() -> float:
    return float(min(60, max(10, _lease_seconds() // 4)))


def heartbeat_once() -> int:
    """Renew the lease of every job this process is running right now. A single step can
    outlast the lease (a long model call, a slow warehouse query) and still not look
    abandoned, because liveness no longer depends on the job reporting progress."""
    with _cancel_lock:
        ids = list(_local_cancel)
    if not ids:
        return 0
    c = store._c()
    try:
        marks = ",".join("?" * len(ids))
        cur = c.execute(
            f"UPDATE operation_jobs SET lease_until=? WHERE status='running' AND id IN ({marks})",
            (time.time() + _lease_seconds(), *ids),
        )
        c.commit()
        return cur.rowcount
    finally:
        c.close()


def _heartbeat() -> None:
    while not _stopping.wait(_heartbeat_every()):
        try:
            heartbeat_once()
        except Exception:
            _log.exception("background job heartbeat failed")


def release_running() -> int:
    """Hand this process's running jobs back to the queue: called on shutdown (SIGTERM
    from a rollout or an autoscaler eviction), so another replica picks them up within a
    second instead of after the lease. They restart from the beginning; every job writes
    its result idempotently by job id."""
    _stopping.set()
    with _cancel_lock:
        ids = list(_local_cancel)
        _released.update(ids)
    if not ids:
        return 0
    c = store._c()
    try:
        marks = ",".join("?" * len(ids))
        cur = c.execute(
            f"""UPDATE operation_jobs SET status='queued',phase='queued',progress=0,
                       lease_owner=NULL,lease_until=NULL,updated=?
                WHERE status='running' AND id IN ({marks})""",
            (store.now(), *ids),
        )
        c.commit()
        released = cur.rowcount
    finally:
        c.close()
    _log.warning("released %s running job(s) on shutdown: %s", released, ", ".join(ids))
    return released


def _cleanup_completed() -> None:
    """Bound result/event storage; secret references are purged with their rows."""
    global _last_cleanup
    now_monotonic = time.monotonic()
    if now_monotonic - _last_cleanup < 3600:
        return
    with _cleanup_lock:
        if now_monotonic - _last_cleanup < 3600:
            return
        try:
            hours = max(1, int(os.getenv("JOB_RETENTION_HOURS", "168")))
        except ValueError:
            hours = 168
        cutoff = store.ts_after(-hours * 3600)
        c = store._c()
        rows = c.execute(
            """SELECT id,payload,result FROM operation_jobs
               WHERE status IN ('succeeded','failed','cancelled')
                 AND finished IS NOT NULL AND finished<?""",
            (cutoff,),
        ).fetchall()
        ids = [row["id"] for row in rows]
        for job_id in ids:
            c.execute("DELETE FROM operation_job_events WHERE job_id=?", (job_id,))
            c.execute("DELETE FROM operation_jobs WHERE id=?", (job_id,))
        c.commit()
        c.close()
        for row in rows:
            if row["payload"]:
                sec.purge(row["payload"])
            if row["result"]:
                sec.purge(row["result"])
        _last_cleanup = now_monotonic


def _lane_select(lane: str) -> tuple[str, tuple]:
    """The one-row pick for a lane as (sql, params) — FIFO within what the lane may take."""
    kinds = tuple(sorted(INTERACTIVE_KINDS))
    marks = ",".join("?" * len(kinds))
    base = "SELECT id FROM operation_jobs WHERE status='queued' AND cancel_requested=0"
    if lane == LANE_INTERACTIVE:
        return (f"{base} AND kind IN ({marks}) ORDER BY created "
                "FOR UPDATE SKIP LOCKED LIMIT 1", kinds)
    if lane == LANE_BATCH:
        return (f"{base} ORDER BY CASE WHEN kind IN ({marks}) THEN 1 ELSE 0 END, created "
                "FOR UPDATE SKIP LOCKED LIMIT 1", kinds)
    return f"{base} ORDER BY created FOR UPDATE SKIP LOCKED LIMIT 1", ()


def _claim(worker_id: str, lane: str = LANE_ANY) -> dict | None:
    c = store._c()
    abandoned = []                      # payload refs to purge once their rows committed
    try:
        # A cancel asked for work whose pod died has no worker left to honour it, and the
        # queue never claims a cancel-requested row: finish it as cancelled instead of
        # requeueing it into a "queued" that lasts forever.
        gone = c.execute(
            """WITH gone AS (
                   SELECT id, payload FROM operation_jobs
                    WHERE cancel_requested=1 AND (status='queued' OR (
                          status='running' AND lease_until IS NOT NULL AND lease_until<?))
                    FOR UPDATE SKIP LOCKED)
               UPDATE operation_jobs o SET status='cancelled',phase='cancelled',payload=NULL,
                      error_code='CANCELLED',error_message='Proses dibatalkan.',
                      lease_owner=NULL,lease_until=NULL,updated=?,finished=?
                 FROM gone WHERE o.id=gone.id
               RETURNING gone.payload AS payload""",
            (time.time(), store.now(), store.now()),
        ).fetchall()
        c.commit()
        abandoned = [row["payload"] for row in gone if row["payload"]]
        # Reclaim work abandoned by a terminated pod, then lock one queued row.
        c.execute(
            """UPDATE operation_jobs SET status='queued',phase='queued',
                      lease_owner=NULL,lease_until=NULL,updated=?
               WHERE status='running' AND lease_until IS NOT NULL AND lease_until<?""",
            (store.now(), time.time()),
        )
        sql, params = _lane_select(lane)
        row = c.execute(sql, params).fetchone()
        if not row:
            c.commit()
            return None
        ts = store.now()
        claimed = c.execute(
            """UPDATE operation_jobs SET status='running',progress=1,phase='starting',
                      lease_owner=?,lease_until=?,started=COALESCE(started,?),updated=?
               WHERE id=? RETURNING *""",
            (worker_id, time.time() + _lease_seconds(), ts, ts, row["id"]),
        ).fetchone()
        c.commit()
        return dict(claimed)
    finally:
        c.close()
        for ref in abandoned:
            sec.purge(ref)


def _finish(job: dict, status: str, *, result=None, code="", message="") -> None:
    payload_ref = job.get("payload")
    result_ref = None
    if status == "succeeded":
        serializable = json.loads(json.dumps(result, default=str))
        result_ref = sec.encrypt_json(serializable)
    c = store._c()
    try:
        c.execute(
            """UPDATE operation_jobs SET status=?,
                      progress=CASE WHEN ?='succeeded' THEN 100 ELSE progress END,
                      phase=?,result=?,
                      error_code=?,error_message=?,payload=NULL,lease_owner=NULL,
                      lease_until=NULL,updated=?,finished=?
               WHERE id=?""",
            (
                status,
                status,
                "completed" if status == "succeeded" else status,
                result_ref,
                code or None,
                message or None,
                store.now(),
                store.now(),
                job["id"],
            ),
        )
        c.commit()
    except Exception:
        if result_ref:
            sec.purge(result_ref)
        raise
    finally:
        c.close()
    if payload_ref:
        sec.purge(payload_ref)


def _job_callbacks(job: dict, event):
    """(report, publish) for a handler: both go quiet once the job was handed back on
    shutdown, and both stop the handler with PipelineCancelled once it was cancelled."""
    def report(value: int, phase: str, detail: str = ""):
        if job["id"] in _released:
            return
        if event.is_set():
            raise PipelineCancelled(job["id"])
        progress(job["id"], value, phase, detail)

    def publish(item: dict):
        if job["id"] in _released:
            return
        if event.is_set():
            raise PipelineCancelled(job["id"])
        emit(job["id"], item)

    return report, publish


def _log_expected_failure(job: dict, exc: "JobFailure") -> None:
    if exc.__cause__ is None:
        return
    _log.error(
        "background_job_expected_failure job=%s kind=%s rid=%s code=%s",
        job["id"], job["kind"], job.get("request_id") or "-", exc.code,
        exc_info=(
            type(exc.__cause__),
            exc.__cause__,
            exc.__cause__.__traceback__,
        ),
    )


def _run(job: dict) -> None:
    event = _PersistentCancelEvent(job["id"])
    with _cancel_lock:
        _local_cancel[job["id"]] = event.local
    set_tenant_db_scope(job["tenant_id"])
    set_request_id(job.get("request_id") or job["id"])
    set_budget_reservation("")
    set_cancellation(event, job["id"])
    def finish(status: str, **kw):
        # handed back on shutdown: the replica that claims it next writes the outcome
        if job["id"] not in _released:
            _finish(job, status, **kw)

    try:
        if event.is_set():
            finish("cancelled", code="CANCELLED", message="Proses dibatalkan.")
            return
        payload = sec.decrypt_json(job["payload"])
        report, publish = _job_callbacks(job, event)
        result = _handlers[job["kind"]](job, payload, report, publish)
        if event.is_set():
            raise PipelineCancelled(job["id"])
        finish("succeeded", result=result)
    except PipelineCancelled:
        finish("cancelled", code="CANCELLED", message="Proses dibatalkan.")
    except JobFailure as exc:
        _log_expected_failure(job, exc)
        finish("failed", code=exc.code, message=exc.message)
    except Exception as exc:  # keep internal provider/connector details out of polling API
        _log.exception(
            "background_job_failed job=%s kind=%s rid=%s",
            job["id"], job["kind"], job.get("request_id") or "-",
        )
        finish(
            "failed",
            code="JOB_FAILED",
            message="Proses gagal. Gunakan request id untuk memeriksa log backend.",
        )
    finally:
        set_cancellation()
        set_budget_reservation("")
        set_tenant_db_scope("*")
        with _cancel_lock:
            _local_cancel.pop(job["id"], None)
            _released.discard(job["id"])


def run_one(worker_id: str = "inline", lane: str = LANE_ANY) -> bool:
    job = _claim(worker_id, lane)
    if not job:
        return False
    _run(job)
    return True


def _worker(worker_id: str, lane: str) -> None:
    while not _stopping.is_set():
        try:
            _cleanup_completed()
            if not run_one(worker_id, lane):
                threading.Event().wait(0.5)
        except Exception:
            _log.exception("background job worker loop failed worker=%s", worker_id)
            threading.Event().wait(1.0)


def worker_lanes(total: int, reserved: int | None = None) -> list[str]:
    """Lane per worker: `reserved` interactive-only workers (default half), the rest batch-
    first. At least one worker always stays general so batch work cannot starve either,
    and a single worker keeps plain FIFO — reserving it would strand every batch job."""
    total = max(1, min(int(total), 8))
    if reserved is None:
        reserved = total // 2
    reserved = max(0, min(int(reserved), total - 1))
    if reserved == 0:
        return [LANE_ANY] * total
    return [LANE_INTERACTIVE] * reserved + [LANE_BATCH] * (total - reserved)


def worker_summary(lanes: list[str] | None = None) -> dict:
    """How this process split its workers — what the platform env actually produced."""
    lanes = _lanes if lanes is None else lanes
    return {"total": len(lanes), "interactive": lanes.count(LANE_INTERACTIVE),
            "batch": lanes.count(LANE_BATCH), "any": lanes.count(LANE_ANY)}


def _env_int(name: str, default):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def start_workers() -> None:
    global _started, _lanes
    with _start_lock:
        if _started:
            return
        from . import job_handlers
        job_handlers.register_all()
        _lanes = worker_lanes(_env_int("JOB_WORKERS", 2),
                              _env_int("JOB_INTERACTIVE_WORKERS", None))
        # pod name first: every container runs as pid 1, so the pid alone cannot say
        # which replica holds a lease; the lane letter says which lane claimed the row
        host = socket.gethostname()[:63]
        for index, lane in enumerate(_lanes):
            worker_id = f"{host}-{os.getpid()}-{lane[0]}{index}-{uuid.uuid4().hex[:6]}"
            threading.Thread(
                target=_worker,
                args=(worker_id, lane),
                daemon=True,
                name=f"poros-job-{lane}-{index}",
            ).start()
        threading.Thread(target=_heartbeat, daemon=True, name="poros-job-heartbeat").start()
        _started = True
