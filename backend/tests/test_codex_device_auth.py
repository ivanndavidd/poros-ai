"""Offline contract tests for the in-console Codex device-login flow."""
import json
import os
import queue
import secrets
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import store
from app.llm.codex_auth import CodexAuthError, CodexDeviceAuthManager, DEVICE_URL
from app.main import app
from app.routers import models_admin


class _FakeStdin:
    def __init__(self):
        self.messages = []

    def write(self, value):
        self.messages.append(json.loads(value))

    def flush(self):
        # The fake stream records complete JSON lines synchronously; no buffer exists.
        pass


class _FakeStdout:
    def __init__(self, *messages):
        self.lines = queue.Queue()
        for message in messages:
            self.feed(message)

    def feed(self, message):
        self.lines.put(json.dumps(message) + "\n")

    def close(self):
        self.lines.put(None)

    def readline(self):
        return self.lines.get()


class _FakeProcess:
    def __init__(self, *messages):
        self.stdin = _FakeStdin()
        self.stdout = _FakeStdout(*messages)
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15
        self.stdout.close()

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self.returncode


def _login_response(url=DEVICE_URL):
    return {
        "id": 4,
        "result": {
            "type": "chatgptDeviceCode",
            "loginId": "login-123",
            "verificationUrl": url,
            "userCode": "ABCD-12345",
        },
    }


def _manager(process, *, logged_in=False):
    def run(command, **_kwargs):
        if command[-1] == "status":
            return SimpleNamespace(
                returncode=0 if logged_in else 1,
                stdout="Logged in using ChatGPT" if logged_in else "Not logged in",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="Logged out", stderr="")

    return CodexDeviceAuthManager(
        which=lambda _name: "/usr/local/bin/codex",
        popen=lambda *_args, **_kwargs: process,
        run=run,
        start_timeout_sec=0.5,
        login_timeout_sec=10,
    )


def _wait_for(manager, phase):
    deadline = time.time() + 1
    while time.time() < deadline:
        if manager.snapshot(include_device_code=True)["phase"] == phase:
            return
        time.sleep(0.01)
    raise AssertionError(f"Codex login did not reach phase {phase}")


def test_device_login_uses_structured_app_server_rpc_and_completes():
    process = _FakeProcess({"id": 0, "result": {}}, _login_response())
    manager = _manager(process)

    started = manager.start()
    assert started["phase"] == "waiting_for_user"
    assert started["verification_url"] == DEVICE_URL
    assert started["user_code"] == "ABCD-12345"
    sent = process.stdin.messages
    assert [message["method"] for message in sent] == [
        "initialize", "initialized", "account/login/start",
    ]
    assert sent[-1]["params"] == {"type": "chatgptDeviceCode"}

    process.stdout.feed({
        "method": "account/login/completed",
        "params": {"loginId": "login-123", "success": True, "error": None},
    })
    _wait_for(manager, "connected")
    assert manager.snapshot(include_device_code=True)["user_code"] == ""


def test_device_login_cancel_sends_login_id_without_exposing_credentials():
    process = _FakeProcess(_login_response())
    manager = _manager(process)
    manager.start()

    cancelled = manager.cancel()
    assert cancelled["phase"] == "cancelled"
    assert process.stdin.messages[-1] == {
        "method": "account/login/cancel",
        "id": 5,
        "params": {"loginId": "login-123"},
    }
    process.stdout.feed({
        "method": "account/login/completed",
        "params": {"loginId": "login-123", "success": False, "error": "cancelled"},
    })


def test_device_login_rejects_untrusted_verification_url():
    process = _FakeProcess(_login_response("https://attacker.example/device"))
    manager = _manager(process)
    with pytest.raises(CodexAuthError) as caught:
        manager.start()
    assert caught.value.code == "CODEX_DEVICE_LOGIN_FAILED"
    assert manager.snapshot(include_device_code=True)["phase"] == "failed"


def test_status_payload_hides_pending_code_from_tenant_manager(monkeypatch):
    class FakeManager:
        @staticmethod
        def auth_status():
            return {"installed": True, "logged_in": False, "status": "Not logged in"}

        @staticmethod
        def snapshot(*, include_device_code):
            return {
                "phase": "waiting_for_user",
                "user_code": "ABCD-12345" if include_device_code else "",
                "login_id": "login-123" if include_device_code else "",
            }

    monkeypatch.setattr(models_admin, "codex_auth_manager", FakeManager())
    tenant_view = models_admin._codex_auth_payload({"role": "tenant_manager"})
    admin_view = models_admin._codex_auth_payload({"role": "admin"})

    assert tenant_view["can_manage"] is False
    assert tenant_view["login"]["user_code"] == ""
    assert tenant_view["login"]["login_id"] == ""
    assert admin_view["login"]["user_code"] == "ABCD-12345"


def test_logout_uses_codex_cli_and_reports_disconnected():
    process = _FakeProcess()
    manager = _manager(process)
    result = manager.logout()
    assert result["installed"] is True
    assert result["logged_in"] is False


def test_start_route_is_platform_admin_only(monkeypatch):
    class FakeManager:
        starts = 0

        @staticmethod
        def auth_status():
            return {"installed": True, "logged_in": False, "status": "Not logged in"}

        @staticmethod
        def snapshot(*, include_device_code):
            return {
                "phase": "waiting_for_user",
                "verification_url": DEVICE_URL,
                "user_code": "ABCD-12345" if include_device_code else "",
                "login_id": "login-123" if include_device_code else "",
            }

        def start(self):
            self.starts += 1
            return self.snapshot(include_device_code=True)

    fake = FakeManager()
    monkeypatch.setattr(models_admin, "codex_auth_manager", fake)
    client = TestClient(app)
    admin_headers = {"X-Admin-Key": os.environ["ADMIN_KEY"]}
    test_password = secrets.token_urlsafe(18)

    status = client.get("/admin/codex-auth/status", headers=admin_headers)
    assert status.status_code == 200
    assert status.headers["cache-control"] == "no-store"

    admin = client.post("/admin/codex-auth/device/start", headers=admin_headers, json={})
    assert admin.status_code == 200
    assert admin.json()["login"]["user_code"] == "ABCD-12345"
    assert fake.starts == 1

    created = client.post("/auth/users", headers=admin_headers, json={
        "email": "codex-manager@test.local",
        "name": "Codex Manager",
        "role": "tenant_manager",
        "password": test_password,
        "provider": "local",
    })
    assert created.status_code == 200
    user_id = created.json()["id"]
    try:
        login = client.post("/auth/login", json={
            "email": "codex-manager@test.local",
            "password": test_password,
        })
        assert login.status_code == 200
        denied = client.post("/admin/codex-auth/device/start", json={})
        assert denied.status_code == 403
        assert denied.json()["detail"]["code"] == "ADMIN_ONLY"
        assert fake.starts == 1
    finally:
        client.cookies.clear()
        store.delete_console_user(user_id)
