"""The evidence page inventory: every receipt and map trip, and WHERE it is.

Every receipt-bundle file is split into pages. Each page is read by the AI
("extract" role) to say what it is — a receipts page, a mileage-map page,
a copy of the report, other — and, in the same read, to list what is on
it: for a receipts page the receipts (vendor, date, amount, currency,
position left/middle/right, anything hard to read); for a map page the
trips (date, purpose, from, to, whether the text says "and back", the km
printed on the map).

Two habits from the invoice pipeline, because scans are misread
CONFIDENTLY:
  - receipts pages are read TWICE, independently; a disagreement on an
    amount or a date marks that receipt low-confidence, and every flag it
    appears in says so;
  - map pages are re-rendered at full resolution (the km on those
    screenshots is tiny) and re-read for the km. A km that cannot be read
    is recorded as unreadable — never guessed.

Nothing here decides anything. checks.py does the matching.
"""
from __future__ import annotations

import asyncio
import io
import logging
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_ai import BinaryContent

from ..model_layer import USAGE_LIMITS, create_agent
from ..pipeline.images import IMAGE_SUFFIXES, MAX_EDGE, document_page_png, document_to_pngs
from ..schemas_ai import CURRENCY_PATTERN, DATE_PATTERN

log = logging.getLogger("claims.evidence")

# Full-resolution render for map pages (and for the preview's zoom).
FULL_DPI = 300
FULL_MAX_EDGE = 3000

# How long ONE AI call may take before the employee is failed with a
# reason instead of the whole run hanging. The provider's own client
# retries rate limits with long back-offs behind a 10-minute default
# timeout; that is exactly the silent stall this cap turns into a named
# failure ("the service did not answer in time"), retryable per employee.
AI_CALL_TIMEOUT = 180

# Page reads across ALL workers at once. Five workers each reading four
# pages in parallel is twenty vision calls — over the provider's rate
# limit, which is what made whole employees stall.
PAGE_READ_CONCURRENCY = 5
_page_sem: asyncio.Semaphore | None = None


def page_semaphore() -> asyncio.Semaphore:
    global _page_sem
    if _page_sem is None:
        _page_sem = asyncio.Semaphore(PAGE_READ_CONCURRENCY)
    return _page_sem


async def ai_call(coro, what: str = "the AI"):
    """Await one AI call under the timeout; a stall raises TimeoutError
    with a plain reason (telemetry names it 'did not answer in time')."""
    try:
        return await asyncio.wait_for(coro, timeout=AI_CALL_TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"{what} did not answer within {AI_CALL_TIMEOUT}s") from exc

Position = Literal["left", "middle", "right"]


class ReceiptRead(BaseModel):
    vendor: str = Field(min_length=1, max_length=120)
    date: str = Field(default="", description="YYYY-MM-DD, or empty if not printed / unreadable")
    # Money is Decimal end to end (a JSON number or a quoted figure both
    # arrive as Decimal); it is never carried through a binary float.
    amount: Decimal = Field(gt=0, allow_inf_nan=False, description="the receipt TOTAL")
    currency: str = Field(pattern=CURRENCY_PATTERN, description="3-letter code; RM means MYR")
    position: Position = Field(description="where on the page: left / middle / right third")
    # field -> note, ONLY for fields that were hard to read
    low_confidence: dict[str, str] = Field(default_factory=dict)


class TripRead(BaseModel):
    date: str = Field(default="", description="YYYY-MM-DD from the narrative, or empty")
    purpose: str = Field(default="", max_length=200)
    from_: str = Field(default="", max_length=200, alias="from")
    to: str = Field(default="", max_length=200)
    return_trip: bool = Field(description="True when the narrative says 'and back' / return")
    km_printed: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False,
                                       description="the km printed on the map; null if it cannot be read")
    low_confidence: dict[str, str] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


class PageRead(BaseModel):
    kind: Literal["receipts", "map", "report_copy", "other"]
    receipts: list[ReceiptRead] = Field(default_factory=list)
    trips: list[TripRead] = Field(default_factory=list)
    why: str = Field(max_length=200)


_INSTRUCTIONS = (
    "You are reading ONE page from an employee's expense claim bundle. First "
    "say what kind of page it is: 'receipts' (one or more till/shop receipts, "
    "often scanned side by side), 'map' (a mileage claim page: a narrative "
    "of trips — date, purpose, from, to, whether it says 'and back' — with "
    "route map screenshots that print a distance in km), 'report_copy' (a "
    "print of the expense report table), or 'other'.\n"
    "For a receipts page, list EVERY receipt on it: vendor, date (as "
    "YYYY-MM-DD; Malaysian receipts print DAY/MONTH/YEAR, so 10/07/2026 is 10 "
    "July 2026; empty if none), the receipt TOTAL amount, currency (RM means "
    "MYR), and its position on the page: left / middle / right third (a lone "
    "receipt is 'middle'; two receipts are left and right). CRITICAL: if any "
    "digit of the amount or date is smudged, blurred or could plausibly be "
    "misread, list that field in low_confidence with a short note — a wrong "
    "value stated confidently is the worst possible answer. Never invent.\n"
    "For a map page, list every trip: the date, purpose, from, to, whether "
    "the narrative says 'and back' (return_trip), and the km figure printed "
    "on the map for that trip. If the km cannot be read, set km_printed to "
    "null and say so in low_confidence — do NOT estimate it.\n"
    "If the page shows both receipts and maps, choose the kind that covers "
    "most of the page and list only that kind."
)


# ---- rendering -----------------------------------------------------------------

def render_page(path: Path, page: int = 1, highlight: str = "", full: bool = False) -> bytes:
    """One page as PNG. page is 1-based; raises IndexError when out of
    range and ValueError for a file type that cannot be rendered (the
    callers map those to 404 / 415). ONLY the requested page is
    rasterised — a single-page request on a 200-page bundle used to
    render all 200. highlight shades everything BUT the named third of
    the page, so a receipt's cited position stands out in the preview."""
    from PIL import Image, ImageDraw

    if full:
        img = _render_full(path, page)
    else:
        img = Image.open(io.BytesIO(document_page_png(path, page))).convert("RGB")
    if highlight in ("left", "middle", "right"):
        w, h = img.size
        thirds = {"left": (0, w // 3), "middle": (w // 3, 2 * w // 3), "right": (2 * w // 3, w)}
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        x0, x1 = thirds[highlight]
        d.rectangle((0, 0, x0, h), fill=(120, 120, 120, 110))
        d.rectangle((x1, 0, w, h), fill=(120, 120, 120, 110))
        d.rectangle((x0, 0, x1 - 1, h - 1), outline=(14, 124, 107, 255), width=6)
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _render_full(path: Path, page: int):
    """The requested page ALONE at FULL_DPI — same contract as
    render_page: IndexError out of range, ValueError for a type that
    cannot be rendered."""
    from PIL import Image

    suffix = path.suffix.lower()
    if suffix == ".pdf":
        import pymupdf

        with pymupdf.open(path) as pdf:
            if page < 1 or page > pdf.page_count:
                raise IndexError(page)
            pix = pdf[page - 1].get_pixmap(dpi=FULL_DPI)
            img = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    elif suffix in IMAGE_SUFFIXES:
        if page != 1:
            raise IndexError(page)
        img = Image.open(path).convert("RGB")
    else:
        raise ValueError(f"Unsupported document type: {path.name}")
    if max(img.size) > FULL_MAX_EDGE:
        img.thumbnail((FULL_MAX_EDGE, FULL_MAX_EDGE))
    return img


def page_pngs(path: Path) -> list[bytes]:
    """Every page at the model's normal size (MAX_EDGE)."""
    return document_to_pngs(path)


# ---- reading ------------------------------------------------------------------

class BudgetExceeded(Exception):
    """The worker's AI request cap is used up; nothing more is sent."""


class Usage:
    """A running count of AI requests and tokens for one worker, with an
    optional cap that is checked BEFORE every model call (reserve), so a
    big bundle cannot schedule hundreds of reads past the limit."""

    def __init__(self, cap: int | None = None) -> None:
        self.requests = 0
        self.tokens = 0
        self.cap = cap

    def reserve(self) -> None:
        """Call before sending a request; raises when the cap is reached."""
        if self.cap is not None and self.requests >= self.cap:
            raise BudgetExceeded(f"{self.requests} of {self.cap} AI requests used")

    def add(self, result) -> None:
        n = 1
        try:
            # pydantic-ai has moved between a usage() method and a usage
            # attribute; take whichever this version offers.
            u = result.usage
            u = u() if callable(u) else u
            # one agent run may be several provider requests (validation retries)
            n = int(getattr(u, "requests", 0) or 1)
            total = getattr(u, "total_tokens", None)
            if not total:
                total = (getattr(u, "input_tokens", 0) or getattr(u, "request_tokens", 0) or 0) + \
                        (getattr(u, "output_tokens", 0) or getattr(u, "response_tokens", 0) or 0)
            self.tokens += int(total or 0)
        except Exception:
            pass
        self.requests += max(1, n)


async def read_bundle(path: Path, rel_path: str, usage: Usage,
                      sem: asyncio.Semaphore | None = None,
                      context: str = "") -> tuple[list[dict], list[dict], list[dict], list[str]]:
    """Read every page of one receipt bundle.

    Returns (receipts, trips, pages, notes):
      receipts — dicts: file, page, position, vendor, date, amount (text),
                 currency, confidence {field: note}
      trips    — dicts: file, page, date, purpose, from, to, return_trip,
                 km_printed (text or None), confidence
      pages    — one dict per page: file, page, kind, why
      notes    — plain lines for the diary
    context is the run's instructions (report_reader.run_context), shown
    with every page and never trusted over what the page prints.
    """
    agent = create_agent("extract", PageRead, _INSTRUCTIONS)
    sem = sem or page_semaphore()
    pngs = page_pngs(path)
    receipts: list[dict] = []
    trips: list[dict] = []
    pages: list[dict] = []
    notes: list[str] = []

    async def one(idx: int, png: bytes):
        async with sem:
            return await _read_page(agent, path, idx + 1, png, usage, context)

    # Every page read finishes (or fails fast at the budget) before the
    # first failure is raised, so no read is left running unobserved.
    results = await asyncio.gather(*(one(i, p) for i, p in enumerate(pngs)), return_exceptions=True)
    failed = next((r for r in results if isinstance(r, BaseException)), None)
    if failed is not None:
        raise failed
    for idx, (kind, why, page_receipts, page_trips, page_notes) in enumerate(results, 1):
        pages.append({"file": rel_path, "page": idx, "kind": kind, "why": why})
        for r in page_receipts:
            receipts.append({"file": rel_path, "page": idx, **r})
        for t in page_trips:
            trips.append({"file": rel_path, "page": idx, **t})
        notes += [f"{rel_path} p.{idx}: {n}" for n in page_notes]
    return receipts, trips, pages, notes


async def _read_page(agent, path: Path, page_no: int, png: bytes, usage: Usage, context: str = ""):
    """One page: classify + read; receipts pages twice; maps at full res."""
    prompt = ["Read this page." + (context or ""), BinaryContent(data=png, media_type="image/png")]
    usage.reserve()
    first = await ai_call(agent.run(prompt, usage_limits=USAGE_LIMITS), f"reading {path.name} p.{page_no}")
    usage.add(first)
    r1 = first.output
    notes: list[str] = []
    if r1.kind == "receipts":
        usage.reserve()
        second = await ai_call(agent.run(prompt, usage_limits=USAGE_LIMITS), f"re-reading {path.name} p.{page_no}")
        usage.add(second)
        r2 = second.output
        receipts, n = _merge_receipt_reads(r1, r2)
        notes += n
        return "receipts", r1.why, receipts, [], notes
    if r1.kind == "map":
        # Re-read at full resolution: the km on the screenshots is tiny.
        # A whole 300 dpi page is shrunk again by the model, so the page is
        # cut into BANDS, each small enough to be read at true resolution.
        try:
            r2 = await _read_map_bands(agent, path, page_no, usage)
        except BudgetExceeded:
            raise
        except Exception as exc:  # the normal read still stands — marked as such
            log.warning("full-resolution re-read of %s p.%d failed: %s", path.name, page_no, exc)
            r2 = r1
            for t in r1.trips:
                t.low_confidence.setdefault(
                    "km_printed", "read at normal resolution only — the full-resolution re-read failed "
                                  f"({type(exc).__name__}); small figures may be misread")
            notes.append(f"full-resolution re-read failed ({type(exc).__name__}); km figures are "
                         "from the normal read only and marked low-confidence")
        trips, n = _merge_trip_reads(r1, r2)
        notes += n
        return "map", r1.why, [], trips, notes
    return r1.kind, r1.why, [], [], notes


# The tallest band the model reads at true resolution (its own limit is
# about 2000 px on the long side); bands overlap so a trip cut by the
# boundary is whole in one of them.
BAND_HEIGHT = 1500
BAND_OVERLAP = 200


async def _read_map_bands(agent, path: Path, page_no: int, usage: Usage) -> PageRead:
    """The full-resolution page, read band by band; trips merged."""
    full = _render_full(path, page_no)
    w, h = full.size
    bands = []
    top = 0
    while True:
        bottom = min(h, top + BAND_HEIGHT)
        bands.append(full.crop((0, top, w, bottom)))
        if bottom >= h:
            break
        top = bottom - BAND_OVERLAP
    trips: list[TripRead] = []
    for i, band in enumerate(bands, 1):
        buf = io.BytesIO()
        band.save(buf, format="PNG")
        usage.reserve()
        result = await ai_call(agent.run(
            [f"This is part {i} of {len(bands)} of a mileage-map page, at full resolution. "
             "List the trips whose narrative AND map are visible in this part; read the km "
             "figure printed on each map carefully, digit by digit.",
             BinaryContent(data=buf.getvalue(), media_type="image/png")],
            usage_limits=USAGE_LIMITS), f"re-reading {path.name} p.{page_no} band {i}")
        usage.add(result)
        for t in result.output.trips:
            same = next((x for x in trips if x.date == t.date and (x.to == t.to or x.purpose == t.purpose)), None)
            if same is None:
                trips.append(t)
            elif same.km_printed is None and t.km_printed is not None:
                trips[trips.index(same)] = t
            elif t.km_printed is not None and same.km_printed is not None and abs(t.km_printed - same.km_printed) > Decimal("0.05"):
                same.low_confidence["km_printed"] = f"two bands read {same.km_printed} and {t.km_printed}"
    return PageRead(kind="map", trips=trips, why="full-resolution bands")


def _money_text(value) -> str:
    """A read amount as a cent string. Half a cent rounds UP (the way a
    till prints it), never to the nearest even cent."""
    return str(_dec(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(repr(value))


def _km_text(value) -> str:
    return str(_dec(value).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _merge_receipt_reads(a: PageRead, b: PageRead) -> tuple[list[dict], list[str]]:
    """Pair the two reads by position; disagreement → low confidence."""
    notes: list[str] = []
    by_pos_b: dict[str, list[ReceiptRead]] = {}
    for r in b.receipts:
        by_pos_b.setdefault(r.position, []).append(r)
    if b.kind != "receipts":
        notes.append("second read did not see a receipts page — every receipt marked low-confidence")
    out = []
    for r in a.receipts:
        conf = dict(r.low_confidence)
        alt: dict = {}  # the second read's value where the two disagree
        twin = by_pos_b.get(r.position, [None]).pop(0) if by_pos_b.get(r.position) else None
        if b.kind != "receipts":
            conf.setdefault("page", "the two reads disagreed on what this page is")
        elif twin is None:
            conf.setdefault("receipt", "the second read did not see a receipt at this position")
        else:
            conf.update({k: v for k, v in twin.low_confidence.items() if k not in conf})
            if _money_text(twin.amount) != _money_text(r.amount):
                conf["amount"] = f"two independent reads disagree: {_money_text(r.amount)} vs {_money_text(twin.amount)}"
                alt["amount_alt"] = _money_text(twin.amount)
            if twin.date != r.date and twin.date and r.date:
                conf["date"] = f"two independent reads disagree: {r.date} vs {twin.date}"
                if _iso(twin.date):
                    alt["date_alt"] = twin.date
            if twin.currency != r.currency:
                conf["currency"] = f"two independent reads disagree: {r.currency} vs {twin.currency}"
        out.append({"vendor": r.vendor, "date": r.date if _iso(r.date) else "",
                    "amount": _money_text(r.amount), "currency": r.currency,
                    "position": r.position, "confidence": conf, **alt})
    # Whatever the second read saw and the first did not is kept — two
    # receipts can share a position, so the leftovers of a position that
    # already paired are extra receipts, not duplicates of one.
    extras = [r for pos in list(by_pos_b) for r in by_pos_b[pos]]
    if b.kind == "receipts" and extras:
        notes.append(f"second read saw {len(b.receipts)} receipts, first saw {len(a.receipts)} — "
                     "the extra one(s) were added as low-confidence")
        for r in extras:
            out.append({"vendor": r.vendor, "date": r.date if _iso(r.date) else "",
                        "amount": _money_text(r.amount), "currency": r.currency,
                        "position": r.position,
                        "confidence": {**r.low_confidence,
                                       "receipt": "only one of two reads saw this receipt"}})
    return out, notes


def _merge_trip_reads(a: PageRead, b: PageRead) -> tuple[list[dict], list[str]]:
    """The narrative from the normal read; the km from the full-resolution
    read where the two disagree, and 'unreadable' when neither could."""
    notes: list[str] = []
    out = []
    b_trips = list(b.trips)
    for t in a.trips:
        conf = dict(t.low_confidence)
        twin = next((x for x in b_trips if x.date == t.date and (x.to == t.to or x.purpose == t.purpose)), None)
        if twin is None and b_trips and len(a.trips) == len(b_trips):
            twin = b_trips[a.trips.index(t)]
        km = t.km_printed
        if twin is not None:
            b_trips = [x for x in b_trips if x is not twin]
            if twin.km_printed is not None:
                if km is not None and abs(twin.km_printed - km) > Decimal("0.05"):
                    # a resolved disagreement (the full-resolution read wins by
                    # design) — noted, but not a doubt about the figure used
                    conf["km_normal_read"] = (f"normal read {km}, full-resolution read {twin.km_printed} — "
                                              "the full-resolution figure is used")
                km = twin.km_printed
            conf.update({k: v for k, v in twin.low_confidence.items() if k not in conf})
        if km is None:
            conf.setdefault("km_printed", "km unreadable on the map")
        out.append({"date": t.date if _iso(t.date) else "", "purpose": t.purpose, "from": t.from_,
                    "to": t.to, "return_trip": bool(t.return_trip),
                    "km_printed": None if km is None else _km_text(km),
                    "confidence": conf})
    return out, notes


def _iso(text: str) -> bool:
    import re

    return bool(re.match(DATE_PATTERN, text or ""))


__all__ = ["read_bundle", "render_page", "Usage", "BudgetExceeded", "PageRead", "MAX_EDGE"]
