"""Store secrets (DB passwords, LLM API keys, JWT secrets) safely at rest.

Two interchangeable backends behind ONE api, selected by env SECRETS_BACKEND:
  * fernet (default) — encrypt locally (AES via `cryptography`), key derived from
    ADMIN_SECRET_KEY; the ciphertext lives inline in SQLite as "enc:...". Falls
    back to clearly-marked reversible base64 ("obf:") only when the lib is
    missing, so the app still runs self-contained in dev.
  * vault  — write the value to HashiCorp Vault / OpenBao (KV v2) and keep only
    a reference "vault:<path>" in SQLite; no secret material stays local.
    Auth: static VAULT_TOKEN (dev) or AppRole VAULT_ROLE_ID+VAULT_SECRET_ID.

decrypt() DUAL-READS every format (enc:/obf:/vault:/legacy plaintext) regardless
of the active backend, so switching backends never breaks existing rows and
migration can be gradual. encrypt() NEVER falls back silently: if the chosen
backend is unusable it raises (production additionally refuses dev-grade keys).
"""
import os, base64, hashlib, json, ssl, threading, time, uuid
import urllib.request, urllib.error

_PREFIX_ENC = "enc:"        # Fernet
_PREFIX_OBF = "obf:"        # base64 fallback (NOT secure)
_PREFIX_VAULT = "vault:"    # legacy: vault:<path>; current: vault:v2:<version>:<path>
_DEV_DEFAULT_KEY = "dev-insecure-default-change-me"
_DEV_ROOT_TOKEN = "tenantos-dev-root"      # fixed root token of deploy/vault dev-mode compose


def _production() -> bool:
    # Single source of truth: absent/unrecognised APP_ENV means HARDENED (runtime_config).
    from .runtime_config import production
    return production()


def _key_is_default() -> bool:
    raw = os.getenv("ADMIN_SECRET_KEY")
    return not raw or raw == _DEV_DEFAULT_KEY


def backend() -> str:
    """Active secrets backend: 'fernet' (default) or 'vault'."""
    return (os.getenv("SECRETS_BACKEND") or "fernet").strip().lower()


def production_problems() -> list:
    """Dev-grade secret handling that must NOT reach production. Checked once at
    startup (app.security.startup_check) and defensively on every encrypt().
    Backend-aware: vault mode has its own requirements and does not need the
    Fernet key (legacy enc: rows still decrypt as long as the key stays set)."""
    if backend() == "vault":
        out = _vault_config_problems()
        if os.getenv("VAULT_TOKEN") == _DEV_ROOT_TOKEN:
            out.append("VAULT_TOKEN is the dev-mode root token — in production use AppRole "
                       "(VAULT_ROLE_ID + VAULT_SECRET_ID) with a least-privilege policy")
        if _production() and os.getenv("VAULT_TOKEN"):
            out.append("production Vault auth must use short-lived AppRole credentials; "
                       "remove VAULT_TOKEN and set VAULT_ROLE_ID + VAULT_SECRET_ID")
        return out
    out = []
    if _key_is_default():
        out.append("ADMIN_SECRET_KEY is unset/default — it derives the Fernet key that encrypts "
                   "DB passwords and LLM API keys at rest; set a strong random value")
    if _fernet() is None:
        out.append("`cryptography` is not installed — secrets would be stored base64-obfuscated "
                   "(reversible, NOT encryption); run: pip install cryptography")
    return out


def _fernet():
    try:
        from cryptography.fernet import Fernet
    except Exception:
        return None
    raw = os.getenv("ADMIN_SECRET_KEY", _DEV_DEFAULT_KEY).encode()
    key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    return Fernet(key)


def encrypt(value: str) -> str:
    if value is None:
        return value
    if backend() == "vault":
        return _vault_put(value)    # raises loudly when Vault is down/misconfigured — no silent fallback
    f = _fernet()
    if _production() and (f is None or _key_is_default()):
        # fail HARD rather than silently storing recoverable/weakly-keyed secrets
        raise RuntimeError("refusing to store secrets with dev-grade protection in APP_ENV=production: "
                           + "; ".join(production_problems()))
    if f:
        return _PREFIX_ENC + f.encrypt(value.encode()).decode()
    return _PREFIX_OBF + base64.urlsafe_b64encode(value.encode()).decode()


def decrypt(token: str) -> str:
    if not isinstance(token, str):
        return token
    if token.startswith(_PREFIX_VAULT):                  # dual-read works on ANY active backend
        return _vault_get(token[len(_PREFIX_VAULT):])
    if token.startswith(_PREFIX_ENC):
        f = _fernet()
        if not f:
            raise RuntimeError("ADMIN_SECRET_KEY/cryptography required to decrypt")
        return f.decrypt(token[len(_PREFIX_ENC):].encode()).decode()
    if token.startswith(_PREFIX_OBF):
        return base64.urlsafe_b64decode(token[len(_PREFIX_OBF):].encode()).decode()
    return token  # legacy plaintext


def encrypt_json(obj) -> str:
    return encrypt(json.dumps(obj))


def decrypt_json(token: str):
    return json.loads(decrypt(token))


def purge(token: str):
    """Best-effort removal of a stored secret's BACKING entry when its row is
    deleted or the secret replaced. Only 'vault:' refs have one (enc:/obf: live
    inline in the row itself). NEVER raises — delete paths must not start
    failing because Vault is briefly unreachable (the ref is gone either way;
    a leftover Vault entry is an orphan, not a leak of anything new)."""
    try:
        if isinstance(token, str) and token.startswith(_PREFIX_VAULT):
            _vault_delete(token[len(_PREFIX_VAULT):])
        return True
    except Exception:
        return False


def mask(value: str, keep: int = 4) -> str:
    """For display: show only the last `keep` chars."""
    if not value:
        return ""
    s = str(value)
    return ("•" * max(0, len(s) - keep)) + s[-keep:] if len(s) > keep else "•" * len(s)


# ---- Vault backend (KV v2 over plain HTTP API; no extra dependency) ----
_vault_lock = threading.Lock()
_vault_token = {"token": None, "expires": 0.0}   # AppRole-issued token, reused until near lease expiry
_vault_cache = {}                                # path -> (value, expires) — spares a round-trip per query


def _vault_addr() -> str: return (os.getenv("VAULT_ADDR") or "").rstrip("/")
def _vault_mount() -> str: return (os.getenv("VAULT_KV_MOUNT") or "secret").strip("/")
def _vault_base() -> str: return (os.getenv("VAULT_BASE_PATH") or "tenantos").strip("/")
def _vault_cache_ttl() -> int: return int(os.getenv("VAULT_CACHE_TTL", "300"))


def _vault_config_problems() -> list:
    out = []
    if not _vault_addr():
        out.append("VAULT_ADDR is unset (e.g. http://localhost:8200)")
    if not (os.getenv("VAULT_TOKEN") or (os.getenv("VAULT_ROLE_ID") and os.getenv("VAULT_SECRET_ID"))):
        out.append("no Vault auth configured — set VAULT_TOKEN (dev) or VAULT_ROLE_ID + VAULT_SECRET_ID (AppRole)")
    return out


def _http(method: str, url: str, body=None, token: str = None):
    """One Vault HTTP round-trip -> (status, parsed-json). Tests monkeypatch THIS.
    Raises RuntimeError only when Vault is unreachable (HTTP errors are returned)."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Vault-Token"] = token
    if os.getenv("VAULT_NAMESPACE"):
        headers["X-Vault-Namespace"] = os.getenv("VAULT_NAMESPACE")
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers=headers, method=method)
    kw = {"timeout": float(os.getenv("VAULT_TIMEOUT_SEC", "5"))}
    if url.startswith("https://"):
        kw["context"] = ssl.create_default_context(cafile=os.getenv("VAULT_CACERT") or None)
    try:
        with urllib.request.urlopen(req, **kw) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, (json.loads(raw) if raw else {})
        except Exception:
            return e.code, {}
    except Exception as e:
        raise RuntimeError(f"Vault unreachable at {url}: {e}")


def _vault_err(resp) -> str:
    return "; ".join((resp or {}).get("errors") or []) or "unknown error"


def _vault_login() -> str:
    """A valid client token: static VAULT_TOKEN, or AppRole login cached until
    ~30s before its lease expires (then transparently re-logged-in)."""
    static = os.getenv("VAULT_TOKEN")
    if static:
        return static
    with _vault_lock:
        if _vault_token["token"] and time.time() < _vault_token["expires"]:
            return _vault_token["token"]
        rid, sid = os.getenv("VAULT_ROLE_ID"), os.getenv("VAULT_SECRET_ID")
        if not (rid and sid):
            raise RuntimeError("SECRETS_BACKEND=vault: " + "; ".join(_vault_config_problems()))
        status, resp = _http("POST", f"{_vault_addr()}/v1/auth/approle/login",
                             {"role_id": rid, "secret_id": sid})
        tok = (resp or {}).get("auth", {}).get("client_token")
        if status != 200 or not tok:
            raise RuntimeError(f"Vault AppRole login failed (HTTP {status}): {_vault_err(resp)}")
        _vault_token["token"] = tok
        _vault_token["expires"] = time.time() + max(30, int(resp["auth"].get("lease_duration") or 300)) - 30
        return tok


def _vault_op(method: str, path: str, body=None):
    """Authenticated KV call with ONE re-login retry on a stale AppRole token."""
    if not _vault_addr():
        raise RuntimeError("VAULT_ADDR is not set — required to resolve 'vault:' secret references")
    url = f"{_vault_addr()}/v1/{path}"
    status, resp = _http(method, url, body, token=_vault_login())
    if status in (401, 403) and not os.getenv("VAULT_TOKEN"):
        _vault_token["token"] = None                     # expired/revoked lease -> fresh login, retry once
        status, resp = _http(method, url, body, token=_vault_login())
    return status, resp


def _vault_put(value: str) -> str:
    path = f"{_vault_base()}/{uuid.uuid4().hex}"
    status, resp = _vault_op("POST", f"{_vault_mount()}/data/{path}", {"data": {"v": value}})
    if status not in (200, 204):
        raise RuntimeError(f"Vault write failed (HTTP {status}): {_vault_err(resp)}")
    version = int(((resp or {}).get("data") or {}).get("version") or 0)
    if _production() and version <= 0:
        raise RuntimeError("Vault KV v2 write returned no key version; refusing an unversioned production reference")
    ref = f"v2:{version}:{path}" if version > 0 else path
    _vault_cache[ref] = (value, time.time() + _vault_cache_ttl())
    return _PREFIX_VAULT + ref


def _split_vault_ref(ref: str):
    if (ref or "").startswith("v2:"):
        try:
            _, version, path = ref.split(":", 2)
            return path, int(version)
        except (ValueError, TypeError):
            raise RuntimeError("invalid versioned Vault reference")
    return ref, None


def is_versioned_vault_ref(token: str) -> bool:
    return isinstance(token, str) and token.startswith(_PREFIX_VAULT + "v2:")


def _vault_get(ref: str) -> str:
    hit = _vault_cache.get(ref)
    if hit and time.time() < hit[1]:
        return hit[0]
    path, version = _split_vault_ref(ref)
    suffix = f"?version={version}" if version is not None else ""
    status, resp = _vault_op("GET", f"{_vault_mount()}/data/{path}{suffix}")
    if status != 200:
        raise RuntimeError(f"Vault read failed for '{path}' (HTTP {status}): {_vault_err(resp)}")
    value = ((resp or {}).get("data", {}).get("data") or {}).get("v")
    if value is None:
        raise RuntimeError(f"Vault secret '{path}' exists but has no value")
    _vault_cache[ref] = (value, time.time() + _vault_cache_ttl())
    return value


def _vault_delete(ref: str):
    _vault_cache.pop(ref, None)
    path, _ = _split_vault_ref(ref)
    _vault_op("DELETE", f"{_vault_mount()}/metadata/{path}")   # removes ALL versions of the entry


def vault_check() -> list:
    """Config + connectivity + auth problems for the vault backend (empty = healthy).
    Used by security.startup_check: warnings in dev, fail-hard in production."""
    out = _vault_config_problems()
    if out:
        return out
    try:
        status, _ = _http("GET", f"{_vault_addr()}/v1/sys/health")
        if status not in (200, 429):                     # 200 active, 429 standby (both usable)
            return [f"Vault at {_vault_addr()} is not ready (HTTP {status} — sealed or uninitialized?)"]
        status, resp = _http("GET", f"{_vault_addr()}/v1/auth/token/lookup-self", token=_vault_login())
        if status != 200:
            return [f"Vault auth token rejected (HTTP {status}): {_vault_err(resp)}"]
    except Exception as e:
        return [f"cannot reach/authenticate to Vault: {e}"]
    return []
