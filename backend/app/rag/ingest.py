"""Ingest pipeline: extract text (PDF/DOCX/PPTX/MD/TXT) -> chunk -> embed -> store.

PDF/DOCX/PPTX parsing needs pypdf / python-docx / python-pptx (in requirements);
when missing we raise a clear error for those types. MD/TXT always work. Unknown
BINARY payloads are rejected loudly — silently ingesting mojibake would poison
the vector store.
"""
import os, re
from .. import store
from .embeddings import embed_texts, embedder_name

_OCR_MAX_IMAGES = int(os.environ.get("OCR_MAX_IMAGES", "60") or 60)   # cap OCR work per document

# Chunking is paragraph-aware and counts WORDS (not tokens). These defaults match the
# historical behavior; the per-tenant Document-config chunk_size/chunk_overlap override them.
_CHUNK_WORDS_DEFAULT = 180
_CHUNK_OVERLAP_DEFAULT = 40

# Raised by every ingest path when parsing yields nothing chunkable (empty file,
# image-only PDF with OCR off, unsupported payload).
_NO_TEXT = "no extractable text in document"


def _chunk_params(cfg: dict):
    """Resolve (target_words, overlap) from a Document-config dict, clamped to a safe range.
    Too-small chunks hurt recall; too-big ones hurt precision + make citations vague — so the
    size is bounded to [40, 800] words and the overlap can never swallow half the chunk."""
    size, overlap = _CHUNK_WORDS_DEFAULT, _CHUNK_OVERLAP_DEFAULT
    cfg = cfg or {}
    raw_size = cfg.get("chunk_size")
    if raw_size not in (None, ""):                   # absent -> keep default (don't force 0)
        try:
            s = int(raw_size)
            if s > 0:
                size = max(40, min(s, 800))
        except (TypeError, ValueError):
            pass
    raw_overlap = cfg.get("chunk_overlap")
    if raw_overlap not in (None, ""):                # a provided 0 = "no overlap" (honored)
        try:
            o = int(raw_overlap)
            if o >= 0:
                overlap = min(o, size // 2)
        except (TypeError, ValueError):
            pass
    return size, overlap


def _iter_pictures(shapes):
    """Yield image blobs from PPTX picture shapes, recursing into group shapes."""
    from pptx.enum.shapes import MSO_SHAPE_TYPE
    for shape in shapes:
        if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.GROUP:
            yield from _iter_pictures(shape.shapes)
            continue
        try:
            blob = shape.image.blob                  # Picture / picture-placeholder shapes
        except Exception:
            continue                                 # non-image shape (text box, chart, table, ...)
        if blob:
            yield blob


def extract_text(filename: str, data: bytes, ocr: bool = False) -> str:
    """Extract text. When `ocr` is on AND Tesseract is available, ALSO read text baked into
    images (PPTX pictures, PDF page images) — otherwise those pixels are invisible to RAG.
    OCR is best-effort: unavailable OCR silently degrades to plain text extraction."""
    name = (filename or "").lower()
    do_ocr = False
    if ocr:
        try:
            from .ocr import available as _ocr_available
            do_ocr = _ocr_available()
        except Exception:
            do_ocr = False
    ocr_count = 0
    if name.endswith(".pdf"):
        try:
            import io, pypdf
        except Exception:
            raise RuntimeError("PDF support needs `pip install pypdf`")
        reader = pypdf.PdfReader(io.BytesIO(data))
        pages = []
        for i, p in enumerate(reader.pages, 1):
            t = (p.extract_text() or "").strip()
            if do_ocr and ocr_count < _OCR_MAX_IMAGES:   # OCR embedded images (scanned pages)
                from .ocr import image_to_text
                extra = []
                try:
                    for img in (getattr(p, "images", None) or []):
                        if ocr_count >= _OCR_MAX_IMAGES:
                            break
                        ocr_count += 1
                        ot = image_to_text(getattr(img, "data", None))
                        if ot:
                            extra.append(ot)
                except Exception:
                    pass
                if extra:
                    t = (t + "  (teks gambar: " + " ".join(extra) + ")").strip()
            if t:
                pages.append(f"[Page {i}] {t}")      # marker -> human citations ("file — Page 3")
        return "\n\n".join(pages)
    if name.endswith(".docx"):
        try:
            import io, docx
        except Exception:
            raise RuntimeError("DOCX support needs `pip install python-docx`")
        d = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in d.paragraphs)
    if name.endswith(".pptx"):
        try:
            import io
            from pptx import Presentation
        except Exception:
            raise RuntimeError("PPTX support needs `pip install python-pptx`")
        prs = Presentation(io.BytesIO(data))
        parts = []
        for i, slide in enumerate(prs.slides, 1):
            texts = []
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False):
                    t = "\n".join(p.text for p in shape.text_frame.paragraphs).strip()
                    if t:
                        texts.append(t)
                if getattr(shape, "has_table", False):
                    for row in shape.table.rows:
                        texts.append(" | ".join(c.text.strip() for c in row.cells))
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                nt = slide.notes_slide.notes_text_frame.text.strip()
                if nt:
                    texts.append(f"(catatan pembicara: {nt})")
            if do_ocr and ocr_count < _OCR_MAX_IMAGES:   # OCR pictures on the slide
                from .ocr import image_to_text
                for blob in _iter_pictures(slide.shapes):
                    if ocr_count >= _OCR_MAX_IMAGES:
                        break
                    ocr_count += 1
                    ot = image_to_text(blob)
                    if ot:
                        texts.append(f"(teks gambar: {ot})")
            if texts:
                # slide marker keeps citations traceable to the exact slide
                parts.append(f"[Slide {i}] " + "\n".join(texts))
        return "\n\n".join(parts)
    # md / txt / fallback — but NEVER silently ingest binary junk (zip/OLE/etc.)
    if data[:4] in (b"PK\x03\x04", b"\xd0\xcf\x11\xe0") or b"\x00" in data[:1024]:
        raise RuntimeError(f"unsupported binary format: {filename} — "
                           "supported: PDF, DOCX, PPTX, MD, TXT (old .doc/.ppt/.xls: convert first)")
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:
        return data.decode("latin-1", errors="replace")


def chunk_text(text: str, target_words: int = 180, overlap: int = 40) -> list:
    """Paragraph-aware chunking with word overlap so citations stay coherent."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    chunks, cur, count = [], [], 0
    for p in paras:
        w = len(p.split())
        if count + w > target_words and cur:
            chunks.append(" ".join(cur))
            # keep tail words as overlap
            tail = " ".join(cur).split()[-overlap:]
            cur, count = ([" ".join(tail)] if tail else []), len(tail)
        cur.append(p); count += w
    if cur:
        chunks.append(" ".join(cur))
    # very long single paragraphs: hard-split
    out = []
    for ch in chunks:
        words = ch.split()
        if len(words) <= target_words * 1.6:
            out.append(ch)
        else:
            for i in range(0, len(words), target_words):
                out.append(" ".join(words[i:i + target_words]))
    return [c for c in out if c.strip()]


def _effective_doc_config(tenant_id: str, override: dict) -> dict:
    """Resolve the config used to vectorize ONE document: per-document override wins, then
    tenant Document-config (backward compat), then the code defaults inside chunk_text/_chunk_params.
    Chunking (size/overlap) + OCR are PER-DOCUMENT; the embedding model stays per-tenant."""
    try:
        base = dict(store.get_tenant_config(tenant_id, "document") or {})
    except Exception:
        base = {}
    if override:
        base.update({k: v for k, v in override.items() if v is not None})
    return base


def ingest_document(tenant_id: str, datasource_id: str, filename: str, mime: str,
                    data: bytes, acl_roles: list, config: dict = None) -> dict:
    eff = _effective_doc_config(tenant_id, config)
    ocr = bool(eff.get("ocr"))                       # per-document OCR toggle (default off)
    text = extract_text(filename, data, ocr=ocr)
    target_words, overlap = _chunk_params(eff)       # per-document chunk size/overlap (words)
    chunks = chunk_text(text, target_words=target_words, overlap=overlap)
    if not chunks:
        raise RuntimeError(_NO_TEXT)
    vectors = embed_texts(tenant_id, chunks)         # tenant embedding model (uniform vector space)
    doc_cfg = {"chunk_size": target_words, "chunk_overlap": overlap, "ocr": ocr}   # resolved -> reproducible on edit
    doc_id = store.create_document(tenant_id, datasource_id, filename, mime, acl_roles, len(chunks), config=doc_cfg)
    store.add_chunks(tenant_id, doc_id,
                     [(i, chunks[i], vectors[i]) for i in range(len(chunks))], acl_roles)
    store.set_document_embedder(doc_id, embedder_name(tenant_id))   # which model made these vectors
    from . import ocr as _ocr
    return {"document_id": doc_id, "filename": filename, "n_chunks": len(chunks), "config": doc_cfg,
            "ocr": {"requested": ocr, "available": _ocr.available() if ocr else False}}


def ingest_existing_document(tenant_id: str, doc_id: str, data: bytes,
                             config: dict = None, on_progress=None) -> dict:
    """Ingest into a pre-created document row.

    Async uploads first durably store the encrypted original and return a job id.
    The worker later calls this function, so extraction/embedding can take minutes
    without keeping the gateway request open.
    """
    doc = store.get_document(doc_id)
    if not doc or doc.get("tenant_id") != tenant_id:
        raise RuntimeError("document not found")
    report = on_progress or (lambda *_args: None)
    eff = _effective_doc_config(tenant_id, config)
    ocr = bool(eff.get("ocr"))
    report(15, "extracting", "Mengekstrak teks dokumen")
    text = extract_text(doc["filename"], data, ocr=ocr)
    target_words, overlap = _chunk_params(eff)
    chunks = chunk_text(text, target_words=target_words, overlap=overlap)
    if not chunks:
        raise RuntimeError(_NO_TEXT)
    report(45, "embedding", f"Membuat embedding untuk {len(chunks)} chunk")
    vectors = embed_texts(tenant_id, chunks)
    doc_cfg = {"chunk_size": target_words, "chunk_overlap": overlap, "ocr": ocr}
    report(82, "saving_index", "Menyimpan indeks pencarian dokumen")
    store.replace_document_chunks(
        tenant_id,
        doc_id,
        [(i, chunks[i], vectors[i]) for i in range(len(chunks))],
        doc.get("acl_roles") or [],
    )
    store.set_document_config(doc_id, doc_cfg)
    store.set_document_embedder(doc_id, embedder_name(tenant_id))
    from . import ocr as _ocr
    return {
        "document_id": doc_id,
        "filename": doc["filename"],
        "n_chunks": len(chunks),
        "config": doc_cfg,
        "embedder": embedder_name(tenant_id),
        "ocr": {"requested": ocr, "available": _ocr.available() if ocr else False},
    }


def reingest_document(tenant_id: str, doc_id: str, new_config: dict) -> dict:
    """Re-chunk + re-embed ONE document from its stored original file after a config edit
    (chunk_size / chunk_overlap / ocr). Uses the CURRENT tenant embedding model; ACL preserved."""
    doc = store.get_document(doc_id)
    if not doc or doc.get("tenant_id") != tenant_id:
        raise RuntimeError("document not found")
    if not store.document_file_available(doc):
        raise RuntimeError("file asli tidak tersimpan — unggah ulang dokumen untuk mengubah konfigurasinya")
    data = store.read_document_file(tenant_id, doc_id)
    eff = dict(doc.get("config") or {})
    if new_config:
        eff.update({k: v for k, v in new_config.items() if v is not None})
    ocr = bool(eff.get("ocr"))
    text = extract_text(doc["filename"], data, ocr=ocr)
    target_words, overlap = _chunk_params(eff)
    chunks = chunk_text(text, target_words=target_words, overlap=overlap)
    if not chunks:
        raise RuntimeError(_NO_TEXT)
    vectors = embed_texts(tenant_id, chunks)
    doc_cfg = {"chunk_size": target_words, "chunk_overlap": overlap, "ocr": ocr}
    store.replace_document_chunks(tenant_id, doc_id,
                                  [(i, chunks[i], vectors[i]) for i in range(len(chunks))],
                                  doc.get("acl_roles") or [])
    store.set_document_config(doc_id, doc_cfg)
    store.set_document_embedder(doc_id, embedder_name(tenant_id))
    from . import ocr as _ocr
    return {"document_id": doc_id, "n_chunks": len(chunks), "config": doc_cfg,
            "embedder": embedder_name(tenant_id),
            "ocr": {"requested": ocr, "available": _ocr.available() if ocr else False}}


def reindex_tenant(tenant_id: str) -> dict:
    """Re-embed EVERY stored chunk with the CURRENT embedder. Run this after switching
    embedding models — old vectors live in a different space (often a different dim)
    and would otherwise be skipped at retrieval. Texts are untouched; only vectors and
    the per-document embedder tag change."""
    name = embedder_name(tenant_id)
    chunks = store.iter_chunks(tenant_id)
    for i in range(0, len(chunks), 64):                             # bounded batches
        batch = chunks[i:i + 64]
        vectors = embed_texts(tenant_id, [c["text"] for c in batch])
        store.update_chunk_embeddings([(c["id"], v) for c, v in zip(batch, vectors)])
    store.set_all_documents_embedder(tenant_id, name)
    return {"reindexed_chunks": len(chunks), "embedder": name}
