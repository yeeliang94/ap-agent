"""Reference documents: the payment listing, policy sheet, bank template.

All bytes come through the DocumentSource adapter (local folder in
development, SharePoint MCP on Windows) — this module only parses them
into the shapes the pipeline uses. Parsed once per run would be ideal;
at demo scale, parsing per call is fine and always fresh.
"""
from __future__ import annotations

import hashlib
import io

from openpyxl import load_workbook

from ..docsource import get_source
from . import listing_agent

# --- finding the files ------------------------------------------------------
# Client folders use human names, so each role is matched by the words that
# must ALL appear in the file name. Deliberately keyword matching and not
# similarity scoring: "FY2025 Payment Listing" and "FY2026 Payment Listing"
# score almost identically, and quietly checking invoices against last
# year's listing is the worst outcome this module can produce.
ROLE_KEYWORDS = {
    "payment_listing": ("payment", "listing"),
    "policy_sheet": ("policy",),
    "bank_template": ("template",),
}

# The fixed names used by the samples folder and the tests. An exact match
# wins outright, so development and CI never depend on keyword matching.
CANONICAL_NAMES = {
    "payment_listing": "payment_listing.xlsx",
    "policy_sheet": "policy_sheet.xlsx",
    "bank_template": "maybank_template.xlsx",
}

# Only these roles stop a run when absent. Missing policy or bank template
# degrade the run instead: see load_policy_clauses / load_maybank_headers.
REQUIRED_ROLES = ("payment_listing",)

_SPREADSHEET_SUFFIXES = (".xlsx", ".xlsm")


class AmbiguousReference(Exception):
    """More than one file could be this role, so the pipeline refuses to pick."""


class MissingReference(Exception):
    """A file the pipeline cannot work without is not in the folder."""


def _candidates(role: str, names: list[str]) -> list[str]:
    keywords = ROLE_KEYWORDS[role]
    out = []
    for name in names:
        # "~$..." are Excel's lock files, created whenever someone has the
        # real workbook open. They are not documents and must never match.
        if name.startswith("~$") or not name.lower().endswith(_SPREADSHEET_SUFFIXES):
            continue
        stem = name.rsplit(".", 1)[0].lower()
        if all(word in stem for word in keywords):
            out.append(name)
    return out


def resolve_name(role: str, names: list[str]) -> str | None:
    """Which file in `names` plays `role`? None means there isn't one.

    Raises AmbiguousReference when several files match, rather than guessing.
    """
    canonical = CANONICAL_NAMES[role]
    for name in names:
        if name.lower() == canonical.lower():
            return name
    matches = _candidates(role, names)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AmbiguousReference(
            f"Found {len(matches)} files that could be the {role.replace('_', ' ')}: "
            f"{', '.join(sorted(matches))}. Leave one in the folder (or rename the "
            f"others) so there is no doubt which one this run is judged against."
        )
    return None


def resolve_reference_files(folder_url: str | None = None) -> dict[str, str | None]:
    """The role → file name map for a folder, for recording on the run.

    Runs once at the start of a run so the run's record shows exactly which
    files it used, and which roles had no file at all.
    """
    names = get_source(folder_url).list_names()
    return {role: resolve_name(role, names) for role in ROLE_KEYWORDS}


def _open(role: str, folder_url: str | None = None):
    """Open the workbook for `role`, or return None if the folder has none."""
    source = get_source(folder_url)
    name = resolve_name(role, source.list_names())
    if name is None:
        if role in REQUIRED_ROLES:
            raise MissingReference(
                f"No {role.replace('_', ' ')} found in the reference folder. "
                f"Expected a spreadsheet whose name contains: "
                f"{', '.join(ROLE_KEYWORDS[role])}."
            )
        return None
    return load_workbook(io.BytesIO(source.get_reference(name)), read_only=True)


# The exact header row the canonical sample listing uses. A workbook whose
# first row matches is parsed instantly in code; anything else is a real
# client file and goes through the AI reading loop.
_CANONICAL_LISTING_HEADERS = ("No.", "Date", "Vendor", "Invoice No.",
                              "Amount (RM)", "Status")

# Parsed listings keyed by (folder_url, content hash). A run touches the
# listing several times (checks, outputs, per-correction re-checks); the AI
# reading must be paid for once per distinct file, not once per touch.
_LISTING_CACHE: dict[tuple[str, str], list[dict]] = {}


def _parse_canonical_listing(data: bytes) -> list[dict] | None:
    """The fixed-shape parse. None when the workbook isn't that shape."""
    wb = load_workbook(io.BytesIO(data), read_only=True)
    try:
        ws = wb.active
        first = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
        if tuple(str(c) if c is not None else "" for c in first[:6]) != _CANONICAL_LISTING_HEADERS:
            return None
        rows = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or row[3] is None:
                continue
            rows.append({
                "no": str(row[0]), "date": str(row[1]), "vendor": str(row[2]),
                "invoice_number": str(row[3]), "amount": float(row[4]), "status": str(row[5]),
            })
        return rows
    finally:
        wb.close()


async def load_payment_listing(folder_url: str | None = None) -> list[dict]:
    """Every payment row as {no, date, vendor, invoice_number, amount, status}.

    Canonical-shaped files (the samples) parse deterministically in code.
    Human-shaped client files go through listing_agent's reason/act loop:
    the AI maps the structure, code extracts and audits the numbers.
    amount can be None where the file genuinely doesn't pair an amount to
    an invoice — callers must treat that as "cannot compare", never zero.
    """
    source = get_source(folder_url)
    name = resolve_name("payment_listing", source.list_names())
    if name is None:
        raise MissingReference(
            "No payment listing found in the reference folder. Expected a "
            "spreadsheet whose name contains: payment, listing.")
    data = source.get_reference(name)
    key = (folder_url or "", hashlib.sha256(data).hexdigest())
    if key in _LISTING_CACHE:
        return _LISTING_CACHE[key]

    rows = _parse_canonical_listing(data)
    if rows is None:
        # data_only=True: balance columns are usually formulas, and the
        # audit needs their cached values, not "=J5-K5" strings.
        wb = load_workbook(io.BytesIO(data), data_only=True)
        try:
            rows = await listing_agent.ingest_workbook(wb)
        finally:
            wb.close()
    _LISTING_CACHE[key] = rows
    return rows


def load_policy_clauses(folder_url: str | None = None) -> list[dict]:
    """Every policy clause as {clause, category, cap, currency, text}.

    Returns [] when the folder has no policy sheet. Callers treat an empty
    list as "no policy to test against", which means no spending cap is
    checked — recorded on the run by resolve_reference_files.
    """
    wb = _open("policy_sheet", folder_url)
    if wb is None:
        return []
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
    """The column layout of the bank upload template — learned, not hardcoded.

    Returns [] when the folder has no template. build_outputs then omits the
    bank upload block entirely rather than inventing a column layout.
    """
    wb = _open("bank_template", folder_url)
    if wb is None:
        return []
    ws = wb.active
    headers = [str(c) for c in next(ws.iter_rows(min_row=1, max_row=1, values_only=True))]
    wb.close()
    return headers
