"""Stage 4 — Checks: judgment by AI, arithmetic by code.

The AI decides WHICH policy clause applies to a claim (and must quote it).
Code then does everything mechanical: listing lookups, date age, cap
arithmetic, duplicates. "Not sure" from the AI always becomes a flag —
never a silent pass.
"""
from __future__ import annotations

import json
from datetime import date, datetime

from ..model_layer import create_agent
from ..schemas_ai import CategoryJudgment
from . import reference

# An invoice older than this many days is suspicious (late submission or
# an already-paid duplicate).
OLD_DAYS = 90

_JUDGE_INSTRUCTIONS = (
    "You categorise ONE staff expense claim against a client's expense policy. "
    "Pick the applicable clause and quote the exact policy line you relied on. "
    "sure=true when exactly one clause plainly covers this kind of expense — "
    "minor wording differences (subscription vs subsidy, taxi vs e-hailing) do "
    "NOT make it unsure. sure=false ONLY when: two clauses could genuinely "
    "apply; none fits without stretching; or applying the clause needs "
    "information the claim does not give (e.g. a per-head cap with no "
    "headcount, or whether clients attended). Explain the ambiguity in why. "
    "Do not compare amounts against caps — that arithmetic happens elsewhere."
)


def _mk_flag(doc_id: str, code: str, reason: str, basis: str = "") -> dict:
    return {"document_id": doc_id, "code": code, "reason": reason, "basis": basis}


async def run_checks(docs: list) -> list[dict]:
    """Return flag dicts for everything a human must decide."""
    flags: list[dict] = []
    listing = reference.load_payment_listing()
    listed_numbers = {r["invoice_number"] for r in listing}
    clauses = reference.load_policy_clauses()
    today = date.today()

    # ---- invoices: pure code -------------------------------------------
    seen_numbers: dict[str, str] = {}
    for d in docs:
        if d.kind != "invoice" or d.status != "extracted":
            continue
        f = d.fields
        number = str(f.get("invoice_number", ""))

        if number in seen_numbers:
            flags.append(_mk_flag(d.id, "DUPLICATE",
                f"Invoice number {number} appears twice in this batch "
                f"(also in {seen_numbers[number]}).",
                "Rule: one invoice number may appear only once per batch."))
        seen_numbers.setdefault(number, d.filename)

        if number not in listed_numbers:
            flags.append(_mk_flag(d.id, "NOT_IN_LISTING",
                f"Invoice {number} ({f.get('vendor')}) is not in the payment listing.",
                "Rule: every invoice must match a planned-payment row in the listing."))

        try:
            inv_date = datetime.strptime(str(f.get("date", "")), "%Y-%m-%d").date()
            age = (today - inv_date).days
            if age > OLD_DAYS:
                flags.append(_mk_flag(d.id, "OLD_DATED",
                    f"Invoice {number} is dated {inv_date} — {age} days old. "
                    "Possible late submission or already-paid duplicate.",
                    f"Rule: invoices older than {OLD_DAYS} days need review."))
        except ValueError:
            flags.append(_mk_flag(d.id, "BAD_DATE",
                f"Could not read a valid date on invoice {number} "
                f"(got: {f.get('date')!r})."))

    # ---- claims: AI picks the clause, code does the cap math ----------
    policy_text = "\n".join(
        f"Clause {c['clause']} [{c['category']}] (cap {c['currency']} {c['cap']:.2f}): {c['text']}"
        for c in clauses
    )
    clause_by_id = {c["clause"]: c for c in clauses}

    for d in docs:
        if d.kind != "claim" or d.status != "extracted":
            continue
        f = d.fields
        agent = create_agent("judge", CategoryJudgment, _JUDGE_INSTRUCTIONS)
        try:
            result = await agent.run(
                f"Client ABC's expense policy:\n{policy_text}\n\n"
                f"The claim:\n{json.dumps(f, indent=2)}\n\n"
                "Which clause applies?"
            )
        except Exception as exc:
            flags.append(_mk_flag(d.id, "JUDGMENT_FAILED",
                f"Could not categorise claim by {f.get('claimant')}: {exc}"))
            continue
        judgment = result.output
        d.fields = {**f, "category": judgment.category, "clause": judgment.clause}

        if not judgment.sure:
            flags.append(_mk_flag(d.id, "AMBIGUOUS_CATEGORY",
                f"Claim by {f.get('claimant')} ({f.get('description')}): {judgment.why}",
                f"Policy {judgment.clause}: \"{judgment.quoted_policy_line}\""))
            continue  # a human settles the category before any cap math applies

        clause = clause_by_id.get(judgment.clause)
        if clause is None:
            flags.append(_mk_flag(d.id, "AMBIGUOUS_CATEGORY",
                f"AI cited unknown policy clause {judgment.clause!r} for "
                f"{f.get('claimant')}'s claim.",))
            continue

        # Cap arithmetic — plain code, so it is repeatable and auditable.
        if str(f.get("currency", "")).upper() != clause["currency"].upper():
            flags.append(_mk_flag(d.id, "CURRENCY_MISMATCH",
                f"Claim by {f.get('claimant')} is in {f.get('currency')} but "
                f"clause {clause['clause']} caps in {clause['currency']}.",
                f"Policy {clause['clause']}: \"{clause['text']}\""))
        elif float(f.get("amount", 0)) > clause["cap"]:
            over = float(f["amount"]) - clause["cap"]
            flags.append(_mk_flag(d.id, "OVER_CAP",
                f"Claim by {f.get('claimant')}: {f['currency']} {f['amount']:.2f} "
                f"exceeds the {clause['currency']} {clause['cap']:.2f} cap "
                f"by {over:.2f}.",
                f"Policy {clause['clause']}: \"{clause['text']}\""))

        if f.get("receipts_match_amount") is False:
            flags.append(_mk_flag(d.id, "RECEIPT_MISMATCH",
                f"Receipts for {f.get('claimant')}'s claim do not support the claimed amount.",
                "Rule: every claim needs receipts matching the amount."))

    # ---- both kinds: reading confidence --------------------------------
    for d in docs:
        if d.status == "extracted" and d.confidence:
            notes = "; ".join(f"{k}: {v}" for k, v in d.confidence.items())
            flags.append(_mk_flag(d.id, "LOW_CONFIDENCE",
                f"{d.filename} was hard to read — {notes}. A human eye is needed.",
                "Rule: uncertain reads are never silently accepted."))
        elif d.status == "error":
            flags.append(_mk_flag(d.id, "PROCESSING_ERROR",
                f"{d.filename} could not be processed: {d.error}"))

    return flags
