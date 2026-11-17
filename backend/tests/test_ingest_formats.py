"""Document ingest format handling: PPTX extraction + binary-junk guard.

Run:  cd backend && python -m pytest tests/test_ingest_formats.py -q
"""
import io, os, sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)

# NOTE: app.rag imports app.store, which freezes store.DB from env at import —
# import lazily so tests/conftest.py has pinned ADMIN_DB_PATH to a tmp dir first.
def _ingest():
    from app.rag.ingest import extract_text, chunk_text
    return extract_text, chunk_text


pptx = pytest.importorskip("pptx")


def _mk_pptx() -> bytes:
    from pptx import Presentation
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])          # title + content layout
    s.shapes.title.text = "Onboarding toko"
    s.placeholders[1].text = "Langkah pertama: registrasi akun marketplace"
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def test_pptx_extracts_slide_text_with_markers():
    extract_text, chunk_text = _ingest()
    text = extract_text("onboarding.pptx", _mk_pptx())
    assert "Onboarding toko" in text
    assert "registrasi akun marketplace" in text
    assert "[Slide 1]" in text                              # citations traceable to the slide
    assert chunk_text(text)                                 # chunkable, not empty


def test_binary_junk_is_rejected_not_silently_ingested():
    extract_text, _ = _ingest()
    with pytest.raises(RuntimeError):                       # zip container (xlsx/unknown)
        extract_text("data.xlsx", b"PK\x03\x04" + os.urandom(64))
    with pytest.raises(RuntimeError):                       # OLE container (old .doc/.ppt/.xls)
        extract_text("legacy.ppt", b"\xd0\xcf\x11\xe0" + os.urandom(64))
    with pytest.raises(RuntimeError):                       # raw binary with NUL bytes
        extract_text("blob.bin", b"\x00\x01\x02garbage")


def test_md_and_txt_passthrough_still_work():
    extract_text, _ = _ingest()
    assert "halo dunia" in extract_text("catatan.md", "halo dunia".encode())
    assert "plain" in extract_text("a.txt", b"plain text")


def test_page_label_derivation():
    """Citations must be human ('Slide 3', 'Page 2', 'bagian 5') — never just #chunkN."""
    from app.rag.retriever import _page_label
    assert _page_label({"text": "[Slide 3] isi", "chunk_index": 7}) == "Slide 3"
    assert _page_label({"text": "[Slide 3] a [Slide 5] b", "chunk_index": 0}) == "Slide 3–5"
    assert _page_label({"text": "[Page 2] halaman dua", "chunk_index": 0}) == "Page 2"
    assert _page_label({"text": "teks polos tanpa marker", "chunk_index": 4}) == "bagian 5"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
