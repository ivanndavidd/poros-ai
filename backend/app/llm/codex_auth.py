"""Managed Codex device-code login for the Poros admin console.

The browser never gets a shell and this module never reads ``auth.json``.  It
starts the Codex app-server over stdio, uses its structured account/login RPC,
and returns only the verification URL, one-time user code, and coarse status.
Credentials are written by Codex itself under ``CODEX_HOME``.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from typing import Callable

from .subprocess_env import codex_env


DEVICE_URL = "https://auth.openai.com/codex/device"
_LOGIN_REQUEST_ID = 4
_CANCEL_REQUEST_ID = 5
_ACTIVE_PHASES = {"starting", "waiting_for_user"}


class CodexAuthError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 500):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class CodexDeviceAuthManager:
    """Own the single host-level Codex login attempt for this backend worker."""

    def __init__(
        self,
        *,
        which: Callable[[str], str | None] = shutil.which,
        popen: Callable = subprocess.Popen,
        run: Callable = subprocess.run,
        start_timeout_sec: float = 12,
        login_timeout_sec: float = 16 * 60,
    ):
        self._which = which
        self._popen = popen
        self._run = run
        self._start_timeout_sec = start_timeout_sec
        self._login_timeout_sec = login_timeout_sec
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._process = None
        self._timer = None
        self._state = self._idle_state()

    @staticmethod
    def _idle_state() -> dict:
        return {
            "phase": "idle",
            "verification_url": DEVICE_URL,
            "user_code": "",
            "login_id": "",
            "started_at": None,
            "expires_at": None,
            "message": "",
        }

    def executable(self) -> str | None:
        return self._which("codex")

    def auth_status(self) -> dict:
        exe = self.executable()
        base = {"installed": bool(exe), "logged_in": False}
        if not exe:
            return {**base, "status": "Codex CLI is not installed on the backend host."}
        try:
            result = self._run(
                [exe, "login", "status"],
                text=True,
                capture_output=True,
                timeout=8,
                env=self._codex_env(),
            )
        except Exception:
            return {**base, "status": "Codex login status cannot be read; check backend logs."}
        message = (result.stdout or result.stderr or "").strip()
        logged_in = result.returncode == 0
        if not logged_in:
            with self._lock:
                if self._state["phase"] == "connected":
                    self._state = self._idle_state()
        return {
            **base,
            "logged_in": logged_in,
            "status": message or "Codex login status unavailable.",
        }

    def snapshot(self, *, include_device_code: bool) -> dict:
        with self._lock:
            state = dict(self._state)
        if not include_device_code:
            state["user_code"] = ""
            state["login_id"] = ""
        return state

    def start(self) -> dict:
        exe = self.executable()
        if not exe:
            raise CodexAuthError(
                "CODEX_CLI_NOT_INSTALLED",
                "Codex CLI is not installed in the backend image.",
                503,
            )
        with self._lock:
            if self._state["phase"] in _ACTIVE_PHASES and self._process is not None:
                return dict(self._state)
            self._ready = threading.Event()
            self._state = {
                **self._idle_state(),
                "phase": "starting",
                "started_at": int(time.time()),
                "expires_at": int(time.time() + self._login_timeout_sec),
                "message": "Starting Codex device login...",
            }
            try:
                process = self._popen(
                    [exe, "app-server", "--listen", "stdio://"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=self._codex_env(),
                    start_new_session=os.name != "nt",
                )
            except Exception as exc:
                self._state.update(
                    phase="failed",
                    message="Codex app-server could not be started.",
                )
                raise CodexAuthError(
                    "CODEX_LOGIN_START_FAILED",
                    "Codex app-server could not be started; check backend logs.",
                    502,
                ) from exc
            self._process = process
            worker = threading.Thread(
                target=self._run_login,
                args=(process,),
                name="poros-codex-device-login",
                daemon=True,
            )
            worker.start()
            timer = threading.Timer(self._login_timeout_sec, self._expire, args=(process,))
            timer.daemon = True
            timer.start()
            self._timer = timer

        if not self._ready.wait(self._start_timeout_sec):
            self._fail_start(process)
            raise CodexAuthError(
                "CODEX_DEVICE_CODE_TIMEOUT",
                "Codex did not return a device code in time; check backend connectivity.",
                504,
            )
        state = self.snapshot(include_device_code=True)
        if state["phase"] == "failed":
            raise CodexAuthError("CODEX_DEVICE_LOGIN_FAILED", state["message"], 502)
        return state

    def cancel(self) -> dict:
        with self._lock:
            process = self._process
            login_id = self._state.get("login_id") or ""
            if not process or self._state["phase"] not in _ACTIVE_PHASES:
                return dict(self._state)
            self._state.update(phase="cancelled", message="Device login cancelled.")
        if login_id:
            try:
                self._send(process, {
                    "method": "account/login/cancel",
                    "id": _CANCEL_REQUEST_ID,
                    "params": {"loginId": login_id},
                })
            except Exception:
                self._terminate(process)
        else:
            self._terminate(process)
        return self.snapshot(include_device_code=True)

    def logout(self) -> dict:
        self.cancel()
        exe = self.executable()
        if not exe:
            raise CodexAuthError("CODEX_CLI_NOT_INSTALLED", "Codex CLI is not installed.", 503)
        try:
            result = self._run(
                [exe, "logout"],
                text=True,
                capture_output=True,
                timeout=8,
                env=self._codex_env(),
            )
        except Exception as exc:
            raise CodexAuthError(
                "CODEX_LOGOUT_FAILED",
                "Codex logout failed; check backend logs.",
                502,
            ) from exc
        if result.returncode != 0:
            raise CodexAuthError(
                "CODEX_LOGOUT_FAILED",
                "Codex logout failed; check backend logs.",
                502,
            )
        with self._lock:
            self._state = self._idle_state()
        return self.auth_status()

    @staticmethod
    def _codex_env() -> dict:
        # Was os.environ.copy() despite the name — see subprocess_env for why an
        # allowlist matters even on these fixed-argv, admin-only invocations.
        return codex_env()

    def _run_login(self, process) -> None:
        try:
            self._start_login_protocol(process)
            self._read_login_messages(process)
        except Exception:
            self._set_failed("Codex device login stopped unexpectedly; check backend logs.")
        finally:
            self._ready.set()
            self._cancel_timer()
            self._terminate(process)
            with self._lock:
                if self._process is process:
                    self._process = None
                if self._state["phase"] in _ACTIVE_PHASES:
                    self._state.update(
                        phase="failed",
                        message="Codex device login stopped before completion.",
                    )

    def _start_login_protocol(self, process) -> None:
        self._send(process, {
            "method": "initialize",
            "id": 0,
            "params": {"clientInfo": {
                "name": "poros",
                "title": "Poros",
                "version": "0.2.0",
            }},
        })
        self._send(process, {"method": "initialized", "params": {}})
        self._send(process, {
            "method": "account/login/start",
            "id": _LOGIN_REQUEST_ID,
            "params": {"type": "chatgptDeviceCode"},
        })

    def _read_login_messages(self, process) -> None:
        while True:
            line = process.stdout.readline()
            if not line:
                return
            try:
                message = json.loads(line)
            except (TypeError, ValueError):
                continue
            if self._handle_login_message(message):
                return

    def _handle_login_message(self, message: dict) -> bool:
        if message.get("id") == _LOGIN_REQUEST_ID:
            return not self._accept_device_code(message)
        if message.get("method") == "account/login/completed":
            return self._complete_login(message.get("params") or {})
        return False

    def _accept_device_code(self, message: dict) -> bool:
        result = message.get("result") or {}
        url = result.get("verificationUrl") or ""
        code = result.get("userCode") or ""
        login_id = result.get("loginId") or ""
        if message.get("error") or url != DEVICE_URL or not code or not login_id:
            self._set_failed("Codex did not return a valid device login response.")
            self._ready.set()
            return False
        with self._lock:
            self._state.update(
                phase="waiting_for_user",
                verification_url=url,
                user_code=str(code),
                login_id=str(login_id),
                message="Open the device page and enter the one-time code.",
            )
        self._ready.set()
        return True

    def _complete_login(self, params: dict) -> bool:
        with self._lock:
            current_login = self._state.get("login_id")
            current_phase = self._state["phase"]
        if params.get("loginId") != current_login:
            return False
        if params.get("success"):
            with self._lock:
                self._state.update(
                    phase="connected",
                    user_code="",
                    message="Logged in using ChatGPT.",
                )
        elif current_phase != "cancelled":
            self._set_failed(
                "Codex device login failed. Check the ChatGPT device-login setting."
            )
        return True

    def _expire(self, process) -> None:
        with self._lock:
            if self._process is not process or self._state["phase"] not in _ACTIVE_PHASES:
                return
            self._state.update(
                phase="expired",
                user_code="",
                message="The device code expired. Start a new login.",
            )
        self._terminate(process)

    def _fail_start(self, process) -> None:
        with self._lock:
            if self._process is process and self._state["phase"] in _ACTIVE_PHASES:
                self._state.update(
                    phase="failed",
                    message="Codex did not return a device code in time.",
                )
        self._terminate(process)

    def _set_failed(self, message: str) -> None:
        with self._lock:
            if self._state["phase"] != "cancelled":
                self._state.update(phase="failed", message=message[:240])

    @staticmethod
    def _send(process, message: dict) -> None:
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def _cancel_timer(self) -> None:
        with self._lock:
            timer = self._timer
            self._timer = None
        if timer:
            timer.cancel()

    @staticmethod
    def _terminate(process) -> None:
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


manager = CodexDeviceAuthManager()
