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


def document_to_pngs(path: Path) -> list[bytes]:
    """Return the document as one PNG per page, downsized."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        images: list[bytes] = []
        with pymupdf.open(path) as pdf:
            for page in pdf:
                # 150 dpi is plenty for typed invoices; scanned faxes would need more.
                pix = page.get_pixmap(dpi=150)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
                images.append(_downsize(img))
        return images
    if suffix in (".png", ".jpg", ".jpeg", ".webp"):
        return [_downsize(Image.open(path))]
    raise ValueError(f"Unsupported document type: {path.name}")


def _downsize(img: Image.Image) -> bytes:
    if max(img.size) > MAX_EDGE:
        img.thumbnail((MAX_EDGE, MAX_EDGE))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
