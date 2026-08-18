"""Step 3 smoke test: one live model call must return validated fields.

Runs against the real API (needs OPENAI_API_KEY in .env and network) and
COSTS MONEY, so it is opt-in — the plain `pytest` run must never make a
paid call by surprise:

    AP_LIVE_TESTS=1 pytest tests/test_model_layer.py

Kept tiny on purpose — one image, one call.
"""
import os
from pathlib import Path

import pytest
from pydantic_ai import BinaryContent

from app.model_layer import create_agent
from app.schemas_ai import InvoiceFields

pytestmark = pytest.mark.skipif(
    not os.environ.get("AP_LIVE_TESTS"),
    reason="paid live-model smoke test: set AP_LIVE_TESTS=1 to run it")

SAMPLE = (
    Path(__file__).resolve().parents[2]
    / "samples" / "generated" / "batch" / "invoice_KLO-0091.png"
)


@pytest.mark.anyio
async def test_extract_one_invoice():
    assert SAMPLE.exists(), "run samples/generate_samples.py first"
    agent = create_agent(
        "extract",
        InvoiceFields,
        "Read this invoice image and fill in every field exactly as printed. "
        "Note any field you are unsure about in low_confidence.",
    )
    result = await agent.run(
        ["Extract the invoice fields.",
         BinaryContent(data=SAMPLE.read_bytes(), media_type="image/png")]
    )
    fields = result.output
    assert fields.invoice_number == "KLO-0091"
    assert fields.amount == 867.20
    assert fields.currency in ("MYR", "RM")


@pytest.fixture
def anyio_backend():
    return "asyncio"
