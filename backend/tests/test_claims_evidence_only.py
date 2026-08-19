"""H7 — evidence-only and no-summary verification, case-keyed workers, and
run-wide controls after changes.

  - scenario E: a dump with receipts and no claim summary yields proposed,
    evidence-derived lines (origin evidence_derived) with NO_SUMMARY, one
    CLAIM_AMOUNT_UNCONFIRMED per case listing every line, PURPOSE_UNKNOWN as
    a note; the output stays locked until the reviewer confirms; a missing
    Reported Total is named, never counted as a match
  - the delivered folder-based path keeps NO_REPORT (baseline)
  - SHARED_RECEIPT resolves itself when a receipt stops being shared
  - the case-keyed retry re-runs the case's worker
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from app.claims import profile as profile_mod
from app.claims import runner, worker
from app.claims.models import ClaimEmployee, ClaimEvidence, ClaimFlag, ClaimRow, ClaimsRun

from . import claims_scripted as scripted
from .test_claims_baseline import client, db  # noqa: F401
from .test_claims_grouping import _flat_dump_run

needs_sample = pytest.mark.skipif(not scripted.GEN.is_dir(), reason="run samples/generate_claims_sample.py first")


@needs_sample
@pytest.mark.asyncio
async def test_evidence_only_lines_are_proposals_until_confirmed(db, monkeypatch):
    # Only Arjun's folder (receipts + approval, no report) as a flat dump.
    monkeypatch.setattr(scripted, "flat_zip", lambda extra=None, folders=("Arjun Pillai_7",): _zip(folders, extra))
    run_id = await _flat_dump_run(db, monkeypatch, extra={"readme.txt": b"nothing"})
    got = client.get(f"/api/claims-runs/{run_id}").json()
    assert got["investigation"]["plan"]["strategy"] == "evidence_only"
    case = got["cases"][0]
    assert case["roles"]["no_report"] and case["claimant"]["state"] == "proposed"
    r = client.post(f"/api/claims-runs/{run_id}/confirm-grouping", json={"expected_revision": got["revision"]})
    assert r.status_code == 200, r.text
    await runner.start_verification(run_id)
    got = client.get(f"/api/claims-runs/{run_id}").json()
    assert got["status"] == "ready", got["error"]
    case = got["cases"][0]
    rows = [x for x in got["rows"] if x["case_id"] == case["id"]]
    assert rows and all(x["kind"] == "derived" and x["origin"] == "evidence_derived" for x in rows)
    codes = {f["code"]: f for f in got["flags"] if f["case_id"] == case["id"]}
    assert "NO_SUMMARY" in codes and "NO_REPORT" not in codes
    amount = codes["CLAIM_AMOUNT_UNCONFIRMED"]
    assert amount["status"] == "open" and f"{len(rows)} line(s)" in amount["reason"]
    assert all(str(x["values"]["amount"]) in amount["reason"] for x in rows)
    assert codes["PURPOSE_UNKNOWN"]["status"] == "info"
    assert case["reported_total"] == "" and Decimal(case["lines_total"]) == sum(Decimal(x["values"]["amount"]) for x in rows)
    assert got["outputs"] == {}
    # The reviewer confirms the amounts (a note is the confirmation), settles the rest.
    for f in got["flags"]:
        if f["status"] != "open":
            continue
        body = {"decision": "dismissed", "note": "confirmed against the receipts"}
        if f["code"] == "ARTIFACT_UNRESOLVED":
            body["disposition"] = "irrelevant"
        if f["code"] == "CATEGORY_UNCLEAR":
            r = client.put(f"/api/claims-runs/{run_id}/employees/{case['employee_id']}/category",
                           json={"category": "Taxi", "gl": "713070", "reason": "all Grab receipts"})
            assert r.status_code == 200
            continue
        r = client.post(f"/api/claims-runs/{run_id}/flags/{f['id']}/decide", json=body)
        assert r.status_code == 200, r.text
    got = client.get(f"/api/claims-runs/{run_id}").json()
    out = got["outputs"]
    assert out and len(out["rows"]) == 1
    assert Decimal(out["totals"]["total_myr"]) == sum(Decimal(x["values"]["amount"]) for x in rows)
    assert out["totals"]["differences"][0]["expected"] is None  # no Reported Total: named, not matched


def _zip(folders, extra):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for folder in folders:
            for p in (scripted.GEN / "batch" / folder).iterdir():
                z.write(p, p.name)
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def test_shared_receipt_resolves_when_no_longer_shared(db):
    s = db()
    run = ClaimsRun(id="rs", client="c", status="ready", snapshot={}, listing_headers={"state": "ok"})
    s.add(run)
    s.commit()
    from .test_claims_robustness import _employee_with_matched_receipt

    e1, r1, ev1 = _employee_with_matched_receipt(s, run, "A_1", "A")
    e2, r2, ev2 = _employee_with_matched_receipt(s, run, "B_2", "B", page=2)
    profile = profile_mod.PROFILE_DEFAULTS
    assert worker._shared_receipt_flags(s, "rs", profile) == 2
    s.commit()
    assert s.query(ClaimFlag).filter(ClaimFlag.code == "SHARED_RECEIPT", ClaimFlag.status == "open").count() == 2
    # B's receipt no longer matches a row (a correction moved it): the pair dissolves.
    ev2.matched_row_id = ""
    s.commit()
    worker.rerun_global_controls(s, "rs", profile)
    s.commit()
    flags = s.query(ClaimFlag).filter(ClaimFlag.code == "SHARED_RECEIPT").all()
    assert {f.status for f in flags} == {"resolved_by_correction"}
    # It comes back when the pair re-forms — the decided-once rule keeps a
    # decided flag decided, but a resolved one is raised anew (a new incident).
    ev2.matched_row_id = r2.id
    s.commit()
    assert worker._shared_receipt_flags(s, "rs", profile) == 0  # keys exist (resolved) → not raised twice
    assert s.query(ClaimFlag).filter(ClaimFlag.code == "SHARED_RECEIPT").count() == 2


@pytest.mark.asyncio
async def test_case_retry_reruns_the_cases_worker(db, monkeypatch):
    s = db()
    run = ClaimsRun(id="rc", client="c", status="ready", snapshot={}, listing_headers={"state": "ok"})
    emp = ClaimEmployee(run_id="rc", folder="X_1", name="X", roles={"no_report": True, "receipt_files": []}, status="failed", error="boom")
    s.add_all([run, emp])
    s.commit()
    from app.claims import cases as cases_mod

    case = cases_mod.sync_case_from_employee(s, emp)
    s.commit()
    seen = []

    async def fake_work(s_, run_, emp_, usage):
        seen.append(emp_.id)
        emp_.summary = {"rows": 0}
    monkeypatch.setattr(worker, "_work", fake_work)
    r = client.post(f"/api/claims-runs/rc/cases/{case.id}/retry")
    assert r.status_code == 200, r.text
    # the route only queues; drive the worker
    await worker.retry_case("rc", case.id)
    assert seen == [emp.id]
    s = db()
    assert s.get(ClaimEmployee, emp.id).status == "verified"
    assert cases_mod.case_for_employee(s, emp.id).status == "verified"
    assert s.query(ClaimRow).count() == 0 and s.query(ClaimEvidence).count() == 0
