"""Document and image tools (H4): `inspect_document`, `render_page`,
`crop_page`. Text and thumbnails only — a PDF's links, JavaScript, forms
and embedded files are COUNTED so the reviewer knows they are there, never
opened, followed or run.
"""
from __future__ import annotations

import io
from pathlib import Path

from .contracts import MAX_PAGE_PIXELS, MAX_TEXT_CHARS

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
PAGE_TEXT_CHARS = 4000


def inspect_document(path: Path, max_pages: int = 200) -> dict:
    suffix = path.suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        from PIL import Image

        with Image.open(path) as img:
            w, h = img.size
        return {"pages": 1, "text_blocks": [], "images": [{"page": 1, "width": w, "height": h}],
                "metadata": {}, "links": 0, "javascript": False, "embedded_files": 0, "kind": "image"}
    if suffix != ".pdf":
        return {"pages": 0, "text_blocks": [], "metadata": {}, "links": 0, "javascript": False,
                "embedded_files": 0, "kind": "other", "note": "not a PDF or image; text not extracted"}
    import pymupdf

    with pymupdf.open(path) as pdf:
        n = pdf.page_count
        blocks, links, js = [], 0, False
        total = 0
        for i, page in enumerate(pdf, 1):
            if i > max_pages:
                break
            text = page.get_text("text") or ""
            links += len(page.get_links())
            if total < MAX_TEXT_CHARS and text.strip():
                snippet = text[:PAGE_TEXT_CHARS]
                total += len(snippet)
                blocks.append({"page": i, "text": snippet, "truncated": len(text) > PAGE_TEXT_CHARS})
        # Scripts are looked for by name in the object table (bounded) and
        # reported — pymupdf never runs them and neither do we.
        try:
            for x in range(1, min(pdf.xref_length(), 400)):
                obj = pdf.xref_object(x) or ""
                if "/JavaScript" in obj or "/JS" in obj:
                    js = True
                    break
        except Exception:
            js = False
        try:
            embedded = pdf.embfile_count()
        except Exception:
            embedded = 0
        meta = {k: str(v)[:120] for k, v in (pdf.metadata or {}).items() if v and k in ("title", "author", "creator", "producer")}
        return {"pages": n, "text_blocks": blocks, "metadata": meta, "links": links, "javascript": js,
                "embedded_files": embedded, "kind": "pdf", "text_truncated": total >= MAX_TEXT_CHARS,
                "note": "links, scripts and embedded files are counted, never opened or run"}


def page_text(path: Path, max_pages: int = 200):
    """(page, text) per page with text — the search index."""
    if path.suffix.lower() != ".pdf":
        return
    import pymupdf

    with pymupdf.open(path) as pdf:
        for i, page in enumerate(pdf, 1):
            if i > max_pages:
                break
            text = page.get_text("text") or ""
            if text.strip():
                yield i, text


def render_page(path: Path, page: int, full: bool = False) -> tuple[bytes, int, int]:
    """PNG bytes plus size, longest edge capped."""
    from PIL import Image

    from ..evidence import render_page as _render

    png = _render(path, page, full=full)
    img = Image.open(io.BytesIO(png))
    if max(img.size) > MAX_PAGE_PIXELS:
        img.thumbnail((MAX_PAGE_PIXELS, MAX_PAGE_PIXELS))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        png = buf.getvalue()
    return png, img.size[0], img.size[1]


def crop_page(path: Path, page: int, region: list[int]) -> tuple[bytes, int, int]:
    """A crop of the FULL-resolution page; region = x0, y0, x1, y1 in the
    full render's pixels. Bounded to the page and to MAX_PAGE_PIXELS."""
    from PIL import Image

    from ..evidence import _render_full

    img = _render_full(path, page)
    w, h = img.size
    x0, y0, x1, y1 = [int(v) for v in region]
    x0, y0 = max(0, min(x0, w - 1)), max(0, min(y0, h - 1))
    x1, y1 = max(x0 + 1, min(x1, w)), max(y0 + 1, min(y1, h))
    crop = img.crop((x0, y0, x1, y1))
    if max(crop.size) > MAX_PAGE_PIXELS:
        crop.thumbnail((MAX_PAGE_PIXELS, MAX_PAGE_PIXELS))
    buf = io.BytesIO()
    crop.convert("RGB").save(buf, format="PNG")
    return buf.getvalue(), crop.size[0], crop.size[1]
