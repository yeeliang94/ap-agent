"""The answer forms AI workers must fill in.

Each class is a strict shape: field names, types, allowed values, and value
constraints (dates must look like dates, amounts must be positive finite
numbers, currencies must be 3-letter codes). If the model's reply doesn't
fit, PydanticAI rejects it and retries with the validation error — so
malformed AI output never enters the pipeline.
"""
from typing import Literal

from pydantic import BaseModel, Field

# YYYY-MM-DD — no prose, no partial dates.
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
# 3-letter uppercase code (MYR, USD, ...). "RM" must be normalized to MYR.
CURRENCY_PATTERN = r"^[A-Z]{3}$"


class SortResult(BaseModel):
    """Stage 2: what kind of document is this?"""
    kind: Literal["invoice", "claim", "receipt", "unknown"]
    # Short human phrase, e.g. "has TAX INVOICE header and invoice number"
    why: str = Field(max_length=200)


class InvoiceFields(BaseModel):
    """Stage 3: the facts read off one invoice."""
    vendor: str = Field(min_length=1, max_length=200)
    invoice_number: str = Field(min_length=1, max_length=60)
    date: str = Field(pattern=DATE_PATTERN, description="ISO format YYYY-MM-DD")
    # Positive and finite: a negative, zero, or infinite invoice amount is
    # never a real reading — force the model to re-answer.
    amount: float = Field(gt=0, allow_inf_nan=False)
    currency: str = Field(pattern=CURRENCY_PATTERN, description="3-letter code; RM means MYR")
    # Field name -> note, ONLY for fields the model wasn't sure about,
    # e.g. {"amount": "low - digits partially blurred"}
    low_confidence: dict[str, str] = Field(default_factory=dict)


class ClaimFields(BaseModel):
    """Stage 3: the facts read off one staff claim (with its receipts)."""
    claimant: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=300)
    amount: float = Field(gt=0, allow_inf_nan=False)
    currency: str = Field(pattern=CURRENCY_PATTERN)
    receipts_match_amount: bool = Field(
        description="Do the attached receipts support the claimed amount?"
    )
    low_confidence: dict[str, str] = Field(default_factory=dict)


class CategoryJudgment(BaseModel):
    """Stage 4: which policy category applies to a claim, with citation.

    The rule of this stage: never guess silently. If the category is
    genuinely arguable, say sure=False and it becomes a flag for a human.
    The quoted line is verified against the real policy text downstream —
    a quote that doesn't appear in the cited clause is itself a flag.
    """
    category: str = Field(min_length=1, max_length=60)
    clause: str = Field(min_length=1, max_length=20, description="Policy clause number, e.g. '4.2'")
    quoted_policy_line: str = Field(min_length=5, max_length=400,
                                    description="The exact policy text relied on, verbatim")
    sure: bool
    why: str = Field(max_length=300)
