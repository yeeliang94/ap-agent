"""AI-assisted reading of a client payment listing.

The listing is the record of PAST payments. The one question the pipeline
asks of it: "has this invoice been paid before — and if so, on which tab,
which row, which voucher, when, how much, to whom?" Everything in this
module exists to answer that reliably. Reconciling the client's books is
not the job; the arithmetic below is used only as evidence that the AI
mapped the sheet correctly.

Real listings (see docs: the ICMR file) look nothing like a flat table:
title rows above the headers, one tab per month, one payment entry
spanning several rows with several invoices inside it, balance-b/f and
fund-received lines mixed in, and a summary block at the bottom.

Division of labour, strictly kept:

  - The AI READS STRUCTURE ONLY. It sees the sheet as a labelled grid and
    answers with coordinates: which column plays which role, which row
    spans form one entry, which rows are not payments at all. It never
    retypes an amount.
  - CODE pulls every number out of the cells the AI pointed at, then
    audits the AI's reading against the file's own arithmetic: line
    amounts must sum to their entry's payment, and the running balance
    column must move by exactly receipts minus payments.
  - When the audit fails, the mismatches go back to the AI and it looks
    again — the reason/act loop — up to MAX_ROUNDS per sheet.

Two kinds of audit problem, treated differently at the end of the loop:

  - STRUCTURE problems (a required column unnamed, money cells outside
    every span, two payees merged into one entry, overlapping spans) mean
    the invoice→payment association itself cannot be trusted. A sheet
    that still has these after MAX_ROUNDS fails the run.
  - ARITHMETIC problems (a line total or a running-balance step that is
    off) usually mean a wrong span or column — but sometimes just a typo
    in the client's file. Once the structure checks pass, an arithmetic
    leftover is accepted WITH A WARNING naming the rows, so a client's
    8-cent typo never blocks the duplicate-payment check.

Every flattened row carries where it came from (sheet, row, voucher,
date) so a match can be shown to a reviewer as a place to look, not just
a number.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from ..model_layer import USAGE_LIMITS, create_agent

log = logging.getLogger("listing_agent")

MAX_ROUNDS = 3

# The most rows WITH CONTENT one sheet may hold. Beyond this we FAIL,
# loudly — a grid quietly cut short would let a partial reading pass its
# own audit. Content rows, not Excel's max_row: a client who formats the
# grid down to row 500 "just in case" has not written 500 rows.
MAX_SHEET_ROWS = 300

# Hard ceiling on the cells a sheet may physically hold (styled-but-empty
# included). A workbook styled to Excel's last row is not a listing; refuse
# it before walking a million cells.
MAX_SHEET_CELLS = 200_000

# The other input boundaries, each a refusal rather than a quiet cut: a
# listing wider than this is not a listing; a workbook with this many tabs
# would cost that many AI reads for one run.
MAX_SHEET_COLUMNS = 60
MAX_SHEETS = 40
# Cell texts (invoice numbers, payees, notes) are capped on the flat rows so
# a runaway cell never lands in a flag or an output. The grid the AI sees
# is truncated separately (80 characters per cell).
MAX_CELL_TEXT = 200

# Cell text is sent to the model as DATA. The model answers only
# coordinates, schema-checked (ColumnRoles / EntrySpan), so text hidden in a
# cell cannot become an instruction the pipeline acts on: nothing it says
# reaches anything but the Activity tab (observations) — and that is text.

# Reconciliation tolerance: real sheets round at 2dp, so half a cent of
# accumulated drift per comparison is noise, anything more is a misread.
TOLERANCE = 0.02


class ListingUnreadable(Exception):
    """The AI's reading of a sheet could not be confirmed after MAX_ROUNDS
    attempts. Failing the run beats checking invoices against a reading
    whose structure nothing corroborates."""


# A spreadsheet column letter ("A".."XFD"). Anything else — a header text,
# a number — is rejected at the schema layer, so pydantic-ai makes the
# model re-answer instead of the coordinate lookup blowing up mid-loop.
_COL = r"^[A-Za-z]{1,3}$"


class ColumnRoles(BaseModel):
    """Which column letter plays which role on ONE sheet. None = absent."""
    date: str | None = Field(default=None, pattern=_COL)
    voucher_no: str | None = Field(default=None, pattern=_COL,
                                   description="PV / cheque / journal number column")
    invoice_no: str | None = Field(default=None, pattern=_COL,
                                   description="invoice or reference number column")
    payee: str | None = Field(default=None, pattern=_COL)
    description: str | None = Field(default=None, pattern=_COL)
    line_amount: str | None = Field(default=None, pattern=_COL,
                                    description="per-line amounts inside a grouped entry")
    receipt: str | None = Field(default=None, pattern=_COL, description="money IN column")
    payment: str | None = Field(default=None, pattern=_COL,
                                description="money OUT column (the entry's total)")
    balance: str | None = Field(default=None, pattern=_COL,
                                description="running balance column")
    status: str | None = Field(default=None, pattern=_COL,
                               description="a Paid/Planned status column, if the sheet has one")


class EntrySpan(BaseModel):
    """One money-moving block of consecutive rows.

    kind="payment": an actual payment entry (has payee/invoices).
    kind="other": balance b/f, fund received, bank charges — rows that move
    the balance but are not payments. Marking these lets code verify the
    running balance across the whole sheet without inventing gaps.
    """
    first_row: int = Field(ge=1)
    last_row: int = Field(ge=1)
    kind: Literal["payment", "other"]


class SheetReading(BaseModel):
    """The AI's structural answer for one sheet."""
    is_payment_sheet: bool = Field(
        description="False for cover pages, summaries, notes — they are skipped")
    columns: ColumnRoles | None = None
    entries: list[EntrySpan] = Field(default_factory=list)
    why: str = Field(max_length=300)
    # Quirks worth a human's eye, in the AI's words: "column F has no
    # header", "rows 26/28 are recurring payments with no reference".
    # Printed in the Activity tab, never acted on by code.
    observations: list[str] = Field(default_factory=list, max_length=10)


@dataclass
class SheetResult:
    """What reading one sheet produced: the flat rows, plus the plain
    sentences the run's Activity tab shows about how it went."""
    rows: list[dict] = field(default_factory=list)
    notes: list[tuple[str, str]] = field(default_factory=list)  # (level, text)


_INSTRUCTIONS = (
    "You are reading one sheet of an accounts-payable payment listing "
    "workbook. The sheet is given as a grid of cells labelled like 'D13: "
    "some value'. Do NOT copy any numbers into your answer — answer only "
    "with structure: column letters for each role, and row spans.\n"
    "- A single payment entry often spans SEVERAL consecutive rows: the "
    "payee and voucher number on the first row, extra invoice numbers, "
    "description lines and per-line amounts on the rows below it. The span "
    "covers ALL of those rows.\n"
    "- A column of per-line amounts may have NO header — recognise it by "
    "its numbers sitting beside description lines inside grouped entries.\n"
    "- Mark balance b/f, fund received, bank charges and similar "
    "non-payment money movements as kind='other' spans, so every movement "
    "of the running balance is accounted for.\n"
    "- Title and header rows ABOVE the first entry belong to NO span. "
    "Summary blocks BELOW the entries (totals row, opening balance, net "
    "payment, total fund to request) are kind='other' spans, so every "
    "value on the sheet is accounted for. Signature lines with no figures "
    "need no span.\n"
    "- If the sheet holds no payment records at all, answer "
    "is_payment_sheet=false and nothing else.\n"
    "- In observations, list (briefly) anything a reviewer should know "
    "about this sheet: an unlabelled column, entries with no reference "
    "number, hidden or odd rows, notes in the margins. Up to 10 short lines."
)


# ---- pulling values out of the cells the AI pointed at ---------------------

_CURRENCY_PREFIX = re.compile(r"^\s*(RM|MYR)\s*", re.IGNORECASE)

# Text in the invoice column that is a remark, not a reference number:
# "(Revised invoice)", "credit note", "cancelled". Kept as a note on the
# row instead of being matched against uploaded invoices. Keyword-based
# and conservative: a bare "(INV-123)" is still a reference number.
_INVOICE_NOTE = re.compile(
    r"\b(revised|credit note|cancelled|canceled|replaces|see note)\b",
    re.IGNORECASE)


def _num(value) -> float | None:
    """A cell's numeric value, tolerating text like 'RM 1,200.00' and
    accountant negatives '(500.00)'. None when the cell isn't a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = _CURRENCY_PREFIX.sub("", str(value).strip()).replace(",", "")
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    try:
        return float(s)
    except ValueError:
        return None


def _text(ws, col: str | None, row: int) -> str:
    if not col:
        return ""
    v = ws[f"{col}{row}"].value
    return str(v).strip()[:MAX_CELL_TEXT] if v is not None and str(v).strip() else ""


def _texts(ws, col: str | None, span: EntrySpan) -> list[str]:
    """Non-empty cell texts in one column across a span, top to bottom."""
    if not col:
        return []
    return [t for row in range(span.first_row, span.last_row + 1)
            if (t := _text(ws, col, row))]


def _nums(ws, col: str | None, span: EntrySpan) -> list[float]:
    if not col:
        return []
    out = []
    for row in range(span.first_row, span.last_row + 1):
        n = _num(ws[f"{col}{row}"].value)
        if n is not None:
            out.append(n)
    return out


def _iso_date(value) -> str:
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    return str(value).strip()


def content_rows(ws) -> list[int]:
    """Row numbers that hold at least one non-empty value.

    Excel's max_row counts rows that merely carry a fill or a border; a
    client's green grid formatted to row 500 is not 500 rows of listing.
    Walks only the cells the file actually stores (ws._cells) — iter_rows
    would materialise every cell in the rectangle, which for a sheet styled
    to Excel's last row is a million objects.
    """
    cells = getattr(ws, "_cells", None)
    if cells is None:  # read-only worksheets: fall back to iteration
        cells = {(c.row, c.column): c for row in ws.iter_rows() for c in row}
    if len(cells) > MAX_SHEET_CELLS:
        raise ListingUnreadable(
            f"Sheet {ws.title} holds {len(cells)} cells (formatted or filled) "
            f"— more than the {MAX_SHEET_CELLS} this reader supports.")
    filled = [(r, c) for (r, c), cell in cells.items()
              if cell.value is not None and str(cell.value).strip()]
    widest = max((c for _r, c in filled), default=0)
    if widest > MAX_SHEET_COLUMNS:
        raise ListingUnreadable(
            f"Sheet {ws.title} has content in column {widest} — wider than "
            f"the {MAX_SHEET_COLUMNS} columns this reader supports.")
    return sorted({r for r, _c in filled})


def stale_formula_cells(ws_values, ws_formulas) -> list[str]:
    """Coordinates of formula cells that carry NO cached value.

    ws_values is the sheet opened data_only (what the audit reads);
    ws_formulas the same sheet opened normally. A workbook saved without
    recalculating (or written by a tool that computes nothing) stores the
    formula but no result, so data_only shows None where a balance should
    be — indistinguishable from an empty cell unless we look at both.
    """
    if ws_formulas is None:
        return []
    stale = []
    cells = getattr(ws_formulas, "_cells", None)
    if cells is None:
        cells = {(c.row, c.column): c for row in ws_formulas.iter_rows() for c in row}
    for (row, col), cell in sorted(cells.items()):
        v = cell.value
        is_formula = (isinstance(v, str) and v.startswith("=")) or \
            getattr(cell, "data_type", None) == "f"
        if is_formula and ws_values.cell(row=row, column=col).value is None:
            stale.append(cell.coordinate)
    return stale


def _tab_label(ws) -> str:
    """'Tab X' — plus '(hidden tab)' when Excel would not show it. Hidden
    tabs are read like any other (a hidden month is still a month), but a
    reviewer opening the workbook needs to know why they cannot see it."""
    state = getattr(ws, "sheet_state", "visible")
    return f"Tab {ws.title}" + ("" if state == "visible" else " (hidden tab)")


def grid_text(ws) -> str:
    """The sheet as the AI sees it: every non-empty cell, labelled.

    Refuses over-long sheets rather than truncating: rows the AI never saw
    are rows the audit can never question.
    """
    rows = content_rows(ws)
    if len(rows) > MAX_SHEET_ROWS:
        raise ListingUnreadable(
            f"Sheet {ws.title} has {len(rows)} rows of content — more than "
            f"the {MAX_SHEET_ROWS} this reader supports. Split the sheet, or "
            "raise MAX_SHEET_ROWS deliberately.")
    lines = [f"Sheet name: {ws.title!r}"]
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row or 1):
        cells = [
            f"{c.column_letter}{c.row}: {str(c.value)[:80]}"
            for c in row if c.value is not None and str(c.value).strip()
        ]
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines)


# ---- the audit ---------------------------------------------------------------

STRUCTURE = "structure"
ARITHMETIC = "arithmetic"


def audit_reading(ws, reading: SheetReading) -> list[tuple[str, str]]:
    """Audit a reading against the sheet's own arithmetic.

    Returns (kind, problem) pairs — kind is STRUCTURE or ARITHMETIC (see
    the module docstring for why they end differently). Empty = the
    file's numbers corroborate the reading.

    The audit assumes the AI is wrong until the sheet proves otherwise:
    - required roles: a payment sheet must name payment, invoice and payee
      columns — without them the flattened rows would silently lose the
      duplicate-payment guard
    - coverage: EVERY row holding a number in the payment, receipt or
      balance columns must lie inside some span, so the AI cannot pass by
      showing only a subset of the sheet
    - identity: one payment entry has exactly one payment total, one payee
      and at most one voucher — merged neighbours pass pure arithmetic
      (sums are additive) but would mis-attribute invoices to the first
      payee, so identity is what actually separates entries
    - line amounts inside an entry must add up to its payment
    - the running balance walk, which anchors every covered movement
    """
    problems: list[tuple[str, str]] = []
    cols = reading.columns or ColumnRoles()
    spans = sorted(reading.entries, key=lambda s: s.first_row)

    if reading.is_payment_sheet:
        for role in ("payment", "invoice_no", "payee"):
            if getattr(cols, role) is None:
                problems.append((STRUCTURE,
                    f"a payment sheet needs its {role} column identified — "
                    "if the sheet truly has none, it is not a payment sheet"))

    for a, b in zip(spans, spans[1:]):
        if a.last_row >= b.first_row:
            problems.append((STRUCTURE,
                f"spans rows {a.first_row}-{a.last_row} and "
                f"{b.first_row}-{b.last_row} overlap — each row belongs to at "
                "most one span"))

    # Coverage: money that moved but sits outside every span means the
    # reading skipped part of the sheet.
    covered = set()
    for span in spans:
        covered.update(range(span.first_row, span.last_row + 1))
    rows = content_rows(ws)
    for col in {cols.payment, cols.receipt, cols.balance} - {None}:
        for row in rows:
            if row in covered:
                continue
            if _num(ws[f"{col}{row}"].value) is not None:
                problems.append((STRUCTURE,
                    f"cell {col}{row} holds a number in a money column but "
                    "belongs to no span — every money movement (including "
                    "balance b/f, fund received, totals rows) must be inside "
                    "a span, kind='other' if it is not a payment"))
    # Coverage of the invoice and line-amount columns too: an entry cut
    # short leaves its later invoice rows outside every span, and those
    # rows often carry no money-column number at all (payment on the
    # first row, balance on the last). Rows above the first span are the
    # title and header block and are exempt; anything below must be
    # inside a span — kind='other' for summary figures.
    first_span_row = spans[0].first_row if spans else 1
    for col in {cols.invoice_no, cols.line_amount} - {None}:
        for row in rows:
            if row in covered or row < first_span_row:
                continue
            if _text(ws, col, row):
                problems.append((STRUCTURE,
                    f"cell {col}{row} holds a value in the "
                    f"{'invoice' if col == cols.invoice_no else 'line amount'} "
                    "column but belongs to no span — extend the entry it "
                    "belongs to, or mark summary rows kind='other'"))

    prev_balance: float | None = None
    pending = 0.0  # net movement since the last span that showed a balance
    for span in spans:
        if span.first_row > span.last_row:
            problems.append((STRUCTURE,
                f"span rows {span.first_row}-{span.last_row} is inverted"))
            continue
        payments = _nums(ws, cols.payment, span)
        payment = sum(payments)
        receipt = sum(_nums(ws, cols.receipt, span))
        lines = _nums(ws, cols.line_amount, span)

        if span.kind == "payment":
            # Identity: one entry, one total, one payee, one voucher.
            if len(payments) != 1:
                problems.append((STRUCTURE,
                    f"rows {span.first_row}-{span.last_row}: a payment span "
                    f"must hold exactly one payment total, found "
                    f"{len(payments)} — split merged entries apart"))
            payees = set(_texts(ws, cols.payee, span))
            if len(payees) > 1:
                problems.append((STRUCTURE,
                    f"rows {span.first_row}-{span.last_row}: {len(payees)} "
                    "different payees in one span — one span is one payee's "
                    "payment, split it"))
            vouchers = set(_texts(ws, cols.voucher_no, span))
            if len(vouchers) > 1:
                problems.append((STRUCTURE,
                    f"rows {span.first_row}-{span.last_row}: {len(vouchers)} "
                    "voucher numbers in one span — split it"))
            # Several invoices but no per-line amounts is how a missed
            # (often header-less) line-amount column slips through: the
            # invoices would all lose their amounts silently.
            invoices = [t for t in _texts(ws, cols.invoice_no, span)
                        if not _INVOICE_NOTE.search(t)]
            if len(invoices) > 1 and not lines and cols.line_amount is None:
                problems.append((STRUCTURE,
                    f"rows {span.first_row}-{span.last_row}: {len(invoices)} "
                    "invoice numbers but no line_amount column named — look "
                    "for a column of per-line amounts (it may have no header)"))

        # Inside one entry, the per-line amounts must add up to its payment.
        # STRUCTURE, not arithmetic: this is the signature of an entry cut
        # at the wrong row or absorbed into its neighbour — a reading that
        # would attribute invoices to the wrong payment. (A client typo in
        # a line amount also lands here and fails the tab; that trade-off
        # is deliberate — the association matters more than the tab.)
        if payment and lines and abs(sum(lines) - payment) > TOLERANCE:
            problems.append((STRUCTURE,
                f"rows {span.first_row}-{span.last_row}: the line amounts sum "
                f"to {sum(lines):.2f} but the payment column says {payment:.2f} "
                "— the span may be cut wrong or a column misassigned"))

        # The running balance must move by exactly receipts - payments.
        balances = _nums(ws, cols.balance, span)
        movement = receipt - payment
        if balances:
            if prev_balance is not None:
                expected = prev_balance + pending + movement
                if abs(expected - balances[-1]) > TOLERANCE:
                    problems.append((ARITHMETIC,
                        f"rows {span.first_row}-{span.last_row}: running "
                        f"balance should be {expected:.2f} but the balance "
                        f"column shows {balances[-1]:.2f}"))
            prev_balance, pending = balances[-1], 0.0
        else:
            pending += movement

    if reading.is_payment_sheet and not any(s.kind == "payment" for s in spans):
        problems.append((STRUCTURE,
            "the sheet was called a payment sheet but no span has kind='payment'"))
    return problems


def verify_reading(ws, reading: SheetReading) -> list[str]:
    """The audit's problems as plain sentences (kinds dropped)."""
    return [text for _, text in audit_reading(ws, reading)]


# ---- flattening ----------------------------------------------------------------

def _pair_invoices(ws, cols: ColumnRoles, span: EntrySpan,
                   payment: float) -> tuple[list[tuple[int, str, float | None]], list[str]]:
    """(row, invoice number, amount) for each invoice in one entry, plus any
    note texts found in the invoice column.

    Pairing follows only what the sheet shows:
      1. an invoice number and a line amount on the SAME ROW are a pair;
      2. an entry with exactly one invoice number takes the payment total;
      3. anything else keeps amount=None — an honest "this file doesn't
         say", which downstream treats as "cannot compare", never as zero.
    There is no pairing by list position: two numbers above two amounts
    is a layout the sheet has not actually paired, so neither do we.
    """
    per_row: list[tuple[int, str, float | None]] = []
    notes: list[str] = []
    for r in range(span.first_row, span.last_row + 1):
        text = _text(ws, cols.invoice_no, r)
        if text and _INVOICE_NOTE.search(text):
            notes.append(text)
            text = ""
        amt = _num(ws[f"{cols.line_amount}{r}"].value) if cols.line_amount else None
        if text or amt is not None:
            per_row.append((r, text, amt))

    invoices = [(r, t) for r, t, _ in per_row if t]
    if not invoices:
        return [], notes
    line_amounts = [a for _, _, a in per_row if a is not None]

    if len(invoices) == 1:
        r, t = invoices[0]
        amount = payment or (line_amounts[0] if len(line_amounts) == 1 else None)
        return [(r, t, amount)], notes
    return [(r, t, a) for r, t, a in per_row if t], notes


def flatten_reading(ws, reading: SheetReading) -> list[dict]:
    """A verified reading, as the flat rows the rest of the pipeline uses.

    One row per invoice number, each carrying WHERE it was found (sheet,
    row, voucher, date) so a match can be pointed at, not just asserted.
    """
    cols = reading.columns or ColumnRoles()
    rows: list[dict] = []
    for span in sorted(reading.entries, key=lambda s: s.first_row):
        if span.kind != "payment":
            continue
        payment = sum(_nums(ws, cols.payment, span))
        pairs, notes = _pair_invoices(ws, cols, span, payment)
        if not pairs:
            continue  # a payment with no invoice reference: nothing to match on
        dates = _texts(ws, cols.date, span)
        payees = _texts(ws, cols.payee, span)
        vouchers = _texts(ws, cols.voucher_no, span)
        statuses = _texts(ws, cols.status, span)

        raw_date = None
        if cols.date:
            for r in range(span.first_row, span.last_row + 1):
                v = ws[f"{cols.date}{r}"].value
                if v is not None and str(v).strip():
                    raw_date = v
                    break
        # A recorded payment IS paid — matching an uploaded invoice against
        # it must raise ALREADY_PAID, the duplicate-payment guard. A sheet
        # with its own status column says so itself; otherwise only a
        # no-payment row counts as still planned.
        status = statuses[0] if statuses else ("Paid" if payment else "Planned")
        for invoice_row, number, amount in pairs:
            rows.append({
                "sheet": ws.title,
                "row": invoice_row,          # the cell the number sits in
                "entry_row": span.first_row,  # where its payment entry starts
                "no": vouchers[0] if vouchers else "",
                "date": _iso_date(raw_date) if raw_date is not None else (dates[0] if dates else ""),
                "vendor": payees[0] if payees else "",
                "invoice_number": number,
                "amount": amount,
                "status": status,
                "note": "; ".join(notes),
            })
    return rows


# ---- the loop ----------------------------------------------------------------

# Words that mark a header row of a payment sheet. "payment"/"listing"
# alone don't count — a cover page can carry the workbook's title.
_PAYMENT_HEADER_WORDS = ("invoice", "voucher", "payee", "vendor", "cheque",
                        "balance", "receipt")


def _looks_like_payments(grid: str) -> bool:
    lowered = grid.lower()
    return sum(w in lowered for w in _PAYMENT_HEADER_WORDS) >= 2


def _describe_columns(cols: ColumnRoles | None) -> str:
    if cols is None:
        return "no columns"
    return ", ".join(f"{role.replace('_', ' ')}={letter}"
                     for role, letter in cols.model_dump().items() if letter)


def _observation_notes(ws, reading: SheetReading) -> list[tuple[str, str]]:
    """The AI's own remarks about the sheet, as one Activity line."""
    obs = [o.strip()[:200] for o in reading.observations if o.strip()]
    if not obs:
        return []
    return [("INFO", f"{_tab_label(ws)}: AI observations — " + "; ".join(obs) + ".")]


def _stale_notes(ws, ws_formulas) -> list[tuple[str, str]]:
    stale = stale_formula_cells(ws, ws_formulas)
    if not stale:
        return []
    shown = ", ".join(stale[:8]) + (f" … ({len(stale)} in all)" if len(stale) > 8 else "")
    return [("WARNING",
             f"{_tab_label(ws)}: {len(stale)} formula cell(s) have no saved value "
             f"({shown}) — the workbook was saved without recalculating. Those "
             "cells read as empty, so the audit could not use them. Open the "
             "file in Excel, let it recalculate, and save it back.")]


async def read_sheet(ws, ws_formulas=None) -> SheetResult:
    """The reason/act loop for one sheet.

    ws is the sheet opened data_only (numbers where formulas were);
    ws_formulas, when given, is the same sheet opened normally, used only
    to name formula cells whose value was never computed.

    Returns the flat rows (none for a non-payment sheet) and the notes the
    Activity tab shows. Raises ListingUnreadable only when the STRUCTURE
    of the reading never verifies; arithmetic-only leftovers are accepted
    with a warning note.
    """
    grid = grid_text(ws)
    stale_notes = _stale_notes(ws, ws_formulas)
    agent = create_agent("judge", SheetReading, _INSTRUCTIONS, temperature=0)
    feedback = ""
    problems: list[tuple[str, str]] = []
    first_objection = ""  # what round 1 got wrong, for the Activity tab
    challenged = False  # "not a payment sheet" gets one push-back, not more
    for round_no in range(1, MAX_ROUNDS + 1):
        result = await agent.run(grid + feedback, usage_limits=USAGE_LIMITS)
        reading = result.output
        if not reading.is_payment_sheet:
            # Skipping a whole month of payments because the AI shrugged
            # would be silent data loss — when the sheet plainly carries
            # payment-style headers, make it look once more. A second
            # "no" is accepted: code cannot out-judge it, only question it.
            if _looks_like_payments(grid) and not challenged:
                challenged = True
                feedback = (
                    "\n\nYou answered is_payment_sheet=false, but the sheet "
                    "contains payment-style column headers. Look again; if "
                    "you still conclude it holds no payment records, answer "
                    "false again and it will be accepted."
                )
                continue
            if challenged:
                # It looked like payments to code and the AI said no
                # twice. Code cannot out-judge it — but this is exactly
                # the shape of a silently skipped month, so it is a
                # WARNING for the reviewer, not a quiet note.
                return SheetResult(notes=[(
                    "WARNING",
                    f"{_tab_label(ws)}: SKIPPED although it carries payment-style "
                    f"headers — the AI twice judged it holds no payment records "
                    f"({reading.why!r}). If this tab lists payments, no invoice "
                    "was checked against it: open it and confirm.")]
                    + _observation_notes(ws, reading))
            return SheetResult(notes=[(
                "INFO", f"{_tab_label(ws)}: skipped, not a payment sheet — "
                        f"AI: {reading.why!r}.")] + _observation_notes(ws, reading))
        try:
            problems = audit_reading(ws, reading)
        except Exception as exc:
            # A reading that crashes coordinate lookups is just another
            # wrong reading — feed it back, don't escape the loop.
            problems = [(STRUCTURE, f"applying your reading failed: {exc}")]
        structural = [t for k, t in problems if k == STRUCTURE]
        arithmetic = [t for k, t in problems if k == ARITHMETIC]
        if not problems or (round_no == MAX_ROUNDS and not structural):
            rows = flatten_reading(ws, reading)
            n_entries = sum(1 for s in reading.entries if s.kind == "payment")
            notes = [(
                "INFO",
                f"{_tab_label(ws)}: payment sheet — {_describe_columns(reading.columns)}; "
                f"{n_entries} payment entr{'y' if n_entries == 1 else 'ies'} → "
                f"{len(rows)} invoice row(s); confirmed on round {round_no}"
                + (f" (round 1 was corrected: {first_objection})"
                   if round_no > 1 and first_objection else "")
                + ".")]
            if arithmetic:
                notes.append((
                    "WARNING",
                    f"{_tab_label(ws)}: the reading's structure verified but "
                    f"{len(arithmetic)} arithmetic step(s) in the file did not "
                    f"reconcile after {MAX_ROUNDS} rounds; accepted so the "
                    "duplicate-payment check still runs. Look at: "
                    + "; ".join(arithmetic)))
            notes += stale_notes + _observation_notes(ws, reading)
            return SheetResult(rows=rows, notes=notes)
        log.info("sheet %r round %d: %d problem(s): %s",
                 ws.title, round_no, len(problems),
                 "; ".join(t for _, t in problems)[:500])
        if not first_objection:
            first_objection = "; ".join(t for _, t in problems)[:200]
        feedback = (
            "\n\nYour previous reading failed verification against the "
            "sheet's own arithmetic:\n- " + "\n- ".join(t for _, t in problems) +
            "\nLook at the grid again and correct the column roles or spans."
        )
    last = "; ".join(t for _, t in problems)
    log.error("sheet %r: unverifiable after %d rounds; last problems: %s",
              ws.title, MAX_ROUNDS, last)
    raise ListingUnreadable(
        f"Could not confirm a reading of sheet {ws.title} after "
        f"{MAX_ROUNDS} attempts. Last problems: {last}"
    )


async def ingest_workbook(wb, wb_formulas=None) -> SheetResult:
    """Every payment row from every sheet, plus the per-tab notes.

    wb is opened data_only; wb_formulas (optional) is the same file opened
    normally, so uncomputed formula cells can be named per tab.
    """
    if len(wb.worksheets) > MAX_SHEETS:
        raise ListingUnreadable(
            f"The payment listing has {len(wb.worksheets)} tabs — more than "
            f"the {MAX_SHEETS} this reader supports.")
    total = SheetResult()
    for ws in wb.worksheets:
        twin = wb_formulas[ws.title] if wb_formulas is not None else None
        one = await read_sheet(ws, twin)
        total.rows.extend(one.rows)
        total.notes.extend(one.notes)
    if not total.rows:
        raise ListingUnreadable(
            "No sheet in the payment listing produced any payment entries."
        )
    tabs = {r["sheet"] for r in total.rows}
    total.notes.insert(0, (
        "INFO",
        f"Payment listing read: {len(wb.worksheets)} tab(s) examined, "
        f"{len(tabs)} with payments, {len(total.rows)} invoice row(s) in total. "
        "The AI mapped each tab's structure; code extracted and verified the numbers."))
    return total
