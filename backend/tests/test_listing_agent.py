"""The AI payment-listing reader, with the AI faked out.

The workbook built here is a miniature of the real ICMR listing: title
rows, headers on row 4, monthly-style tab, one payment spanning several
rows with several invoices inside, balance b/f and fund-received lines,
and a summary block. The "AI" is a scripted stand-in, so these tests cost
nothing and never flake — they pin the CODE half of the loop:

  - a correct reading extracts, pairs and flattens the grouped entries
  - the arithmetic audit catches a wrong column and a wrongly-cut span
  - the loop feeds problems back and accepts the corrected second answer
  - a reading that never verifies raises ListingUnreadable, not a guess
  - unpairable invoice groups keep amount=None, and checks skips (not
    zero-compares) such rows
"""
from __future__ import annotations

import pytest
from openpyxl import Workbook

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
    # headers on row 4, not row 1
    for col, head in zip("ABCDEFGH", ["Date", "Cheque/Journal No.",
                                      "Invoice / Reference No.", "Payee Name",
                                      "Description", "Line (MYR)",
                                      "Payment (MYR)", "Balance (MYR)"]):
        ws[f"{col}4"] = head
    # balance b/f, then money in
    ws["E5"] = "Balance b/f"; ws["H5"] = 7.90
    ws["E6"] = "Fund received"; ws["G6"] = None; ws["H6"] = 37203.88
    ws["F6"] = None
    ws["A6"] = "2026-07-07"
    # (The real sheet has a receipt column for fund-received rows; this
    # miniature has none, so tests start the balance walk after row 6.)
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
    # summary block — belongs to no span
    ws["A18"] = "Opening balance to utilise"; ws["B18"] = 7.90
    ws["A19"] = "Net Payment"; ws["B19"] = 37193.88
    return wb, ws


GOOD_COLUMNS = ColumnRoles(date="A", voucher_no="B", invoice_no="C",
                           payee="D", description="E", line_amount="F",
                           payment="G", balance="H")

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
    # The fund-received jump (row 6) has no receipt column in the miniature,
    # so restrict the walk start to after it: spans from row 8 on.
    reading = GOOD_READING.model_copy(update={
        "entries": [e for e in GOOD_READING.entries if e.first_row >= 8]})
    assert verify_reading(ws, reading) == []

    rows = flatten_reading(ws, reading)
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
        "columns": GOOD_COLUMNS.model_copy(update={"payment": "F", "line_amount": None}),
        "entries": [e for e in GOOD_READING.entries if e.first_row >= 8],
    })
    problems = verify_reading(ws, bad)
    # balance moves by G, not F — the walk must object
    assert any("running balance" in p for p in problems)


def test_wrongly_cut_span_is_caught():
    wb, ws = _icmr_sheet()
    bad = GOOD_READING.model_copy(update={
        "entries": [
            # cuts the two-row group in half: line sum no longer matches
            EntrySpan(first_row=8, last_row=8, kind="payment"),
            EntrySpan(first_row=10, last_row=10, kind="payment"),
        ]})
    problems = verify_reading(ws, bad)
    assert any("line amounts sum" in p for p in problems)


def test_overlapping_spans_are_caught():
    wb, ws = _icmr_sheet()
    bad = GOOD_READING.model_copy(update={
        "entries": [EntrySpan(first_row=8, last_row=10, kind="payment"),
                    EntrySpan(first_row=10, last_row=10, kind="payment")]})
    assert any("overlap" in p for p in verify_reading(ws, bad))


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
        "columns": GOOD_COLUMNS.model_copy(update={"payment": "F", "line_amount": None}),
        "entries": [e for e in GOOD_READING.entries if e.first_row >= 8],
    })
    right = GOOD_READING.model_copy(update={
        "entries": [e for e in GOOD_READING.entries if e.first_row >= 8]})
    agent = _ScriptedAgent([wrong, right])
    monkeypatch.setattr(listing_agent, "create_agent", lambda *a, **k: agent)

    rows = await listing_agent.read_sheet(ws)

    assert len(agent.prompts) == 2
    # the second prompt must carry the verifier's objection — that feedback
    # IS the "reason through and act" loop
    assert "failed verification" in agent.prompts[1]
    assert "running balance" in agent.prompts[1]
    assert {r["invoice_number"] for r in rows} >= {"580261111513", "13561"}


@pytest.mark.asyncio
async def test_unverifiable_reading_raises_not_guesses(monkeypatch):
    wb, ws = _icmr_sheet()
    wrong = GOOD_READING.model_copy(update={
        "columns": GOOD_COLUMNS.model_copy(update={"payment": "F", "line_amount": None}),
        "entries": [e for e in GOOD_READING.entries if e.first_row >= 8],
    })
    agent = _ScriptedAgent([wrong] * listing_agent.MAX_ROUNDS)
    monkeypatch.setattr(listing_agent, "create_agent", lambda *a, **k: agent)

    with pytest.raises(ListingUnreadable):
        await listing_agent.read_sheet(ws)


@pytest.mark.asyncio
async def test_non_payment_sheet_is_skipped(monkeypatch):
    wb, ws = _icmr_sheet()
    agent = _ScriptedAgent([SheetReading(is_payment_sheet=False, why="cover page")])
    monkeypatch.setattr(listing_agent, "create_agent", lambda *a, **k: agent)
    assert await listing_agent.read_sheet(ws) == []


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
