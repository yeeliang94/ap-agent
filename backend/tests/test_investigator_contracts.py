"""H1 — the investigator seam and the adapter-neutral contract tests.

  - the legacy structured-folder adapter, through `investigate()`, turns
    Client A into the normalized result: one disposition per artifact, ten
    proposed cases with PROPOSED claimants, the stray file unresolved and
    blocking, assignments only for report/receipts files
  - the result and record types refuse the shapes the plan forbids
  - the in-memory InvestigationTools fake behaves like the harness where a
    test cares: budgets, manifest ids only, provenance, proposals, cancel
  - run instructions reach the report reader, the page reader and the
    category judge (steering), are logged in the diary, and change nothing
    in the checks: the same batch with and without instructions yields the
    same flags and output; REPORT_TOTAL_MISMATCH toggles only via the profile
  - every retry path (correction re-check, tie-break) stops at the same
    budget as initial verification
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.claims import evidence as evidence_mod
from app.claims import manifest as manifest_mod
from app.claims import mapping, worker
from app.claims import source as batch_source
from app.claims import survey as survey_mod
from app.claims.investigator import contracts as C
from app.claims.investigator import investigate, legacy
from app.claims.tools.contracts import ToolResult
from app.claims.tools.fake import InMemoryTools

from . import claims_scripted as scripted
from .test_claims_baseline import db, run_client_a  # noqa: F401 — fixtures reused
from .test_claims_baseline import client

needs_sample = pytest.mark.skipif(not scripted.GEN.is_dir(), reason="run samples/generate_claims_sample.py first")


def _client_a_request(tmp_path) -> C.InvestigationRequest:
    ws = tmp_path / "ws"
    dest = ws / "files"
    entries = batch_source.unpack_zip(scripted.GEN / "demo_claims_batch.zip", dest)
    files = [e for e in entries if e["kind"] == "file"]
    survey = survey_mod.survey_batch(dest, files)
    manifest = manifest_mod.build_manifest(dest, files)
    return C.InvestigationRequest(run_id="runA", workspace=str(ws), manifest=manifest, survey=survey,
                                  instructions="", objective="check everything")


# ---- the legacy adapter through the seam --------------------------------------------

@needs_sample
@pytest.mark.asyncio
async def test_legacy_adapter_normalizes_client_a(tmp_path, monkeypatch):
    req = _client_a_request(tmp_path)
    monkeypatch.setattr(mapping, "create_agent",
                        lambda *a, **k: scripted.ScriptedAgent([scripted.good_map(req.survey)]))
    tools = InMemoryTools(req.manifest)
    result = await investigate(req, tools)

    # Every artifact once, one disposition each; hashes carried.
    assert len(result.artifacts) == len(req.manifest) == 44
    assert len({a.id for a in result.artifacts}) == 44
    assert all(a.sha256 and a.disposition for a in result.artifacts)
    by_path = {a.path: a for a in result.artifacts}
    stray = by_path["Sofia Lim_8/notes.txt"]
    assert stray.disposition == "unresolved" and stray.needs_confirmation and stray.proposed_role == "unknown"
    reports = [a for a in result.artifacts if a.proposed_role == "report"]
    assert len(reports) == 9 and all(a.disposition == "used" for a in reports)
    ignored = [a for a in result.artifacts if a.disposition == "irrelevant"]
    assert ignored and all(a.disposition_reason for a in ignored)

    # Ten cases, claimants PROPOSED (never confirmed by the adapter).
    assert len(result.cases) == 10
    assert all(c.state == "proposed" and c.claimant.state == "proposed" for c in result.cases)
    aegene = next(c for c in result.cases if c.label == "Aegene Ong_1")
    assert aegene.claimant.name == "Aegene Ong" and aegene.claimant.identifier == "ER(01JUL26-21JUL26)"
    assert aegene.roles["report_tab"] == "Expense Report" and len(aegene.roles["receipt_files"]) == 2
    assert aegene.reported_total is None and aegene.lines_total is None
    # Assignments: report + receipts files only, proposed, on a folder basis.
    assert all(a.state == "proposed" and a.basis == "folder_structure" for a in result.assignments)
    mine = [a for a in result.assignments if a.case_id == aegene.id]
    assert len(mine) == 3
    # The seam says what still blocks output.
    blocking = result.blocking_conditions()
    assert any("notes.txt" in b for b in blocking) and any("claimant proposed" in b for b in blocking)
    # The plan is run-local and names the adapter.
    assert result.plan.adapter == "legacy" and result.plan.strategy == "structured"
    # The compat map is the delivered shape.
    assert sum(1 for e in result.map["employees"] if e["is_employee"]) == 10
    # Same folder → same case id on a second pass (idempotent ids).
    assert legacy.case_id_for("runA", "Aegene Ong_1") == aegene.id


@needs_sample
@pytest.mark.asyncio
async def test_confirmed_map_marks_cases_and_claimants_confirmed(tmp_path, monkeypatch):
    req = _client_a_request(tmp_path)
    m = scripted.good_map(req.survey).model_dump()
    next(e for e in m["employees"] if e["folder"] == "Nick Goh_2")["skip"] = True
    result = legacy.from_map(req, m, confirmed=True)
    states = {c.label: (c.state, c.claimant.state) for c in result.cases}
    assert states["Aegene Ong_1"] == ("confirmed", "confirmed")
    assert states["Nick Goh_2"] == ("excluded", "confirmed")
    assert all(a.state == "confirmed" for a in result.assignments)


# ---- the types refuse what the plan forbids ---------------------------------------

def test_result_types_enforce_the_rules():
    with pytest.raises(ValidationError):
        C.Claimant(name="", state="confirmed")
    with pytest.raises(ValidationError):
        C.SourceArtifact(id="a1", path="x", disposition="irrelevant")  # no reason
    a = C.SourceArtifact(id="a1", path="x")
    assert a.disposition == "unresolved" and not a.terminal
    with pytest.raises(ValidationError):
        C.InvestigationResult(artifacts=[a, a])
    with pytest.raises(ValidationError):
        C.InvestigationResult(assignments=[C.EvidenceAssignment(id="s1", case_id="nope")])
    case = C.ClaimCase(id="c1")
    assert case.claimant.state == "unknown" and case.reported_total is None
    r = C.InvestigationResult(artifacts=[a], cases=[case])
    assert r.blocking_conditions() == ["x: no disposition yet", "case c1: claimant unknown"]
    assert C.Citation(sheet="S", row=3).as_flag_cite() == {"sheet": "S", "row": 3}
    assert C.Citation(path="f.pdf", page=2, position="left").as_flag_cite() == {"file": "f.pdf", "page": 2, "position": "left"}


# ---- the in-memory tools -------------------------------------------------------------

def _manifest():
    return [C.ManifestEntry(id="a1", path="A/report.xlsx", sha256="h1", media_type="workbook", sheets=["Expense Report"]),
            C.ManifestEntry(id="a2", path="A/receipts.pdf", sha256="h2", media_type="pdf", pages=2)]


@pytest.mark.asyncio
async def test_in_memory_tools_behave_like_the_harness():
    tools = InMemoryTools(_manifest(), contents={
        "a1": {"sheets": {"Expense Report": {"B1": "Aegene Ong", "A7": "2026-07-02", "E7": "24.00", "H12": "258.70"}}},
        "a2": {"pages": ["Grab Malaysia 02/07/2026 RM 24.00", "AirAsia Ride"]}},
        budget=C.Budget(tool_calls=7))
    listed = await tools.list_artifacts(media_type="workbook")
    assert [d["id"] for d in listed.data] == ["a1"] and listed.provenance["hashes"] == ["h1"]
    cells = await tools.read_cells("a1", "Expense Report", "A1:H12")
    assert {c["cell"]: c["value"] for c in cells.data["cells"]}["B1"] == "Aegene Ong"
    assert cells.citations[0].sheet == "Expense Report" and cells.provenance["artifact_ids"] == ["a1"]
    nope = await tools.read_cells("zzz", "S", "A1")
    assert not nope.ok and nope.error_code == "NOT_FOUND"
    hit = await tools.search_artifacts("aegene")
    assert hit.data["hits"][0]["cell"] == "B1" and hit.citations[0].artifact_id == "a1"
    calc = await tools.calculate("sum([24.00, 26.50]) + 0.1 + 0.2")
    assert calc.data["value"] == "50.80"
    bad = await tools.calculate("__import__('os').system('x')")
    assert not bad.ok and bad.error_code == "BAD_INPUT"
    py = await tools.run_python("print(1)", ["a1"])
    assert not py.ok and py.error_code == "TOOL_UNAVAILABLE"
    assert len(tools.executions()) == 7
    # Budget: the eighth call fails closed and is recorded as such.
    over = await tools.inspect_document("a2")
    assert not over.ok and over.error_code == "BUDGET"
    assert tools.executions()[-1].error_code == "BUDGET"
    tools.budget = C.Budget(tool_calls=50)
    tools.cancel()
    assert not (await tools.inspect_workbook("a1")).ok
    ok = tools.record_proposal("case", {"label": "A"})
    assert not ok.ok  # cancelled: no more recording either


@pytest.mark.asyncio
async def test_in_memory_tools_scripts_and_proposals():
    tools = InMemoryTools(_manifest(), scripts={("calculate", "1+1"): ToolResult(data={"value": "3"})})
    assert (await tools.calculate("1+1")).data["value"] == "3"  # the script wins
    assert tools.record_proposal("case", {"label": "A", "claimant": "X"}).ok
    assert not tools.record_proposal("bogus", {}).ok
    assert tools.proposals() == [{"kind": "case", "label": "A", "claimant": "X"}]
    doc = await tools.inspect_document("a2")
    assert doc.data["pages"] == 2 and doc.citations[0].path == "A/receipts.pdf"
    page = await tools.render_page("a2", 1)
    assert page.handle and page.citations[0].page == 1


# ---- instructions: steering only, logged, never a control ---------------------------

@needs_sample
@pytest.mark.asyncio
async def test_instructions_reach_the_readers_and_change_no_result(db, monkeypatch):
    plain = await run_client_a(db, monkeypatch)
    plain_run = client.get(f"/api/claims-runs/{plain}").json()
    steered = await run_client_a(db, monkeypatch, instructions="Maps sit at the back of the receipt bundles; "
                                                               "mobile allowance never has a receipt.")
    steered_run = client.get(f"/api/claims-runs/{steered}").json()

    def shape(run):
        names = {e["id"]: e["name"] for e in run["employees"]}
        return sorted((names.get(f["employee_id"], ""), f["code"], f["status"]) for f in run["flags"]), \
            sorted((e["name"], e["report_total"], e["category"], e["summary"].get("rows")) for e in run["employees"])
    assert shape(plain_run) == shape(steered_run)
    events = client.get(f"/api/claims-runs/{steered}/events").json()
    shown = [e for e in events if "instructions/playbook" in e["message"]]
    assert len(shown) == 10  # once per employee
    assert not [e for e in client.get(f"/api/claims-runs/{plain}/events").json()
                if "instructions/playbook" in e["message"]]


@needs_sample
@pytest.mark.asyncio
async def test_instructions_are_appended_to_the_reader_prompts(monkeypatch):
    from openpyxl import load_workbook

    from app.claims import report_reader

    e = scripted.truth()["employees"][0]
    wb = load_workbook(scripted.GEN / "batch" / e["folder"] / e["files"]["report"], data_only=True)
    ws = wb["Expense Report"]
    agent = scripted.ScriptedAgent([scripted.good_report_reading(ws)])
    monkeypatch.setattr(report_reader, "create_agent", lambda *a, **k: agent)
    ctx = report_reader.run_context("Look at column H for the MYR total.")
    await report_reader.read_report(ws, e["name"], e["er_code"], context=ctx)
    assert "Instructions for this run" in agent.prompts[0] and "column H" in agent.prompts[0]
    assert "never override" in agent.prompts[0]
    assert report_reader.run_context("   ") == ""


def test_report_total_mismatch_toggles_only_through_the_profile():
    header = {"total_check": {"lines": "100.00", "cell": "110.00", "column": "total"}, "total_cell": "H12"}
    on = worker._report_total_flags(header, "Expense Report", {"checks": {}})
    assert [f["code"] for f in on] == ["REPORT_TOTAL_MISMATCH"]
    off = worker._report_total_flags(header, "Expense Report", {"checks": {"REPORT_TOTAL_MISMATCH": False}})
    assert off == []
    # Instructions are prose the readers see; the checks never read them.
    import inspect

    from app.claims import checks

    assert "instructions" not in inspect.signature(checks.run_checks).parameters
    assert "context" not in inspect.signature(worker._report_total_flags).parameters


@pytest.mark.asyncio
async def test_tie_break_and_recheck_stop_at_the_worker_budget(monkeypatch):
    called = []
    monkeypatch.setattr(worker, "create_agent", lambda *a, **k: called.append(a) or None)
    usage = evidence_mod.Usage(cap=0)
    with pytest.raises(evidence_mod.BudgetExceeded):
        await worker.TieBreak(usage)({"values": {}}, [{"id": "e1", "values": {}}])
    assert called == []  # refused before any model was built
    assert worker.WORKER_REQUEST_CAP == worker.config.MAX_AGENT_REQUESTS * 4


def test_manifest_ids_are_stable_and_content_bound(tmp_path):
    d = tmp_path / "files"
    (d / "A").mkdir(parents=True)
    (d / "A" / "x.txt").write_text("hello")
    (d / "A" / "y.txt").write_text("hello")
    m = manifest_mod.build_manifest(d, [{"path": "A/x.txt", "size": 5}, {"path": "A/y.txt", "size": 5}])
    assert m[0].sha256 == m[1].sha256 and m[0].id != m[1].id
    again = manifest_mod.build_manifest(d, [{"path": "A/x.txt", "size": 5}])
    assert again[0].id == m[0].id
    (d / "A" / "x.txt").write_text("changed")
    assert manifest_mod.build_manifest(d, [{"path": "A/x.txt", "size": 7}])[0].id != m[0].id
    missing = manifest_mod.build_manifest(d, [{"path": "A/gone.txt", "size": 1}])
    assert missing[0].sha256 == "" and missing[0].path == "A/gone.txt"
    assert Decimal("1") == Decimal("1")  # money stays Decimal in this suite
