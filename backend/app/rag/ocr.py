"""Offline OCR via Tesseract (pytesseract + Pillow).

Reads text that lives INSIDE images — screenshots, infographics, charts pasted as
pictures, scanned slides/pages — which python-pptx / pypdf text extraction cannot see.

BEST-EFFORT by design: if pytesseract/Pillow are not installed, OR the tesseract binary
is missing, available() is False and callers simply skip OCR — ingest never breaks and
plain text extraction still works. Everything runs ON THE SERVER: no image ever leaves
the host (safe for PII), unlike a cloud OCR API.

Config (env):
  OCR_LANG          language(s) for tesseract, default 'ind+eng' (needs those traineddata).
  TESSERACT_CMD     absolute path to the tesseract binary (Windows installs are often not on PATH).
  OCR_MIN_IMAGE_PX  skip images whose largest side is below this (tiny icons/logos), default 80.
"""
import io
import os

_LANG = os.environ.get("OCR_LANG", "ind+eng")
_MIN_SIDE = int(os.environ.get("OCR_MIN_IMAGE_PX", "80") or 80)
_MIN_TEXT = 3                      # drop near-empty OCR noise

_available = None                  # cached availability check


def available() -> bool:
    """True only when pytesseract + Pillow import AND the tesseract binary is reachable."""
    global _available
    if _available is not None:
        return _available
    try:
        import pytesseract
        from PIL import Image  # noqa: F401
        exe = os.environ.get("TESSERACT_CMD")
        if exe:
            pytesseract.pytesseract.tesseract_cmd = exe
        pytesseract.get_tesseract_version()      # raises if the binary is missing
        _available = True
    except Exception:
        _available = False
    return _available


def image_to_text(data: bytes, lang: str = None) -> str:
    """OCR one image (bytes) -> recognized text, or '' on any problem. NEVER raises.

    Falls back through the requested language -> 'eng' -> tesseract default, so a missing
    'ind' traineddata degrades instead of erroring."""
    if not data or not available():
        return ""
    try:
        import pytesseract
        from PIL import Image
    except Exception:
        return ""
    try:
        img = Image.open(io.BytesIO(data))
        w, h = img.size
        if max(w, h) < _MIN_SIDE:                # tiny decorative image -> skip (noise)
            return ""
    except Exception:
        return ""                                # not a decodable image
    for l in [lang or _LANG, "eng", None]:
        try:
            txt = (pytesseract.image_to_string(img, lang=l) if l
                   else pytesseract.image_to_string(img)) or ""
            txt = " ".join(txt.split())          # collapse whitespace/newlines
            return txt if len(txt) >= _MIN_TEXT else ""
        except Exception:
            continue
    return ""
