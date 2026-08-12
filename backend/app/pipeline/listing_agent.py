"""AI-assisted reading of a real client payment listing.

Real listings (see docs: the ICMR file) look nothing like the canonical
sample: title rows above the headers, one tab per month, one payment entry
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
    again — the reason/act loop — up to MAX_ROUNDS per sheet. A reading
    the file's own numbers refuse to confirm is never accepted.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from ..model_layer import USAGE_LIMITS, create_agent

log = logging.getLogger("listing_agent")

MAX_ROUNDS = 3

# The most rows one sheet may hold. Beyond this we FAIL, loudly — a grid
# quietly cut short would let a partial reading pass its own audit.
MAX_SHEET_ROWS = 300

# Reconciliation tolerance: real sheets round at 2dp, so half a cent of
# accumulated drift per comparison is noise, anything more is a misread.
TOLERANCE = 0.02


class ListingUnreadable(Exception):
    """The AI's reading of a sheet could not be confirmed by the sheet's
    own arithmetic after MAX_ROUNDS attempts. Failing the run beats
    checking invoices against numbers nothing corroborates."""


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


_INSTRUCTIONS = (
    "You are reading one sheet of an accounts-payable payment listing "
    "workbook. The sheet is given as a grid of cells labelled like 'D13: "
    "some value'. Do NOT copy any numbers into your answer — answer only "
    "with structure: column letters for each role, and row spans.\n"
    "- A single payment entry often spans SEVERAL consecutive rows: the "
    "payee and voucher number on the first row, extra invoice numbers, "
    "description lines and per-line amounts on the rows below it. The span "
    "covers ALL of those rows.\n"
    "- Mark balance b/f, fund received, bank charges and similar "
    "non-payment money movements as kind='other' spans, so every movement "
    "of the running balance is accounted for.\n"
    "- Title rows, header rows, blank rows and summary blocks (opening "
    "balance, net payment, total fund to request) belong to NO span.\n"
    "- If the sheet holds no payment records at all, answer "
    "is_payment_sheet=false and nothing else."
)


# ---- pulling values out of the cells the AI pointed at ---------------------

_CURRENCY_PREFIX = re.compile(r"^\s*(RM|MYR)\s*", re.IGNORECASE)


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


def _texts(ws, col: str | None, span: EntrySpan) -> list[str]:
    """Non-empty cell texts in one column across a span, top to bottom."""
    if not col:
        return []
    out = []
    for row in range(span.first_row, span.last_row + 1):
        v = ws[f"{col}{row}"].value
        if v is not None and str(v).strip():
            out.append(str(v).strip())
    return out


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


def grid_text(ws) -> str:
    """The sheet as the AI sees it: every non-empty cell, labelled.

    Refuses over-long sheets rather than truncating: rows the AI never saw
    are rows the audit can never question.
    """
    if (ws.max_row or 1) > MAX_SHEET_ROWS:
        raise ListingUnreadable(
            f"Sheet {ws.title!r} has {ws.max_row} rows — more than the "
            f"{MAX_SHEET_ROWS} this reader supports. Split the sheet, or "
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


def verify_reading(ws, reading: SheetReading) -> list[str]:
    """Audit a reading against the sheet's own arithmetic.

    Returns problems in plain sentences (they are fed back to the AI).
    Empty list = the file's numbers corroborate the reading.

    The audit assumes the AI is wrong until the sheet proves otherwise:
    - required roles: a payment sheet must name payment, invoice and payee
      columns — without them the flattened rows would silently lose the
      duplicate-payment guard (amount=None, status "Planned")
    - coverage: EVERY row holding a number in the payment, receipt or
      balance columns must lie inside some span, so the AI cannot pass by
      showing only a subset of the sheet
    - identity: one payment entry has exactly one payment total, one payee
      and at most one voucher — merged neighbours pass pure arithmetic
      (sums are additive) but would mis-attribute invoices to the first
      payee, so identity is what actually separates entries
    - the running balance walk, which anchors every covered movement
    """
    problems: list[str] = []
    cols = reading.columns or ColumnRoles()
    spans = sorted(reading.entries, key=lambda s: s.first_row)

    if reading.is_payment_sheet:
        for role in ("payment", "invoice_no", "payee"):
            if getattr(cols, role) is None:
                problems.append(
                    f"a payment sheet needs its {role} column identified — "
                    "if the sheet truly has none, it is not a payment sheet")

    for a, b in zip(spans, spans[1:]):
        if a.last_row >= b.first_row:
            problems.append(
                f"spans rows {a.first_row}-{a.last_row} and "
                f"{b.first_row}-{b.last_row} overlap — each row belongs to at "
                "most one span")

    # Coverage: money that moved but sits outside every span means the
    # reading skipped part of the sheet.
    covered = set()
    for span in spans:
        covered.update(range(span.first_row, span.last_row + 1))
    for col in {cols.payment, cols.receipt, cols.balance} - {None}:
        for row in range(1, (ws.max_row or 1) + 1):
            if row in covered:
                continue
            if _num(ws[f"{col}{row}"].value) is not None:
                problems.append(
                    f"cell {col}{row} holds a number in a money column but "
                    "belongs to no span — every money movement (including "
                    "balance b/f, fund received, totals rows) must be inside "
                    "a span, kind='other' if it is not a payment")

    prev_balance: float | None = None
    pending = 0.0  # net movement since the last span that showed a balance
    for span in spans:
        if span.first_row > span.last_row:
            problems.append(f"span rows {span.first_row}-{span.last_row} is inverted")
            continue
        payments = _nums(ws, cols.payment, span)
        payment = sum(payments)
        receipt = sum(_nums(ws, cols.receipt, span))
        lines = _nums(ws, cols.line_amount, span)

        if span.kind == "payment":
            # Identity: one entry, one total, one payee, one voucher.
            if len(payments) != 1:
                problems.append(
                    f"rows {span.first_row}-{span.last_row}: a payment span "
                    f"must hold exactly one payment total, found "
                    f"{len(payments)} — split merged entries apart")
            payees = set(_texts(ws, cols.payee, span))
            if len(payees) > 1:
                problems.append(
                    f"rows {span.first_row}-{span.last_row}: {len(payees)} "
                    "different payees in one span — one span is one payee's "
                    "payment, split it")
            vouchers = set(_texts(ws, cols.voucher_no, span))
            if len(vouchers) > 1:
                problems.append(
                    f"rows {span.first_row}-{span.last_row}: {len(vouchers)} "
                    "voucher numbers in one span — split it")

        # Inside one entry, the per-line amounts must add up to its payment.
        if payment and lines and abs(sum(lines) - payment) > TOLERANCE:
            problems.append(
                f"rows {span.first_row}-{span.last_row}: the line amounts sum "
                f"to {sum(lines):.2f} but the payment column says {payment:.2f} "
                "— the span may be cut wrong or a column misassigned")

        # The running balance must move by exactly receipts - payments.
        balances = _nums(ws, cols.balance, span)
        movement = receipt - payment
        if balances:
            if prev_balance is not None:
                expected = prev_balance + pending + movement
                if abs(expected - balances[-1]) > TOLERANCE:
                    problems.append(
                        f"rows {span.first_row}-{span.last_row}: running "
                        f"balance should be {expected:.2f} but the balance "
                        f"column shows {balances[-1]:.2f}")
            prev_balance, pending = balances[-1], 0.0
        else:
            pending += movement

    if reading.is_payment_sheet and not any(s.kind == "payment" for s in spans):
        problems.append("the sheet was called a payment sheet but no span has "
                        "kind='payment'")
    return problems


def flatten_reading(ws, reading: SheetReading) -> list[dict]:
    """A verified reading, as the flat rows the rest of the pipeline uses.

    Grouped entries flatten to one row per invoice number. When an entry's
    invoice numbers and line amounts pair up one-to-one they are zipped;
    a lone invoice takes the entry's payment total; anything else keeps
    amount=None — an honest "this file doesn't say", which downstream
    checks treat as "cannot compare", never as zero.
    """
    cols = reading.columns or ColumnRoles()
    rows: list[dict] = []
    for span in sorted(reading.entries, key=lambda s: s.first_row):
        if span.kind != "payment":
            continue
        invoices = _texts(ws, cols.invoice_no, span)
        if not invoices:
            continue  # a payment with no invoice reference: nothing to match on
        lines = _nums(ws, cols.line_amount, span)
        payment = sum(_nums(ws, cols.payment, span))
        dates = _texts(ws, cols.date, span)
        payees = _texts(ws, cols.payee, span)
        vouchers = _texts(ws, cols.voucher_no, span)

        if len(invoices) == 1:
            amounts: list[float | None] = [payment or (lines[0] if len(lines) == 1 else None)]
        elif len(lines) == len(invoices):
            amounts = list(lines)
        else:
            amounts = [None] * len(invoices)

        raw_date = None
        if cols.date:
            for r in range(span.first_row, span.last_row + 1):
                v = ws[f"{cols.date}{r}"].value
                if v is not None and str(v).strip():
                    raw_date = v
                    break
        for number, amount in zip(invoices, amounts):
            rows.append({
                "no": vouchers[0] if vouchers else "",
                "date": _iso_date(raw_date) if raw_date is not None else (dates[0] if dates else ""),
                "vendor": payees[0] if payees else "",
                "invoice_number": number,
                "amount": amount,
                # A recorded payment IS paid — matching an uploaded invoice
                # against it must raise ALREADY_PAID, the duplicate-payment
                # guard. Only a no-payment row counts as still planned.
                "status": "Paid" if payment else "Planned",
            })
    return rows


# Words that mark a header row of a payment sheet. "payment"/"listing"
# alone don't count — a cover page can carry the workbook's title.
_PAYMENT_HEADER_WORDS = ("invoice", "voucher", "payee", "cheque",
                        "balance", "receipt")


def _looks_like_payments(grid: str) -> bool:
    lowered = grid.lower()
    return sum(w in lowered for w in _PAYMENT_HEADER_WORDS) >= 2


async def read_sheet(ws) -> list[dict]:
    """The reason/act loop for one sheet. Returns flat rows ([] for
    non-payment sheets); raises ListingUnreadable when no reading survives
    the arithmetic audit."""
    grid = grid_text(ws)
    agent = create_agent("judge", SheetReading, _INSTRUCTIONS, temperature=0)
    feedback = ""
    problems: list[str] = []
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
            return []
        try:
            problems = verify_reading(ws, reading)
            if not problems:
                return flatten_reading(ws, reading)
        except Exception as exc:
            # A reading that crashes coordinate lookups is just another
            # wrong reading — feed it back, don't escape the loop.
            problems = [f"applying your reading failed: {exc}"]
        log.info("sheet %r round %d: %d problem(s): %s",
                 ws.title, round_no, len(problems), "; ".join(problems)[:500])
        feedback = (
            "\n\nYour previous reading failed verification against the "
            "sheet's own arithmetic:\n- " + "\n- ".join(problems) +
            "\nLook at the grid again and correct the column roles or spans."
        )
    log.error("sheet %r: unverifiable after %d rounds; last problems: %s",
              ws.title, MAX_ROUNDS, "; ".join(problems))
    raise ListingUnreadable(
        f"Could not confirm a reading of sheet {ws.title!r} after "
        f"{MAX_ROUNDS} attempts. Last problems: {'; '.join(problems)}"
    )


async def ingest_workbook(wb) -> list[dict]:
    """Every payment row from every sheet of a human-shaped listing."""
    rows: list[dict] = []
    for ws in wb.worksheets:
        rows.extend(await read_sheet(ws))
    if not rows:
        raise ListingUnreadable(
            "No sheet in the payment listing produced any payment entries."
        )
    return rows
