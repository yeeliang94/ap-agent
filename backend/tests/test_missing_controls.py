"""Two peer-review fixes, proven:

1. A control that is missing must be SEEN. A batch with staff claims but no
   expense policy, or any batch without a bank upload template, raises a
   run-level MISSING_REFERENCE flag the reviewer must acknowledge — instead
   of a diary line nobody reads. Without a policy no AI judgment is paid
   for, and the receipt check (which needs no policy) still runs.

2. Runs are in-process tasks, so a server restart orphans any run that was
   under way. At startup such runs are marked failed with a plain reason,
   instead of showing "extracting" forever.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Run, RunEvent
from app.pipeline import checks, reference, runner


class _Claim:
    def __init__(self, doc_id: str, receipts_ok: bool = True) -> None:
        self.id, self.kind, self.status = doc_id, "claim", "extracted"
        self.filename = f"{doc_id}.png"
        self.fields = {"claimant": "Aegene", "description": "taxi", "amount": 33.0,
                       "currency": "MYR", "receipts_match_amount": receipts_ok}
        self.confidence, self.error, self.parent_id = {}, "", None


def _no_ai(*a, **k):
    raise AssertionError("the AI must not be called when there is no policy")


@pytest.fixture()
def no_listing(monkeypatch):
    async def fake_listing(*a, **k):
        return []
    monkeypatch.setattr(reference, "load_payment_listing", fake_listing)


@pytest.mark.asyncio
async def test_claims_without_policy_raise_one_run_level_flag(monkeypatch, tmp_path, no_listing):
    monkeypatch.setattr(reference, "load_policy_clauses", lambda *a, **k: [])
    monkeypatch.setattr(reference, "load_maybank_headers", lambda *a, **k: ["Amount (RM)"])
    monkeypatch.setattr(checks, "create_agent", _no_ai)
    docs = [_Claim("c1"), _Claim("c2", receipts_ok=False)]

    flags = await checks.run_checks(docs, refs=tmp_path)

    missing = [f for f in flags if f["code"] == "MISSING_REFERENCE"]
    assert len(missing) == 1, flags
    assert missing[0]["document_id"] == ""          # about the batch, not a file
    assert "2 staff claims" in missing[0]["reason"]
    assert "policy" in missing[0]["reason"].lower()
    # The receipt check needs no policy, so it still protects the batch.
    assert [f["document_id"] for f in flags if f["code"] == "RECEIPT_MISMATCH"] == ["c2"]
    # And no per-claim "could not categorise" noise on top.
    assert not [f for f in flags if f["code"] in ("AMBIGUOUS_CATEGORY", "JUDGMENT_FAILED")]


@pytest.mark.asyncio
async def test_no_flag_when_the_batch_has_no_claims(monkeypatch, tmp_path, no_listing):
    monkeypatch.setattr(reference, "load_policy_clauses", lambda *a, **k: [])
    monkeypatch.setattr(reference, "load_maybank_headers", lambda *a, **k: ["Amount (RM)"])
    flags = await checks.run_checks([], refs=tmp_path)
    assert flags == []


@pytest.mark.asyncio
async def test_missing_bank_template_is_flagged(monkeypatch, tmp_path, no_listing):
    monkeypatch.setattr(reference, "load_policy_clauses", lambda *a, **k: [])
    monkeypatch.setattr(reference, "load_maybank_headers", lambda *a, **k: [])
    flags = await checks.run_checks([], refs=tmp_path)
    assert [f["code"] for f in flags] == ["MISSING_REFERENCE"]
    assert "bank upload template" in flags[0]["reason"]
    assert flags[0]["document_id"] == ""


@pytest.mark.asyncio
async def test_per_document_recheck_never_raises_run_level_flags(monkeypatch, tmp_path, no_listing):
    """A correction re-checks one document; the batch-level flags already
    exist and must not be duplicated on every save."""
    monkeypatch.setattr(reference, "load_policy_clauses", lambda *a, **k: [])
    monkeypatch.setattr(reference, "load_maybank_headers", lambda *a, **k: [])
    monkeypatch.setattr(checks, "create_agent", _no_ai)
    flags = await checks.run_checks([_Claim("c1")], only_doc_ids={"c1"}, refs=tmp_path)
    assert not [f for f in flags if f["code"] == "MISSING_REFERENCE"]


def test_startup_marks_interrupted_runs_failed(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path/'t.sqlite3'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(runner, "SessionLocal", Session)

    s = Session()
    for i, status in enumerate(("queued", "sorting", "extracting", "checking",
                                "ready", "failed")):
        s.add(Run(id=f"r{i}", client="C", status=status,
                  error="old reason" if status == "failed" else ""))
    s.commit()

    assert runner.fail_interrupted_runs() == 4

    runs = {r.id: r for r in s.query(Run).all()}
    for i in range(4):
        assert runs[f"r{i}"].status == "failed"
        assert "restarted" in runs[f"r{i}"].error
    assert runs["r4"].status == "ready"                 # untouched
    assert runs["r5"].error == "old reason"             # untouched
    events = s.query(RunEvent).filter(RunEvent.code == "RUN_INTERRUPTED").all()
    assert sorted(e.run_id for e in events) == ["r0", "r1", "r2", "r3"]
    s.close()

    # Idempotent: a second start (or --reload) finds nothing to do.
    assert runner.fail_interrupted_runs() == 0
