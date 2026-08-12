"""Reference documents: the payment listing, policy sheet, bank template.

All bytes come through the DocumentSource adapter (local folder in
development, SharePoint MCP on Windows) — this module only parses them
into the shapes the pipeline uses. Parsed once per run would be ideal;
at demo scale, parsing per call is fine and always fresh.
"""
from __future__ import annotations

import io

from openpyxl import load_workbook

from ..docsource import get_source


def _open(name: str, folder_url: str | None = None):
    return load_workbook(
        io.BytesIO(get_source(folder_url).get_reference(name)), read_only=True
    )


def load_payment_listing(folder_url: str | None = None) -> list[dict]:
    """Every row of the payment listing as {no, date, vendor, invoice_number, amount, status}."""
    wb = _open("payment_listing.xlsx", folder_url)
    ws = wb.active
    rows = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or row[3] is None:
            continue
        rows.append({
            "no": str(row[0]), "date": str(row[1]), "vendor": str(row[2]),
            "invoice_number": str(row[3]), "amount": float(row[4]), "status": str(row[5]),
        })
    wb.close()
    return rows


def load_policy_clauses(folder_url: str | None = None) -> list[dict]:
    """Every policy clause as {clause, category, cap, currency, text}."""
    wb = _open("policy_sheet.xlsx", folder_url)
    ws = wb.active
    clauses = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or row[0] is None:
            continue
        clauses.append({
            "clause": str(row[0]), "category": str(row[1]),
            "cap": float(row[2]), "currency": str(row[3]), "text": str(row[4]),
        })
    wb.close()
    return clauses


def load_maybank_headers(folder_url: str | None = None) -> list[str]:
    """The column layout of the bank upload template — learned, not hardcoded."""
    wb = _open("maybank_template.xlsx", folder_url)
    ws = wb.active
    headers = [str(c) for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
    wb.close()
    return headers
