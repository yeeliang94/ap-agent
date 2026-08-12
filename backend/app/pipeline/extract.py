"""Stage 3 — Extract: one focused AI worker per document.

Each worker sees ONE invoice (or one claim plus its receipts), answers once,
and is gone. Workers run in parallel up to a small cap. Results are saved on
the document row, so a re-run never pays for the same reading twice.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic_ai import BinaryContent

from .. import config
from ..model_layer import create_agent
from ..schemas_ai import ClaimFields, InvoiceFields
from .images import document_to_pngs

_INVOICE_INSTRUCTIONS = (
    "Read this vendor invoice exactly as printed. Dates in YYYY-MM-DD. "
    "Currency as a 3-letter code (RM means MYR). "
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

    async def worker(doc) -> None:
        async with sem:
            try:
                pages = [
                    BinaryContent(data=png, media_type="image/png")
                    for png in document_to_pngs(workspace / doc.filename)
                ]
                if doc.kind == "invoice":
                    agent = create_agent("extract", InvoiceFields, _INVOICE_INSTRUCTIONS)
                    result = await agent.run(["Extract the invoice fields.", *pages])
                else:  # claim — include its receipts in the same look
                    for r in receipts_by_parent.get(doc.id, []):
                        for png in document_to_pngs(workspace / r.filename):
                            pages.append(BinaryContent(data=png, media_type="image/png"))
                    agent = create_agent("extract", ClaimFields, _CLAIM_INSTRUCTIONS)
                    result = await agent.run(["Extract the claim fields.", *pages])
                doc.fields = result.output.model_dump(exclude={"low_confidence"})
                doc.confidence = result.output.low_confidence
                doc.status = "extracted"
            except Exception as exc:  # one bad document must not sink the batch
                doc.status = "error"
                doc.error = f"extraction failed: {exc}"
            on_progress()

    await asyncio.gather(
        *(worker(d) for d in docs if d.kind in ("invoice", "claim"))
    )
