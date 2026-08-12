"""Database tables.

Four ideas, four tables:
  Run       — one uploaded batch working its way through the pipeline
  Document  — one file inside a run, plus what the AI extracted from it
  Flag      — one thing that needs a human decision, with its cited basis
  AuditEvent— who decided what, when (the audit trail)
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
    # pending / sorted / extracted / checked / error
    status: Mapped[str] = mapped_column(String, default="pending")
    error: Mapped[str] = mapped_column(Text, default="")

    run: Mapped["Run"] = relationship(back_populates="documents")


class Flag(Base):
    __tablename__ = "flags"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    document_id: Mapped[str] = mapped_column(String, default="")
    # Machine code, e.g. NOT_IN_LISTING / OVER_CAP / OLD_DATED / LOW_CONFIDENCE
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
