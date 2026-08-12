"""The AI payment-listing reader, with the AI faked out.

The workbook built here is a miniature of the real ICMR listing: title
rows, headers on row 4, monthly-style tab, one payment spanning several
rows with several invoices inside, balance b/f and fund-received lines,
and a summary block. The "AI" is a scripted stand-in, so these tests cost
nothing and never flake — they pin the CODE half of the loop:

  - a correct reading extracts, pairs and flattens the grouped entries
  - the audit catches a wrong column, a wrongly-cut span, a merged span,
    an omitted payment, and missing required columns — the adversarial
    cases where pure arithmetic alone would be fooled
  - the loop feeds problems back and accepts the corrected second answer
  - a reading that never verifies raises ListingUnreadable, not a guess
  - unpairable invoice groups keep amount=None (checks skips, never
    zero-compares)
"""
from __future__ import annotations

import pytest
from openpyxl import Workbook
from pydantic import ValidationError

from app.pipeline import listing_agent
from app.pipeline.listing_agent import (
    ColumnRoles, EntrySpan, ListingUnreadable, SheetReading,
    flatten_reading, verify_reading,
)


def _icmr_sheet():
    """Rows mirror the structure (not the data) of the real client file."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Jul'26"
    ws["A1"] = "Name:"; ws["B1"] = "Institute For Capital Market Research"
    ws["A2"] = "A/C No:"; ws["B2"] = "514712417644"
    # headers on row 4, not row 1 — receipt (money in) is column I
    for col, head in zip("ABCDEFGHI", ["Date", "Cheque/Journal No.",
                                       "Invoice / Reference No.", "Payee Name",
                                       "Description", "Line (MYR)",
                                       "Payment (MYR)", "Balance (MYR)",
                                       "Receipt (MYR)"]):
        ws[f"{col}4"] = head
    # balance b/f, then money in
    ws["E5"] = "Balance b/f"; ws["H5"] = 7.90
    ws["A6"] = "2026-07-07"; ws["E6"] = "Fund received"
    ws["I6"] = 37195.98; ws["H6"] = 37203.88
    # one grouped payment: two invoices, two line amounts, one payment
    ws["A8"] = "2026-07-23"; ws["B8"] = "PV0726/01"; ws["C8"] = "580261111513"
    ws["D8"] = "PricewaterhouseCoopers Taxation Services Sdn. Bhd."
    ws["E8"] = "April 2026 payroll fees"; ws["F8"] = 5572.80
    ws["C9"] = "580271100169"; ws["E9"] = "Accounting fee"; ws["F9"] = 10044.00
    ws["G8"] = 15616.80; ws["H9"] = 21587.08
    # single-line payment
    ws["A10"] = "2026-07-23"; ws["B10"] = "PV0726/02"; ws["C10"] = "13561"
    ws["D10"] = "Good News Resources Sdn Bhd"; ws["E10"] = "Name cards"
    ws["G10"] = 195.00; ws["H10"] = 21392.08
    # unpairable group: two invoices, FOUR line amounts
    ws["A12"] = "2026-07-23"; ws["B12"] = "PV0726/07"; ws["C12"] = "245DHNQL-0015"
    ws["D12"] = "Lim Shea-Fee"; ws["F12"] = 8000.00
    ws["C13"] = "CA50CBEE-0015"; ws["F13"] = 1044.95
    ws["F14"] = 343.70; ws["F15"] = 127.60
    ws["G12"] = 9516.25; ws["H15"] = 11875.83
    # summary block (column B — outside the money columns, so no span)
    ws["A18"] = "Opening balance to utilise"; ws["B18"] = 7.90
    ws["A19"] = "Net Payment"; ws["B19"] = 37193.88
    return wb, ws


GOOD_COLUMNS = ColumnRoles(date="A", voucher_no="B", invoice_no="C",
                           payee="D", description="E", line_amount="F",
                           payment="G", balance="H", receipt="I")

GOOD_READING = SheetReading(
    is_payment_sheet=True,
    columns=GOOD_COLUMNS,
    entries=[
        EntrySpan(first_row=5, last_row=5, kind="other"),
        EntrySpan(first_row=6, last_row=6, kind="other"),
        EntrySpan(first_row=8, last_row=9, kind="payment"),
        EntrySpan(first_row=10, last_row=10, kind="payment"),
        EntrySpan(first_row=12, last_row=15, kind="payment"),
    ],
    why="headers row 4, grouped entries",
)


def test_correct_reading_verifies_and_flattens():
    wb, ws = _icmr_sheet()
    assert verify_reading(ws, GOOD_READING) == []

    rows = flatten_reading(ws, GOOD_READING)
    by_number = {r["invoice_number"]: r for r in rows}
    # grouped entry: invoice numbers paired to line amounts one-to-one
    assert by_number["580261111513"]["amount"] == pytest.approx(5572.80)
    assert by_number["580271100169"]["amount"] == pytest.approx(10044.00)
    assert by_number["580261111513"]["vendor"].startswith("PricewaterhouseCoopers")
    # single-invoice entry takes the payment total
    assert by_number["13561"]["amount"] == pytest.approx(195.00)
    # unpairable group (2 invoices, 4 lines): honest None, never a guess
    assert by_number["245DHNQL-0015"]["amount"] is None
    assert by_number["CA50CBEE-0015"]["amount"] is None
    # recorded payments are Paid — the duplicate-payment guard depends on it
    assert all(r["status"] == "Paid" for r in rows)
    assert by_number["13561"]["date"] == "2026-07-23"
    assert by_number["13561"]["no"] == "PV0726/02"


def test_wrong_payment_column_is_caught():
    wb, ws = _icmr_sheet()
    bad = GOOD_READING.model_copy(update={
        "columns": GOOD_COLUMNS.model_copy(update={"payment": "F", "line_amount": None})})
    problems = verify_reading(ws, bad)
    # the balance moves by column G, not F — the walk must object somewhere
    assert any("running balance" in p for p in problems)


def test_wrongly_cut_span_is_caught():
    wb, ws = _icmr_sheet()
    bad = GOOD_READING.model_copy(update={
        "entries": [e for e in GOOD_READING.entries
                    if not (e.first_row == 8 and e.last_row == 9)]
        + [EntrySpan(first_row=8, last_row=8, kind="payment")]})
    problems = verify_reading(ws, bad)
    assert any("line amounts sum" in p for p in problems)


def test_merged_spans_are_caught():
    """Two payments glued into one span pass pure arithmetic (sums are
    additive) — identity constraints are what must catch them."""
    wb, ws = _icmr_sheet()
    merged = GOOD_READING.model_copy(update={
        "entries": [
            EntrySpan(first_row=5, last_row=5, kind="other"),
            EntrySpan(first_row=6, last_row=6, kind="other"),
            EntrySpan(first_row=8, last_row=10, kind="payment"),  # 01 + 02 merged
            EntrySpan(first_row=12, last_row=15, kind="payment"),
        ]})
    problems = verify_reading(ws, merged)
    assert any("exactly one payment total" in p for p in problems)
    assert any("different payees" in p for p in problems)


def test_omitted_payment_is_caught():
    """Showing only part of the sheet must fail: every number in a money
    column has to be inside some span."""
    wb, ws = _icmr_sheet()
    partial = GOOD_READING.model_copy(update={
        "entries": [e for e in GOOD_READING.entries if e.first_row != 12]})
    problems = verify_reading(ws, partial)
    assert any("belongs to no span" in p and "G12" in p for p in problems)


def test_missing_required_columns_is_caught():
    wb, ws = _icmr_sheet()
    bare = GOOD_READING.model_copy(update={
        "columns": GOOD_COLUMNS.model_copy(update={"payment": None, "payee": None})})
    problems = verify_reading(ws, bare)
    assert any("payment column" in p for p in problems)
    assert any("payee column" in p for p in problems)


def test_overlapping_spans_are_caught():
    wb, ws = _icmr_sheet()
    bad = GOOD_READING.model_copy(update={
        "entries": [EntrySpan(first_row=8, last_row=10, kind="payment"),
                    EntrySpan(first_row=10, last_row=15, kind="payment")]})
    assert any("overlap" in p for p in verify_reading(ws, bad))


def test_header_text_as_column_letter_is_rejected_at_schema():
    with pytest.raises(ValidationError):
        ColumnRoles(payment="Payment")


def test_overlong_sheet_fails_loudly():
    wb = Workbook()
    ws = wb.active
    ws[f"A{listing_agent.MAX_SHEET_ROWS + 1}"] = "x"
    with pytest.raises(ListingUnreadable):
        listing_agent.grid_text(ws)


class _ScriptedAgent:
    """Stands in for the AI: answers from a fixed script, records prompts."""

    def __init__(self, outputs: list[SheetReading]) -> None:
        self._outputs = list(outputs)
        self.prompts: list[str] = []

    async def run(self, prompt, **kwargs):
        self.prompts.append(prompt)

        class R:
            output = self._outputs.pop(0)
        return R()


@pytest.mark.asyncio
async def test_loop_feeds_problems_back_and_accepts_correction(monkeypatch):
    wb, ws = _icmr_sheet()
    wrong = GOOD_READING.model_copy(update={
        "columns": GOOD_COLUMNS.model_copy(update={"payment": "F", "line_amount": None})})
    agent = _ScriptedAgent([wrong, GOOD_READING])
    monkeypatch.setattr(listing_agent, "create_agent", lambda *a, **k: agent)

    rows = await listing_agent.read_sheet(ws)

    assert len(agent.prompts) == 2
    # the second prompt must carry the verifier's objection — that feedback
    # IS the "reason through and act" loop
    assert "failed verification" in agent.prompts[1]
    assert {r["invoice_number"] for r in rows} >= {"580261111513", "13561"}


@pytest.mark.asyncio
async def test_unverifiable_reading_raises_not_guesses(monkeypatch):
    wb, ws = _icmr_sheet()
    wrong = GOOD_READING.model_copy(update={
        "columns": GOOD_COLUMNS.model_copy(update={"payment": "F", "line_amount": None})})
    agent = _ScriptedAgent([wrong] * listing_agent.MAX_ROUNDS)
    monkeypatch.setattr(listing_agent, "create_agent", lambda *a, **k: agent)

    with pytest.raises(ListingUnreadable):
        await listing_agent.read_sheet(ws)


@pytest.mark.asyncio
async def test_not_payment_sheet_is_challenged_once(monkeypatch):
    """The miniature carries payment-style headers, so a 'not a payment
    sheet' answer gets one push-back; a repeated 'no' is accepted."""
    wb, ws = _icmr_sheet()
    no = SheetReading(is_payment_sheet=False, why="cover page")
    agent = _ScriptedAgent([no, no])
    monkeypatch.setattr(listing_agent, "create_agent", lambda *a, **k: agent)

    assert await listing_agent.read_sheet(ws) == []
    assert len(agent.prompts) == 2
    assert "payment-style column headers" in agent.prompts[1]


@pytest.mark.asyncio
async def test_plain_cover_sheet_is_skipped_without_challenge(monkeypatch):
    wb = Workbook()
    ws = wb.active
    ws["A1"] = "ICMR - FY2026 Payment Listing"; ws["A2"] = "Prepared by: WC"
    agent = _ScriptedAgent([SheetReading(is_payment_sheet=False, why="cover")])
    monkeypatch.setattr(listing_agent, "create_agent", lambda *a, **k: agent)
    assert await listing_agent.read_sheet(ws) == []
    assert len(agent.prompts) == 1


@pytest.mark.asyncio
async def test_crashing_reading_becomes_feedback_not_exception(monkeypatch):
    """A structurally valid but nonsensical reading (column beyond the
    sheet) must turn into loop feedback, not escape as a crash."""
    wb, ws = _icmr_sheet()
    weird = GOOD_READING.model_copy(update={
        "columns": GOOD_COLUMNS.model_copy(update={"payment": "ZZZ"})})
    agent = _ScriptedAgent([weird, GOOD_READING])
    monkeypatch.setattr(listing_agent, "create_agent", lambda *a, **k: agent)

    rows = await listing_agent.read_sheet(ws)
    assert len(agent.prompts) == 2
    assert rows  # recovered on the corrected reading


def test_number_parsing_tolerates_text_amounts():
    assert listing_agent._num("RM 1,200.00") == pytest.approx(1200.00)
    assert listing_agent._num("(500.00)") == pytest.approx(-500.00)
    assert listing_agent._num("MYR 37,195.98") == pytest.approx(37195.98)
    assert listing_agent._num("two hundred") is None
    assert listing_agent._num(None) is None
    assert listing_agent._num(True) is None


def test_grid_text_labels_cells_with_coordinates():
    wb, ws = _icmr_sheet()
    grid = listing_agent.grid_text(ws)
    assert "Sheet name: \"Jul'26\"" in grid
    assert "B8: PV0726/01" in grid
    assert "G8: 15616.8" in grid
