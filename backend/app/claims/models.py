"""Database tables for the claims module.

Kept SEPARATE from the invoice pipeline's tables on purpose (docs/PLAN.md,
Key Decisions): isolation beats reuse here, and rollback is "drop the
claims_* tables".

  ClaimsRun      one batch: a folder of claim files (structured or a dump)
  ClaimEmployee  one employee inside a run (their folder, their file roles,
                 their totals and category, their worker's status) — the
                 delivered unit of verification, kept during the case
                 compatibility period (hardening H2)
  ClaimCase      one Claim Case: a proposed payment-listing decision whose
                 Claimant may be proposed or unknown; mirrors a
                 ClaimEmployee 1:1 while both exist (legacy_employee_id)
  ClaimRow       one expense or mileage line read from a report — or, for
                 a case with no report, one line built from a receipt
  ClaimEvidence  one receipt or one map trip found on a page, with WHERE
                 it is (file, page, left/middle/right, and an approximate box)
  ClaimFlag      one thing a person must decide, with its reason, its
                 basis (the rule and where it came from) and its citation
  ClaimSourceArtifact     one submitted file with its hash, proposed role and
                          disposition (used/duplicate/irrelevant/unreadable/
                          unresolved) — nothing uploaded vanishes silently
  ClaimEvidenceAssignment one proposed/confirmed/rejected relationship from
                          evidence (or a whole file) to a case and a line
  ClaimInvestigation      the run-local Investigation Plan and summary
  ClaimToolExecution      one tool call of the investigation, for replay
  ClaimsSchema            the applied claims schema versions (migrations.py)

Rows, evidence and flags carry BOTH employee_id and case_id during the
compatibility period (additive migration; both written, either read).

The audit trail (AuditEvent) and the run diary (RunEvent) are shared with
the invoice pipeline: they key on a run id string, and a claims run id is
just another id.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text
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
    # The immutable manifest: every file of the snapshot with its hash
    # (claims/manifest.py). Empty for runs made before H1.
    manifest: Mapped[list] = mapped_column(JSON, default=list)
    # Bumped on every reviewer mutation; routes that change the run take
    # the revision the screen last saw and refuse a stale one (H6/H9).
    revision: Mapped[int] = mapped_column(Integer, default=0)
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
    # Additive, reviewer-facing live progress. Written from a separate short
    # session so a heartbeat never commits this worker's staged rows/evidence.
    progress: Mapped[dict] = mapped_column(JSON, default=dict)

    run: Mapped["ClaimsRun"] = relationship(back_populates="employees")


class ClaimRow(Base):
    __tablename__ = "claim_rows"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(String, index=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("claim_employees.id"), index=True)
    # The Claim Case this line belongs to (H2; "" on rows made before it).
    case_id: Mapped[str] = mapped_column(String, default="", index=True)
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
    case_id: Mapped[str] = mapped_column(String, default="", index=True)
    # receipt / map_trip
    kind: Mapped[str] = mapped_column(String, default="receipt")
    file: Mapped[str] = mapped_column(String, default="")
    page: Mapped[int] = mapped_column(Integer, default=0)
    # left / middle / right for a receipt; "" for a map trip
    position: Mapped[str] = mapped_column(String, default="")
    # The AI's approximate outline of a receipt: [left, top, right, bottom]
    # in percent of the page, drawn on the preview. None when the read
    # did not give one (older runs, map trips).
    box: Mapped[list | None] = mapped_column(JSON, default=None, nullable=True)
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
    case_id: Mapped[str] = mapped_column(String, default="", index=True)
    # A flag about a whole file (ARTIFACT_UNRESOLVED …): the artifact's manifest id.
    artifact_id: Mapped[str] = mapped_column(String, default="")
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


# ---- hardening H2: the case model (additive) ------------------------------------

class ClaimCase(Base):
    __tablename__ = "claim_cases"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(String, index=True)
    # The ClaimEmployee this case mirrors during the compatibility period
    # ("" for a case that has no employee record yet).
    legacy_employee_id: Mapped[str] = mapped_column(String, default="", index=True)
    # The grouping label on screen: the folder for a structured batch, a
    # proposed name or "Case 3" for a dump.
    label: Mapped[str] = mapped_column(String, default="")
    claimant_name: Mapped[str] = mapped_column(String, default="")
    claimant_identifier: Mapped[str] = mapped_column(String, default="")
    # confirmed / proposed / unknown
    claimant_state: Mapped[str] = mapped_column(String, default="unknown")
    claimant_basis: Mapped[str] = mapped_column(Text, default="")
    claimant_citations: Mapped[list] = mapped_column(JSON, default=list)
    # proposed / confirmed / blocked / excluded
    state: Mapped[str] = mapped_column(String, default="proposed")
    grouping_basis: Mapped[str] = mapped_column(Text, default="")
    citations: Mapped[list] = mapped_column(JSON, default=list)
    artifact_ids: Mapped[list] = mapped_column(JSON, default=list)
    # The worker's file roles (report_file, report_tab, mileage_tab,
    # receipt_files, ignored, unplaced, no_report) — see ClaimEmployee.roles.
    roles: Mapped[dict] = mapped_column(JSON, default=dict)
    # Worker status, mirrored: pending / verifying / verified / failed / skipped
    status: Mapped[str] = mapped_column(String, default="pending")
    error: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String, default="")
    gl: Mapped[str] = mapped_column(String, default="")
    category_basis: Mapped[str] = mapped_column(Text, default="")
    # Reported Total: the figure the source states, as text; "" when absent.
    # NEVER filled from the lines.
    reported_total: Mapped[str] = mapped_column(String, default="")
    reported_total_cite: Mapped[dict] = mapped_column(JSON, default=dict)
    # Calculated Lines Total: the Decimal sum of the lines, as text.
    lines_total: Mapped[str] = mapped_column(String, default="")
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ClaimSourceArtifact(Base):
    __tablename__ = "claim_source_artifacts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(String, index=True)
    # The manifest id (content-bound; unique within the run).
    artifact_id: Mapped[str] = mapped_column(String, index=True)
    path: Mapped[str] = mapped_column(String, default="")
    sha256: Mapped[str] = mapped_column(String, default="")
    media_type: Mapped[str] = mapped_column(String, default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    pages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sheets: Mapped[list] = mapped_column(JSON, default=list)
    inspection_state: Mapped[str] = mapped_column(String, default="not_inspected")
    failure_reason: Mapped[str] = mapped_column(Text, default="")
    proposed_role: Mapped[str] = mapped_column(String, default="unknown")
    role_reason: Mapped[str] = mapped_column(Text, default="")
    role_citations: Mapped[list] = mapped_column(JSON, default=list)
    # used / duplicate / irrelevant / unreadable / unresolved
    disposition: Mapped[str] = mapped_column(String, default="unresolved")
    disposition_reason: Mapped[str] = mapped_column(Text, default="")
    # "" / adapter / reviewer — a reviewer's disposition is never overwritten
    disposition_by: Mapped[str] = mapped_column(String, default="")
    needs_confirmation: Mapped[int] = mapped_column(Integer, default=0)
    # The case the file is assigned to, if any.
    case_id: Mapped[str] = mapped_column(String, default="", index=True)


class ClaimEvidenceAssignment(Base):
    __tablename__ = "claim_evidence_assignments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(String, index=True)
    # The normalized assignment id (deterministic per case + evidence/file).
    key: Mapped[str] = mapped_column(String, default="", index=True)
    evidence_id: Mapped[str] = mapped_column(String, default="", index=True)
    artifact_id: Mapped[str] = mapped_column(String, default="", index=True)
    case_id: Mapped[str] = mapped_column(String, default="", index=True)
    line_id: Mapped[str] = mapped_column(String, default="")
    # proposed / confirmed / rejected
    state: Mapped[str] = mapped_column(String, default="proposed")
    basis: Mapped[str] = mapped_column(String, default="ai_inference")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    citations: Mapped[list] = mapped_column(JSON, default=list)


class ClaimInvestigation(Base):
    __tablename__ = "claim_investigations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(String, index=True)
    adapter: Mapped[str] = mapped_column(String, default="")
    strategy: Mapped[str] = mapped_column(String, default="")
    # proposed / confirmed / failed
    status: Mapped[str] = mapped_column(String, default="proposed")
    plan: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    rounds: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ClaimToolExecution(Base):
    __tablename__ = "claim_tool_executions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(String, index=True)
    investigation_id: Mapped[str] = mapped_column(String, default="", index=True)
    tool: Mapped[str] = mapped_column(String, default="")
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_hashes: Mapped[list] = mapped_column(JSON, default=list)
    output_hash: Mapped[str] = mapped_column(String, default="")
    truncated: Mapped[int] = mapped_column(Integer, default=0)
    error_code: Mapped[str] = mapped_column(String, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ClaimsSchema(Base):
    """One row per applied claims migration (claims/migrations.py)."""
    __tablename__ = "claims_schema"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, default="")
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
