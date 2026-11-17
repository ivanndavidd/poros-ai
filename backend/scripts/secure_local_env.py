"""Create/repair backend/.env without ever printing secret values.

The checked-in .env.example intentionally contains blank secrets. Local launchers run
this helper before importing the app so every developer gets unique credentials instead
of a repository-wide default. Existing custom secrets are preserved.

Use ``--disable-gateway`` during a local security reset to turn off trust in the
X-User-Id header until the backend is actually isolated behind Apps Gateway.
"""
from __future__ import annotations

import argparse
import secrets
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
EXAMPLE_PATH = ROOT / ".env.example"

LEGACY_VALUES = {
    "ADMIN_KEY": {"admin-dev-key"},
    "JWT_SECRET": {"tenantos-dev-jwt-secret-change-me-please-32", "test-secret"},
    "ADMIN_SECRET_KEY": {"tenantos-dev-secret-key-change-me"},
}
GENERATED = ("ADMIN_KEY", "JWT_SECRET", "ADMIN_SECRET_KEY", "API_KEY_PEPPER")


def _parse(lines: list[str]) -> dict[str, int]:
    positions: dict[str, int] = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key:
            positions[key] = i
    return positions


def _value(line: str) -> str:
    return line.split("=", 1)[1].strip() if "=" in line else ""


def _set(lines: list[str], positions: dict[str, int], key: str, value: str) -> None:
    replacement = f"{key}={value}\n"
    if key in positions:
        lines[positions[key]] = replacement
    else:
        positions[key] = len(lines)
        lines.append(replacement)


def secure_local_env(disable_gateway: bool = False) -> list[str]:
    if not ENV_PATH.exists():
        if not EXAMPLE_PATH.exists():
            raise SystemExit("backend/.env.example tidak ditemukan")
        ENV_PATH.write_bytes(EXAMPLE_PATH.read_bytes())

    text = ENV_PATH.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    positions = _parse(lines)
    changed: list[str] = []

    for key in GENERATED:
        current = _value(lines[positions[key]]) if key in positions else ""
        if not current or current in LEGACY_VALUES.get(key, set()):
            _set(lines, positions, key, secrets.token_urlsafe(48))
            changed.append(key)

    if disable_gateway:
        current = (_value(lines[positions["APPS_GATEWAY_TRUST_HEADER"]])
                   if "APPS_GATEWAY_TRUST_HEADER" in positions else "")
        if current.strip().lower() not in {"", "0", "false", "no", "off"}:
            _set(lines, positions, "APPS_GATEWAY_TRUST_HEADER", "0")
            changed.append("APPS_GATEWAY_TRUST_HEADER")

    ENV_PATH.write_text("".join(lines), encoding="utf-8", newline="")
    return changed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--disable-gateway", action="store_true")
    args = parser.parse_args()
    names = secure_local_env(args.disable_gateway)
    if names:
        print("Konfigurasi lokal diamankan:", ", ".join(names), "(nilai tidak ditampilkan)")
    else:
        print("Konfigurasi lokal sudah aman; tidak ada secret yang diubah.")
