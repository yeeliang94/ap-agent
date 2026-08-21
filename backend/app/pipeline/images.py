"""Turn any batch document into model-ready images.

Two jobs, both about cost and reliability:
  - PDFs become PNG images (vision models read images, and page renders
    are predictable — no font/encoding surprises).
  - Oversized photos are downsized before sending: a receipt does not need
    to be a 12 MB picture, and tokens are billed per image size.
"""
from __future__ import annotations

import io
from pathlib import Path

import pymupdf
from PIL import Image

# Longest edge sent to a model. Big enough to read small print, small
# enough to keep the per-document token bill down.
MAX_EDGE = 1400
# 150 dpi is plenty for typed invoices; scanned faxes would need more.
PAGE_DPI = 150
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


def document_page_count(path: Path) -> int:
    """Return a preview page count without rasterising the document."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        with pymupdf.open(path) as pdf:
            return pdf.page_count
    if suffix in IMAGE_SUFFIXES:
        return 1
    raise ValueError(f"Unsupported document type: {path.name}")


def document_page_png(path: Path, page: int) -> bytes:
    """ONE page (1-based) as a downsized PNG — and only that page is
    rasterised. A single-page request must never cost a whole bundle:
    `document_to_pngs` on a 200-page PDF renders 200 pages, so every
    single-page caller (the tool harness's `render_page`, the preview
    route) comes here instead.

    IndexError when the page is out of range; ValueError when the file
    type cannot be rendered — the contract the callers map to 404 / 415."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        with pymupdf.open(path) as pdf:
            if page < 1 or page > pdf.page_count:
                raise IndexError(page)
            pix = pdf[page - 1].get_pixmap(dpi=PAGE_DPI)
            return _downsize(Image.open(io.BytesIO(pix.tobytes("png"))))
    if suffix in IMAGE_SUFFIXES:
        if page != 1:
            raise IndexError(page)
        return _downsize(Image.open(path))
    raise ValueError(f"Unsupported document type: {path.name}")


def document_to_pngs(path: Path) -> list[bytes]:
    """Return the document as one PNG per page, downsized. For a single
    page use `document_page_png` — this one renders everything."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        images: list[bytes] = []
        with pymupdf.open(path) as pdf:
            for page in pdf:
                pix = page.get_pixmap(dpi=PAGE_DPI)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                images.append(_downsize(img))
        return images
    if suffix in IMAGE_SUFFIXES:
        return [_downsize(Image.open(path))]
    raise ValueError(f"Unsupported document type: {path.name}")


def _downsize(img: Image.Image) -> bytes:
    if max(img.size) > MAX_EDGE:
        img.thumbnail((MAX_EDGE, MAX_EDGE))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
