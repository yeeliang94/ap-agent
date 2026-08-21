"""The receipt outline: the AI's approximate [left, top, right, bottom]
box travels from the page read to the flag's cite and is drawn on the
preview; a bad box is dropped, never a reason to fail the read, and runs
from before the box keep the third-of-page shading."""
from __future__ import annotations

import io
from decimal import Decimal

from PIL import Image

from app.claims import checks, evidence


def _receipt(position="right", box=None, amount="45.00"):
    return evidence.ReceiptRead(vendor="Sunlight Taxi", date="2026-07-07", amount=Decimal(amount),
                                currency="MYR", position=position, box=box)


def test_a_malformed_box_is_dropped_and_a_fraction_box_is_scaled():
    assert _receipt(box=[70, 10, 98, 60]).box == [70, 10, 98, 60]
    assert _receipt(box=[0.7, 0.1, 0.98, 0.6]).box == [70, 10, 98, 60]
    assert _receipt(box=[98, 60, 70, 10]).box == [70, 10, 98, 60]      # corners given backwards
    assert _receipt(box=[120, -5, 140, 50]).box == [100, 0, 100, 50] or _receipt(box=[120, -5, 140, 50]).box is None
    assert _receipt(box=[1, 2, 3]).box is None                           # three numbers
    assert _receipt(box=[float("inf"), 0, 50, 50]).box is None           # infinite, not an OverflowError
    assert _receipt(box=[float("nan"), 0, 50, 50]).box is None
    assert _receipt(box=["a", "b", "c", "d"]).box is None                # not numbers
    assert _receipt(box=[10, 10, 10, 10]).box is None                    # no area
    assert _receipt(box=None).box is None
    # the read itself is still valid without a box (scripted/fake models, older prompts)
    page = evidence.PageRead(kind="receipts", receipts=[_receipt()], why="x")
    assert evidence.PageRead.model_validate(page.model_dump(mode="json")).receipts[0].box is None


def test_merged_reads_keep_the_first_box_or_the_twins_and_the_cite_carries_it():
    a = evidence.PageRead(kind="receipts", receipts=[_receipt(box=[66, 5, 99, 70])], why="x")
    b = evidence.PageRead(kind="receipts", receipts=[_receipt(box=[64, 4, 98, 72])], why="x")
    out, _ = evidence._merge_receipt_reads(a, b)
    assert out[0]["box"] == [66, 5, 99, 70]
    a2 = evidence.PageRead(kind="receipts", receipts=[_receipt(box=None)], why="x")
    out2, _ = evidence._merge_receipt_reads(a2, b)
    assert out2[0]["box"] == [64, 4, 98, 72]
    cite = checks._ev_cite({"file": "r.pdf", "page": 1, "position": "right", "box": out[0]["box"]})
    assert cite == {"file": "r.pdf", "page": 1, "position": "right", "box": [66, 5, 99, 70]}
    assert "box" not in checks._ev_cite({"file": "r.pdf", "page": 1, "position": "right", "box": None})


def _white_page(tmp_path, name="page.png", size=(400, 300)):
    p = tmp_path / name
    Image.new("RGB", size, (255, 255, 255)).save(p)
    return p


def test_render_page_outlines_the_box_and_shades_the_rest(tmp_path):
    page = _white_page(tmp_path)
    png = evidence.render_page(page, 1, highlight="right", box=[50, 25, 75, 75])
    img = Image.open(io.BytesIO(png)).convert("RGB")
    inside = img.getpixel((250, 150))          # centre of the box: untouched white
    outside = img.getpixel((40, 40))           # far outside: shaded
    edge = img.getpixel((198, 150))            # on the left outline (50% of 400 = 200, minus pad)
    assert inside == (255, 255, 255)
    assert outside != (255, 255, 255) and outside[0] < 240
    assert edge[1] > edge[0] and edge[1] > edge[2]   # the outline is the green cite colour
    # no box: the older third-of-page shading still applies
    png2 = evidence.render_page(page, 1, highlight="right")
    img2 = Image.open(io.BytesIO(png2)).convert("RGB")
    assert img2.getpixel((40, 150)) != (255, 255, 255) and img2.getpixel((330, 150)) == (255, 255, 255)
    # a nonsense box falls back to the third, never crashes
    png3 = evidence.render_page(page, 1, highlight="right", box=[1, 2, 3])
    assert Image.open(io.BytesIO(png3)).convert("RGB").getpixel((330, 150)) == (255, 255, 255)
