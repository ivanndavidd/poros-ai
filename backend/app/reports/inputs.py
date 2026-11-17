"""Report inputs: the exports a person attaches for their MBR reports.

    create -> chunks (4 MiB each, any replica) -> close (a quick look at the names) ->
    a report job reads, normalizes and uses them -> kept until ``TTL_SEC`` after upload,
    so the person can generate the report again (a fix, another month) without
    attaching them again.

The raw files carry buyer data, so they are kept only in the per-tenant encrypted store,
only for this flow, and no longer than ``TTL_SEC`` (a report started near the end keeps
them ``RUN_GRACE_SEC`` more). The full read happens in the report job
(``uploads.load_files``); close only lists what the names say, so the person sees at once
whether the right files arrived.
"""
from __future__ import annotations

import io
import os
import zipfile
from datetime import datetime, timezone

from .. import store

CHUNK_BYTES = 4 * 1024 * 1024
MAX_FILE_BYTES = 200 * 1024 * 1024
MAX_TOTAL_BYTES = 400 * 1024 * 1024
MAX_FILES = 100
MAX_CHUNKS = 1000
MAX_LIVE_INPUTS = 3            # per app key + end user; each may hold MAX_TOTAL_BYTES
TTL_SEC = float(os.environ.get("REPORT_INPUT_TTL_SEC") or 6 * 3600)
RUN_GRACE_SEC = 3600           # a report that takes the files keeps them at least this long
SUPPORTED = (".zip", ".csv", ".xlsx", ".xls")


class InputError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def limits() -> dict:
    return {"chunk_bytes": CHUNK_BYTES, "max_file_bytes": MAX_FILE_BYTES,
            "max_total_bytes": MAX_TOTAL_BYTES, "max_files": MAX_FILES}


def iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def owned(tenant_id: str, input_id: str, app_key_id: str, actor_id: str) -> dict:
    """The input when this app key and end user created it; else 404 (never 403: an id
    that belongs to someone else does not exist for this caller)."""
    row = store.get_report_input(tenant_id, input_id)
    if (not row or row["app_key_id"] != (app_key_id or "")
            or row["actor_id"] != (actor_id or "")):
        raise InputError(404, "INPUT_NOT_FOUND", "Those attached files were not found or expired.")
    return row


def make_room(tenant_id: str, app_key_id: str, actor_id: str) -> bool:
    """Room for one more input under MAX_LIVE_INPUTS. A set already used for a report is
    kept only so the person can generate again, so the oldest such set that no queued or
    running report is about to read goes first. False when every live set is still being
    uploaded, waiting for its report, or being read."""
    live = store.list_live_report_inputs(tenant_id, app_key_id, actor_id)
    excess = len(live) - MAX_LIVE_INPUTS + 1
    if excess <= 0:
        return True
    from .. import jobs
    busy = {(job.get("params") or {}).get("inputs")
            for job in jobs.list_for_actor("mbr_report", tenant_id, app_key_id, actor_id, 100)
            if job["status"] in ("queued", "running")}
    idle = [row["id"] for row in live if row["status"] == "used" and row["id"] not in busy]
    if len(idle) < excess:
        return False
    for input_id in idle[:excess]:
        store.delete_report_input(tenant_id, input_id)
    return True


def clean_name(raw: str) -> str:
    name = (raw or "").strip()
    if (not name or len(name) > 200 or any(ch in name for ch in "/\\") or ".." in name
            or any(ord(ch) < 32 for ch in name)):
        raise InputError(400, "BAD_FILE_NAME", "A file name must be a plain name of at most 200 characters.")
    if not name.lower().endswith(SUPPORTED):
        raise InputError(400, "BAD_FILE_TYPE", "Attach .zip, .csv, .xlsx or .xls exports.")
    return name


def check_chunk(row: dict, file_no: int, chunk_no: int, name: str, size: int) -> None:
    """Refuse a chunk that would break the input's limits. Chunks may arrive in any order
    (the browser sends several at once); ``check_complete`` finds a gap at close."""
    if row["status"] != "open":
        raise InputError(409, "INPUT_CLOSED", "These files were already sent for a report.")
    if not 0 <= file_no < MAX_FILES or not 0 <= chunk_no < MAX_CHUNKS:
        raise InputError(400, "BAD_CHUNK", "File or chunk number out of range.")
    if size <= 0:
        raise InputError(400, "BAD_CHUNK", "A chunk cannot be empty.")
    if size > CHUNK_BYTES:
        raise InputError(413, "INPUT_TOO_LARGE", f"A chunk holds at most {CHUNK_BYTES} bytes.")
    parts = [p for p in row["parts"] if not (p["file_no"] == file_no and p["chunk_no"] == chunk_no)]
    same = [p for p in parts if p["file_no"] == file_no]
    if same and same[0]["name"] != name:
        raise InputError(400, "BAD_CHUNK", "Every chunk of a file must carry the same file name.")
    if sum(p["size"] for p in same) + size > MAX_FILE_BYTES:
        raise InputError(413, "INPUT_TOO_LARGE", "One file can be at most 200 MB.")
    if sum(p["size"] for p in parts) + size > MAX_TOTAL_BYTES:
        raise InputError(413, "INPUT_TOO_LARGE", "The attached files can be at most 400 MB together.")


def check_complete(row: dict) -> None:
    """At close: files numbered 0..n-1 and every file's chunks 0..k-1, nothing missing."""
    chunks: dict = {}
    for p in row["parts"]:
        chunks.setdefault(p["file_no"], (p["name"], set()))[1].add(p["chunk_no"])
    if sorted(chunks) != list(range(len(chunks))):
        raise InputError(409, "INPUT_INCOMPLETE", "A file did not arrive; attach the files again.")
    for name, got in chunks.values():
        if got != set(range(len(got))):
            raise InputError(409, "INPUT_INCOMPLETE",
                             f"Part of {name} did not arrive; attach the files again.")


class ChunkFile(io.RawIOBase):
    """A read-only, seekable view of one attached file that fetches its chunks from the
    store on demand, so listing a 100 MB zip reads only its directory at the end."""

    def __init__(self, tenant_id: str, input_id: str, file_no: int, sizes: list):
        super().__init__()
        self._t, self._i, self._f = tenant_id, input_id, file_no
        self._sizes = sizes
        self._starts = [sum(sizes[:n]) for n in range(len(sizes))]
        self._size = sum(sizes)
        self._pos = 0
        self._cache: dict = {}

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._pos, io.SEEK_END: self._size}[whence]
        self._pos = max(0, base + offset)
        return self._pos

    def _chunk(self, n: int) -> bytes:
        if n not in self._cache:
            if len(self._cache) >= 2:
                self._cache.pop(next(iter(self._cache)))
            self._cache[n] = store.read_report_input_chunk(self._t, self._i, self._f, n)
        return self._cache[n]

    def readinto(self, buf) -> int:
        """Fill ``buf`` across chunk boundaries (zipfile expects a full read)."""
        done = 0
        while done < len(buf) and self._pos < self._size:
            n = max(i for i, s in enumerate(self._starts) if s <= self._pos)
            off = self._pos - self._starts[n]
            piece = self._chunk(n)[off:off + len(buf) - done]
            if not piece:
                break
            buf[done:done + len(piece)] = piece
            done += len(piece)
            self._pos += len(piece)
        return done


def _files(row: dict) -> list:
    """[(file_no, name, [chunk sizes])] in attach order."""
    out: dict = {}
    for p in row["parts"]:
        out.setdefault(p["file_no"], (p["name"], []))[1].append(p["size"])
    return [(n, name, sizes) for n, (name, sizes) in sorted(out.items())]


def _look(path: str) -> tuple[dict | None, str]:
    """(recognized entry, "") or (None, why not) for one name."""
    from .uploads import classify
    found = classify(path)
    if not found.get("kind"):
        return None, "not an export the review reads"
    return {"path": path, "kind": found["kind"], "marketplace": found.get("marketplace") or "",
            "month": found.get("month") or ""}, ""


def _entries(tenant_id: str, input_id: str, file_no: int, name: str, sizes: list) -> list:
    if not name.lower().endswith(".zip"):
        return [name]
    try:
        with zipfile.ZipFile(ChunkFile(tenant_id, input_id, file_no, sizes)) as zf:
            return [i.filename for i in zf.infolist()
                    if not i.is_dir() and "__MACOSX" not in i.filename
                    and not i.filename.rsplit("/", 1)[-1].startswith(".")]
    except (zipfile.BadZipFile, OSError, ValueError):
        return []


def manifest(tenant_id: str, row: dict) -> dict:
    """What each attached file looks like by its name (and each zip by its entries)."""
    files, kinds, months, recognized, ignored = [], {}, set(), 0, 0
    for file_no, name, sizes in _files(row):
        item = {"file_no": file_no, "name": name, "size": sum(sizes), "recognized": [],
                "ignored": []}
        paths = _entries(tenant_id, row["id"], file_no, name, sizes)
        if not paths:
            item["ignored"].append({"path": name, "reason": "not a readable zip file"})
        for path in paths:
            entry, why = _look(path)
            if entry:
                item["recognized"].append(entry)
                kinds.setdefault(entry["kind"], set()).add(entry["marketplace"])
                if entry["month"]:
                    months.add(entry["month"])
            else:
                item["ignored"].append({"path": path, "reason": why})
        recognized += len(item["recognized"])
        ignored += len(item["ignored"])
        files.append(item)
    ordered = sorted(months)
    return {"files": files,
            "summary": {"files": len(files), "recognized": recognized, "ignored": ignored,
                        "kinds": {k: sorted(m for m in v if m) for k, v in sorted(kinds.items())},
                        "months": ordered, "latest_month": ordered[-1] if ordered else None}}


def public(row: dict) -> dict:
    out = {"input_id": row["id"], "status": row["status"], "expires_at": iso(row["expires"])}
    if row.get("manifest"):
        out.update(row["manifest"])
    return out
