"""Per-tenant AES-GCM envelope encryption for original uploaded documents."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets


_MAGIC = b"POROSDOC\x01"


def _master_key() -> bytes:
    value = (
        os.getenv("DOCUMENT_ENCRYPTION_KEY")
        or os.getenv("ADMIN_SECRET_KEY")
        or ""
    ).encode()
    if len(value) < 32:
        raise RuntimeError(
            "DOCUMENT_ENCRYPTION_KEY or ADMIN_SECRET_KEY must be at least 32 characters"
        )
    return value


def _tenant_key(tenant_id: str) -> bytes:
    return hmac.new(
        _master_key(),
        b"poros/document/v1/" + str(tenant_id).encode(),
        hashlib.sha256,
    ).digest()


def encrypted(data: bytes) -> bool:
    return bytes(data or b"").startswith(_MAGIC)


def encrypt(data: bytes, tenant_id: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = secrets.token_bytes(12)
    aad = b"tenant:" + str(tenant_id).encode()
    ciphertext = AESGCM(_tenant_key(tenant_id)).encrypt(nonce, bytes(data), aad)
    return _MAGIC + nonce + ciphertext


def decrypt(data: bytes, tenant_id: str) -> bytes:
    """Decrypt an envelope; legacy plaintext is dual-read for safe migration."""
    raw = bytes(data or b"")
    if not encrypted(raw):
        return raw
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = raw[len(_MAGIC):len(_MAGIC) + 12]
    ciphertext = raw[len(_MAGIC) + 12:]
    aad = b"tenant:" + str(tenant_id).encode()
    return AESGCM(_tenant_key(tenant_id)).decrypt(nonce, ciphertext, aad)


def write(path: str, data: bytes, tenant_id: str):
    """Atomically replace a file with its encrypted envelope."""
    payload = encrypt(data, tenant_id)
    tmp = path + ".encrypting"
    with open(tmp, "wb") as target:
        target.write(payload)
        target.flush()
        os.fsync(target.fileno())
    os.replace(tmp, path)
