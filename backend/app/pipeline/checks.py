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
from dataclasses import dataclass
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


# A status column's words. Negative forms are checked FIRST: "Unpaid" and
# "Not paid" both contain "paid". Anything unrecognised is NOT paid — the
# duplicate-payment flag must rest on a positive statement, not a guess.
_NOT_PAID = re.compile(
    r"\b(un\s*paid|not\s+(yet\s+)?paid|pending|planned|outstanding|unsettled|"
    r"scheduled|to\s+pay|on\s+hold|awaiting|cancel+ed|void(ed)?)\b",
    re.IGNORECASE)
_PAID = re.compile(r"\b(paid|settled|cleared|released|done|completed?|transferred)\b",
                   re.IGNORECASE)


def is_paid_status(status) -> bool:
    """Does a listing row's status say the payment HAPPENED?"""
    text = str(status or "")
    if _NOT_PAID.search(text):
        return False
    return bool(_PAID.search(text))


def where_in_listing(row: dict) -> str:
    """Point a reviewer at a listing row: tab, row, voucher, date, amount,
    payee — whatever the row knows. Rows read from a real workbook know
    all of it; older or hand-built rows may know less, so each part is
    optional and the sentence still reads."""
    bits = []
    if row.get("sheet"):
        where = f"tab {row['sheet']}"
        if row.get("row"):
            where += f" row {row['row']}"
            if row.get("entry_row") and row["entry_row"] != row["row"]:
                where += f" (entry starts row {row['entry_row']})"
        bits.append(where)
    if row.get("no"):
        bits.append(f"voucher {row['no']}")
    if row.get("date"):
        bits.append(f"dated {row['date']}")
    if row.get("amount") is not None:
        bits.append(f"RM {float(row['amount']):.2f}")
    if row.get("vendor"):
        bits.append(f"payee {row['vendor']}")
    if row.get("note"):
        bits.append(f"note '{row['note']}'")
    return ", ".join(bits) if bits else "the payment listing"


def reference_key(number: str) -> str:
    """A reference number as a tolerant lookup key: case, spaces and the
    usual separators (- _ / .) ignored, so 'INV 1023' finds 'INV-1023'.
    Display always uses the raw values — the key is for finding, not
    showing."""
    return re.sub(r"[\s\-_/.]+", "", str(number)).upper()


@dataclass
class ListingMatch:
    row: dict | None          # the one row this invoice matches, if any
    candidates: list[dict]    # every row considered (for the ambiguous flag)
    loose: bool = False       # found by normalised key, not the raw string


class ListingIndex:
    """Past-payment rows indexed for lookup by invoice number.

    Invoice numbers are vendor-scoped in the real world: a year of monthly
    tabs can hold the same number twice for different vendors. So a number
    hit alone is only a match when it is unambiguous; several candidates
    need the vendor to break the tie; anything else is genuinely
    ambiguous, which callers must surface, never guess through.

    The candidates are every row whose number equals the invoice's after
    normalisation (see reference_key) — raw-equal rows and re-typed ones
    alike. Then:
      - ONE candidate, raw-equal: a match outright (a vendor mismatch is
        then its own flag, so the reviewer sees the row).
      - ONE candidate, loose (raw differs): a match ONLY if the vendor
        agrees — the key can collapse two genuinely different references,
        so a loose hit alone is not enough. Otherwise ambiguous.
      - SEVERAL candidates: a match only when exactly one is vendor-
        supported (an invoice number is vendor-scoped). Otherwise
        ambiguous — never a pick.
    A match found by key rather than raw string is marked loose, so the
    flag can show both spellings.
    """

    def __init__(self, rows: list[dict]) -> None:
        # A plain dict keyed by number would keep only the last row and
        # silently discard same-numbered rows from other vendors or tabs.
        self._by_key: dict[str, list[dict]] = {}
        for r in rows:
            self._by_key.setdefault(reference_key(str(r["invoice_number"])), []).append(r)

    def match(self, number: str, vendor: str) -> ListingMatch:
        number = str(number).strip()
        candidates = self._by_key.get(reference_key(number), [])
        if not candidates:
            return ListingMatch(None, [])

        def is_loose(row: dict) -> bool:
            return str(row["invoice_number"]).strip() != number

        if len(candidates) == 1:
            only = candidates[0]
            if not is_loose(only):
                return ListingMatch(only, candidates)
            if _vendor_matches(vendor, only["vendor"]):
                return ListingMatch(only, candidates, loose=True)
            return ListingMatch(None, candidates, loose=True)
        supported = [r for r in candidates if _vendor_matches(vendor, r["vendor"])]
        loose_any = any(is_loose(r) for r in candidates)
        if len(supported) == 1:
            return ListingMatch(supported[0], candidates, loose=is_loose(supported[0]))
        return ListingMatch(None, candidates, loose=loose_any)


def _loosely(number: str, row: dict) -> str:
    """The 'matched loosely' remark for a row found by key, else ''."""
    raw = str(row.get("invoice_number", "")).strip()
    return f" (matched loosely: {number!r} ↔ {raw!r})" if raw != str(number).strip() else ""


async def run_checks(docs: list, only_doc_ids: set[str] | None = None,
                     refs=None, notes: list[tuple[str, str]] | None = None
                     ) -> list[dict]:
    """Return flag dicts for everything a human must decide.

    only_doc_ids narrows WHICH documents get flags (used when re-checking a
    single corrected document) — cross-document context (duplicate numbers)
    is still built from the whole batch. refs is the run's private copy of
    the reference files (reference.run_refs), so a run is always judged
    against the files it started with — not whatever the folder holds now.

    notes, if given, receives (level, sentence) pairs for the run's Activity
    tab — batch-level facts that are not anyone's flag, such as how many
    invoices are new. Only written for a whole-batch check.
    """
    flags: list[dict] = []
    # How the batch fared against the listing: new / matched / ambiguous.
    tally = {"new": 0, "matched": 0, "ambiguous": 0}

    def want(d) -> bool:
        return only_doc_ids is None or d.id in only_doc_ids
    listing = await reference.load_payment_listing(refs)
    index = ListingIndex(listing)
    # What "not found" was measured against, so the flag can say so.
    listing_tabs = sorted({r["sheet"] for r in listing if r.get("sheet")})
    searched = (f"{len(listing)} invoice row(s)"
                + (f" across {len(listing_tabs)} tab(s): {', '.join(listing_tabs)}"
                   if listing_tabs else ""))
    clauses = reference.load_policy_clauses(refs)
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

        m = index.match(number, str(f.get("vendor", "")))
        listed, candidates = m.row, m.candidates
        if not candidates:
            # The listing is PAST payments, so "not found" is the normal,
            # healthy case for a new invoice: no flag. The batch-level
            # count is written to the Activity tab below, so a reviewer
            # can still see how many were new and what was searched.
            tally["new"] += 1
        elif listed is None:
            tally["ambiguous"] += 1
            flags_out.append(_mk_flag(d.id, "LISTING_AMBIGUOUS",
                f"Invoice {number} {'loosely ' if m.loose else ''}matches "
                f"{len(candidates)} listing row(s) and the document vendor "
                f"'{f.get('vendor')}' does not single one out. Candidates: "
                + "; ".join(where_in_listing(r) + _loosely(number, r)
                            for r in candidates) + ".",
                "Rule: a listing match must be unambiguous — a human picks."))
        else:
            tally["matched"] += 1
            # A number match alone is not enough — the matched row must
            # actually be this invoice, and must not already be paid.
            where = where_in_listing(listed) + _loosely(number, listed)
            if is_paid_status(listed["status"]):
                flags_out.append(_mk_flag(d.id, "ALREADY_PAID",
                    f"Invoice {number} was already paid: {where} (status "
                    f"'{listed['status']}'). Paying it again would be a "
                    "duplicate payment.",
                    "Rule: invoices matching a Paid listing row need review."))
            # amount None = the file grouped several invoices under one
            # payment without per-line amounts, so there is no number to
            # compare against. Skipping is honest; comparing to 0 is not.
            if listed["amount"] is not None and \
                    abs(float(f.get("amount", 0)) - listed["amount"]) > 0.01:
                flags_out.append(_mk_flag(d.id, "AMOUNT_MISMATCH",
                    f"Invoice {number}: document reads {f.get('currency')} "
                    f"{f.get('amount'):.2f} but the listing row says "
                    f"{listed['amount']:.2f} ({where}).",
                    "Rule: extracted amount must equal the listing row's amount."))
            v_doc, v_listing = _norm(f.get("vendor", "")), _norm(listed["vendor"])
            if v_doc and v_listing and v_doc not in v_listing and v_listing not in v_doc:
                flags_out.append(_mk_flag(d.id, "VENDOR_MISMATCH",
                    f"Invoice {number}: document vendor '{f.get('vendor')}' does not "
                    f"match the listing row's payee '{listed['vendor']}' ({where}).",
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

    # The batch against the listing, in one line for the Activity tab. Only
    # for a whole-batch check: a one-document re-check would say "0 of 1".
    total = sum(tally.values())
    if notes is not None and only_doc_ids is None and total:
        parts = [f"{tally['new']} of {total} invoice(s) are new — not in any "
                 f"past listing tab (searched {searched})"]
        if tally["matched"]:
            parts.append(f"{tally['matched']} matched a past payment (see the "
                         "ALREADY_PAID / mismatch flags)")
        if tally["ambiguous"]:
            parts.append(f"{tally['ambiguous']} ambiguous (LISTING_AMBIGUOUS)")
        notes.append(("INFO", "; ".join(parts) + "."))

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
        if d.kind == "unknown" and d.status == "error":
            # The system never managed to LOOK at this file. Saying it
            # "does not look like an invoice" would blame the document for
            # a failure it had nothing to do with.
            #
            # But the opposite mistake is just as costly: a corrupt PDF
            # also lands here, and calling that "a problem with the
            # system" sends the reviewer to IT over a file they could
            # simply rescan. So only a KNOWN dependency failure is named
            # as one; anything else stays neutral and points at the diary.
            # rstrip('.'): the reason is a sentence fragment from
            # telemetry and may or may not already end in a full stop.
            from ..telemetry import is_service_failure

            where = ("This is a problem with the system, not with the document."
                     if is_service_failure(d.error)
                     else "The Activity tab shows what failed.")
            flags.append(_mk_flag(d.id, "COULD_NOT_PROCESS",
                f"{d.filename} could not be processed: {d.error.rstrip('.')}. "
                f"{where} It is excluded from all output.",
                "Rule: a file the system could not read is never treated as reviewed."))
        elif d.kind == "unknown":
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
