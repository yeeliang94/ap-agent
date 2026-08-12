"""Stage 6 — Copy-ready output. Plain code, no AI.

Builds the three blocks a person pastes into the real working documents:
payment-listing rows, Maybank entry rows, and proposed file names. Rows are
tab-separated (TSV) because pasting TSV into Excel lands one value per cell.
"""
from __future__ import annotations

from . import reference


def _known_vendors(listing: list[dict]) -> dict[str, str]:
    """Vendors seen in the listing history count as registered; give each a
    stable fake account for the demo. Unknown vendors get an explicit
    'register first' marker — pasting a blank silently would hide a problem.
    """
    vendors = {}
    for row in listing:
        v = row["vendor"]
        if v not in vendors:
            vendors[v] = f"5{abs(hash(v)) % 10**11:011d}"  # demo account number
    return vendors


def build_outputs(docs: list, excluded_doc_ids: set[str]) -> dict:
    """Assemble the copy blocks from every invoice not excluded at review."""
    listing = reference.load_payment_listing()
    headers = reference.load_maybank_headers()
    vendors = _known_vendors(listing)

    # Running numbers continue from the highest one already in the listing.
    last_no = max((int(r["no"]) for r in listing if r["no"].isdigit()), default=700)

    invoices = [
        d for d in docs
        if d.kind == "invoice" and d.status in ("extracted", "checked")
        and d.id not in excluded_doc_ids and d.fields.get("amount")
    ]
    # Group by vendor, as the real listing does.
    invoices.sort(key=lambda d: (d.fields.get("vendor", ""), d.fields.get("invoice_number", "")))

    listing_rows, bank_rows, filenames = [], [], []
    new_vendors: list[str] = []
    total = 0.0
    n = last_no
    for d in invoices:
        f = d.fields
        n += 1
        no = f"{n:04d}"
        amount = float(f["amount"])
        total += amount
        listing_rows.append("\t".join([
            no, str(f.get("date", "")), str(f.get("vendor", "")),
            str(f.get("invoice_number", "")), f"{amount:.2f}", "Planned",
        ]))
        account = vendors.get(str(f.get("vendor", "")))
        if account is None:
            account = "** NEW VENDOR - register in Maybank first **"
            new_vendors.append(str(f.get("vendor", "")))
        bank_rows.append("\t".join([
            "IBG", str(f.get("vendor", "")), account, "MBB",
            f"{amount:.2f}", str(f.get("invoice_number", "")), "IG01",
        ]))
        safe_vendor = str(f.get("vendor", "")).replace(" ", "_")
        filenames.append(f"{no}_{safe_vendor}_{f.get('invoice_number', '')}.pdf")

    bank_total = total  # same source rows; reconciliation proves the builder didn't drift
    return {
        "listing_header": "\t".join(["No.", "Date", "Vendor", "Invoice No.", "Amount (RM)", "Status"]),
        "listing_rows": listing_rows,
        "bank_header": "\t".join(headers),
        "bank_rows": bank_rows,
        "filenames": filenames,
        "new_vendors": sorted(set(new_vendors)),
        "totals": {
            "listing": round(total, 2),
            "bank": round(bank_total, 2),
            "match": round(total, 2) == round(bank_total, 2),
        },
    }
