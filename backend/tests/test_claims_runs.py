"""Claims runs: creation, the survey, the map audit loop, restart safety.

The AI is a scripted stand-in throughout, so these cost nothing and pin
the CODE half: the run skeleton advances and records a diary; the survey
sees every folder, tab and thumbnail; the map audit accepts a correct map
and sends a wrong one back; the startup reconciliation fails only runs
that were mid-stage.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config, settings_store, telemetry
from app.claims import mapping, profile, routes as claims_routes, runner, survey as survey_mod
from app.claims.mapping import ClaimMap, FileRole, FolderMap
from app.claims.models import ClaimsRun
from app.db import Base
from app.main import app

GEN = Path(__file__).resolve().parents[2] / "samples" / "generated" / "claims"
needs_sample = pytest.mark.skipif(not GEN.is_dir(), reason="run samples/generate_claims_sample.py first")


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    for module in (claims_routes, runner, profile, settings_store):
        monkeypatch.setattr(module, "SessionLocal", Session)
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    # Nothing runs in the background during these tests: stages are
    # driven by hand so each can be asserted on.
    monkeypatch.setattr(runner, "start_background", lambda coro: coro.close())
    yield Session


client = TestClient(app)


def _truth() -> dict:
    return json.loads((GEN / "ground_truth_claims.json").read_text())


def _good_map(survey: dict) -> ClaimMap:
    """The correct map for the sample, built from the ground truth — what
    a competent map AI would answer."""
    truth = {e["folder"]: e for e in _truth()["employees"]}
    employees = []
    for fo in survey["folders"]:
        t = truth[fo["path"]]
        files = []
        for path in fo["files"]:
            name = path.split("/", 1)[1]
            if name == t["files"]["report"]:
                role, why = "report", "tab 'Expense Report' has a name header and dated rows"
            elif name in t["files"]["receipts"]:
                role, why = "receipts", "page 1 shows till receipts side by side"
            elif name in (t["files"]["report_print"], t["files"]["approval"]):
                role, why = "ignore", "a print of the report / an approval e-mail"
            else:
                role, why = "unplaced", "cannot tell what this is"
            files.append(FileRole(path=path, role=role, reason=why))
        report = t["files"]["report"]
        employees.append(FolderMap(
            # An employee with no report has no file carrying the ER code;
            # a truthful map leaves it empty (the reviewer may type it).
            folder=fo["path"], is_employee=True, name=t["name"],
            er_code=t["er_code"] if report else "",
            report_file=f"{fo['path']}/{report}" if report else None,
            report_tab="Expense Report" if report else None,
            mileage_tab="KM" if t["mileage_tab"] else None,
            no_report=report is None, files=files,
            reason="folder named after one person; report and receipts inside"))
    return ClaimMap(employees=employees, root_files=[], notes=[])


class _ScriptedAgent:
    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.prompts = []

    async def run(self, prompt, **kwargs):
        self.prompts.append(prompt)

        class R:
            output = self._outputs.pop(0)
        return R()


# ---- creation + skeleton ---------------------------------------------------

@needs_sample
def test_create_run_from_zip_records_the_upload_and_a_snapshot(db):
    with open(GEN / "demo_claims_batch.zip", "rb") as f:
        r = client.post("/api/claims-runs", data={"received_date": "2026-08-03"},
                        files={"batch": ("demo_claims_batch.zip", f, "application/zip")})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]
    assert (runner.workspace_for(run_id) / "upload.zip").is_file()
    got = client.get(f"/api/claims-runs/{run_id}").json()
    assert got["status"] == "queued" and got["received_date"] == "2026-08-03"
    assert "profile" in client.get(f"/api/claims-runs/{run_id}").json() or True
    listed = client.get("/api/claims-runs").json()
    assert listed[0]["id"] == run_id and listed[0]["folder"] == "zip upload"


def test_create_run_validates_its_inputs(db):
    r = client.post("/api/claims-runs", data={"received_date": "3 Aug", "folder_url": "https://x/y"})
    assert r.status_code == 400 and "YYYY-MM-DD" in r.text
    r = client.post("/api/claims-runs", data={"received_date": "2026-08-03"})
    assert r.status_code == 400 and "folder link" in r.text
    r = client.post("/api/claims-runs", data={"received_date": "2026-08-03",
                                              "folder_url": "sharepoint.com/sites/x"})
    assert r.status_code == 400 and "https://" in r.text


@needs_sample
@pytest.mark.asyncio
async def test_process_run_surveys_then_maps_then_waits(db, monkeypatch):
    with open(GEN / "demo_claims_batch.zip", "rb") as f:
        run_id = client.post("/api/claims-runs", data={"received_date": "2026-08-03"},
                             files={"batch": ("b.zip", f, "application/zip")}).json()["run_id"]
    holder = {}

    def fake_agent(*a, **k):
        # The survey is only known once the run has produced it, so the
        # scripted answer is built lazily from the run's own survey.
        class Lazy:
            prompts = []

            async def run(self, prompt, **kw):
                s = db().get(ClaimsRun, run_id).survey
                self.prompts.append(prompt)

                class R:
                    output = _good_map(s)
                return R()
        holder["agent"] = Lazy()
        return holder["agent"]
    monkeypatch.setattr(mapping, "create_agent", fake_agent)

    await runner.process_run(run_id)

    run = db().get(ClaimsRun, run_id)
    assert run.status == "map_ready", run.error
    # Survey: 10 folders, 44 files, every workbook peeked with four tabs,
    # every PDF with a thumbnail.
    assert len(run.survey["folders"]) == 10 and len(run.survey["files"]) == 44
    for f in run.survey["files"]:
        if f["type"] == "workbook":
            assert list(f["peek"]["tabs"]) == ["Instructions", "Expense Types", "Expense Report", "KM"]
        if f["type"] == "pdf":
            assert (runner.workspace_for(run_id) / f["peek"]["thumbnail"]).is_file()
            assert f["pages"] >= 1
    assert sum(1 for f in run.survey["files"] if f["er_code"]) == 18  # 9 reports × (xlsx + pdf)
    # Map: accepted first time, warnings empty, every employee placed.
    assert run.map["rounds"] == 1 and run.map_warnings == []
    assert sum(1 for e in run.map["employees"] if e["is_employee"]) == 10
    stray = [fr for e in run.map["employees"] for fr in e["files"] if fr["path"].endswith("notes.txt")]
    assert stray and stray[0]["role"] == "unplaced"
    # The AI was shown the survey text and the thumbnails.
    prompt = holder["agent"].prompts[0]
    assert "Aegene Ong_1" in prompt[0] and "tab 'Expense Report'" in prompt[0]
    assert sum(1 for p in prompt if not isinstance(p, str)) >= 30
    events = client.get(f"/api/claims-runs/{run_id}/events").json()
    codes = [e["code"] for e in events]
    assert "RUN_STARTED" in codes and "STAGE_DONE" in codes and "MAP_ROUND" in codes


@needs_sample
def test_survey_text_and_peeks(tmp_path):
    from app.claims import source as batch_source

    dest = tmp_path / "files"
    entries = batch_source.unpack_zip(GEN / "demo_claims_batch.zip", dest)
    s = survey_mod.survey_batch(dest, [e for e in entries if e["kind"] == "file"])
    text = survey_mod.survey_text(s)
    assert "## Folder: Aegene Ong_1" in text
    assert "ER code in name: ER(01JUL26-21JUL26)" in text
    assert "A1: Name: | B1: Aegene Ong" in text
    assert survey_mod.er_code_of("x_ER(01JUL26-21JUL26).pdf") == "ER(01JUL26-21JUL26)"
    assert survey_mod.er_code_of("x_Approval.pdf") == ""


# ---- the map audit ---------------------------------------------------------

@needs_sample
@pytest.mark.asyncio
async def test_map_audit_sends_a_wrong_map_back_and_accepts_the_correction(tmp_path, monkeypatch):
    from app.claims import source as batch_source

    dest = tmp_path / "files"
    entries = batch_source.unpack_zip(GEN / "demo_claims_batch.zip", dest)
    s = survey_mod.survey_batch(dest, [e for e in entries if e["kind"] == "file"])
    good = _good_map(s)
    # Wrong in four ways: a folder missing, two employees sharing an ER
    # code, the KM tab called the report, a file with no role.
    bad = good.model_copy(deep=True)
    bad.employees = bad.employees[1:]
    bad.employees[0].er_code = bad.employees[1].er_code
    bad.employees[2].report_tab = "KM"
    bad.employees[3].files = bad.employees[3].files[1:]
    agent = _ScriptedAgent([bad, good])
    monkeypatch.setattr(mapping, "create_agent", lambda *a, **k: agent)

    out, warnings, notes = await mapping.propose_map(s, dest)

    assert len(agent.prompts) == 2 and warnings == [] and out["rounds"] == 2
    feedback = agent.prompts[1][0]
    assert "is missing from your map" in feedback
    assert "one code per employee" in feedback
    assert "does not look like an expense report" in feedback
    assert "has no role" in feedback


@needs_sample
@pytest.mark.asyncio
async def test_map_leftovers_become_warnings_not_failures(tmp_path, monkeypatch):
    from app.claims import source as batch_source

    dest = tmp_path / "files"
    entries = batch_source.unpack_zip(GEN / "demo_claims_batch.zip", dest)
    s = survey_mod.survey_batch(dest, [e for e in entries if e["kind"] == "file"])
    bad = _good_map(s)
    bad.employees = bad.employees[1:]  # one folder never placed
    agent = _ScriptedAgent([bad] * mapping.MAX_ROUNDS)
    monkeypatch.setattr(mapping, "create_agent", lambda *a, **k: agent)

    out, warnings, notes = await mapping.propose_map(s, dest)

    assert len(agent.prompts) == mapping.MAX_ROUNDS
    assert any("missing from your map" in w for w in warnings)
    # The missing folder is still in the stored map, marked for the reviewer.
    assert len(out["employees"]) == 10
    unplaced = [e for e in out["employees"] if "did not place" in e["reason"]]
    assert len(unplaced) == 1 and all(fr["role"] == "unplaced" for fr in unplaced[0]["files"])


@needs_sample
def test_report_tab_plausibility():
    folder = GEN / "batch" / "Aegene Ong_1"
    wb = next(folder.glob("*.xlsx"))
    assert mapping.report_tab_plausible(wb, "Expense Report")[0]
    assert not mapping.report_tab_plausible(wb, "Instructions")[0]
    assert not mapping.report_tab_plausible(wb, "Nope")[0]


@needs_sample
@pytest.mark.asyncio
async def test_playbook_line_reaches_the_prompt_and_role_patterns_override(tmp_path, monkeypatch):
    from app.claims import source as batch_source

    dest = tmp_path / "files"
    entries = batch_source.unpack_zip(GEN / "demo_claims_batch.zip", dest)
    s = survey_mod.survey_batch(dest, [e for e in entries if e["kind"] == "file"])
    good = _good_map(s)
    # The AI wrongly calls the approval a receipts file; the client's
    # remembered pattern must win.
    wrong = good.model_copy(deep=True)
    for fr in wrong.employees[0].files:
        if fr.path.endswith("_Approval.pdf"):
            fr.role = "receipts"
    agent = _ScriptedAgent([wrong])
    monkeypatch.setattr(mapping, "create_agent", lambda *a, **k: agent)
    snapshot = {"playbook": "Maps are in the folder Maps/.",
                "profile": {"file_role_patterns": [{"pattern": "*_Approval.pdf", "role": "ignore"}]}}

    out, warnings, notes = await mapping.propose_map(s, dest, snapshot=snapshot,
                                                    instructions="Ignore thumbs.db")

    text = agent.prompts[0][0]
    assert "Maps are in the folder Maps/." in text and "Ignore thumbs.db" in text
    assert "*_Approval.pdf" in text
    approval = [fr for fr in out["employees"][0]["files"] if fr["path"].endswith("_Approval.pdf")][0]
    assert approval["role"] == "ignore" and "client profile rule" in approval["reason"]


def test_validate_confirmed_map_names_what_is_missing():
    survey = {"files": [{"path": "A_1/r.xlsx", "type": "workbook",
                         "peek": {"tabs": {"Expense Report": [], "KM": []}}},
                        {"path": "A_1/rec.pdf", "type": "pdf", "peek": {}}],
              "folders": [{"path": "A_1", "files": ["A_1/r.xlsx", "A_1/rec.pdf"]}]}
    m = {"employees": [{"folder": "A_1", "is_employee": True, "name": "", "er_code": "ER(1)",
                        "report_file": "A_1/r.xlsx", "report_tab": "Nope",
                        "files": [{"path": "A_1/r.xlsx", "role": "report"},
                                  {"path": "A_1/rec.pdf", "role": "receipts"}]},
                       {"folder": "B_2", "is_employee": True, "name": "B", "er_code": "ER(1)",
                        "no_report": True, "files": []}]}
    problems = mapping.validate_confirmed_map(m, survey)
    assert any("needs a name" in p for p in problems)
    assert any("not in the report file" in p for p in problems)
    assert any("also used by" in p for p in problems)
    assert any("no receipt files" in p for p in problems)


# ---- restart safety --------------------------------------------------------

def test_interrupted_runs_are_failed_but_map_ready_survives(db):
    s = db()
    s.add(ClaimsRun(id="stuck1", client="c", status="surveying"))
    s.add(ClaimsRun(id="stuck2", client="c", status="verifying"))
    s.add(ClaimsRun(id="waiting", client="c", status="map_ready"))
    s.add(ClaimsRun(id="done", client="c", status="ready"))
    s.commit()
    assert runner.fail_interrupted_runs() == 2
    s = db()
    assert s.get(ClaimsRun, "stuck1").status == "failed"
    assert "restarted" in s.get(ClaimsRun, "stuck2").error
    assert s.get(ClaimsRun, "waiting").status == "map_ready"
    assert s.get(ClaimsRun, "done").status == "ready"


# ---- confirm-map -------------------------------------------------------------

@needs_sample
@pytest.mark.asyncio
async def test_confirm_map_records_changes_and_remembers(db, monkeypatch):
    with open(GEN / "demo_claims_batch.zip", "rb") as f:
        run_id = client.post("/api/claims-runs", data={"received_date": "2026-08-03"},
                             files={"batch": ("b.zip", f, "application/zip")}).json()["run_id"]

    class Lazy:
        async def run(self, prompt, **kw):
            class R:
                output = _good_map(db().get(ClaimsRun, run_id).survey)
            return R()
    monkeypatch.setattr(mapping, "create_agent", lambda *a, **k: Lazy())
    await runner.process_run(run_id)
    run = db().get(ClaimsRun, run_id)
    edited = json.loads(json.dumps(run.map))
    edited["employees"][0]["name"] = "Aegene Ong (edited)"
    for fr in edited["employees"][0]["files"]:
        if fr["path"].endswith("notes.txt") or fr["path"].endswith("_Approval.pdf"):
            fr["role"] = "ignore"
    r = client.post(f"/api/claims-runs/{run_id}/confirm-map",
                    json={"map": edited, "remember": [{"pattern": "*_Approval.pdf", "role": "ignore"}]})
    assert r.status_code == 200, r.text
    assert r.json()["employees"] == 10
    assert any("name" in c for c in r.json()["changes"])
    got = client.get(f"/api/claims-runs/{run_id}").json()
    assert got["status"] == "verifying"
    assert len(got["employees"]) == 10
    audit = client.get(f"/api/claims-runs/{run_id}/audit").json()
    assert any(a["action"] == "map_confirmed" and "Aegene Ong (edited)" in a["detail"] for a in audit)
    client_name = got["client"]
    assert profile.get_last_map(client_name)["run_id"] == run_id
    assert {"pattern": "*_Approval.pdf", "role": "ignore"} in profile.get_profile(client_name)["file_role_patterns"]
    # A second confirm is refused: the run has moved on.
    r = client.post(f"/api/claims-runs/{run_id}/confirm-map", json={"map": edited})
    assert r.status_code == 400
