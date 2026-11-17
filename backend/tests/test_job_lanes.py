"""Two-lane job queue: reserved interactive workers can never be parked behind batch work.

The live symptom this pins: a chat question sat in "queued, waiting its turn" while every
worker was busy with reindex/enrich jobs — one FIFO for every kind, so widening it with
JOB_WORKERS only widened the same line.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app  # noqa: F401  - initializes the test schema
from app import jobs, store

INTERACTIVE = "lane_test_chat"
BATCH = "lane_test_reindex"
ADMIN = {"X-Admin-Key": "admin-dev-key"}


@pytest.fixture
def lanes(monkeypatch):
    """Two throwaway kinds, one declared interactive, and a clock that ticks one second
    per call so enqueue order IS `created` order (the real clock only has seconds)."""
    ran = []
    jobs.register(INTERACTIVE, lambda job, payload, report, emit: ran.append(("i", payload["n"])))
    jobs.register(BATCH, lambda job, payload, report, emit: ran.append(("b", payload["n"])))
    monkeypatch.setattr(jobs, "INTERACTIVE_KINDS", frozenset({INTERACTIVE}))
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ticks = iter(range(1, 10_000))
    monkeypatch.setattr(
        store, "now",
        lambda: (base + timedelta(seconds=next(ticks))).strftime(store._TS_FMT),
    )
    return store.create_tenant("Lane test"), ran


def _drain(lane):
    n = 0
    while jobs.run_one(f"lane-{lane}", lane=lane):
        n += 1
        assert n < 20, "queue never drained"
    return n


def test_reserved_lane_never_takes_batch_work_even_when_it_is_older(lanes):
    tenant, ran = lanes
    batch = jobs.enqueue(BATCH, tenant, {"n": 1})          # older
    jobs.enqueue(INTERACTIVE, tenant, {"n": 2})
    assert _drain(jobs.LANE_INTERACTIVE) == 1
    assert ran == [("i", 2)]
    assert jobs.get(batch["id"])["status"] == "queued"      # left for a batch/any worker
    _drain(jobs.LANE_ANY)


def test_batch_lane_takes_batch_first_then_helps_the_chat_lane(lanes):
    tenant, ran = lanes
    jobs.enqueue(INTERACTIVE, tenant, {"n": 1})            # older
    jobs.enqueue(BATCH, tenant, {"n": 2})
    assert jobs.run_one("w", lane=jobs.LANE_BATCH) is True
    assert ran == [("b", 2)]                                # younger batch row wins its lane
    assert jobs.run_one("w", lane=jobs.LANE_BATCH) is True
    assert ran == [("b", 2), ("i", 1)]                      # nothing batch left -> helps chat
    assert jobs.run_one("w", lane=jobs.LANE_BATCH) is False


def test_single_worker_lane_stays_plain_fifo(lanes):
    tenant, ran = lanes
    jobs.enqueue(INTERACTIVE, tenant, {"n": 1})
    jobs.enqueue(BATCH, tenant, {"n": 2})
    jobs.enqueue(INTERACTIVE, tenant, {"n": 3})
    assert _drain(jobs.LANE_ANY) == 3
    assert ran == [("i", 1), ("b", 2), ("i", 3)]


def test_worker_lane_allocation():
    i, b, a = jobs.LANE_INTERACTIVE, jobs.LANE_BATCH, jobs.LANE_ANY
    assert jobs.worker_lanes(1) == [a]                      # nothing to reserve
    assert jobs.worker_lanes(2) == [i, b]
    assert jobs.worker_lanes(3) == [i, b, b]
    assert jobs.worker_lanes(4) == [i, i, b, b]             # default: half reserved
    assert jobs.worker_lanes(4, reserved=3) == [i, i, i, b]
    assert jobs.worker_lanes(4, reserved=4) == [i, i, i, b] # one general worker always kept
    assert jobs.worker_lanes(4, reserved=0) == [a, a, a, a]
    assert jobs.worker_lanes(99) == [i] * 4 + [b] * 4       # clamped to 8 like before


def test_monitoring_reports_the_worker_split(monkeypatch):
    monkeypatch.setattr(jobs, "_lanes", jobs.worker_lanes(4))
    mon = TestClient(app).get("/admin/monitoring", headers=ADMIN).json()
    assert mon["job_workers"] == {"total": 4, "interactive": 2, "batch": 2, "any": 0}
