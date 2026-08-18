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

import io

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


def test_missing_payment_listing_stops_the_run(monkeypatch, tmp_path):
    """Snapshotting refuses before copying anything: a run never starts
    against a folder it cannot be judged by."""
    monkeypatch.setattr(reference, "get_source",
                        lambda *a, **k: _Source(["Client Brief.pdf"]))
    with pytest.raises(reference.MissingReference):
        reference.snapshot_references(None, tmp_path / "reference")
    assert not (tmp_path / "reference").exists()


def test_missing_optional_files_return_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(reference, "get_source",
                        lambda *a, **k: _Source(["payment_listing.xlsx"]))
    refs = tmp_path / "reference"
    reference.snapshot_references(None, refs)
    assert reference.load_policy_clauses(refs) == []
    assert reference.load_maybank_headers(refs) == []


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
    assert res["totals"]["match"] is True
    assert res["filenames"] == ["Acme_INV-2.pdf"]


def test_match_listing_row_is_vendor_scoped():
    from app.pipeline.checks import match_listing_row
    by_number = {"13561": [
        {"invoice_number": "13561", "vendor": "Good News Resources Sdn Bhd"},
        {"invoice_number": "13561", "vendor": "Maxis Bhd"},
    ]}
    # the vendor breaks the tie
    row, cands = match_listing_row(by_number, "13561", "Maxis")
    assert row is not None and row["vendor"] == "Maxis Bhd" and len(cands) == 2
    # no vendor singles one out -> ambiguous, never a guess
    row, cands = match_listing_row(by_number, "13561", "Unrelated Trading")
    assert row is None and len(cands) == 2
    # unknown number -> no candidates at all
    assert match_listing_row(by_number, "99999", "Maxis") == (None, [])


@pytest.mark.asyncio
async def test_new_vendor_is_one_the_listing_never_paid(monkeypatch):
    """A vendor absent from every past-payment row is probably not a Maybank
    beneficiary yet; the output says so. A vendor the listing has paid
    before is not listed as new."""
    listing = [{"sheet": "Jul'26", "row": 8, "no": "PV0726/01",
                "date": "2026-07-23", "vendor": "Maxis Bhd",
                "invoice_number": "1000", "amount": 50.0, "status": "Paid"}]

    async def fake_listing(*a, **k):
        return listing
    monkeypatch.setattr(reference, "load_payment_listing", fake_listing)
    monkeypatch.setattr(reference, "load_maybank_headers", lambda *a, **k: [])

    res = await output.build_outputs([_Doc("a", "1000", 250.0)], excluded_doc_ids=set())
    assert res["new_vendors"] == ["Acme"]


class _Source:
    """Stands in for a DocumentSource holding exactly these file names."""

    def __init__(self, names: list[str]) -> None:
        self._names = names

    def list_names(self) -> list[str]:
        return self._names

    def get_reference(self, name: str) -> bytes:
        return b"PK"  # a stand-in blob; these tests never parse it


class _Doc:
    def __init__(self, doc_id: str, number: str, amount: float) -> None:
        self.id, self.kind, self.status = doc_id, "invoice", "extracted"
        self.filename = f"{doc_id}.pdf"
        self.fields = {"invoice_number": number, "amount": amount,
                       "currency": "MYR", "vendor": "Acme", "date": "2026-01-05"}
        self.confidence, self.error, self.parent_id = {}, "", None


@pytest.mark.asyncio
async def test_listing_flags_point_at_the_workbook_row(monkeypatch):
    """A match is a place to look, not just a verdict: the flag names the
    tab, row, voucher, date, amount and payee of the listing entry."""
    from app.pipeline import checks

    listing = [
        {"sheet": "Jul'26", "row": 12, "no": "PV0726/07", "date": "2026-07-23",
         "vendor": "Lim Shea Fee", "invoice_number": "245DHNQL-0015",
         "amount": 1044.95, "status": "Paid", "note": ""},
        {"sheet": "Apr'26", "row": 9, "no": "PV0426/01", "date": "2026-04-23",
         "vendor": "Howzat Creation", "invoice_number": "4115",
         "amount": 72.0, "status": "Paid", "note": ""},
        {"sheet": "Jul'26", "row": 16, "no": "PV0726/02", "date": "2026-07-23",
         "vendor": "Good News Resources Sdn Bhd", "invoice_number": "4115",
         "amount": 195.0, "status": "Paid", "note": ""},
    ]

    async def fake_listing(*a, **k):
        return listing
    monkeypatch.setattr(reference, "load_payment_listing", fake_listing)
    monkeypatch.setattr(reference, "load_policy_clauses", lambda *a, **k: [])

    docs = [_Doc("a", "245DHNQL-0015", 1200.0), _Doc("b", "4115", 72.0),
            _Doc("c", "NEW-1", 10.0)]
    docs[0].fields["vendor"] = "Genspark"
    docs[1].fields["vendor"] = "Unrelated Trading"
    flags = await checks.run_checks(docs)
    by_code = {}
    for fl in flags:
        by_code.setdefault(fl["code"], []).append(fl["reason"])

    paid = by_code["ALREADY_PAID"][0]
    assert "tab Jul'26 row 12" in paid and "voucher PV0726/07" in paid
    assert "dated 2026-07-23" in paid and "RM 1044.95" in paid
    assert "payee Lim Shea Fee" in paid
    assert "tab Jul'26 row 12" in by_code["AMOUNT_MISMATCH"][0]
    assert "tab Jul'26 row 12" in by_code["VENDOR_MISMATCH"][0]
    ambiguous = by_code["LISTING_AMBIGUOUS"][0]
    assert "tab Apr'26 row 9" in ambiguous and "tab Jul'26 row 16" in ambiguous
    not_found = by_code["NOT_IN_LISTING"][0]
    assert "3 invoice row(s) across 2 tab(s): Apr'26, Jul'26" in not_found


def test_each_run_keeps_its_own_copy_of_the_reference_files(monkeypatch, tmp_path):
    """The files are copied ONCE, when the run starts. Every later touch —
    flag decisions, corrections, output rebuilds — reads the run's copy
    and never goes back to the source. A change to the folder afterwards
    (or another run starting) cannot change what an earlier run sees."""
    from openpyxl import Workbook

    def _xlsx(fill) -> bytes:
        wb = Workbook(); fill(wb.active); buf = io.BytesIO(); wb.save(buf)
        return buf.getvalue()

    def _policy(cap):
        def fill(ws):
            ws.append(["Clause", "Category", "Cap", "Currency", "Text"])
            ws.append(["1.1", "Travel", cap, "MYR", f"Travel is capped at RM {cap}."])
        return fill

    def _template(ws):
        ws.append(["Payment Type", "Beneficiary Name", "Amount (RM)"])

    calls = {"list": 0, "get": 0}
    folder = {"cap": 100.0}  # what SharePoint holds right now

    class _Counting:
        def list_names(self):
            calls["list"] += 1
            return ["policy_sheet.xlsx", "maybank_template.xlsx", "payment_listing.xlsx"]

        def get_reference(self, name):
            calls["get"] += 1
            return {"policy_sheet.xlsx": _xlsx(_policy(folder["cap"])),
                    "maybank_template.xlsx": _xlsx(_template)}.get(name, b"PK")

    monkeypatch.setattr(reference, "get_source", lambda *a, **k: _Counting())

    # run 1 starts: one listing, one download per role file
    refs1 = tmp_path / "run1" / "reference"
    roles = reference.snapshot_references("https://x/AP", refs1)
    assert roles["payment_listing"] == "payment_listing.xlsx"
    assert calls == {"list": 1, "get": 3}
    # a review's worth of touches: nothing goes back to the source
    for _ in range(5):
        assert reference.load_policy_clauses(refs1)[0]["cap"] == 100.0
        reference.load_maybank_headers(refs1)
    assert calls == {"list": 1, "get": 3}

    # the client changes the policy, and run 2 starts on the same folder
    folder["cap"] = 250.0
    refs2 = tmp_path / "run2" / "reference"
    reference.snapshot_references("https://x/AP", refs2)
    assert calls == {"list": 2, "get": 6}
    # run 2 sees the new file; run 1's review still sees the file IT started with
    assert reference.load_policy_clauses(refs2)[0]["cap"] == 250.0
    assert reference.load_policy_clauses(refs1)[0]["cap"] == 100.0
    assert calls == {"list": 2, "get": 6}

    # an older run with no snapshot yet gets one on first touch, once
    refs0 = tmp_path / "run0" / "reference"
    reference.ensure_snapshot(refs0, "https://x/AP")
    reference.ensure_snapshot(refs0, "https://x/AP")
    assert calls == {"list": 3, "get": 9}
