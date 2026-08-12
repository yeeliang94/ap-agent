"""Stage 2 — Sort: one cheap AI question per document.

"Is this an invoice, a staff claim, or a receipt?" Nothing else. Receipts are
then attached to a claim by a simple filename convention first, model second —
the boring rule wins when it works.
"""
from __future__ import annotations

from pathlib import Path

from pydantic_ai import BinaryContent

from ..model_layer import USAGE_LIMITS, create_agent
from ..schemas_ai import SortResult

_INSTRUCTIONS = (
    "You classify one scanned accounts-payable document. "
    "invoice = a bill from a vendor with an invoice number. "
    "claim = a staff expense claim form. "
    "receipt = a shop/till receipt supporting a claim. "
    "If genuinely none of these, use unknown."
)


async def sort_document(path: Path, first_page_png: bytes) -> SortResult:
    agent = create_agent("sort", SortResult, _INSTRUCTIONS)
    result = await agent.run(
        ["Classify this document.",
         BinaryContent(data=first_page_png, media_type="image/png")],
        usage_limits=USAGE_LIMITS,
    )
    return result.output


def attach_receipts(docs: list) -> None:
    """Pair each receipt with its claim.

    Convention first: 'claim_02_receipt.png' belongs to 'claim_02.png'.
    Falls back to leaving the receipt unattached (a human will see it) —
    guessing wrong silently would be worse.
    """
    claims_by_stem = {Path(d.filename).stem: d for d in docs if d.kind == "claim"}
    for d in docs:
        if d.kind == "receipt":
            stem = Path(d.filename).stem
            base = stem.replace("_receipt", "")
            if base in claims_by_stem:
                d.parent_id = claims_by_stem[base].id
