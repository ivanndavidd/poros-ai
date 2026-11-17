"""Poros backend package.

Loads a local `.env` file (if present) into the process environment BEFORE any
submodule reads configuration. This matters because store.py / auth.py / secrets_util
read os.environ at IMPORT time (e.g. store.DB_URL freezes on first import), so the
`.env` must be applied here — in the package __init__, which runs before any `app.*`
submodule — not later.

Precedence (highest first):
  real shell environment  >  backend/.env  >  repo-root/.env  >  in-code defaults
Existing environment variables are never overwritten (override=False), so an explicit
`set VAR=...` (or a launcher / CI) always wins over the file. Tests set POROS_SKIP_DOTENV=1
in conftest.py to stay fully hermetic — a developer's local .env can never leak in.

Best-effort: if python-dotenv is not installed or the file is unreadable, we silently
fall back to real env vars + in-code defaults and never block startup.
"""
import os

if not os.environ.get("POROS_SKIP_DOTENV"):
    try:
        from pathlib import Path
        from dotenv import load_dotenv

        _backend = Path(__file__).resolve().parent.parent  # .../backend
        load_dotenv(_backend / ".env", override=False)          # backend/.env (preferred)
        load_dotenv(_backend.parent / ".env", override=False)   # repo-root/.env (fallback)
    except Exception:
        pass
