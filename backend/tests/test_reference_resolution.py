"""Which file in the reference folder plays which role.

Client folders use human file names ("ICMR - FY2026 Payment Listing.xlsx"),
so roles are matched by keyword rather than by fixed name. These tests pin
the behaviours that protect the numbers:

  - a human-named file is found for its role
  - Excel's "~$" lock files never match a role
  - two plausible candidates refuse to resolve rather than guess, because
    checking invoices against last year's listing must never happen quietly
  - the fixed sample names still win outright, so samples and CI never
    depend on keyword matching
  - a missing policy sheet or bank template degrades the run; a missing
    payment listing stops it
"""
from __future__ import annotations

import pytest

from app.pipeline import output, reference


def test_human_named_files_resolve_to_their_roles():
    names = [
        "ICMR - FY2026 Payment Listing.xlsx",
        "Maybank Bulk Upload Template.xlsx",
        "Client Brief.pdf",
    ]
    assert reference.resolve_name("payment_listing", names) == names[0]
    assert reference.resolve_name("bank_template", names) == names[1]
    # Nothing in the folder is a policy sheet.
    assert reference.resolve_name("policy_sheet", names) is None


def test_excel_lock_files_never_match():
    # "~$..." exists only while somebody has the workbook open.
    names = ["~$ICMR - FY2026 Payment Listing.xlsx"]
    assert reference.resolve_name("payment_listing", names) is None


def test_non_spreadsheets_never_match():
    assert reference.resolve_name("payment_listing",
                                  ["FY2026 Payment Listing.pdf"]) is None


def test_two_candidates_refuse_to_resolve():
    names = ["ICMR - FY2025 Payment Listing.xlsx",
             "ICMR - FY2026 Payment Listing.xlsx"]
    with pytest.raises(reference.AmbiguousReference) as exc:
        reference.resolve_name("payment_listing", names)
    # The message must name both, or the user cannot act on it.
    assert "FY2025" in str(exc.value) and "FY2026" in str(exc.value)


def test_canonical_name_wins_over_keyword_match():
    # Both match the keywords; the exact sample name must be chosen.
    names = ["payment_listing.xlsx", "ICMR - FY2026 Payment Listing.xlsx"]
    assert reference.resolve_name("payment_listing", names) == "payment_listing.xlsx"


@pytest.mark.asyncio
async def test_missing_payment_listing_stops_the_run(monkeypatch):
    monkeypatch.setattr(reference, "get_source",
                        lambda *a, **k: _Source(["Client Brief.pdf"]))
    with pytest.raises(reference.MissingReference):
        await reference.load_payment_listing()


def test_missing_optional_files_return_empty(monkeypatch):
    monkeypatch.setattr(reference, "get_source",
                        lambda *a, **k: _Source(["Client Brief.pdf"]))
    assert reference.load_policy_clauses() == []
    assert reference.load_maybank_headers() == []


@pytest.mark.asyncio
async def test_outputs_omit_bank_block_when_template_missing(monkeypatch):
    listing = [{"no": "0701", "date": "2026-01-01", "vendor": "Acme",
                "invoice_number": "INV-1", "amount": 100.0, "status": "Planned"}]

    async def fake_listing(*a, **k):
        return listing
    monkeypatch.setattr(reference, "load_payment_listing", fake_listing)
    monkeypatch.setattr(reference, "load_maybank_headers", lambda *a, **k: [])

    res = await output.build_outputs([_Doc("b", "INV-2", 250.0)], excluded_doc_ids=set())

    assert res["bank_skipped"] is True
    assert res["bank_rows"] == [] and res["bank_header"] == ""
    # The listing half must still reconcile for real, not be waved through.
    assert res["listing_rows"] and res["totals"]["match"] is True
    assert res["totals"]["listing"] == 250.0


class _Source:
    """Stands in for a DocumentSource holding exactly these file names."""

    def __init__(self, names: list[str]) -> None:
        self._names = names

    def list_names(self) -> list[str]:
        return self._names

    def get_reference(self, name: str) -> bytes:  # pragma: no cover
        raise AssertionError(f"should not fetch {name!r} in these tests")


class _Doc:
    def __init__(self, doc_id: str, number: str, amount: float) -> None:
        self.id, self.kind, self.status = doc_id, "invoice", "extracted"
        self.filename = f"{doc_id}.pdf"
        self.fields = {"invoice_number": number, "amount": amount,
                       "currency": "MYR", "vendor": "Acme", "date": "2026-01-05"}
