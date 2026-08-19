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
from .test_claims_baseline import client, db, run_client_a  # noqa: F401

needs_sample = pytest.mark.skipif(not scripted.GEN.is_dir(), reason="run samples/generate_claims_sample.py first")


@needs_sample
@pytest.mark.asyncio
async def test_bundle_reproduces_and_tampering_is_caught(db, monkeypatch):
    run_id = await run_client_a(db, monkeypatch)
    got = client.get(f"/api/claims-runs/{run_id}").json()
    for f in got["flags"]:
        if f["status"] == "open":
            body = {"decision": "dismissed", "note": "x"}
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


@pytest.mark.asyncio
async def test_cancel_stops_tools_fails_the_run_and_nothing_becomes_ready(db, monkeypatch):
    s = db()
    run = ClaimsRun(id="rc", client="c", status="verifying", snapshot={}, listing_headers={"state": "ok"}, progress={"what": "verifying"})
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
    assert any(a["action"] == "run_cancelled" for a in client.get("/api/claims-runs/rc/audit").json())


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
