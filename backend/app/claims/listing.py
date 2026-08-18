"""The month's listing ("Summary of Invoices"): its columns, and the batch rows.

Columns come from the client's own listing (PRD decision 2): the workbook
linked at run start is opened, the AI maps its header row (which column is
Received Date, Category, GL Account, Name of Vendor, Invoice Number,
amount, …) — the same way the invoice pipeline maps a template's headers
— and code emits one row per employee in that order. A header the app has
no value for is left blank for a clean paste. If the header row cannot be
mapped, the output falls back to a fixed minimal set and says so.

The listing's past tabs also give the category judge its precedent: rows
whose invoice number is an ER(...) code, as "Name — Category (GL) — MYR".

Totals are recomputed independently from the emitted text with Decimal
and reconciled against the employees' report totals.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal
from pathlib import Path
from typing import Literal

from openpyxl import load_workbook
from pydantic import BaseModel, Field, model_validator

from .. import telemetry
from ..docsource import SourceUnavailable, get_source
from ..model_layer import USAGE_LIMITS, create_agent
from .evidence import ai_call
from .models import ClaimEmployee, ClaimFlag, ClaimRow, ClaimsRun

log = logging.getLogger("claims.listing")

ROLES = ("serial", "processed_by", "received_date", "p2p_ref", "po_number", "cost_center",
         "category", "gl_account", "vendor_name", "invoice_number", "amount", "remarks")

# The minimal set when the header row cannot be read.
FALLBACK_HEADER = ["Name", "ER code", "Category", "GL", "Amount (MYR)"]
FALLBACK_ROLES = ["vendor_name", "invoice_number", "category", "gl_account", "amount"]

_ER = re.compile(r"ER\s*\([^)]*\)", re.IGNORECASE)


class HeaderMap(BaseModel):
    """Which header (by its 0-based index in the header row) plays which role."""

    @model_validator(mode="after")
    def _distinct(self):
        seen: dict[int, str] = {}
        for role, idx in self.model_dump().items():
            if role in ("header_row", "why"):
                continue
            if isinstance(idx, int):
                if idx in seen:
                    raise ValueError(f"header {idx} given two roles ({seen[idx]} and {role})")
                seen[idx] = role
        return self

    header_row: int = Field(ge=1, description="the 1-based row holding the column headings")
    serial: int | None = Field(default=None, ge=0, description="S/N running number")
    processed_by: int | None = Field(default=None, ge=0)
    received_date: int | None = Field(default=None, ge=0)
    p2p_ref: int | None = Field(default=None, ge=0, description="helpline / P2P reference")
    po_number: int | None = Field(default=None, ge=0)
    cost_center: int | None = Field(default=None, ge=0)
    category: int | None = Field(default=None, ge=0)
    gl_account: int | None = Field(default=None, ge=0)
    vendor_name: int | None = Field(default=None, ge=0, description="name of vendor / payee (an employee, for claims)")
    invoice_number: int | None = Field(default=None, ge=0, description="invoice number (the ER(...) code, for claims)")
    amount: int | None = Field(default=None, ge=0, description="the amount column")
    remarks: int | None = Field(default=None, ge=0)
    why: str = Field(max_length=200)


_INSTRUCTIONS = (
    "You are shown the first rows of one tab of an accounts-payable 'Summary "
    "of Invoices' listing as a grid ('C3: Received Date'). Say which row holds "
    "the column headings and, for each role, the 0-based position of its "
    "heading in that row (column A = 0). Roles: serial (S/N), processed_by, "
    "received_date, p2p_ref, po_number, cost_center, category, gl_account, "
    "vendor_name (name of vendor / payee), invoice_number, amount, remarks. "
    "Leave a role null when no heading plays it. Answer with positions only."
)


def listing_path(run: ClaimsRun) -> Path | None:
    """The run's private copy of the listing, fetching it once if a link
    was given. None when there is neither a file nor a link."""
    from .runner import workspace_for

    ws = workspace_for(run.id)
    local = ws / "listing.xlsx"
    if local.is_file():
        return local
    if not run.listing_url:
        return None
    url = run.listing_url
    if Path(url).expanduser().is_file():
        local.write_bytes(Path(url).expanduser().read_bytes())
        return local
    # A SharePoint link to the file: its folder is the parent, its name the
    # last segment.
    from urllib.parse import unquote, urlsplit

    parts = urlsplit(url)
    path = unquote(parts.path)
    folder_url, _, name = url.rpartition("/")
    if not name:
        raise SourceUnavailable("The listing link does not end in a file name.")
    source = get_source(folder_url)
    entries = source.list_folder(folder_url, "")
    entry = next((e for e in entries if e["name"] == unquote(name)), None)
    if entry is None:
        raise SourceUnavailable(f"{unquote(name)!r} is not in the linked folder ({path}).")
    local.write_bytes(source.download(folder_url, entry))
    return local


async def prepare_listing(db, run: ClaimsRun) -> None:
    """Read the linked listing once: header map + past examples. Stores the
    result on run.listing_headers with a state: ok / fallback / missing /
    unreadable. Never raises — a run without a listing still verifies; the
    output then uses the fallback set and a run-level flag says so."""
    try:
        path = listing_path(run)
    except SourceUnavailable as exc:
        run.listing_headers = {"state": "unreadable", "why": str(exc)}
        db.commit()
        telemetry.record(db, run.id, "output", telemetry.WARNING, "LISTING_UNREADABLE",
                         f"The linked listing could not be fetched: {exc}")
        return
    if path is None:
        run.listing_headers = {"state": "missing", "why": "no listing link or file was given"}
        db.commit()
        telemetry.record(db, run.id, "output", telemetry.WARNING, "LISTING_MISSING",
                         "No listing was linked to this run — the output will use the fallback columns.")
        return
    try:
        result = await read_listing(path)
    except Exception as exc:
        reason = telemetry.record_failure(db, run.id, "output", "LISTING_UNREADABLE",
                                          "Could not read the listing workbook", exc)
        run.listing_headers = {"state": "unreadable", "why": reason}
        db.commit()
        return
    run.listing_headers = result
    db.commit()
    telemetry.record(db, run.id, "output", telemetry.INFO, "LISTING_READ",
                     (f"Listing header read from tab {result['tab']!r}: {len(result['header'])} "
                      f"columns, roles for {sum(1 for r in result['roles'].values() if r is not None)}; "
                      f"{len(result['past_examples'])} past employee row(s) found as precedent.")
                     if result["state"] == "ok" else
                     f"Listing header could not be mapped ({result.get('why')}); the fallback "
                     "columns will be used and the output says so.")


async def read_listing(path: Path, usage=None) -> dict:
    """AI maps the header row of the CURRENT tab (the last one, or the one
    with the fewest rows — the month being filled); code reads the past
    ER rows from every tab."""
    wb = load_workbook(path, data_only=True, read_only=True)
    try:
        sheets = wb.worksheets
        if not sheets:
            return {"state": "unreadable", "why": "the workbook has no tabs"}
        current = sheets[-1]
        grid = _grid(current)
        agent = create_agent("judge", HeaderMap, _INSTRUCTIONS, temperature=0)
        result = await ai_call(agent.run(grid, usage_limits=USAGE_LIMITS), "the listing header reader")
        if usage is not None:
            usage.add(result)
        hm = result.output
        header = _row_values(current, hm.header_row)
        roles = {r: getattr(hm, r) for r in ROLES}
        # Code checks the answer: positions inside the row, and the roles
        # the output cannot do without (a name, a number, an amount).
        for role, idx in roles.items():
            if idx is not None and (idx >= len(header) or not header[idx]):
                roles[role] = None
        must = ("vendor_name", "amount")
        if any(roles[r] is None for r in must) or not any(header):
            return {"state": "fallback", "tab": current.title, "header": FALLBACK_HEADER,
                    "roles": dict(zip(FALLBACK_ROLES, range(len(FALLBACK_ROLES)))),
                    "why": f"the header row of tab {current.title!r} did not yield a vendor and an "
                           f"amount column (AI: {hm.why})",
                    "past_examples": _past_examples(sheets, hm, header)}
        return {"state": "ok", "tab": current.title, "header_row": hm.header_row, "header": header,
                "roles": roles, "why": hm.why, "past_examples": _past_examples(sheets, hm, header)}
    finally:
        wb.close()


def _row_values(ws, row: int) -> list[str]:
    for r in ws.iter_rows(min_row=row, max_row=row, values_only=True):
        vals = ["" if v is None else str(v).strip() for v in r]
        while vals and not vals[-1]:
            vals.pop()
        return vals
    return []


def _grid(ws, max_rows: int = 12) -> str:
    from openpyxl.utils import get_column_letter

    lines = [f"Sheet name: {ws.title!r}"]
    for r, row in enumerate(ws.iter_rows(min_row=1, max_row=max_rows, values_only=True), 1):
        cells = [f"{get_column_letter(c)}{r}: {str(v).strip()[:60]}"
                 for c, v in enumerate(row, 1) if v is not None and str(v).strip()]
        if cells:
            lines.append(" | ".join(cells))
    return "\n".join(lines)


def _past_examples(sheets, hm: HeaderMap, header: list[str]) -> list[str]:
    """'Nick Goh — Taxi (713070) — MYR 240.00' for every past row whose
    invoice number is an ER(...) code. Assumes the past tabs share the
    current tab's layout (they are the same file)."""
    out = []
    if hm.invoice_number is None or hm.vendor_name is None:
        return out
    for ws in sheets:
        for row in ws.iter_rows(min_row=hm.header_row + 1, max_row=hm.header_row + 400, values_only=True):
            vals = ["" if v is None else str(v).strip() for v in row]
            inv = vals[hm.invoice_number] if hm.invoice_number < len(vals) else ""
            if not _ER.search(inv):
                continue
            name = vals[hm.vendor_name] if hm.vendor_name < len(vals) else ""
            cat = vals[hm.category] if hm.category is not None and hm.category < len(vals) else ""
            gl = vals[hm.gl_account] if hm.gl_account is not None and hm.gl_account < len(vals) else ""
            amt = vals[hm.amount] if hm.amount is not None and hm.amount < len(vals) else ""
            out.append(f"{name} — {cat}" + (f" ({gl})" if gl else "") + (f" — MYR {amt}" if amt else "")
                       + f" [tab {ws.title}]")
    return out[:40]


# ---- the batch rows -----------------------------------------------------------------

def _cell(value) -> str:
    """One TSV cell, made safe: no tabs/newlines, no leading formula characters."""
    s = re.sub(r"[\x00-\x1f]", " ", str(value if value is not None else "")).strip()
    if s[:1] in ("=", "+", "-", "@"):
        s = "'" + s
    return s


def build_outputs(db, run: ClaimsRun) -> dict:
    """One row per included employee, in the client's own column order.

    Included = verified employees not excluded (an accepted flag excludes
    its ROW; the employee stays with the remaining rows). Excluded rows are
    subtracted from the employee's total and named. Employees marked failed
    or skipped are listed under not_included. Totals are recomputed from
    the emitted text with Decimal and reconciled with the report totals.
    """
    lh = run.listing_headers or {}
    if lh.get("state") == "ok":
        header, roles = list(lh["header"]), dict(lh["roles"])
        fallback, note = False, ""
    else:
        header, roles = list(FALLBACK_HEADER), dict(zip(FALLBACK_ROLES, range(len(FALLBACK_ROLES))))
        fallback = True
        note = ("The listing's header row could not be read"
                + (f" ({lh.get('why')})" if lh.get("why") else "")
                + ", so these rows use the fallback columns; paste them by hand into the right places.")
    employees = db.query(ClaimEmployee).filter(ClaimEmployee.run_id == run.id).all()
    rows_by_emp: dict[str, list[ClaimRow]] = {}
    for r in db.query(ClaimRow).filter(ClaimRow.run_id == run.id).all():
        rows_by_emp.setdefault(r.employee_id, []).append(r)
    excluded_rows = {f.row_id for f in db.query(ClaimFlag).filter(
        ClaimFlag.run_id == run.id, ClaimFlag.status == "accepted") if f.row_id}

    out_rows: list[list[str]] = []
    included, not_included, exclusions = [], [], []
    source_total = Decimal("0")
    for n, e in enumerate(sorted(employees, key=lambda x: x.name or x.folder), 1):
        if e.status != "verified":
            not_included.append({"name": e.name or e.folder,
                                 "why": {"failed": f"verification failed: {e.error}",
                                         "skipped": "skipped by the reviewer at the map",
                                         "pending": "not verified yet", "verifying": "still verifying"}.get(e.status, e.status)})
            continue
        e_rows = [r for r in rows_by_emp.get(e.id, []) if r.kind != "mileage"]
        kept = [r for r in e_rows if r.id not in excluded_rows]
        gone = [r for r in e_rows if r.id in excluded_rows]
        if e_rows and not kept:
            not_included.append({"name": e.name or e.folder, "why": "every row was excluded in review"})
            continue
        amount = sum((_money(r) for r in kept), Decimal("0")).quantize(Decimal("0.01"))
        if not e_rows:
            amount = Decimal(e.report_total or "0").quantize(Decimal("0.01"))
        for r in gone:
            exclusions.append({"name": e.name or e.folder, "row": r.row, "amount": str(_money(r)),
                               "why": "flag accepted in review"})
        values = {"serial": str(n), "processed_by": "", "received_date": run.received_date,
                  "p2p_ref": "", "po_number": "", "cost_center": "", "category": e.category,
                  "gl_account": e.gl, "vendor_name": e.name, "invoice_number": e.er_code,
                  "amount": f"{amount:.2f}", "remarks": "employee claim" + (f" ({len(gone)} row(s) excluded)" if gone else "")}
        line = [""] * len(header)
        for role, idx in roles.items():
            if idx is not None and idx < len(header):
                line[idx] = _cell(values.get(role, ""))
        out_rows.append(line)
        included.append({"name": e.name, "er_code": e.er_code, "amount": f"{amount:.2f}",
                         "category": e.category, "gl": e.gl})
        source_total += amount
    amount_idx = roles.get("amount")
    emitted_total = sum((Decimal(r[amount_idx].lstrip("'") or "0") for r in out_rows), Decimal("0")) \
        if amount_idx is not None else Decimal("0")
    tsv = "\n".join("\t".join(_cell(h) for h in header) + "" for _ in [0]) + "\n" + \
        "\n".join("\t".join(r) for r in out_rows)
    return {"header": header, "rows": out_rows, "tsv": tsv,
            "totals": {"total_myr": f"{emitted_total:.2f}", "source_total": f"{source_total:.2f}",
                       "match": emitted_total == source_total,
                       "difference": f"{(emitted_total - source_total):.2f}"},
            "included": included, "not_included": not_included, "exclusions": exclusions,
            "header_fallback": fallback, "header_note": note, "received_date": run.received_date}


def _money(r: ClaimRow) -> Decimal:
    v = r.values or {}
    text = v.get("total") or v.get("amount") or "0"
    try:
        return Decimal(str(text))
    except Exception:
        return Decimal("0")
