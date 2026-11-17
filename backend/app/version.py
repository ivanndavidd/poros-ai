"""Which code is this process actually running?

Answers the operational question "has the deployment picked up my commit yet?"
without guessing from behavior. Resolution order:

1. env POROS_BUILD_SHA / POROS_BUILD_TIME (CI override),
2. BUILD_INFO file baked into the image by the Dockerfile (sha= may be "unknown"
   when the pipeline passes no GIT_SHA build-arg, but built= always identifies
   the image build moment),
3. `git rev-parse` against the working tree (local dev),
4. "unknown".

Exposed on the PUBLIC health path (/dangerously-skip-login/health) — reachable
without a gateway login, so an operator can curl it right after a deploy.
"""
import os
import subprocess
from functools import lru_cache

_BUILD_INFO_PATH = os.path.join(os.path.dirname(__file__), "..", "BUILD_INFO")


def _from_file() -> dict:
    out = {}
    try:
        with open(_BUILD_INFO_PATH, encoding="utf-8") as fh:
            for line in fh:
                key, _, value = line.strip().partition("=")
                if key in ("sha", "built") and value:
                    out[key] = value[:64]
    except OSError:
        pass
    return out


def _from_git() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(__file__), capture_output=True, text=True, timeout=3,
        ).stdout.strip()[:64]
    except Exception:
        return ""


@lru_cache(maxsize=1)
def build_info() -> dict:
    baked = _from_file()
    sha = os.getenv("POROS_BUILD_SHA", "").strip() or baked.get("sha", "")
    built = os.getenv("POROS_BUILD_TIME", "").strip() or baked.get("built", "")
    if not sha or sha == "unknown":
        sha = _from_git() or sha
    return {"version": sha or "unknown", "built": built or None}
