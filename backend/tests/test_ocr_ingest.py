"""Offline tests for OCR-in-ingest (app/rag/ocr.py + ingest.extract_text).

No real Tesseract needed: ocr.available()/image_to_text() are monkeypatched. We build a
REAL in-memory .pptx (text box + a generated picture) and assert that:
  - with ocr=True, image text is appended under the same [Slide N] marker;
  - with ocr=False, the picture is ignored (only real text extracted);
  - the ocr helpers never raise on junk / when unavailable.
"""
import io

from PIL import Image
from pptx import Presentation
from pptx.util import Inches

from app.rag import ocr as ocrmod
from app.rag.ingest import extract_text, _iter_pictures


def _pptx_with_text_and_image():
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])                 # blank layout
    tb = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1))
    tb.text_frame.text = "Judul Slide Teks Asli"
    buf = io.BytesIO(); Image.new("RGB", (320, 200), "white").save(buf, "PNG"); buf.seek(0)
    slide.shapes.add_picture(buf, Inches(1), Inches(3), Inches(3), Inches(2))
    out = io.BytesIO(); prs.save(out); return out.getvalue()


def test_pptx_ocr_appends_image_text(monkeypatch):
    monkeypatch.setattr(ocrmod, "available", lambda: True)
    monkeypatch.setattr(ocrmod, "image_to_text", lambda data, lang=None: "TEKS DARI GAMBAR")
    text = extract_text("deck.pptx", _pptx_with_text_and_image(), ocr=True)
    assert "[Slide 1]" in text
    assert "Judul Slide Teks Asli" in text          # real text box
    assert "TEKS DARI GAMBAR" in text               # OCR'd picture text, under the slide marker


def test_pptx_without_ocr_ignores_image(monkeypatch):
    monkeypatch.setattr(ocrmod, "available", lambda: True)               # available, but...
    monkeypatch.setattr(ocrmod, "image_to_text", lambda data, lang=None: "TEKS DARI GAMBAR")
    text = extract_text("deck.pptx", _pptx_with_text_and_image(), ocr=False)   # ...ocr not requested
    assert "Judul Slide Teks Asli" in text
    assert "TEKS DARI GAMBAR" not in text


def test_pptx_ocr_skipped_when_unavailable(monkeypatch):
    monkeypatch.setattr(ocrmod, "available", lambda: False)              # no tesseract on host
    called = {"n": 0}
    monkeypatch.setattr(ocrmod, "image_to_text", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or "X")
    text = extract_text("deck.pptx", _pptx_with_text_and_image(), ocr=True)
    assert "Judul Slide Teks Asli" in text
    assert called["n"] == 0                         # never even attempted OCR


def test_iter_pictures_finds_the_image():
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    buf = io.BytesIO(); Image.new("RGB", (100, 100), "blue").save(buf, "PNG"); buf.seek(0)
    slide.shapes.add_picture(buf, Inches(1), Inches(1), Inches(2), Inches(2))
    blobs = list(_iter_pictures(slide.shapes))
    assert len(blobs) == 1 and isinstance(blobs[0], (bytes, bytearray))


def test_ocr_helpers_never_raise():
    # junk bytes / unavailable engine -> '' (never an exception); available() -> bool
    assert ocrmod.image_to_text(b"not-an-image") == ""
    assert ocrmod.image_to_text(b"") == ""
    assert isinstance(ocrmod.available(), bool)
