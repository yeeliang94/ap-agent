"""Stage 4 — Checks: judgment by AI, arithmetic by code.

The AI decides WHICH policy clause applies to a claim — and its quoted
policy line is verified against the real clause text, because a citation
the reviewer can't trust is worse than none. Code does everything
mechanical: listing lookups (number AND status AND amount AND vendor),
date age, duplicates, cap arithmetic. "Not sure" from the AI always
becomes a flag — never a silent pass. Documents the pipeline couldn't
place (unknown kind, orphan receipts) are flagged too: nothing uploaded
may silently vanish.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime

from ..model_layer import USAGE_LIMITS, create_agent
from ..schemas_ai import CategoryJudgment
from . import reference

# An invoice older than this many days is suspicious (late submission or
# an already-paid duplicate).
OLD_DAYS = 90

_JUDGE_INSTRUCTIONS = (
    "You categorise ONE staff expense claim against a client's expense policy. "
    "Pick the applicable clause and quote the exact policy line you relied on, "
    "verbatim — it is checked against the policy text. "
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


# Which correctable fields each rule's outcome depends on. A human decision
# on a flag covers those values only: when a correction changes one of them,
# the old decision no longer covers the new situation, so the rule may raise
# a fresh flag. Codes missing from this map are treated as depending on
# every corrected field (conservative — re-raise rather than hide).
RULE_FIELDS: dict[str, set[str]] = {
    "DUPLICATE": {"invoice_number"},
    "NOT_IN_LISTING": {"invoice_number"},
    "LISTING_AMBIGUOUS": {"invoice_number", "vendor"},
    "ALREADY_PAID": {"invoice_number"},
    "AMOUNT_MISMATCH": {"invoice_number", "amount"},
    "VENDOR_MISMATCH": {"invoice_number", "vendor"},
    "NON_MYR_INVOICE": {"currency"},
    "OLD_DATED": {"date"},
    "BAD_DATE": {"date"},
    # Claims are re-judged as a whole, so every claim field feeds every rule.
    "JUDGMENT_FAILED": {"claimant", "description", "amount", "currency"},
    "AMBIGUOUS_CATEGORY": {"claimant", "description", "amount", "currency"},
    "QUOTE_MISMATCH": {"claimant", "description", "amount", "currency"},
    "CURRENCY_MISMATCH": {"claimant", "description", "amount", "currency"},
    "OVER_CAP": {"claimant", "description", "amount", "currency"},
    "RECEIPT_MISMATCH": {"claimant", "description", "amount", "currency"},
    # These do not rest on any correctable value.
    "UNCLASSIFIED": set(),
    "UNATTACHED_RECEIPT": set(),
    "LOW_CONFIDENCE": set(),
    "PROCESSING_ERROR": set(),
}


def _norm(text: str) -> str:
    """Lowercase and collapse whitespace/punctuation, for tolerant comparison."""
    return re.sub(r"[^a-z0-9%]+", " ", str(text).lower()).strip()


def _vendor_matches(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    return bool(na) and bool(nb) and (na in nb or nb in na)


def match_listing_row(by_number: dict[str, list[dict]], number: str,
                      vendor: str) -> tuple[dict | None, list[dict]]:
    """(matched row, all candidates) for an invoice number + vendor.

    Invoice numbers are vendor-scoped in the real world: a year of monthly
    tabs can hold the same number twice for different vendors. So a number
    hit alone is only a match when it is unambiguous; several candidates
    need the vendor to break the tie, and (None, many) = genuinely
    ambiguous, which callers must surface, never guess through.
    """
    candidates = by_number.get(number, [])
    if len(candidates) == 1:
        return candidates[0], candidates
    matched = [r for r in candidates if _vendor_matches(vendor, r["vendor"])]
    if len(matched) == 1:
        return matched[0], candidates
    return None, candidates


async def run_checks(docs: list, only_doc_ids: set[str] | None = None,
                     folder_url: str | None = None) -> list[dict]:
    """Return flag dicts for everything a human must decide.

    only_doc_ids narrows WHICH documents get flags (used when re-checking a
    single corrected document) — cross-document context (duplicate numbers)
    is still built from the whole batch. folder_url is the run's snapshotted
    SharePoint folder, so an old run is always judged against ITS client's
    reference files, not whichever client Settings points at today.
    """
    flags: list[dict] = []

    def want(d) -> bool:
        return only_doc_ids is None or d.id in only_doc_ids
    listing = await reference.load_payment_listing(folder_url)
    # Number -> ALL rows with that number. A plain dict would keep only the
    # last row, silently discarding same-numbered rows from other vendors
    # or other monthly tabs.
    listing_by_number: dict[str, list[dict]] = {}
    for r in listing:
        listing_by_number.setdefault(r["invoice_number"], []).append(r)
    clauses = reference.load_policy_clauses(folder_url)
    today = date.today()

    # ---- invoices: pure code -------------------------------------------
    seen_numbers: dict[str, str] = {}
    for d in docs:
        if d.kind != "invoice" or d.status not in ("extracted", "checked"):
            continue
        f = d.fields
        number = str(f.get("invoice_number", "")).strip()
        # Documents outside the re-check scope still feed cross-document
        # context (seen_numbers) but their flags go nowhere.
        flags_out = flags if want(d) else []

        if number in seen_numbers:
            flags_out.append(_mk_flag(d.id, "DUPLICATE",
                f"Invoice number {number} appears twice in this batch "
                f"(also in {seen_numbers[number]}).",
                "Rule: one invoice number may appear only once per batch."))
        seen_numbers.setdefault(number, d.filename)

        listed, candidates = match_listing_row(
            listing_by_number, number, str(f.get("vendor", "")))
        if not candidates:
            flags_out.append(_mk_flag(d.id, "NOT_IN_LISTING",
                f"Invoice {number} ({f.get('vendor')}) is not in the payment listing.",
                "Rule: every invoice must match a planned-payment row in the listing."))
        elif listed is None:
            vendors = sorted({str(r["vendor"]) for r in candidates})
            flags_out.append(_mk_flag(d.id, "LISTING_AMBIGUOUS",
                f"Invoice {number} matches {len(candidates)} listing rows "
                f"(vendors: {', '.join(vendors)}) and the document vendor "
                f"'{f.get('vendor')}' does not single one out.",
                "Rule: a listing match must be unambiguous — a human picks."))
        else:
            # A number match alone is not enough — the matched row must
            # actually be this invoice, and must not already be paid.
            if "paid" in str(listed["status"]).lower():
                flags_out.append(_mk_flag(d.id, "ALREADY_PAID",
                    f"Invoice {number} matches a listing row marked "
                    f"'{listed['status']}' — paying it again would be a duplicate payment.",
                    "Rule: invoices matching a Paid listing row need review."))
            # amount None = the file grouped several invoices under one
            # payment without per-line amounts, so there is no number to
            # compare against. Skipping is honest; comparing to 0 is not.
            if listed["amount"] is not None and \
                    abs(float(f.get("amount", 0)) - listed["amount"]) > 0.01:
                flags_out.append(_mk_flag(d.id, "AMOUNT_MISMATCH",
                    f"Invoice {number}: document reads {f.get('currency')} "
                    f"{f.get('amount'):.2f} but the listing row says {listed['amount']:.2f}.",
                    "Rule: extracted amount must equal the listing row's amount."))
            v_doc, v_listing = _norm(f.get("vendor", "")), _norm(listed["vendor"])
            if v_doc and v_listing and v_doc not in v_listing and v_listing not in v_doc:
                flags_out.append(_mk_flag(d.id, "VENDOR_MISMATCH",
                    f"Invoice {number}: document vendor '{f.get('vendor')}' does not "
                    f"match the listing row's vendor '{listed['vendor']}'.",
                    "Rule: the matched listing row must belong to the same vendor."))

        if str(f.get("currency", "")).upper() != "MYR":
            flags_out.append(_mk_flag(d.id, "NON_MYR_INVOICE",
                f"Invoice {number} is in {f.get('currency')} — the Maybank file "
                "carries RM amounts only, so this invoice is excluded from the "
                "bank block and needs separate handling.",
                "Rule: only MYR invoices go into the Maybank upload rows."))

        try:
            inv_date = datetime.strptime(str(f.get("date", "")), "%Y-%m-%d").date()
            age = (today - inv_date).days
            if age > OLD_DAYS:
                flags_out.append(_mk_flag(d.id, "OLD_DATED",
                    f"Invoice {number} is dated {inv_date} — {age} days old. "
                    "Possible late submission or already-paid duplicate.",
                    f"Rule: invoices older than {OLD_DAYS} days need review."))
        except ValueError:
            flags_out.append(_mk_flag(d.id, "BAD_DATE",
                f"Could not read a valid date on invoice {number} "
                f"(got: {f.get('date')!r})."))

    # ---- claims: AI picks the clause, code verifies and does the math --
    policy_text = "\n".join(
        f"Clause {c['clause']} [{c['category']}] (cap {c['currency']} {c['cap']:.2f}): {c['text']}"
        for c in clauses
    )
    clause_by_id = {c["clause"]: c for c in clauses}

    for d in docs:
        if d.kind != "claim" or d.status not in ("extracted", "checked") or not want(d):
            continue
        f = d.fields
        agent = create_agent("judge", CategoryJudgment, _JUDGE_INSTRUCTIONS,
                             temperature=0)
        try:
            result = await agent.run(
                f"The client's expense policy:\n{policy_text}\n\n"
                f"The claim:\n{json.dumps(f, indent=2)}\n\n"
                "Which clause applies?",
                usage_limits=USAGE_LIMITS,
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

        # Trust, then verify: the quoted line must actually appear in the
        # cited clause, and the named category must be that clause's
        # category. A fabricated citation shown to a reviewer as authority
        # would be worse than no citation.
        if _norm(judgment.quoted_policy_line) not in _norm(clause["text"]):
            flags.append(_mk_flag(d.id, "QUOTE_MISMATCH",
                f"For {f.get('claimant')}'s claim, the AI quoted a policy line "
                f"that does not appear in clause {clause['clause']}. Its "
                f"categorisation cannot be trusted without a human look.",
                f"AI quoted: \"{judgment.quoted_policy_line}\" — actual clause "
                f"{clause['clause']}: \"{clause['text']}\""))
            continue
        if _norm(judgment.category) != _norm(clause["category"]):
            flags.append(_mk_flag(d.id, "AMBIGUOUS_CATEGORY",
                f"AI named category '{judgment.category}' but cited clause "
                f"{clause['clause']}, which is '{clause['category']}'. "
                "Inconsistent — needs a human decision.",
                f"Policy {clause['clause']}: \"{clause['text']}\""))
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

    # ---- nothing may silently vanish -----------------------------------
    for d in docs:
        if not want(d):
            continue
        if d.kind == "unknown":
            flags.append(_mk_flag(d.id, "UNCLASSIFIED",
                f"{d.filename} does not look like an invoice, claim, or receipt. "
                "It is excluded from all output until a person decides what it is.",
                "Rule: every uploaded file must be accounted for."))
        elif d.kind == "receipt" and not d.parent_id:
            flags.append(_mk_flag(d.id, "UNATTACHED_RECEIPT",
                f"{d.filename} is a receipt that could not be matched to any "
                "claim in this batch.",
                "Rule: every receipt must belong to a claim."))

    # ---- both kinds: reading confidence --------------------------------
    for d in docs:
        if not want(d):
            continue
        if d.status in ("extracted", "checked") and d.confidence:
            notes = "; ".join(f"{k}: {v}" for k, v in d.confidence.items())
            flags.append(_mk_flag(d.id, "LOW_CONFIDENCE",
                f"{d.filename} was hard to read — {notes}. A human eye is needed.",
                "Rule: uncertain reads are never silently accepted."))
        elif d.status == "error":
            flags.append(_mk_flag(d.id, "PROCESSING_ERROR",
                f"{d.filename} could not be processed: {d.error}"))

    return flags
