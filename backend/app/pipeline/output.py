"""Stage 6 — Copy-ready output. Plain code, no AI.

Builds the blocks a person pastes into the real working documents. Rules
hardened after peer review:

- NEVER synthesize a bank account number. Without a vendor master file,
  every account cell says so explicitly — a realistic-looking fake number
  in a bank row is the worst thing this app could emit.
- Listing rows are only for invoices NOT already in the listing; re-emitting
  existing rows would create duplicates on paste.
- Bank rows are built by mapping the template's actual headers, and only
  MYR invoices qualify (the checks stage flags the rest).
- Reconciliation is real: totals are recomputed independently by re-parsing
  the emitted text rows with Decimal and compared against the source
  documents — the check can actually fail.
- Every cell is sanitized: control characters stripped, spreadsheet formula
  prefixes neutralized, filenames made Windows-safe.
"""
from __future__ import annotations

import re
from decimal import Decimal

from . import reference
from .checks import _norm, _vendor_matches

ACCOUNT_UNKNOWN = "[ACCOUNT UNKNOWN - fill from vendor master]"

# What each known template header means. Building rows by header name means
# a reordered template still fills correctly — and an unrecognised header
# fails loudly instead of silently misaligning money columns.
_BANK_COLUMN_VALUES = {
    "payment type": lambda f, ctx: "IBG",
    "beneficiary name": lambda f, ctx: f.get("vendor", ""),
    "beneficiary account": lambda f, ctx: ACCOUNT_UNKNOWN,
    "bank code": lambda f, ctx: "MBB",
    "amount (rm)": lambda f, ctx: f"{Decimal(str(f['amount'])):.2f}",
    "payment reference": lambda f, ctx: f.get("invoice_number", ""),
    "ig code": lambda f, ctx: "IG01",
}


class UnsupportedTemplate(Exception):
    """The bank template has a column this code does not understand."""


def _cell(value) -> str:
    """Make one TSV cell safe: no tabs/newlines to shift columns, no
    leading formula characters for Excel to execute on paste."""
    s = re.sub(r"[\x00-\x1f]", " ", str(value)).strip()
    if s[:1] in ("=", "+", "@") or (s[:1] == "-" and not re.match(r"^-\d", s)):
        s = "'" + s
    return s


def _safe_filename(name: str) -> str:
    """Strip characters Windows filenames cannot contain."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip(". ")
    return s[:120]


def _tsv_total(rows: list[str], amount_index: int) -> Decimal:
    """Re-parse the emitted rows and total the amount column — an
    independent count, so a builder bug shows up as a mismatch."""
    total = Decimal("0")
    for row in rows:
        total += Decimal(row.split("\t")[amount_index])
    return total


async def build_outputs(docs: list, excluded_doc_ids: set[str],
                        folder_url: str | None = None) -> dict:
    listing = await reference.load_payment_listing(folder_url)
    headers = reference.load_maybank_headers(folder_url)
    # Paste-ready listing rows exist only for the canonical column layout.
    # For a real client workbook (grouped rows, monthly tabs) we do not
    # know where or how new rows are written — emitting six-column rows
    # labelled "paste this" would be confidently wrong, so the block is
    # omitted and the UI says why.
    listing_skipped = not await reference.listing_is_canonical(folder_url)

    # No template in the folder means no bank upload block. Omitting it is
    # safe and visible; inventing a column layout for money values is not.
    bank_skipped = not headers

    unknown_headers = [h for h in headers if h.lower() not in _BANK_COLUMN_VALUES]
    if unknown_headers:
        raise UnsupportedTemplate(
            f"Bank template has unrecognised column(s): {unknown_headers}. "
            "Refusing to guess where money values belong."
        )

    approved = [
        d for d in docs
        if d.kind == "invoice" and d.status in ("extracted", "checked")
        and d.id not in excluded_doc_ids and d.fields.get("amount")
    ]
    approved.sort(key=lambda d: (str(d.fields.get("vendor", "")),
                                 str(d.fields.get("invoice_number", ""))))

    # Only MYR invoices belong in the Maybank block (checks flagged others).
    bank_docs = [] if bank_skipped else [
        d for d in approved
        if str(d.fields.get("currency", "")).upper() == "MYR"
    ]
    # Only invoices NOT already in the listing get new rows. "Already in"
    # is the composite test (number + vendor): a number shared with a
    # DIFFERENT vendor's row must not suppress this vendor's genuinely new
    # invoice. Ambiguous matches count as listed — conservative, and the
    # checks stage has already flagged them for a human.
    by_number: dict[str, list[dict]] = {}
    for r in listing:
        by_number.setdefault(r["invoice_number"], []).append(r)

    def _is_listed(d) -> bool:
        candidates = by_number.get(str(d.fields.get("invoice_number", "")), [])
        if not candidates:
            return False
        if len(candidates) > 1:
            return True  # matched or ambiguous — either way, no new row
        # One candidate: listed unless its vendor is CLEARLY someone else.
        # A missing vendor on either side cannot prove a collision, so it
        # stays listed — a duplicate paste row is recoverable, a silently
        # unscheduled payment is not.
        v_doc = str(d.fields.get("vendor", ""))
        v_row = str(candidates[0]["vendor"])
        if not _norm(v_doc) or not _norm(v_row):
            return True
        return _vendor_matches(v_doc, v_row)

    new_docs = [] if listing_skipped else [d for d in approved if not _is_listed(d)]

    last_no = max((int(r["no"]) for r in listing if r["no"].isdigit()), default=700)

    listing_rows, filenames = [], []
    known_vendors = {r["vendor"] for r in listing}
    new_vendors: list[str] = []
    number_by_doc: dict[str, str] = {}
    for i, d in enumerate(new_docs, 1):
        f = d.fields
        no = f"{last_no + i:04d}"
        number_by_doc[d.id] = no
        listing_rows.append("\t".join([
            no, _cell(f.get("date", "")), _cell(f.get("vendor", "")),
            _cell(f.get("invoice_number", "")),
            f"{Decimal(str(f['amount'])):.2f}", "Planned",
        ]))
        if str(f.get("vendor", "")) not in known_vendors:
            new_vendors.append(str(f.get("vendor", "")))

    bank_rows = []
    for d in bank_docs:
        f = d.fields
        bank_rows.append("\t".join(
            _cell(_BANK_COLUMN_VALUES[h.lower()](f, None)) for h in headers
        ))

    for d in approved:
        f = d.fields
        no = number_by_doc.get(d.id, "listed")
        filenames.append(_safe_filename(
            f"{no}_{f.get('vendor', '')}_{f.get('invoice_number', '')}.pdf"
        ).replace(" ", "_"))

    # ---- reconciliation that can actually fail --------------------------
    listing_total = _tsv_total(listing_rows, 4)
    source_new = sum((Decimal(str(d.fields["amount"])) for d in new_docs), Decimal("0"))
    match = listing_total == source_new
    if bank_skipped:
        bank_total = Decimal("0")
    else:
        amount_col = [h.lower() for h in headers].index("amount (rm)")
        bank_total = _tsv_total(bank_rows, amount_col)
        source_bank = sum((Decimal(str(d.fields["amount"])) for d in bank_docs),
                          Decimal("0"))
        match = match and (bank_total == source_bank)

    return {
        "listing_header": "\t".join(["No.", "Date", "Vendor", "Invoice No.", "Amount (RM)", "Status"]),
        "listing_rows": listing_rows,
        # True = the client's listing is not the canonical layout, so no
        # paste-ready rows exist for it. Checks still ran against its data.
        "listing_skipped": listing_skipped,
        "already_listed": len(approved) - len(new_docs),
        "bank_header": "\t".join(headers),
        "bank_rows": bank_rows,
        # True = no bank template in the folder, so no upload block was built.
        "bank_skipped": bank_skipped,
        "excluded_non_myr": 0 if bank_skipped else len(approved) - len(bank_docs),
        "filenames": filenames,
        "new_vendors": sorted(set(new_vendors)),
        "totals": {
            "listing": float(listing_total),
            "bank": float(bank_total),
            "match": match,
        },
    }
