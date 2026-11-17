"""Bounded document upload validation for the admin RAG endpoint."""
from __future__ import annotations

import io
import os
import re
import zipfile


class UploadRejected(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_MIME = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".md": "text/markdown",
    ".txt": "text/plain",
}
_CLIENT_MIME = {
    ".pdf": {"application/pdf", "application/octet-stream"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip", "application/octet-stream",
    },
    ".pptx": {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/zip", "application/octet-stream",
    },
    ".md": {"text/markdown", "text/plain", "application/octet-stream"},
    ".txt": {"text/plain", "application/octet-stream"},
}


def max_upload_bytes() -> int:
    try:
        mb = max(1, min(int(os.getenv("DOCUMENT_MAX_UPLOAD_MB", "25")), 250))
    except ValueError:
        mb = 25
    return mb * 1024 * 1024


def tenant_quota_bytes() -> int:
    try:
        mb = max(1, min(int(os.getenv("DOCUMENT_TENANT_QUOTA_MB", "250")), 10000))
    except ValueError:
        mb = 250
    return mb * 1024 * 1024


async def read_bounded(upload, limit: int | None = None) -> bytes:
    limit = int(limit or max_upload_bytes())
    chunks, total = [], 0
    while True:
        chunk = await upload.read(min(1024 * 1024, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise UploadRejected(
                "FILE_TOO_LARGE",
                f"ukuran dokumen melebihi batas {limit // (1024 * 1024)} MB",
            )
    if not chunks:
        raise UploadRejected("FILE_EMPTY", "dokumen kosong")
    return b"".join(chunks)


def _safe_filename(filename: str) -> str:
    base = os.path.basename(str(filename or "").replace("\\", "/")).strip()
    if not base or base in {".", ".."}:
        raise UploadRejected("FILENAME_INVALID", "nama file tidak valid")
    base = re.sub(r"[\x00-\x1f\x7f]", "_", base)[:180]
    return base


def _validate_office(data: bytes, ext: str):
    if not data.startswith(b"PK"):
        raise UploadRejected("FILE_SIGNATURE_INVALID", f"isi file tidak cocok dengan {ext}")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > 2000:
                raise UploadRejected("ARCHIVE_UNSAFE", "dokumen memiliki terlalu banyak bagian")
            total = sum(max(0, item.file_size) for item in infos)
            compressed = sum(max(1, item.compress_size) for item in infos)
            if total > max_upload_bytes() * 20 or total / compressed > 100:
                raise UploadRejected("ARCHIVE_UNSAFE", "rasio kompresi dokumen tidak aman")
            names = {item.filename for item in infos}
            marker = "word/document.xml" if ext == ".docx" else "ppt/presentation.xml"
            if "[Content_Types].xml" not in names or marker not in names:
                raise UploadRejected("FILE_SIGNATURE_INVALID", f"isi file tidak cocok dengan {ext}")
    except zipfile.BadZipFile as exc:
        raise UploadRejected("FILE_SIGNATURE_INVALID", f"arsip {ext} rusak") from exc


def validate(filename: str, client_mime: str, data: bytes) -> tuple[str, str]:
    filename = _safe_filename(filename)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in _MIME:
        raise UploadRejected(
            "FILE_TYPE_NOT_ALLOWED",
            "jenis dokumen harus PDF, DOCX, PPTX, MD, atau TXT",
        )
    supplied = str(client_mime or "application/octet-stream").split(";", 1)[0].strip().lower()
    if supplied not in _CLIENT_MIME[ext]:
        raise UploadRejected("MIME_MISMATCH", "content-type tidak cocok dengan ekstensi dokumen")
    if ext == ".pdf" and not data.startswith(b"%PDF-"):
        raise UploadRejected("FILE_SIGNATURE_INVALID", "isi file tidak cocok dengan PDF")
    if ext in {".docx", ".pptx"}:
        _validate_office(data, ext)
    if ext in {".md", ".txt"}:
        if b"\x00" in data[:8192]:
            raise UploadRejected("FILE_SIGNATURE_INVALID", "file teks mengandung data biner")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise UploadRejected("TEXT_ENCODING_INVALID", "file teks harus menggunakan UTF-8") from exc
    return filename, _MIME[ext]
