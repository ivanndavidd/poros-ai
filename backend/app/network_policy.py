"""Outbound URL policy for tenant-configurable model endpoints.

Blank api_base values use the SDK/provider's built-in official endpoint and do not
need validation. Custom endpoints are checked both when saved and immediately before
use. Cloud metadata targets are always blocked. Production requires an exact
allowlist; development keeps loopback endpoints usable for Ollama/local gateways.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit, urlunsplit


class UnsafeAPIBase(ValueError):
    pass


_METADATA_HOSTS = {
    "metadata.google.internal",
    "metadata.google",
    "metadata.aws.internal",
    "instance-data",
}
_METADATA_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("100.100.100.200"),
    ipaddress.ip_address("fd00:ec2::254"),
}


def _truthy(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or "").strip().lower() in {"1", "true", "yes", "on"}


def _production() -> bool:
    # Single source of truth: absent/unrecognised APP_ENV means HARDENED (runtime_config).
    from .runtime_config import production
    return production()


def _origin(value: str) -> tuple[str, str, int, str]:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise UnsafeAPIBase("api_base must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise UnsafeAPIBase("api_base must not contain URL credentials")
    if parsed.fragment:
        raise UnsafeAPIBase("api_base must not contain a URL fragment")
    host = parsed.hostname.rstrip(".").lower()
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise UnsafeAPIBase("api_base has an invalid port") from exc
    netloc = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    normalized = urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))
    return host, parsed.scheme.lower(), port, normalized


def _allowlist() -> set[str]:
    out = set()
    for raw in (os.getenv("LLM_API_BASE_ALLOWLIST") or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            _host, _scheme, _port, normalized = _origin(raw)
            out.add(normalized)
        except UnsafeAPIBase:
            # A bare hostname is intentionally broader than a URL entry. URL
            # entries remain exact by scheme + host + port.
            out.add(raw.rstrip("/").lower())
    return out


def _addresses(host: str, port: int) -> set[ipaddress._BaseAddress]:
    try:
        direct = ipaddress.ip_address(host)
        return {direct}
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeAPIBase("api_base hostname cannot be resolved") from exc
    result = set()
    for info in infos:
        try:
            result.add(ipaddress.ip_address(info[4][0]))
        except (ValueError, IndexError):
            continue
    if not result:
        raise UnsafeAPIBase("api_base hostname has no usable address")
    return result


def _private(address: ipaddress._BaseAddress) -> bool:
    return bool(
        address.is_private or address.is_loopback or address.is_link_local
        or address.is_reserved or address.is_unspecified or address.is_multicast
    )


def validate_api_base(value: str, *, purpose: str = "LLM api_base") -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    host, scheme, _port, normalized = _origin(value)
    if host in _METADATA_HOSTS:
        raise UnsafeAPIBase(f"{purpose} targets a blocked metadata host")
    addresses = _addresses(host, _port)
    if any(address in _METADATA_IPS for address in addresses):
        raise UnsafeAPIBase(f"{purpose} targets a blocked metadata address")

    allowlist = _allowlist()
    listed = normalized in allowlist or host in allowlist
    private = any(_private(address) for address in addresses)
    loopback = all(address.is_loopback for address in addresses)

    if _production():
        if not listed:
            raise UnsafeAPIBase(
                f"{purpose} is not in LLM_API_BASE_ALLOWLIST"
            )
        if private and not _truthy("LLM_ALLOW_PRIVATE_BASE"):
            raise UnsafeAPIBase(
                f"{purpose} resolves to a private address; set LLM_ALLOW_PRIVATE_BASE=1"
            )
        if scheme != "https" and not _truthy("LLM_ALLOW_INSECURE_BASE"):
            raise UnsafeAPIBase(
                f"{purpose} must use HTTPS in production"
            )
    else:
        # Local Ollama/OpenAI-compatible gateways remain zero-config in development.
        # Other RFC1918 targets require an explicit opt-in or exact allowlist.
        if private and not loopback and not listed and not _truthy("LLM_ALLOW_PRIVATE_BASE"):
            raise UnsafeAPIBase(
                f"{purpose} resolves to a private address; allowlist it or set "
                "LLM_ALLOW_PRIVATE_BASE=1 for development"
            )
    return value.rstrip("/")
