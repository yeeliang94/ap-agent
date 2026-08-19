"""Database tables for the claims module.

Five ideas, five tables — kept SEPARATE from the invoice pipeline's tables
on purpose (docs/PLAN.md, Key Decisions): isolation beats reuse here, and
rollback is "drop the claims_* tables".

  ClaimsRun      one batch: a SharePoint folder of employee subfolders
  ClaimEmployee  one employee inside a run (their folder, their file roles,
                 their totals and category, their worker's status)
  ClaimRow       one expense or mileage line read from a report — or, for
                 an employee with no report, one line built from a receipt
  ClaimEvidence  one receipt or one map trip found on a page, with WHERE
                 it is (file, page, left/middle/right)
  ClaimFlag      one thing a person must decide, with its reason, its
                 basis (the rule and where it came from) and its citation

The audit trail (AuditEvent) and the run diary (RunEvent) are shared with
the invoice pipeline: they key on a run id string, and a claims run id is
just another id.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base


def _id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# The statuses a run passes through. map_ready and ready are RESTING
# states (a run waiting for a click survives a restart); the rest are
# in-progress and are failed by the startup reconciliation if the server
# died under them.
STATUSES = ("queued", "surveying", "mapping", "map_ready", "verifying", "ready", "failed")
IN_PROGRESS_STATUSES = ("queued", "surveying", "mapping", "verifying")


class ClaimsRun(Base):
    __tablename__ = "claims_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    client: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="queued")
    error: Mapped[str] = mapped_column(Text, default="")
    # Where the batch came from: a SharePoint folder link, or "" when a zip
    # was uploaded (local development). The listing link likewise.
    folder_url: Mapped[str] = mapped_column(Text, default="")
    listing_url: Mapped[str] = mapped_column(Text, default="")
    # The received date written on every listing row, YYYY-MM-DD.
    received_date: Mapped[str] = mapped_column(String, default="")
    # The optional paragraph of instructions for this client, as typed
    # (prefilled from the playbook; today the AI is shown it at the map step
    # only — hardening H1 passes it to case verification as well).
    instructions: Mapped[str] = mapped_column(Text, default="")
    # Live counters the frontend polls, e.g. {"done": 3, "total": 10}.
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    # What the survey found (folders, files, peeks) — the map AI's input.
    survey: Mapped[dict] = mapped_column(JSON, default=dict)
    # The claim map: proposed by the AI, corrected and confirmed by the
    # reviewer. Warnings from the map audit live beside it.
    map: Mapped[dict] = mapped_column(JSON, default=dict)
    map_warnings: Mapped[list] = mapped_column(JSON, default=list)
    # The header map read from the linked listing workbook (which column
    # is which), so the batch rows come out in the client's own order.
    listing_headers: Mapped[dict] = mapped_column(JSON, default=dict)
    # The copy-ready output, built once review is complete.
    outputs: Mapped[dict] = mapped_column(JSON, default=dict)
    # The client profile + playbook this run was judged under. Frozen at
    # start: a Settings change must never change how an older run is judged.
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    employees: Mapped[list["ClaimEmployee"]] = relationship(back_populates="run")


class ClaimEmployee(Base):
    __tablename__ = "claim_employees"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("claims_runs.id"), index=True)
    folder: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String, default="")
    er_code: Mapped[str] = mapped_column(String, default="")
    # The confirmed roles of this employee's files:
    # {"report_file", "report_tab", "mileage_tab", "receipt_files": [],
    #  "ignored": [], "unplaced": [], "no_report": bool}
    roles: Mapped[dict] = mapped_column(JSON, default=dict)
    # pending / verifying / verified / failed / skipped
    status: Mapped[str] = mapped_column(String, default="pending")
    error: Mapped[str] = mapped_column(Text, default="")
    # The report's own total, as text ("258.70") — Decimal-safe.
    report_total: Mapped[str] = mapped_column(String, default="")
    category: Mapped[str] = mapped_column(String, default="")
    gl: Mapped[str] = mapped_column(String, default="")
    # The header text the AI relied on for the category, quoted.
    category_basis: Mapped[str] = mapped_column(Text, default="")
    # Counts, timing and AI cost for the diary and the summary table:
    # {"rows": n, "flagged": n, "verified": n, "seconds": s, "requests": n}
    summary: Mapped[dict] = mapped_column(JSON, default=dict)

    run: Mapped["ClaimsRun"] = relationship(back_populates="employees")


class ClaimRow(Base):
    __tablename__ = "claim_rows"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(String, index=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("claim_employees.id"), index=True)
    # expense (report tab) / mileage (KM tab) / derived (built from a receipt
    # for an employee with no report)
    kind: Mapped[str] = mapped_column(String, default="expense")
    sheet: Mapped[str] = mapped_column(String, default="")
    row: Mapped[int] = mapped_column(Integer, default=0)
    # The values read: date, item, gl, reason, receipt_included, amount,
    # currency, rate, total — money as text ("45.00"), never floats.
    values: Mapped[dict] = mapped_column(JSON, default=dict)
    # Reviewer corrections: field -> {"from", "to", "reason"}.
    corrections: Mapped[dict] = mapped_column(JSON, default=dict)
    # The evidence this row matched, if any.
    matched_evidence_id: Mapped[str] = mapped_column(String, default="")
    # matched / no_evidence / ambiguous / duplicate / optional / unchecked
    verdict: Mapped[str] = mapped_column(String, default="unchecked")


class ClaimEvidence(Base):
    __tablename__ = "claim_evidence"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(String, index=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("claim_employees.id"), index=True)
    # receipt / map_trip
    kind: Mapped[str] = mapped_column(String, default="receipt")
    file: Mapped[str] = mapped_column(String, default="")
    page: Mapped[int] = mapped_column(Integer, default=0)
    # left / middle / right for a receipt; "" for a map trip
    position: Mapped[str] = mapped_column(String, default="")
    # Receipt: vendor, date, amount, currency. Map trip: date, purpose,
    # from, to, return_trip, km_printed (a number, or "unreadable").
    values: Mapped[dict] = mapped_column(JSON, default=dict)
    # Field -> note, for values the two reads disagreed on or the AI
    # marked hard to read.
    confidence: Mapped[dict] = mapped_column(JSON, default=dict)
    matched_row_id: Mapped[str] = mapped_column(String, default="")


class ClaimFlag(Base):
    __tablename__ = "claim_flags"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(String, index=True)
    # "" for a run-level flag (a control the batch needed and could not find)
    employee_id: Mapped[str] = mapped_column(String, default="", index=True)
    row_id: Mapped[str] = mapped_column(String, default="")
    evidence_id: Mapped[str] = mapped_column(String, default="")
    code: Mapped[str] = mapped_column(String)
    reason: Mapped[str] = mapped_column(Text)
    # The rule the flag rests on and where it came from, e.g.
    # "client profile: car RM 0.64/km".
    basis: Mapped[str] = mapped_column(Text, default="")
    # Where to look: {"file", "page", "position"} or {"sheet", "row"}.
    cite: Mapped[dict] = mapped_column(JSON, default=dict)
    # open / accepted / rejected / resolved_by_correction
    status: Mapped[str] = mapped_column(String, default="open")
    resolution: Mapped[str] = mapped_column(Text, default="")
