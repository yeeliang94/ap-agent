"""Stage 6 — Copy-ready output. Plain code, no AI.

Builds the blocks a person pastes into the real working documents. Rules
hardened after peer review:

- NEVER synthesize a bank account number. Without a vendor master file,
  every account cell says so explicitly — a realistic-looking fake number
  in a bank row is the worst thing this app could emit.
- No paste-ready listing rows. The client's listing has its own layout
  (monthly tabs, grouped entries); emitting six-column rows labelled "paste
  this" would be confidently wrong. Writing new entries in the client's own
  layout is planned work (docs/LISTING-HARDENING.md), not this module yet.
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
from .checks import _vendor_matches

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
                        refs=None) -> dict:
    """refs: the run's private copy of the reference files (reference.run_refs)."""
    listing = await reference.load_payment_listing(refs)
    headers = reference.load_maybank_headers(refs)

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

    # Vendors the listing shows no past payment to. Absence from history
    # does not prove they are unregistered with the bank — the UI words it
    # as "verify beneficiary registration". Tolerant name comparison, the
    # same one the checks use, so "ABC Sdn. Bhd." is not "new" next to
    # "ABC Sdn Bhd".
    known_vendors = [str(r["vendor"]) for r in listing if r.get("vendor")]
    new_vendors = sorted({
        str(d.fields.get("vendor", "")) for d in approved
        if not any(_vendor_matches(str(d.fields.get("vendor", "")), k)
                   for k in known_vendors)
    })

    bank_rows = []
    for d in bank_docs:
        f = d.fields
        bank_rows.append("\t".join(
            _cell(_BANK_COLUMN_VALUES[h.lower()](f, None)) for h in headers
        ))

    filenames = []
    for d in approved:
        f = d.fields
        filenames.append(_safe_filename(
            f"{f.get('vendor', '')}_{f.get('invoice_number', '')}.pdf"
        ).replace(" ", "_"))

    # ---- reconciliation that can actually fail --------------------------
    if bank_skipped:
        bank_total = Decimal("0")
        match = True
    else:
        amount_col = [h.lower() for h in headers].index("amount (rm)")
        bank_total = _tsv_total(bank_rows, amount_col)
        source_bank = sum((Decimal(str(d.fields["amount"])) for d in bank_docs),
                          Decimal("0"))
        match = bank_total == source_bank

    return {
        "bank_header": "\t".join(headers),
        "bank_rows": bank_rows,
        # True = no bank template in the folder, so no upload block was built.
        "bank_skipped": bank_skipped,
        "excluded_non_myr": 0 if bank_skipped else len(approved) - len(bank_docs),
        "filenames": filenames,
        "new_vendors": new_vendors,
        "totals": {
            "bank": float(bank_total),
            "match": match,
        },
    }
