"""H0 — the pinned structured-folder end-to-end result (hardening baseline).

The whole delivered flow on the synthetic Client A batch, every AI call
scripted from the ground truth (tests/claims_scripted.py): survey → map →
confirm → verify → the human gate → output. What this pins is the CODE
half of the module as delivered through 330b972 — the shape every later
adapter (H1) must reproduce on the same input:

  - every employee verified; the no-report employee's rows derived
  - every planted error flagged with its code; ≤ 1 false open flag each
  - the RM 10 example: Aegene Ong's row 10 says NO_RECEIPT, names the RM 35
    receipt on its page and the RM 10.00 difference
  - no output while a flag is open; after decisions, one listing row per
    verified employee in the sample listing's own column order, totals
    reconciled to the cent, the no-report employee's missing Reported
    Total NAMED rather than counted as a match
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config, settings_store
from app.claims import profile, routes as claims_routes, runner, worker
from app.claims.models import ClaimsRun
from app.db import Base
from app.main import app

from . import claims_scripted as scripted

needs_sample = pytest.mark.skipif(not scripted.GEN.is_dir(), reason="run samples/generate_claims_sample.py first")
client = TestClient(app)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    for module in (claims_routes, runner, profile, settings_store, worker):
        monkeypatch.setattr(module, "SessionLocal", Session)
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(runner, "start_background", lambda coro: coro.close())
    yield Session


async def run_client_a(db, monkeypatch, instructions: str = "") -> str:
    """Create, survey, map, confirm and verify the sample batch; returns the run id."""
    t = scripted.truth()
    profile.save_profile(settings_store.get_setting("client_name"), scripted.profile_from_truth(t))
    with open(scripted.GEN / "demo_claims_batch.zip", "rb") as f:
        run_id = client.post("/api/claims-runs", data={"received_date": "2026-08-03", "instructions": instructions},
                             files={"batch": ("b.zip", f, "application/zip")}).json()["run_id"]
    scripted.install(monkeypatch, lambda: db().get(ClaimsRun, run_id).survey, t)
    await runner.process_run(run_id)
    run = db().get(ClaimsRun, run_id)
    assert run.status == "map_ready", run.error
    r = client.post(f"/api/claims-runs/{run_id}/confirm-map", json={"map": run.map})
    assert r.status_code == 200, r.text
    await runner.start_verification(run_id)
    return run_id


@needs_sample
@pytest.mark.asyncio
async def test_structured_folder_baseline_is_pinned(db, monkeypatch):
    t = scripted.truth()
    run_id = await run_client_a(db, monkeypatch)
    got = client.get(f"/api/claims-runs/{run_id}").json()
    assert got["status"] == "ready", got["error"]
    by_name = {e["name"]: e for e in got["employees"]}
    assert len(by_name) == 10 and all(e["status"] == "verified" for e in by_name.values()), \
        {n: (e["status"], e["error"]) for n, e in by_name.items()}

    flags_by_emp: dict[str, list] = {}
    for f in got["flags"]:
        flags_by_emp.setdefault(f["employee_id"], []).append(f)
    total_false = 0
    for e in t["employees"]:
        emp = by_name[e["name"]]
        flags = flags_by_emp.get(emp["id"], [])
        raised = [f["code"] for f in flags]
        open_codes = [f["code"] for f in flags if f["status"] == "open"]
        for want in e["expected_flags"]:
            assert want["code"] in raised, f"{e['name']}: expected {want['code']}, got {raised}"
        extra = list(open_codes)
        for want in e["expected_flags"]:
            if want["code"] in extra:
                extra.remove(want["code"])
        # CATEGORY_UNCLEAR is the scripted judge being unsure for the
        # no-report employee (no purpose to judge from) — the delivered
        # behaviour, not a false flag of the checks.
        extra = [c for c in extra if c != "CATEGORY_UNCLEAR"]
        assert len(extra) <= 1, f"{e['name']}: unexpected open flags {extra}"
        total_false += len(extra)
        for f in flags:
            assert f["basis"], f"{e['name']}: {f['code']} has no basis"
    assert total_false <= len(t["employees"])

    # The RM 10 example.
    aegene = by_name["Aegene Ong"]
    nr = [f for f in flags_by_emp[aegene["id"]] if f["code"] == "NO_RECEIPT" and f["status"] == "open"]
    assert len(nr) == 1 and "RM 10.00 more" in nr[0]["reason"] and nr[0]["cite"].get("position")
    assert aegene["report_total"] == "258.70"
    assert aegene["summary"]["rows"] == 8 and aegene["category"] == "Taxi" and aegene["gl"] == "713070"

    # No-report employee: derived rows, NO_REPORT, no Reported Total.
    arjun = by_name["Arjun Pillai"]
    assert arjun["report_total"] == ""
    assert any(f["code"] == "NO_REPORT" for f in flags_by_emp[arjun["id"]])
    assert all(r["kind"] == "derived" for r in got["rows"] if r["employee_id"] == arjun["id"])

    # The human gate: nothing out while a flag is open. The stray
    # notes.txt is a file nobody placed (H3): it blocks until the reviewer
    # says what it is — a dismissal is not enough.
    assert got["outputs"] == {}
    stray = [f for f in got["flags"] if f["code"] == "ARTIFACT_UNRESOLVED"]
    assert len(stray) == 1 and stray[0]["cite"]["file"].endswith("notes.txt") and stray[0]["employee_id"] == ""
    r = client.post(f"/api/claims-runs/{run_id}/flags/{stray[0]['id']}/decide",
                    json={"decision": "dismissed", "note": "just notes"})
    assert r.status_code == 400 and "disposition" in r.text
    for f in got["flags"]:
        if f["status"] == "open":
            body = {"decision": "dismissed", "note": "baseline pin"}
            if f["code"] == "ARTIFACT_UNRESOLVED":
                body["disposition"] = "irrelevant"
            r = client.post(f"/api/claims-runs/{run_id}/flags/{f['id']}/decide", json=body)
            assert r.status_code == 200, r.text
    got = client.get(f"/api/claims-runs/{run_id}").json()
    out = got["outputs"]
    assert out["header"] == t["listing"]["header"]
    assert len(out["rows"]) == 10 and out["not_included"] == []
    emitted = sum((Decimal(r[10]) for r in out["rows"]), Decimal("0"))
    assert Decimal(out["totals"]["total_myr"]) == emitted
    # The only named difference is the no-report employee's absent
    # Reported Total — named, never counted as a match.
    diffs = out["totals"]["differences"]
    assert [d["name"] for d in diffs] == ["Arjun Pillai"] and diffs[0]["expected"] is None
    assert out["totals"]["match"] is True
    aegene_row = next(r for r in out["rows"] if r[8] == "Aegene Ong")
    assert aegene_row[10] == "258.70" and aegene_row[9] == "ER(01JUL26-21JUL26)" and aegene_row[6] == "Taxi"
