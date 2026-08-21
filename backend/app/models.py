"""Database tables.

Five ideas, five tables:
  Run       — one uploaded batch working its way through the pipeline
  Document  — one file inside a run, plus what the AI extracted from it
  Flag      — one thing that needs a human decision, with its cited basis
  AuditEvent— who decided what, when (the audit trail)
  RunEvent  — what the SYSTEM did and where it struggled (the run diary)

AuditEvent and RunEvent answer different questions and are kept apart on
purpose: the audit trail is about people and must never be diluted by
machine noise, while the diary is about the pipeline and is expected to
be noisy.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import String, Integer, Float, Text, JSON, ForeignKey, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    client: Mapped[str] = mapped_column(String)
    # queued → sorting → extracting → checking → ready → failed
    status: Mapped[str] = mapped_column(String, default="queued")
    error: Mapped[str] = mapped_column(Text, default="")
    # Live counters the frontend polls, e.g. {"extracted": 14, "total": 22}
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    # The copy-ready blocks, built once review is approved.
    outputs: Mapped[dict] = mapped_column(JSON, default=dict)
    # The settings this run was created under (client_name,
    # sharepoint_folder_url). Checks, corrections, and output rebuilds use
    # THIS snapshot, never the current global settings — switching clients
    # in Settings must not change how an existing run is judged.
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    documents: Mapped[list["Document"]] = relationship(back_populates="run")
    flags: Mapped[list["Flag"]] = relationship(back_populates="run")


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    filename: Mapped[str] = mapped_column(String)
    # invoice / claim / receipt / unknown — decided by the sort stage
    kind: Mapped[str] = mapped_column(String, default="unknown")
    # Receipts point at the claim they belong to.
    parent_id: Mapped[str] = mapped_column(String, default="")
    # What the AI read out of the document (vendor, amount, date, ...).
    fields: Mapped[dict] = mapped_column(JSON, default=dict)
    # Per-field confidence notes, e.g. {"amount": "low — blurry scan"}
    confidence: Mapped[dict] = mapped_column(JSON, default=dict)
    # Human corrections: field -> {"from": ..., "to": ..., "reason": ...}.
    # The corrected value lives in fields; this keeps the before/after
    # visible in the app (the audit trail records who and when).
    corrections: Mapped[dict] = mapped_column(JSON, default=dict)
    # pending / sorted / extracted / checked / error
    status: Mapped[str] = mapped_column(String, default="pending")
    error: Mapped[str] = mapped_column(Text, default="")
    # Number of previewable pages discovered during processing. NULL means
    # an older run whose page count was never recorded.
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)

    run: Mapped["Run"] = relationship(back_populates="documents")


class Flag(Base):
    __tablename__ = "flags"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    document_id: Mapped[str] = mapped_column(String, default="")
    # Machine code, e.g. ALREADY_PAID / OVER_CAP / OLD_DATED / LOW_CONFIDENCE
    code: Mapped[str] = mapped_column(String)
    # Human sentence: why this was flagged.
    reason: Mapped[str] = mapped_column(Text)
    # The quoted policy line or rule the flag rests on — every judgment cites
    # its basis so a reviewer can verify the reasoning, not just the result.
    basis: Mapped[str] = mapped_column(Text, default="")
    # open / accepted / rejected — set by the human on the review screen
    status: Mapped[str] = mapped_column(String, default="open")
    resolution: Mapped[str] = mapped_column(Text, default="")

    run: Mapped["Run"] = relationship(back_populates="flags")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String)
    actor: Mapped[str] = mapped_column(String)   # who (pilot: "reviewer")
    action: Mapped[str] = mapped_column(String)  # what they did
    detail: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class RunEvent(Base):
    """One moment in a run's life — a stage starting, a document failing.

    Written by telemetry.record(), which also logs it. The review screen
    reads these so a failure is visible to the person using the app, not
    only to whoever can read the server log.
    """
    __tablename__ = "run_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String, index=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    # Which pipeline stage: reference / sort / extract / check / output / run
    stage: Mapped[str] = mapped_column(String, default="")
    # info / warning / error — what the reviewer's attention is owed
    level: Mapped[str] = mapped_column(String, default="info")
    # Machine code, e.g. STAGE_DONE / DOCUMENT_FAILED / ALL_SORTS_FAILED
    code: Mapped[str] = mapped_column(String, default="")
    # The plain sentence a reviewer reads.
    message: Mapped[str] = mapped_column(Text, default="")
    # The engineer's version (exception type and text), already redacted.
    detail: Mapped[str] = mapped_column(Text, default="")
    # Set when the event is about one specific file.
    document_id: Mapped[str] = mapped_column(String, default="")


class AppSetting(Base):
    """One row per app-level setting the reviewer may change on screen
    (client name, SharePoint folder). Secrets stay in .env on purpose."""
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
