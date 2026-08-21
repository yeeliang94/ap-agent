"""H11 — replay bundles, the verifier, cancellation and retention.

  - a finished Client A run's bundle carries manifest hashes, versions, the
    plan, tool records, decisions, cases, lines, flags and the output; the
    verifier re-derives every material total and reports `reproduces`
  - tampering is caught: a changed published total, a cited file whose
    stored hash differs, a calculation that no longer re-evaluates
  - cancel: an in-flight run's harness is cancelled, the run is failed, the
    workers do not start and the run is never closed as ready; a resting
    run cannot be cancelled
  - retention: tool_output is pruned when a run closes; the snapshot stays
"""
from __future__ import annotations

import pytest

from app import config
from app.claims import replay, retention, runner, worker
from app.claims.investigator import contracts as C
from app.claims.investigator import investigator as inv
from app.claims.models import ClaimEmployee, ClaimSourceArtifact, ClaimToolExecution, ClaimsRun
from app.claims.tools.fake import InMemoryTools

from . import claims_scripted as scripted
from .test_claims_baseline import client, db, rev, run_client_a  # noqa: F401

needs_sample = pytest.mark.skipif(not scripted.GEN.is_dir(), reason="run samples/generate_claims_sample.py first")


@needs_sample
@pytest.mark.asyncio
async def test_bundle_reproduces_and_tampering_is_caught(db, monkeypatch):
    run_id = await run_client_a(db, monkeypatch)
    got = client.get(f"/api/claims-runs/{run_id}").json()
    for f in got["flags"]:
        if f["status"] == "open":
            body = {"decision": "dismissed", "note": "x", "expected_revision": rev(run_id)}
            if f["code"] == "ARTIFACT_UNRESOLVED":
                body["disposition"] = "irrelevant"
            client.post(f"/api/claims-runs/{run_id}/flags/{f['id']}/decide", json=body)
    assert client.get(f"/api/claims-runs/{run_id}").json()["outputs"]  # published
    s = db()
    # a recorded calculation, as the harness would leave it
    s.add(ClaimToolExecution(run_id=run_id, tool="calculate", note="sum([24.00, 26.50]) = 50.50", output_hash="h"))
    s.commit()
    bundle = client.get(f"/api/claims-runs/{run_id}/replay").json()
    assert bundle["bundle_version"] and len(bundle["manifest"]) == 44 and all(m["sha256"] for m in bundle["manifest"])
    assert bundle["versions"]["adapter"] == "legacy" and bundle["versions"]["judge_model"]
    assert bundle["investigation"]["plan"]["steps"] and bundle["calculations"] == ["sum([24.00, 26.50]) = 50.50"]
    assert len(bundle["cases"]) == 10 and bundle["lines"] and bundle["flags"]
    assert any(a["action"] == "flag_dismissed" for a in bundle["reviewer_decisions"])
    assert bundle["output"]["rows"] and bundle["profile_snapshot"]["mileage_rates"]
    report = client.get(f"/api/claims-runs/{run_id}/replay?verify=1").json()
    assert report["reproduces"] is True, report["problems"]
    assert report["checked"]["calculations"] == 1 and report["checked"]["output_rows"] == 10
    # Tamper 1: the published total is edited in storage.
    run = s.get(ClaimsRun, run_id)
    out = dict(run.outputs)
    out["totals"] = {**out["totals"], "total_myr": "1.00"}
    run.outputs = out
    s.commit()
    report = replay.verify_bundle(s, run)
    assert not report["reproduces"] and any("published emitted total differs" in p for p in report["problems"])
    assert any("re-sums to" in p for p in report["problems"])
    run.outputs = {}
    s.commit()
    # Tamper 2: an artifact's stored hash no longer matches the manifest.
    art = s.query(ClaimSourceArtifact).filter(ClaimSourceArtifact.run_id == run_id).first()
    art.sha256 = "deadbeef"
    s.commit()
    report = replay.verify_bundle(s, run)
    assert any("stored hash differs" in p for p in report["problems"])
    # Tamper 3: a calculation that does not re-evaluate.
    s.add(ClaimToolExecution(run_id=run_id, tool="calculate", note="1 + 1 = 3", output_hash="h"))
    s.commit()
    report = replay.verify_bundle(s, run)
    assert any("recorded '3', re-evaluated 2" in p for p in report["problems"])
    # Tamper 4: the bytes on disk change after the inventory. The snapshot
    # is read-only since the manifest (a plain write is refused); forcing
    # it is caught because the verifier re-hashes the files, not the database.
    import os
    import stat

    assert report["checked"]["snapshot_files_rehashed"] == 44
    target = runner.files_dir(run_id) / bundle["manifest"][0]["path"]
    assert not (target.stat().st_mode & stat.S_IWUSR)
    with pytest.raises(PermissionError):
        target.open("ab")
    os.chmod(target, 0o644)
    with target.open("ab") as f:
        f.write(b"tampered")
    report = replay.verify_bundle(s, run)
    assert any("bytes on disk" in p and bundle["manifest"][0]["path"] in p for p in report["problems"]), report["problems"]
    target.unlink()
    report = replay.verify_bundle(s, run)
    assert any("missing from disk" in p for p in report["problems"])


@pytest.mark.asyncio
async def test_cancel_stops_tools_fails_the_run_and_nothing_becomes_ready(db, monkeypatch):
    s = db()
    run = ClaimsRun(id="rc", client="c", status="verifying", snapshot={}, listing_headers={"state": "ok"},
                    progress={"phase": "checking", "step": "matching_evidence", "done": 2, "total": 4, "unit": "claims"})
    emp = ClaimEmployee(run_id="rc", folder="X_1", name="X", roles={"no_report": True, "receipt_files": []}, status="pending")
    s.add_all([run, emp])
    s.commit()
    tools = InMemoryTools([C.ManifestEntry(id="a1", path="x", media_type="other")])
    inv.ACTIVE_TOOLS["rc"] = tools
    r = client.post("/api/claims-runs/rc/cancel", json={})
    assert r.status_code == 200 and r.json()["tools_cancelled"] is True
    assert not (await tools.list_artifacts()).ok  # the harness refuses everything now
    inv.ACTIVE_TOOLS.pop("rc", None)
    s = db()
    assert s.get(ClaimsRun, "rc").status == "failed" and "cancelled" in s.get(ClaimsRun, "rc").error
    # A worker asked to start on the cancelled run does nothing; the run is not closed as ready.
    called = []

    async def fake_work(*a, **k):
        called.append(1)
    monkeypatch.setattr(worker, "_work", fake_work)
    await worker.verify_employee("rc", emp.id)
    assert called == [] and s.get(ClaimEmployee, emp.id).status == "pending"
    assert worker._finish_run("rc", 0.0) is False
    assert db().get(ClaimsRun, "rc").status == "failed"
    # A resting run cannot be cancelled.
    s.add(ClaimsRun(id="rr", client="c", status="ready"))
    s.commit()
    assert client.post("/api/claims-runs/rr/cancel", json={}).status_code == 400
    cancellation = next(a for a in client.get("/api/claims-runs/rc/audit").json()
                        if a["action"] == "run_cancelled")
    assert cancellation["detail"] == "cancelled while matching evidence"


def test_retention_prunes_scratch_and_keeps_the_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    base = tmp_path / "r1" / "claims"
    (base / "files" / "A").mkdir(parents=True)
    (base / "files" / "A" / "x.pdf").write_bytes(b"%PDF")
    (base / "tool_output").mkdir()
    (base / "tool_output" / "h0001.png").write_bytes(b"\x89PNG" + b"0" * 100)
    sizes = retention.workspace_size("r1")
    assert sizes["tool_output"] == 104 and sizes["files"] == 4
    assert retention.prune_tool_output("r1") == 104
    assert not (base / "tool_output").exists() and (base / "files" / "A" / "x.pdf").is_file()
    assert retention.prune_tool_output("r1") == 0
    assert retention.workspace_size("nope") == {}


@pytest.mark.asyncio
async def test_finish_run_prunes_tool_output(db, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path)
    s = db()
    run = ClaimsRun(id="rp", client="c", status="verifying", snapshot={}, listing_headers={"state": "ok"})
    emp = ClaimEmployee(run_id="rp", folder="X_1", name="X", roles={}, status="verified")
    s.add_all([run, emp])
    s.commit()
    d = tmp_path / "rp" / "claims" / "tool_output"
    d.mkdir(parents=True)
    (d / "h.png").write_bytes(b"x")
    assert worker._finish_run("rp", 0.0) is True
    assert not d.exists()
    assert runner.workspace_for("rp") == tmp_path / "rp" / "claims"


@pytest.mark.asyncio
async def test_cancel_during_survey_or_mapping_is_never_resurrected(tmp_path, monkeypatch):
    """A cancel that lands while the conductor is inside a long stage
    (survey, mapping) must stick: the stale run object in process_run
    cannot write 'mapping' or 'map_ready' over 'failed'."""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import settings_store
    from app.claims import profile, routes as claims_routes
    from app.claims import source as batch_source
    from app.claims import survey as survey_mod
    from app.db import Base
    from app.main import app

    engine = create_engine(f"sqlite:///{tmp_path / 't.sqlite3'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    for module in (claims_routes, runner, profile, settings_store):
        monkeypatch.setattr(module, "SessionLocal", Session)
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    client_ = TestClient(app)

    async def scenario(stage: str) -> str:
        run_id = f"cx-{stage}"
        s = Session()
        s.add(ClaimsRun(id=run_id, client="c", status="queued", snapshot={}, folder_url=""))
        s.commit()
        s.close()
        # the batch: one file, copied from nowhere
        monkeypatch.setattr(runner, "_fetch_batch", lambda db, run, dest: [{"path": "A/x.txt", "kind": "file", "size": 1}])

        def cancel_now():
            r = client_.post(f"/api/claims-runs/{run_id}/cancel", json={})
            assert r.status_code == 200, r.text

        def fake_survey(dest, files):
            if stage == "survey":
                cancel_now()
            return {"folders": [{"path": "A"}], "files": [{"path": "A/x.txt", "type": "other"}]}
        monkeypatch.setattr(survey_mod, "survey_batch", fake_survey)

        async def fake_investigate(request):
            if stage == "mapping":
                cancel_now()
            return C.InvestigationResult(map={"employees": []})
        from app.claims import investigator as seam
        monkeypatch.setattr(seam, "investigate", fake_investigate)
        from app.claims import manifest as manifest_mod
        monkeypatch.setattr(manifest_mod, "build_manifest", lambda dest, files: [])
        await runner.process_run(run_id)
        return run_id

    for stage in ("survey", "mapping"):
        run_id = await scenario(stage)
        s = Session()
        run = s.get(ClaimsRun, run_id)
        assert run.status == "failed", (stage, run.status)
        assert "cancelled by the reviewer" in run.error, (stage, run.error)
        codes = [e["code"] for e in client_.get(f"/api/claims-runs/{run_id}/events").json()]
        assert "RUN_CANCELLED" in codes and "RUN_FAILED" not in codes, (stage, codes)
        s.close()


@pytest.mark.asyncio
async def test_a_long_calculation_is_recorded_whole_and_still_re_evaluates(db, tmp_path):
    """The replay bundle re-evaluates every recorded calculation. The note
    that carries it used to be capped at 300 characters, so a long total —
    a case with forty lines is an ordinary one — reached the verifier
    truncated mid-expression and was reported as "no longer evaluates":
    a false tamper alarm on an honest run. The whole expression is
    recorded now (the calculator bounds its length), and a note that IS
    genuinely broken is still caught."""
    from app.claims.tools.harness import ToolHarness

    ws = tmp_path / "ws"
    (ws / "files").mkdir(parents=True)
    amounts = [f"{n + 1}.{n % 100:02d}" for n in range(60)]
    expression = f"sum([{', '.join(amounts)}]) - {sum(float(a) for a in amounts):.2f}"
    assert len(expression) > 300
    tools = ToolHarness(ws, [])
    r = await tools.calculate(expression)
    assert r.ok
    note = tools.executions()[0].note
    assert note.startswith(expression) and len(note) > 300  # recorded WHOLE, not truncated

    s = db()
    run = ClaimsRun(id="rlong", client="c", status="ready", snapshot={}, manifest=[])
    s.add(run)
    s.add(ClaimToolExecution(run_id="rlong", tool="calculate", note=note, input_hashes=[]))
    s.commit()
    report = replay.verify_bundle(s, s.get(ClaimsRun, "rlong"))
    assert report["checked"]["calculations"] == 1
    assert not any("no longer evaluates" in p or "re-evaluated" in p for p in report["problems"]), report["problems"]

    # a note that really does not re-evaluate is still named
    s.add(ClaimToolExecution(run_id="rlong", tool="calculate", note="1 + 1 = 3", input_hashes=[]))
    s.commit()
    report = replay.verify_bundle(s, s.get(ClaimsRun, "rlong"))
    assert any("recorded '3', re-evaluated 2" in p for p in report["problems"])
