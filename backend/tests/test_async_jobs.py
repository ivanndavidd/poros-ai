"""Persistent job queue: gateway-safe lifecycle, encrypted payloads and cancellation."""
from app.main import app  # noqa: F401  - initializes the test schema
from app import jobs, store
from fastapi.testclient import TestClient

client = TestClient(app)
ADMIN = {"X-Admin-Key": "admin-dev-key"}


def test_job_payload_is_encrypted_and_result_survives_polling():
    tenant_id = store.create_tenant("Async test")
    seen = {}

    def handler(job, payload, report, emit):
        seen.update(payload)
        report(40, "working", "sedang bekerja")
        emit({"type": "finding", "text": "hasil antara"})
        return {"answer": payload["question"]}

    jobs.register("test_persistent", handler)
    created = jobs.enqueue(
        "test_persistent",
        tenant_id,
        {"question": "rahasia-job"},
        actor_id="user-1",
        request_id="req-test-job",
    )
    raw = jobs.get(created["id"])
    assert raw["status"] == "queued"
    assert "rahasia-job" not in raw["payload"]

    assert jobs.run_one("pytest-worker") is True
    done = jobs.public(created["id"])
    assert done["status"] == "succeeded"
    assert done["progress"] == 100
    assert done["result"] == {"answer": "rahasia-job"}
    assert jobs.get(created["id"])["payload"] is None
    assert seen == {"question": "rahasia-job"}
    assert any(event["type"] == "finding" for event in jobs.events(created["id"]))


def test_queued_job_can_be_cancelled_without_running_or_retaining_payload():
    tenant_id = store.create_tenant("Cancel test")
    ran = []
    jobs.register("test_cancel", lambda *_args: ran.append(True))
    created = jobs.enqueue("test_cancel", tenant_id, {"secret": "do-not-retain"})

    assert jobs.request_cancel(created["id"]) is True
    row = jobs.get(created["id"])
    assert row["status"] == "cancelled"
    assert row["payload"] is None
    assert ran == []


def test_admin_datasource_job_contract_returns_202_then_pollable_result():
    tenant_id = store.create_tenant("Async route")
    started = client.post(
        "/admin/jobs/datasources",
        headers=ADMIN,
        json={"tenant_id": tenant_id, "type": "docs", "name": "Docs", "conn": {}},
    )
    assert started.status_code == 202
    job_id = started.json()["job_id"]
    assert jobs.run_one("route-test-worker")
    state = client.get(f"/admin/jobs/{job_id}", headers=ADMIN)
    assert state.status_code == 200
    assert state.json()["status"] == "succeeded"
    assert state.json()["result"]["id"].startswith("ds_")


def test_shutdown_hands_a_running_job_back_and_the_heartbeat_keeps_it_claimed():
    """An autoscaler eviction (SIGTERM) mid-job must not leave the job "running" until the
    lease runs out: the job goes back to the queue with its payload and the next replica
    runs it from the start. The dying run writes nothing after that."""
    tenant_id = store.create_tenant("Release test")
    calls = []

    def handler(job, payload, report, emit):
        calls.append(payload["n"])
        if len(calls) == 1:
            row = jobs.get(job["id"])
            store_lease = row["lease_until"]
            jobs._released.clear()
            assert jobs.heartbeat_once() == 1                  # this process runs it
            assert jobs.get(job["id"])["lease_until"] >= store_lease
            report(55, "querying", "25 queries done")
            assert jobs.release_running() == 1                 # SIGTERM arrives
            report(57, "querying", "the dying run keeps going")
            emit({"type": "status", "text": "ignored"})
        return {"ok": True}

    jobs.register("test_release", handler)
    created = jobs.enqueue("test_release", tenant_id, {"n": 1})
    try:
        assert jobs.run_one("pytest-dying") is True
        row = jobs.get(created["id"])
        assert row["status"] == "queued" and row["progress"] == 0
        assert row["lease_owner"] is None and row["payload"]   # still runnable
        assert jobs._released == set()
    finally:
        jobs._stopping.clear()
    assert jobs.run_one("pytest-next-replica") is True
    assert jobs.public(created["id"])["status"] == "succeeded"
    assert calls == [1, 1]
