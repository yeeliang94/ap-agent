"""The answer forms AI workers must fill in.

Each class is a strict shape: field names, types, and allowed values. If the
model's reply doesn't fit, PydanticAI rejects it and retries — so malformed
AI output never enters the pipeline.
"""
from typing import Literal

from pydantic import BaseModel, Field


class SortResult(BaseModel):
    """Stage 2: what kind of document is this?"""
    kind: Literal["invoice", "claim", "receipt", "unknown"]
    # Short human phrase, e.g. "has TAX INVOICE header and invoice number"
    why: str = Field(max_length=200)


class InvoiceFields(BaseModel):
    """Stage 3: the facts read off one invoice."""
    vendor: str
    invoice_number: str
    date: str = Field(description="ISO format YYYY-MM-DD")
    amount: float
    currency: str = Field(description="3-letter code, e.g. MYR, USD")
    # Field name -> note, ONLY for fields the model wasn't sure about,
    # e.g. {"amount": "low - digits partially blurred"}
    low_confidence: dict[str, str] = Field(default_factory=dict)


class ClaimFields(BaseModel):
    """Stage 3: the facts read off one staff claim (with its receipts)."""
    claimant: str
    description: str
    amount: float
    currency: str
    receipts_match_amount: bool = Field(
        description="Do the attached receipts support the claimed amount?"
    )
    low_confidence: dict[str, str] = Field(default_factory=dict)


class CategoryJudgment(BaseModel):
    """Stage 4: which policy category applies to a claim, with citation.

    The rule of this stage: never guess silently. If the category is
    genuinely arguable, say sure=False and it becomes a flag for a human.
    """
    category: str
    clause: str = Field(description="Policy clause number, e.g. '4.2'")
    quoted_policy_line: str = Field(description="The exact policy text relied on")
    sure: bool
    why: str = Field(max_length=300)
