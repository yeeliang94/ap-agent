"""Deterministic regression tests for the field-correction endpoint.

No AI calls: only invoice documents (pure-code rules) and stubbed reference
data. Each test covers one of the delicate behaviours found in review:

  - a failed re-check withdraws the published outputs, and retrying the
    same (unchanged) correction repairs the flags instead of no-op'ing
  - correcting an invoice number creates/clears DUPLICATE flags on the
    OTHER document too
  - a human decision keeps covering a rule only while the values that rule
    rests on are unchanged (new incident -> new open flag)
  - every submitted field is validated before ANY is applied
  - the audit reason must be a real string (null must not become "None")
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.main import app
from app.models import Document, Flag, Run
from app import routes
from app.pipeline import checks, reference

LISTING = [
    {"no": "701", "date": "2026-08-01", "vendor": "Alpha",
     "invoice_number": "A-1", "amount": 100.0, "status": "planned"},
    {"no": "702", "date": "2026-08-02", "vendor": "Beta",
     "invoice_number": "B-2", "amount": 200.0, "status": "planned"},
    {"no": "703", "date": "2026-08-03", "vendor": "Gamma",
     "invoice_number": "C-3", "amount": 300.0, "status": "planned"},
]
HEADERS = ["Payment Type", "Beneficiary Name", "Beneficiary Account",
           "Bank Code", "Amount (RM)", "Payment Reference", "IG Code"]
RECENT = (date.today() - timedelta(days=5)).isoformat()


def _invoice(doc_id: str, number: str, vendor: str, amount: float) -> Document:
    return Document(
        id=doc_id, run_id="r1", filename=f"invoice_{number}.pdf",
        kind="invoice", status="checked",
        fields={"vendor": vendor, "invoice_number": number, "date": RECENT,
                "amount": amount, "currency": "MYR"},
        confidence={}, corrections={},
    )


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path/'test.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(routes, "SessionLocal", TestSession)
    async def fake_listing(*a, **k):
        return LISTING  # load_payment_listing is async (AI-capable) now

    async def fake_canonical(*a, **k):
        return True
    monkeypatch.setattr(reference, "load_payment_listing", fake_listing)
    monkeypatch.setattr(reference, "listing_is_canonical", fake_canonical)
    monkeypatch.setattr(reference, "load_policy_clauses", lambda *a, **k: [])
    monkeypatch.setattr(reference, "load_maybank_headers", lambda *a, **k: HEADERS)

    s = TestSession()
    s.add(Run(id="r1", client="Client ABC", status="ready"))
    s.add_all([
        _invoice("docA", "A-1", "Alpha", 100.0),
        _invoice("docB", "B-2", "Beta", 200.0),
        _invoice("docC", "C-3", "Gamma", 300.0),
    ])
    s.commit()
    yield TestSession
    s.close()


client = TestClient(app)


def _correct(doc_id: str, fields: dict, reason="test correction"):
    return client.post(f"/api/runs/r1/documents/{doc_id}/correct",
                       json={"fields": fields, "reason": reason})


def _flags(db, code=None, status=None):
    s = db()
    q = s.query(Flag).filter(Flag.run_id == "r1")
    if code:
        q = q.filter(Flag.code == code)
    if status:
        q = q.filter(Flag.status == status)
    out = q.all()
    s.close()
    return out


def test_reason_must_be_a_real_string(db):
    r = client.post("/api/runs/r1/documents/docA/correct",
                    json={"fields": {"vendor": "X"}, "reason": None})
    assert r.status_code == 400
    r = _correct("docA", {"vendor": "X"}, reason="   ")
    assert r.status_code == 400


def test_all_fields_validated_before_any_applied(db):
    r = _correct("docA", {"vendor": "Changed Co", "amount": "not-a-number"})
    assert r.status_code == 400
    s = db()
    assert s.get(Document, "docA").fields["vendor"] == "Alpha"
    s.close()


def test_failed_recheck_withdraws_outputs_and_retry_repairs(db, monkeypatch):
    s = db()
    s.get(Run, "r1").outputs = {"stale": "built from amount 100"}
    s.commit()
    s.close()

    async def boom(*a, **kw):
        raise RuntimeError("simulated re-check failure")

    with monkeypatch.context() as m:
        m.setattr(checks, "run_checks", boom)
        r = _correct("docA", {"amount": "150"})
    assert r.status_code == 500

    s = db()
    assert s.get(Document, "docA").fields["amount"] == 150.0  # saved
    assert s.get(Run, "r1").outputs == {}  # stale outputs withdrawn
    s.close()

    # Retry with the SAME value must re-check, not short-circuit.
    r = _correct("docA", {"amount": "150"})
    assert r.status_code == 200
    open_mismatch = _flags(db, code="AMOUNT_MISMATCH", status="open")
    assert len(open_mismatch) == 1  # 150 vs the listing's 100.0
    s = db()
    assert s.get(Run, "r1").outputs != {}  # outputs rebuilt
    s.close()


def test_duplicate_raised_and_cleared_across_documents(db):
    # docA takes docB's number: the duplicate lands on the LATER document.
    assert _correct("docA", {"invoice_number": "B-2"}).status_code == 200
    dups = _flags(db, code="DUPLICATE", status="open")
    assert len(dups) == 1 and dups[0].document_id == "docB"

    # Correcting back clears the other document's flag too.
    assert _correct("docA", {"invoice_number": "A-1"}).status_code == 200
    assert _flags(db, code="DUPLICATE", status="open") == []
    assert len(_flags(db, code="DUPLICATE", status="resolved_by_correction")) == 1


def test_decided_flag_not_reraised_for_unrelated_field(db):
    s = db()
    s.add(Flag(run_id="r1", document_id="docA", code="OLD_DATED",
               reason="old", basis="", status="accepted", resolution="fine"))
    doc = s.get(Document, "docA")
    doc.fields = {**doc.fields, "date": "2020-01-01"}  # rule genuinely fires
    s.commit()
    s.close()

    # Correcting the vendor must not resurrect the accepted date warning.
    assert _correct("docA", {"vendor": "Alpha Sdn Bhd"}).status_code == 200
    assert _flags(db, code="OLD_DATED", status="open") == []
    assert len(_flags(db, code="OLD_DATED", status="accepted")) == 1


def test_decided_duplicate_does_not_cover_new_incident(db):
    # docA duplicates docB; the reviewer accepts that duplicate.
    _correct("docA", {"invoice_number": "B-2"})
    dup = _flags(db, code="DUPLICATE", status="open")[0]
    r = client.post(f"/api/runs/r1/flags/{dup.id}/decide",
                    json={"decision": "accepted", "note": "known reissue"})
    assert r.status_code == 200

    # A NEW collision (with docC) is a different incident — it must raise
    # a fresh open flag, not hide behind the accepted one.
    assert _correct("docA", {"invoice_number": "C-3"}).status_code == 200
    fresh = _flags(db, code="DUPLICATE", status="open")
    assert len(fresh) == 1 and "C-3" in fresh[0].reason
    assert len(_flags(db, code="DUPLICATE", status="accepted")) == 1


def test_rejected_flag_superseded_when_its_value_is_corrected(db):
    # A wrong amount raises a flag; the reviewer rejects (excludes) the doc.
    _correct("docA", {"amount": "150"})
    flag = _flags(db, code="AMOUNT_MISMATCH", status="open")[0]
    r = client.post(f"/api/runs/r1/flags/{flag.id}/decide",
                    json={"decision": "rejected", "note": "cannot trust this read"})
    assert r.status_code == 200
    s = db()
    assert "A-1" not in str(s.get(Run, "r1").outputs)  # excluded
    s.close()

    # Correcting the amount to the true value supersedes the rejection:
    # the document returns to the output instead of being excluded forever.
    assert _correct("docA", {"amount": "100"}).status_code == 200
    assert len(_flags(db, code="AMOUNT_MISMATCH", status="superseded_by_correction")) == 1
    assert _flags(db, status="open") == []
    s = db()
    assert "A-1" in str(s.get(Run, "r1").outputs)  # back in the output
    s.close()


def test_recheck_and_rebuild_use_the_runs_snapshot_folder(db, monkeypatch):
    s = db()
    s.get(Run, "r1").snapshot = {"sharepoint_folder_url": "https://snap.example/AP"}
    s.commit()
    s.close()

    seen = {}

    async def spy_checks(docs, only_doc_ids=None, folder_url=None):
        seen["checks"] = folder_url
        return []

    async def spy_outputs(docs, excluded, folder_url=None):
        seen["outputs"] = folder_url
        return {"built": True}

    monkeypatch.setattr(checks, "run_checks", spy_checks)
    monkeypatch.setattr(routes.output_builder, "build_outputs", spy_outputs)
    assert _correct("docA", {"vendor": "Alpha Trading"}).status_code == 200
    assert seen == {"checks": "https://snap.example/AP",
                    "outputs": "https://snap.example/AP"}
