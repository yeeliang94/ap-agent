"""The layout contract between the listing reader and the listing writer.

SheetReading (the AI's structural answer) knows the columns and the entry
spans of one tab. To write NEXT month's entries in the client's own layout,
the writer needs a little more, all of it read by code from the tab the AI
mapped: where the title block ends and the headers sit, where the entries
end and the summary block begins, the closing balance, the last voucher
number, and how the client spells each payee. That is a ListingLayout.

It is learned from the LATEST payment tab (the one whose payments carry the
latest date), verified as it is built (a layout missing what the writer
must have raises LayoutIncomplete, and the draft is skipped with that
reason — never guessed), and cached beside the flattened rows so a review
never re-reads the workbook.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal

from .checks import _norm, _vendor_matches
from .listing_agent import ColumnRoles, SheetReading, _num, _text, _texts, content_cells

# What the writer cannot do without. Description, line amount, balance and
# receipt columns are optional: their absence changes what is written, not
# whether anything is.
REQUIRED_FOR_WRITING = ("date", "voucher_no", "invoice_no", "payee", "payment")


class LayoutIncomplete(Exception):
    """The latest tab did not yield everything the writer needs."""


@dataclass
class ListingLayout:
    sheet: str                        # the tab it was learned from
    header_row: int
    columns: ColumnRoles
    title_cells: list[tuple[str, str]]  # (coordinate, text) above the header row
    first_entry_row: int              # first row of the first span
    last_entry_row: int               # last row of the last span before the summary
    summary_first_row: int | None     # first row of the summary block, if any
    closing_balance: Decimal | None   # last balance value before the summary
    last_voucher: str | None          # voucher of the last payment entry
    last_payment_date: date | None    # latest payment date on the tab
    payees: dict[str, str] = field(default_factory=dict)  # normalised -> spelling
    column_widths: dict[str, float] = field(default_factory=dict)

    def payee_spelling(self, vendor: str) -> str | None:
        """The client's own spelling of a payee the listing has paid before,
        matched the way the checks match vendors (one name contains the
        other, after normalising). None if the listing never paid them."""
        hits = {spelling for spelling in self.payees.values()
                if _vendor_matches(vendor, spelling)}
        return sorted(hits)[0] if len(hits) == 1 else None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["columns"] = self.columns.model_dump()
        d["closing_balance"] = None if self.closing_balance is None else str(self.closing_balance)
        d["last_payment_date"] = None if self.last_payment_date is None else self.last_payment_date.isoformat()
        d["title_cells"] = [list(t) for t in self.title_cells]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ListingLayout":
        return cls(
            sheet=d["sheet"], header_row=d["header_row"],
            columns=ColumnRoles(**d["columns"]),
            title_cells=[tuple(t) for t in d["title_cells"]],
            first_entry_row=d["first_entry_row"], last_entry_row=d["last_entry_row"],
            summary_first_row=d.get("summary_first_row"),
            closing_balance=None if d.get("closing_balance") is None else Decimal(d["closing_balance"]),
            last_voucher=d.get("last_voucher"),
            last_payment_date=None if d.get("last_payment_date") is None
            else date.fromisoformat(d["last_payment_date"]),
            payees=dict(d.get("payees", {})),
            column_widths=dict(d.get("column_widths", {})),
        )


# Text dates a client sheet may hold. Day-first where ambiguous (23/07/2026):
# the client is Malaysian, and 07/23/2026 would fail to parse rather than
# be misread. Real Excel date cells arrive as datetime and skip all this.
_DATE_TEXT_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y",
                      "%d.%m.%Y", "%d.%m.%y", "%d %b %Y", "%d-%b-%Y", "%d-%b-%y",
                      "%d %B %Y", "%Y/%m/%d", "%Y.%m.%d", "%d %b %y")


def _as_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in _DATE_TEXT_FORMATS:
        try:
            return datetime.strptime(text[:19] if "T" in text else text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()  # '2026-07-23 00:00:00' and friends
    except ValueError:
        return None


def learn_layout(ws, reading: SheetReading) -> ListingLayout:
    """Fill the layout contract from a VERIFIED reading of one tab.

    Raises LayoutIncomplete naming what is missing, so the draft can say
    why it was not written.
    """
    cols = reading.columns or ColumnRoles()
    missing = [role for role in REQUIRED_FOR_WRITING if getattr(cols, role) is None]
    if missing:
        raise LayoutIncomplete(
            f"tab {ws.title}: the reading names no {', '.join(missing)} column — "
            "the writer needs those to place new entries")
    if reading.header_row is None:
        raise LayoutIncomplete(
            f"tab {ws.title}: the reading did not identify the header row")
    spans = sorted(reading.entries, key=lambda s: s.first_row)
    if not spans:
        raise LayoutIncomplete(f"tab {ws.title}: the reading has no entries")
    payments = [s for s in spans if s.kind == "payment"]
    if not payments:
        raise LayoutIncomplete(f"tab {ws.title}: the reading has no payment entries")

    body = [s for s in spans
            if reading.summary_first_row is None or s.last_row < reading.summary_first_row]
    last_entry_row = body[-1].last_row if body else payments[-1].last_row

    closing: Decimal | None = None
    if cols.balance:
        for row in range(last_entry_row, spans[0].first_row - 1, -1):
            n = _num(ws[f"{cols.balance}{row}"].value)
            if n is not None:
                # via repr(float): 7.9 -> "7.9" -> 7.90, no binary noise
                closing = Decimal(repr(n)).quantize(Decimal("0.01"))
                break

    last_voucher = None
    for span in reversed(payments):
        vouchers = _texts(ws, cols.voucher_no, span)
        if vouchers:
            last_voucher = vouchers[0]
            break

    last_date: date | None = None
    unreadable_dates: list[str] = []
    payees: dict[str, str] = {}
    for span in payments:
        for row in range(span.first_row, span.last_row + 1):
            raw = _text(ws, cols.date, row)
            if not raw:
                continue
            d = _as_date(ws[f"{cols.date}{row}"].value)
            if d is None:
                unreadable_dates.append(raw)
            elif last_date is None or d > last_date:
                last_date = d
        for spelling in _texts(ws, cols.payee, span):
            payees.setdefault(_norm(spelling), spelling)
    if last_date is None and unreadable_dates:
        # Dated entries whose dates cannot be read: the draft month and the
        # voucher month code would silently come out wrong. Refuse.
        shown = ", ".join(repr(t) for t in unreadable_dates[:3])
        raise LayoutIncomplete(
            f"tab {ws.title}: the payment dates could not be read ({shown}) — "
            "the draft's month and voucher numbers depend on them")

    title_cells = [(cell.coordinate, str(cell.value).strip())
                   for (r, _c), cell in sorted(content_cells(ws).items())
                   if r < reading.header_row]

    widths = {letter: dim.width for letter, dim in ws.column_dimensions.items()
              if dim.width}

    return ListingLayout(
        sheet=ws.title, header_row=reading.header_row, columns=cols,
        title_cells=title_cells, first_entry_row=spans[0].first_row,
        last_entry_row=last_entry_row, summary_first_row=reading.summary_first_row,
        closing_balance=closing, last_voucher=last_voucher,
        last_payment_date=last_date, payees=payees, column_widths=widths,
    )
