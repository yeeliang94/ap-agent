"""H3 — the global inventory: run-wide quotas, a flat folder reaching
investigation with every file visible, per-case budgets after grouping,
and the artifact-completeness control (ARTIFACT_UNRESOLVED) with the
reviewer's disposition as the only release.
"""
from __future__ import annotations

import io
import zipfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.claims import mapping, profile, runner, worker
from app.claims import cases as cases_mod
from app.claims.mapping import ClaimMap, FileRole
from app.claims.models import ClaimEmployee, ClaimFlag, ClaimSourceArtifact, ClaimsRun
from app.db import Base

from . import claims_scripted as scripted
from .test_claims_baseline import client, db, rev  # noqa: F401

needs_sample = pytest.mark.skipif(not scripted.GEN.is_dir(), reason="run samples/generate_claims_sample.py first")


def _flat_zip() -> bytes:
    """Three files straight in the batch folder: a report workbook, a
    receipts PDF, a text note. No subfolders at all."""
    folder = scripted.GEN / "batch" / "Aegene Ong_1"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.write(next(folder.glob("*.xlsx")), "report.xlsx")
        z.write(folder / "Aegene Ong_Receipt .pdf", "receipts.pdf")
        z.writestr("notes.txt", "hello")
    return buf.getvalue()


@needs_sample
@pytest.mark.asyncio
async def test_flat_folder_reaches_investigation_with_every_file_visible(db, monkeypatch):
    run_id = client.post("/api/claims-runs", data={"received_date": "2026-08-03"},
                         files={"batch": ("flat.zip", _flat_zip(), "application/zip")}).json()["run_id"]

    class Agent:
        async def run(self, prompt, **kw):
            class R:
                output = ClaimMap(employees=[], root_files=[
                    FileRole(path="report.xlsx", role="report", reason="an expense report tab"),
                    FileRole(path="receipts.pdf", role="receipts", reason="till receipts"),
                    FileRole(path="notes.txt", role="unplaced", reason="cannot look inside")], notes=[])

                def usage(self):
                    class U:
                        total_tokens = 1
                        requests = 1
                    return U()
            return R()
    monkeypatch.setattr(mapping, "create_agent", lambda *a, **k: Agent())
    await runner.process_run(run_id)
    run = db().get(ClaimsRun, run_id)
    assert run.status == "map_ready", run.error
    # The manifest was built before mapping: every file, hashed.
    assert sorted(m["path"] for m in run.manifest) == ["notes.txt", "receipts.pdf", "report.xlsx"]
    assert all(m["sha256"] for m in run.manifest)
    got = client.get(f"/api/claims-runs/{run_id}").json()
    assert got["artifact_counts"] == {"total": 3, "unresolved": 1, "needs_review": 1}
    by_path = {a["path"]: a for a in got["artifacts"]}
    assert by_path["report.xlsx"]["proposed_role"] == "report" and by_path["report.xlsx"]["disposition"] == "used"
    assert by_path["notes.txt"]["disposition"] == "unresolved"
    assert got["cases"] == []  # nothing grouped yet — proposed at the map, not assumed
    events = client.get(f"/api/claims-runs/{run_id}/events").json()
    assert any(e["code"] == "FLAT_FOLDER" for e in events)


@pytest.mark.asyncio
async def test_a_case_over_its_budget_fails_alone_and_the_run_closes(db, monkeypatch):
    s = db()
    run = ClaimsRun(id="rb", client="c", status="verifying", snapshot={}, listing_headers={"state": "ok"},
                    manifest=[{"path": f"Big_1/r{i}.pdf", "pages": 10, "sha256": "x", "id": f"a{i}"} for i in range(30)]
                    + [{"path": "Small_2/r.pdf", "pages": 1, "sha256": "y", "id": "b1"}])
    s.add(run)
    big = ClaimEmployee(run_id="rb", folder="Big_1", name="Big", roles={"no_report": True,
                        "receipt_files": [f"Big_1/r{i}.pdf" for i in range(30)]}, status="pending")
    small = ClaimEmployee(run_id="rb", folder="Small_2", name="Small", roles={"no_report": True,
                          "receipt_files": ["Small_2/r.pdf"]}, status="pending")
    s.add_all([big, small])
    s.commit()
    assert worker._case_budget_problem(run, big).startswith("Big has 300 pages")
    assert worker._case_budget_problem(run, small) == ""

    async def fake_work(s_, run_, emp, usage):
        emp.summary = {"rows": 0}
    monkeypatch.setattr(worker, "_work", fake_work)
    await worker.verify_employee("rb", big.id)
    await worker.verify_employee("rb", small.id)
    worker._finish_run("rb", 0.0)
    s = db()
    assert s.get(ClaimEmployee, big.id).status == "failed" and "300 pages" in s.get(ClaimEmployee, big.id).error
    assert s.get(ClaimEmployee, small.id).status == "verified"
    assert s.get(ClaimsRun, "rb").status == "ready"
    assert cases_mod.case_for_employee(s, big.id).status == "failed"


def test_unresolved_artifacts_block_until_the_reviewer_settles_them(db):
    s = db()
    run = ClaimsRun(id="ru", client="c", status="ready", snapshot={}, listing_headers={"state": "ok"})
    s.add(run)
    s.add(ClaimSourceArtifact(run_id="ru", artifact_id="a1", path="X/notes.txt", media_type="other",
                              disposition="unresolved", needs_confirmation=1, role_reason="cannot look inside"))
    s.add(ClaimSourceArtifact(run_id="ru", artifact_id="a2", path="X/r.pdf", media_type="pdf", pages=2,
                              disposition="used", case_id="c1"))
    s.commit()
    assert worker._artifact_completeness_flags(s, "ru") == 1
    assert worker._artifact_completeness_flags(s, "ru") == 0  # idempotent
    s.commit()
    flag = s.query(ClaimFlag).filter(ClaimFlag.run_id == "ru").one()
    assert flag.code == "ARTIFACT_UNRESOLVED" and flag.artifact_id == "a1" and flag.employee_id == ""
    assert flag.cite == {"file": "X/notes.txt", "page": 0} and "nothing uploaded vanishes" in flag.reason.lower()
    assert profile.flag_key(flag) == ("ARTIFACT_UNRESOLVED", "a1")
    # A dismissal is refused; a disposition settles it and resolves the flag.
    r = client.post(f"/api/claims-runs/ru/flags/{flag.id}/decide", json={"decision": "dismissed", "note": "meh", "expected_revision": rev("ru")})
    assert r.status_code == 400
    r = client.post("/api/claims-runs/ru/artifacts/a1/disposition", json={"disposition": "used", "reason": "x"})
    assert r.status_code == 400 and "expected_revision is required" in r.text   # every case route takes the revision
    r = client.post("/api/claims-runs/ru/artifacts/a1/disposition", json={"disposition": "used", "reason": "x", "expected_revision": 0})
    assert r.status_code == 400 and "inside a case" in r.text
    r = client.post("/api/claims-runs/ru/artifacts/a1/disposition", json={"disposition": "irrelevant", "reason": "", "expected_revision": 0})
    assert r.status_code == 400 and "reason" in r.text
    r = client.post("/api/claims-runs/ru/artifacts/a1/disposition",
                    json={"disposition": "irrelevant", "reason": "personal notes", "expected_revision": 0})
    assert r.status_code == 200 and r.json()["artifact"]["disposition_by"] == "reviewer"
    s = db()
    flag = s.get(ClaimFlag, flag.id)
    assert flag.status == "resolved_by_correction" and "irrelevant" in flag.resolution
    assert s.get(ClaimsRun, "ru").revision == 1
    assert worker._artifact_completeness_flags(s, "ru") == 0  # settled: never raised again
    # An adapter re-storing the artifact cannot overwrite the reviewer's word.
    from app.claims.investigator import contracts as C

    cases_mod.upsert_artifacts(s, "ru", [C.SourceArtifact(id="a1", path="X/notes.txt", disposition="unresolved")])
    art = s.query(ClaimSourceArtifact).filter(ClaimSourceArtifact.artifact_id == "a1").one()
    assert art.disposition == "irrelevant" and art.disposition_by == "reviewer"
    audit = client.get("/api/claims-runs/ru/audit").json()
    assert any(a["action"] == "artifact_disposition" and "personal notes" in a["detail"] for a in audit)


def test_catalogue_names_identity_for_every_new_code():
    for code in ("ARTIFACT_UNRESOLVED", "CLAIMANT_UNKNOWN", "OWNERSHIP_CONFLICT", "UNASSIGNED_EVIDENCE",
                 "CLAIM_AMOUNT_UNCONFIRMED", "PURPOSE_UNKNOWN", "NO_SUMMARY", "TOOL_UNAVAILABLE",
                 "TOOL_FAILED", "SANDBOX_LIMIT"):
        d = profile.describe(code)
        assert d["title"] and d["meaning"] and d["what_to_do"] and d["identity"], code
    for code in ("ARTIFACT_UNRESOLVED", "CLAIMANT_UNKNOWN", "OWNERSHIP_CONFLICT", "CLAIM_AMOUNT_UNCONFIRMED",
                 "TOOL_UNAVAILABLE", "TOOL_FAILED", "SANDBOX_LIMIT"):
        assert code not in profile.CHECK_CODES, f"{code} must not be switchable"
    assert profile.flag_key({"code": "NO_RECEIPT", "row_id": "r", "evidence_id": "e"}) == ("NO_RECEIPT", "r", "e")
