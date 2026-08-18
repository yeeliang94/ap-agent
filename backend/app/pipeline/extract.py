"""Stage 3 — Extract: one focused AI worker per document.

Each worker sees ONE invoice (or one claim plus its receipts), answers once,
and is gone. Workers run in parallel up to a small cap. Results are saved on
the document row, so a re-run never pays for the same reading twice.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic_ai import BinaryContent

from .. import config, telemetry
from ..model_layer import USAGE_LIMITS, create_agent
from ..schemas_ai import ClaimFields, InvoiceFields
from .images import document_to_pngs

_INVOICE_INSTRUCTIONS = (
    "Read this vendor invoice exactly as printed. Dates in YYYY-MM-DD. "
    "Currency as a 3-letter code (RM means MYR). In description, say in one "
    "short line what the invoice is for, the way a payment listing would "
    "('Mobile lines - Jun 2026', 'Office cleaning - May 2026'); no amounts. "
    "CRITICAL: if the image is blurry, damaged, or ANY character could "
    "plausibly be misread, you MUST list each affected field in "
    "low_confidence with a short note — a wrong value presented confidently "
    "is the worst possible answer. An empty low_confidence asserts that "
    "every character was crisp and certain. Never invent values."
)

_CLAIM_INSTRUCTIONS = (
    "Read this staff expense claim form. Later images, if any, are its "
    "supporting receipts: check whether they support the claimed amount. "
    "Record any hard-to-read field in low_confidence. Never invent values."
)


async def extract_all(docs: list, workspace: Path, on_progress) -> None:
    """Run extraction workers for all invoices and claims, a few at a time."""
    sem = asyncio.Semaphore(config.EXTRACT_CONCURRENCY)
    receipts_by_parent: dict[str, list] = {}
    for d in docs:
        if d.kind == "receipt" and d.parent_id:
            receipts_by_parent.setdefault(d.parent_id, []).append(d)

    # Fields whose disagreement between two independent reads marks the
    # whole document as untrustworthy. These identify the document and the
    # money — exactly what must never be confidently wrong.
    KEY_FIELDS = {
        "invoice": ("vendor", "invoice_number", "date", "amount", "currency"),
        "claim": ("claimant", "amount", "currency"),
    }

    async def worker(doc) -> None:
        async with sem:
            try:
                pages = [
                    BinaryContent(data=png, media_type="image/png")
                    for png in document_to_pngs(workspace / doc.filename)
                ]
                if doc.kind == "invoice":
                    agent = create_agent("extract", InvoiceFields, _INVOICE_INSTRUCTIONS)
                    prompt = ["Extract the invoice fields.", *pages]
                else:  # claim — include its receipts in the same look
                    for r in receipts_by_parent.get(doc.id, []):
                        for png in document_to_pngs(workspace / r.filename):
                            pages.append(BinaryContent(data=png, media_type="image/png"))
                    agent = create_agent("extract", ClaimFields, _CLAIM_INSTRUCTIONS)
                    prompt = ["Extract the claim fields.", *pages]

                # Double-read: two INDEPENDENT reads of the same document.
                # A crisp document reads identically twice; a degraded one
                # doesn't — and models misread degraded scans confidently,
                # so self-reported doubt alone cannot be trusted.
                r1, r2 = await asyncio.gather(
                    agent.run(prompt, usage_limits=USAGE_LIMITS),
                    agent.run(prompt, usage_limits=USAGE_LIMITS),
                )
                first, second = r1.output, r2.output
                doc.fields = first.model_dump(exclude={"low_confidence"})
                confidence = {**second.low_confidence, **first.low_confidence}
                for key in KEY_FIELDS[doc.kind]:
                    v1, v2 = getattr(first, key), getattr(second, key)
                    if v1 != v2:
                        confidence[key] = (
                            f"two independent reads disagree: {v1!r} vs {v2!r}"
                        )
                doc.confidence = confidence
                doc.status = "extracted"
            except Exception as exc:  # one bad document must not sink the batch
                # Named rather than dumped: "extraction failed: <raw
                # exception>" is what made a proxy rejecting every request
                # look like 22 unrelated bad files. The raw text still
                # reaches the log, via the run diary in runner.py.
                doc.status = "error"
                doc.error = telemetry.describe_failure(exc)
            on_progress()

    await asyncio.gather(
        *(worker(d) for d in docs if d.kind in ("invoice", "claim"))
    )
