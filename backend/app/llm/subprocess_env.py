"""Minimal environment for locally-spawned CLI subprocesses (Codex).

Every other LLM provider is a stateless HTTPS call. The `codex` provider is different:
it runs a coding agent as a child process ON the backend host, and its prompt is built
from the conversation — which starts with end-user chat text. The agent runs with
`-a never` (no approval prompt) and `--sandbox read-only`, which stops writes but
explicitly permits reading files and running non-mutating commands. `printenv` is one.

A child that inherits `os.environ` therefore reads ADMIN_KEY (the whole /admin control
plane, across every tenant), DATABASE_URL, DOCUMENT_ENCRYPTION_KEY, API_KEY_PEPPER, the
audit chain keys, and the Vault AppRole credentials — the set enumerated in
`deploy/production/secret.example.yaml`. Passing an explicit allowlist removes that
reach regardless of what the agent is talked into doing, which matters because prompt
screening is corroboration-based defence in depth, not an authorization boundary.

Used by BOTH `llm/gateway._call_codex` and `llm/codex_auth`, and by the model-discovery
spawn in `routers/models_admin`, so a future fourth call site inherits the isolation.
"""
import os

# What the CLI genuinely needs: to be found and run, to locate its own credential store,
# to render text, and to reach the network through a corporate proxy.
_ALLOWED = (
    "PATH", "HOME", "CODEX_HOME", "LANG", "LC_ALL", "TZ",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    # Windows: a child process without these cannot resolve DLLs or a temp directory,
    # so the CLI fails to start at all rather than running with less access.
    "SystemRoot", "SystemDrive", "windir", "COMSPEC", "PATHEXT",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH",
    "TEMP", "TMP", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
)


def codex_env() -> dict:
    """Allowlisted environment for a Codex subprocess — never the parent's secrets."""
    env = {name: os.environ[name] for name in _ALLOWED if name in os.environ}
    env.setdefault("NO_COLOR", "1")
    env.setdefault("TERM", "dumb")
    return env
