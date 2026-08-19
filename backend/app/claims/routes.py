"""API routes for claims runs: /api/claims-runs.

Start a run, watch it, confirm the map, review flags, fetch the output.
Everything a reviewer does here is audited (AuditEvent) and everything the
system does is in the run diary (RunEvent, via telemetry).
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import func

from .. import settings_store, telemetry
from ..db import SessionLocal
from ..models import AuditEvent, RunEvent
from .. import config
from . import cases as cases_mod
from . import profile as profile_mod
from . import runner
from .models import (ClaimCase, ClaimEmployee, ClaimEvidence, ClaimEvidenceAssignment, ClaimFlag,
                     ClaimInvestigation, ClaimRow, ClaimSourceArtifact, ClaimToolExecution, ClaimsRun)

router = APIRouter(prefix="/claims-runs")
log = logging.getLogger("claims.routes")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_INSTRUCTIONS = 4000


def _local_mode() -> bool:
    return os.getenv("DOC_SOURCE", "local").lower() != "mcp"


async def _read_upload(upload: UploadFile | None, max_mb: int, what: str) -> bytes:
    """The upload's bytes, read in chunks and refused as soon as it passes
    the limit — never the whole body into memory first."""
    if upload is None or not upload.filename:
        return b""
    limit = max_mb * 1024 * 1024
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, f"{what} is over the {max_mb} MB limit.")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("")
async def create_claims_run(
    received_date: str = Form(...),
    folder_url: str = Form(""),
    listing_url: str = Form(""),
    instructions: str = Form(""),
    batch: UploadFile | None = File(None),
    listing: UploadFile | None = File(None),
) -> dict:
    """Start a claims run.

    A SharePoint folder link (the folder that CONTAINS the employee
    subfolders) plus a link to the month's listing workbook; or, for local
    development, a zip of the folder tree plus the listing workbook file.
    The received date goes on every listing row. Instructions are the
    optional paragraph for this client.
    """
    received_date = received_date.strip()
    if not DATE_RE.match(received_date):
        raise HTTPException(400, "Received date must be YYYY-MM-DD.")
    folder_url = (folder_url or "").strip()
    listing_url = (listing_url or "").strip()
    instructions = (instructions or "").strip()
    if len(instructions) > MAX_INSTRUCTIONS:
        raise HTTPException(400, f"Instructions are too long (max {MAX_INSTRUCTIONS} characters).")
    from . import source as source_mod

    zip_bytes = await _read_upload(batch, source_mod.MAX_ZIP_MB, "The zip")
    listing_bytes = await _read_upload(listing, source_mod.MAX_FILE_MB, "The listing file")
    if not zip_bytes and not folder_url:
        raise HTTPException(400, "Give the batch folder link, or upload a zip of the folder.")
    if zip_bytes and folder_url:
        raise HTTPException(400, "Give either the folder link or a zip, not both.")
    if folder_url and not folder_url.startswith("https://") and not (
            _local_mode() and Path(folder_url).expanduser().is_dir()):
        raise HTTPException(400, "The folder link must start with https:// — copy it from "
                                 "the browser's address bar."
                                 + (" (In local mode a folder path on this machine also works.)"
                                    if _local_mode() else ""))
    if listing_url and not listing_url.startswith("https://") and not (
            _local_mode() and Path(listing_url).expanduser().is_file()):
        raise HTTPException(400, "The listing link must start with https://.")
    if listing_bytes and listing_url:
        raise HTTPException(400, "Give either the listing link or a listing file, not both.")
    if listing_bytes and not (listing.filename or "").lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "The listing must be an Excel workbook (.xlsx).")

    client = settings_store.get_setting("client_name")
    db = SessionLocal()
    try:
        run = ClaimsRun(client=client, folder_url=folder_url, listing_url=listing_url,
                        received_date=received_date, instructions=instructions,
                        snapshot=profile_mod.snapshot(client))
        db.add(run)
        db.commit()
        ws = runner.workspace_for(run.id)
        ws.mkdir(parents=True, exist_ok=True)
        if zip_bytes:
            (ws / "upload.zip").write_bytes(zip_bytes)
        if listing_bytes:
            (ws / "listing.xlsx").write_bytes(listing_bytes)
        db.add(AuditEvent(run_id=run.id, actor="reviewer", action="claims_run_started",
                          detail=f"client {client}; "
                                 + (f"folder {folder_url}" if folder_url else "zip upload")
                                 + f"; received date {received_date}"
                                 + (f"; instructions: {instructions[:200]}" if instructions else "")))
        db.commit()
        runner.start_background(runner.process_run(run.id))
        return {"run_id": run.id}
    finally:
        db.close()


@router.get("")
def list_claims_runs() -> list[dict]:
    db = SessionLocal()
    try:
        runs = db.query(ClaimsRun).order_by(ClaimsRun.created_at.desc()).all()
        tallies = _tallies(db, [r.id for r in runs])
        return [_summary(r, tallies.get(r.id, {})) for r in runs]
    finally:
        db.close()


@router.get("/{run_id}")
def get_claims_run(run_id: str) -> dict:
    db = SessionLocal()
    try:
        run = db.get(ClaimsRun, run_id)
        if not run:
            raise HTTPException(404, "No such claims run.")
        employees = db.query(ClaimEmployee).filter(ClaimEmployee.run_id == run_id).all()
        flags = db.query(ClaimFlag).filter(ClaimFlag.run_id == run_id).all()
        rows = db.query(ClaimRow).filter(ClaimRow.run_id == run_id).all()
        evidence = db.query(ClaimEvidence).filter(ClaimEvidence.run_id == run_id).all()
        open_flags = [f for f in flags if f.status == "open"]
        return {
            **_summary(run, _tallies(db, [run_id]).get(run_id, {})),
            "folder_url": run.folder_url, "listing_url": run.listing_url,
            "received_date": run.received_date, "instructions": run.instructions,
            "survey": run.survey, "map": run.map, "map_warnings": run.map_warnings,
            "listing_headers": run.listing_headers,
            "employees": [_employee_dict(e) for e in employees],
            "rows": [_row_dict(r) for r in rows],
            "evidence": [_evidence_dict(e) for e in evidence],
            "flags": [_flag_dict(f) for f in flags],
            # The words for every code on screen (title, meaning, what to do).
            "catalogue": _catalogue_payload()["codes"],
            "revision": run.revision or 0,
            # The case model (H2): cases, artifacts, assignments and the
            # investigation record. Hidden while CLAIMS_CASE_MODEL is off
            # (the employee fields above stay authoritative for the UI).
            **(_case_model_payload(db, run_id) if config.CLAIMS_CASE_MODEL else {}),
            # The human gate, enforced server-side: no output leaves while
            # any flag is undecided. Built fresh from the reviewed state
            # (code only, Decimal) and kept on the run for the record.
            "outputs": _outputs_if_unlocked(db, run, open_flags),
            "output_blockers": output_blockers(db, run, open_flags),
        }
    finally:
        db.close()


def _case_model_payload(db, run_id: str) -> dict:
    cases = db.query(ClaimCase).filter(ClaimCase.run_id == run_id).order_by(ClaimCase.label).all()
    artifacts = db.query(ClaimSourceArtifact).filter(ClaimSourceArtifact.run_id == run_id) \
        .order_by(ClaimSourceArtifact.path).all()
    assignments = db.query(ClaimEvidenceAssignment).filter(ClaimEvidenceAssignment.run_id == run_id).all()
    inv = db.query(ClaimInvestigation).filter(ClaimInvestigation.run_id == run_id, ClaimInvestigation.status != "shadow") \
        .order_by(ClaimInvestigation.created_at.desc(), ClaimInvestigation.id.desc()).first()
    shadow = db.query(ClaimInvestigation).filter(ClaimInvestigation.run_id == run_id, ClaimInvestigation.status == "shadow") \
        .order_by(ClaimInvestigation.created_at.desc(), ClaimInvestigation.id.desc()).first()
    tool_counts: dict[str, dict] = {}
    for tool, err, n in (db.query(ClaimToolExecution.tool, ClaimToolExecution.error_code, func.count(ClaimToolExecution.id))
                         .filter(ClaimToolExecution.run_id == run_id)
                         .group_by(ClaimToolExecution.tool, ClaimToolExecution.error_code)):
        t = tool_counts.setdefault(tool, {"calls": 0, "failed": 0})
        t["calls"] += n
        if err:
            t["failed"] += n
    from . import grouping

    return {"cases": [cases_mod.case_dict(c) for c in cases],
            "grouping": grouping.gate(db, db.get(ClaimsRun, run_id)),
            "artifacts": [cases_mod.artifact_dict(a) for a in artifacts],
            "assignments": [cases_mod.assignment_dict(a) for a in assignments],
            "investigation": cases_mod.investigation_dict(inv),
            "shadow_investigation": cases_mod.investigation_dict(shadow),
            "tool_summary": tool_counts,
            "artifact_counts": {"total": len(artifacts),
                                "unresolved": sum(1 for a in artifacts if a.disposition == "unresolved"),
                                "needs_review": sum(1 for a in artifacts if a.needs_confirmation)}}


def output_blockers(db, run: ClaimsRun, open_flags: list) -> list[str]:
    """Universal control 11 (H9), server-side: what keeps the Payment
    Listing locked. Named, so the screen can show it, and enforced here
    regardless of what a screen asks for."""
    why: list[str] = []
    if run.status != "ready":
        why.append(f"the run is {run.status}, not ready")
    if open_flags:
        codes: dict[str, int] = {}
        for f in open_flags:
            codes[f.code] = codes.get(f.code, 0) + 1
        why.append("open flags: " + ", ".join(f"{c} ×{n}" for c, n in sorted(codes.items())))
    for c in db.query(ClaimCase).filter(ClaimCase.run_id == run.id):
        if c.state != "excluded" and c.status == "verified" and c.claimant_state != "confirmed":
            why.append(f"case {c.label}: claimant {c.claimant_state} — set the claimant on the case")
    for a in db.query(ClaimSourceArtifact).filter(ClaimSourceArtifact.run_id == run.id,
                                                  ClaimSourceArtifact.disposition == "unresolved"):
        if not any(f.code == "ARTIFACT_UNRESOLVED" and f.artifact_id == a.artifact_id for f in open_flags):
            why.append(f"{a.path}: no disposition")
    return why


def _outputs_if_unlocked(db, run: ClaimsRun, open_flags: list) -> dict:
    from . import listing as listing_mod

    if output_blockers(db, run, open_flags):
        return {}
    outputs = listing_mod.build_outputs(db, run)
    if outputs != run.outputs:
        run.outputs = outputs
        db.commit()
        if not outputs["totals"]["match"]:
            telemetry.record(db, run.id, "output", telemetry.WARNING, "RECONCILIATION_MISMATCH",
                             f"Emitted total {outputs['totals']['total_myr']} differs from the source "
                             f"total {outputs['totals']['source_total']} by {outputs['totals']['difference']}.")
    return outputs


@router.get("/{run_id}/replay")
def get_replay_bundle(run_id: str, verify: bool = False) -> dict:
    """The replay bundle (H11): manifest, versions, plan, tool hashes and
    calculations, decisions, cases, lines, flags and the final output —
    and, with ?verify=1, the verifier's report re-deriving the money."""
    from . import replay

    db = SessionLocal()
    try:
        run = db.get(ClaimsRun, run_id)
        if not run:
            raise HTTPException(404, "No such claims run.")
        if verify:
            return replay.verify_bundle(db, run)
        return replay.build_bundle(db, run)
    finally:
        db.close()


@router.post("/{run_id}/cancel")
def cancel_claims_run(run_id: str, body: dict | None = None) -> dict:
    """Stop a run that is still working (H11): outstanding tool calls are
    cancelled, the workers stop at their next step, and the run is marked
    failed with the reason — nothing partial becomes 'ready'. A resting
    run (map_ready / ready) cannot be cancelled: there is nothing running."""
    from .investigator import investigator as agentic
    from .models import IN_PROGRESS_STATUSES

    db = SessionLocal()
    try:
        run = db.get(ClaimsRun, run_id)
        if not run:
            raise HTTPException(404, "No such claims run.")
        if run.status not in IN_PROGRESS_STATUSES:
            raise HTTPException(400, f"Only a run that is still working can be cancelled (this one is {run.status}).")
        _revision_check(run, body or {})
        cancelled_tools = agentic.cancel_run(run_id)
        run.status, run.error = "failed", "cancelled by the reviewer"
        cases_mod.bump_revision(run)
        db.add(AuditEvent(run_id=run_id, actor="reviewer", action="run_cancelled",
                          detail="cancelled while " + (run.progress or {}).get("what", "working")))
        db.commit()
        telemetry.record(db, run_id, "run", telemetry.WARNING, "RUN_CANCELLED",
                         "Run cancelled by the reviewer" + ("; outstanding tool calls stopped." if cancelled_tools else "."))
        return {"ok": True, "status": "failed", "tools_cancelled": cancelled_tools}
    finally:
        db.close()


@router.get("/{run_id}/events")
def get_claims_run_events(run_id: str, level: str = "") -> list[dict]:
    db = SessionLocal()
    try:
        if not db.get(ClaimsRun, run_id):
            raise HTTPException(404, "No such claims run.")
        query = db.query(RunEvent).filter(RunEvent.run_id == run_id)
        if level == "problems":
            query = query.filter(RunEvent.level.in_(("warning", "error")))
        return [{"id": e.id, "at": e.at.isoformat(), "stage": e.stage, "level": e.level,
                 "code": e.code, "message": e.message, "detail": e.detail}
                for e in query.order_by(RunEvent.id).all()]
    finally:
        db.close()


@router.get("/{run_id}/audit")
def get_claims_run_audit(run_id: str) -> list[dict]:
    db = SessionLocal()
    try:
        if not db.get(ClaimsRun, run_id):
            raise HTTPException(404, "No such claims run.")
        events = db.query(AuditEvent).filter(AuditEvent.run_id == run_id).order_by(AuditEvent.id).all()
        return [{"id": e.id, "at": e.at.isoformat(), "actor": e.actor, "action": e.action,
                 "detail": e.detail} for e in events]
    finally:
        db.close()


@router.post("/{run_id}/confirm-map")
async def confirm_map(run_id: str, body: dict) -> dict:
    """Save the reviewer's (possibly corrected) map, audit what changed,
    remember it as the client's last confirmed map, start verification.

    body = {"map": {...same shape as run.map...},
            "remember": [{"pattern": "*_Approval.pdf", "role": "ignore"}, ...]}
    """
    from . import mapping

    new_map = body.get("map")
    if not isinstance(new_map, dict) or not isinstance(new_map.get("employees"), list):
        raise HTTPException(400, "map must hold an 'employees' list.")
    remember = body.get("remember") or []
    db = SessionLocal()
    try:
        run = db.get(ClaimsRun, run_id)
        if not run:
            raise HTTPException(404, "No such claims run.")
        if run.status != "map_ready":
            raise HTTPException(400, f"The map can only be confirmed while the run is waiting "
                                     f"at the map (it is {run.status}).")
        _revision_check(run, body)
        problems = mapping.validate_confirmed_map(new_map, run.survey)
        if problems:
            raise HTTPException(400, "The map is not ready to confirm: " + "; ".join(problems))
        from . import source as source_mod

        n_cases = sum(1 for e in new_map["employees"] if e.get("is_employee") and not e.get("skip"))
        if n_cases > source_mod.MAX_CASES_PER_RUN:
            raise HTTPException(400, f"{n_cases} cases to verify — more than the {source_mod.MAX_CASES_PER_RUN} "
                                     "one run may hold. Skip some at the map and run them in a second batch.")
        changes = _map_changes(run.map, new_map)
        # File-role patterns the reviewer ticked "remember" on go into the
        # client profile — the map AI is shown them next time and code
        # applies them over its guess.
        client = (run.snapshot or {}).get("client_name") or run.client
        if remember:
            profile = profile_mod.get_profile(client)
            patterns = list(profile.get("file_role_patterns") or [])
            for r in remember:
                pattern, role = str(r.get("pattern", "")).strip(), str(r.get("role", "")).strip()
                if pattern and role in mapping.ROLES and \
                        not any(p["pattern"] == pattern and p["role"] == role for p in patterns):
                    patterns.append({"pattern": pattern, "role": role})
            profile["file_role_patterns"] = patterns
            profile_mod.save_profile(client, profile, evidence=f"map correction on run {run_id}")
        clean = {"employees": new_map["employees"], "root_files": new_map.get("root_files", []),
                 "notes": new_map.get("notes", []), "rounds": run.map.get("rounds"),
                 "confirmed": True}
        run.map = clean
        # The case model is the source of truth (H6): the edited map is
        # stored as the (reviewer-corrected) proposal, replacing the AI's,
        # and the one grouping gate confirms it — cases and claimants
        # confirmed, one ClaimEmployee per case for the delivered worker,
        # status verifying in the same commit.
        from . import manifest as manifest_mod
        from .investigator import contracts as C
        from .investigator import legacy

        request = C.InvestigationRequest(run_id=run_id, workspace=str(runner.workspace_for(run_id)),
                                         manifest=manifest_mod.from_dicts(run.manifest or []),
                                         instructions=run.instructions or "", profile_snapshot=run.snapshot or {})
        cases_mod.store_result(db, run, legacy.from_map(request, clean, confirmed=False), confirmed=False,
                               replace_cases=True, record=False)
        try:
            n, _gate = cases_mod.confirm_grouping(db, run)
        except cases_mod.GroupingError as exc:
            db.rollback()
            raise HTTPException(400, str(exc))
        db.add(AuditEvent(run_id=run_id, actor="reviewer", action="map_confirmed",
                          detail=(f"{n} employee(s); " + ("; ".join(changes) if changes
                                  else "no changes to the proposed map"))[:2000]))
        db.commit()
        profile_mod.save_last_map(client, clean, run_id)
        telemetry.record(db, run_id, "map", telemetry.INFO, "MAP_CONFIRMED",
                         f"Map confirmed by the reviewer with {len(changes)} change(s); "
                         f"{n} employee(s) to verify.")
        runner.start_background(runner.start_verification(run_id))
        return {"ok": True, "employees": n, "changes": changes}
    finally:
        db.close()


def _map_changes(old: dict, new: dict) -> list[str]:
    """Plain sentences describing what the reviewer changed."""
    changes = []
    old_by = {e["folder"]: e for e in old.get("employees", [])}
    for e in new.get("employees", []):
        o = old_by.get(e["folder"])
        if not o:
            changes.append(f"{e['folder']}: added")
            continue
        for key in ("is_employee", "name", "er_code", "report_file", "report_tab",
                    "mileage_tab", "no_report", "skip"):
            if (o.get(key) or None) != (e.get(key) or None):
                changes.append(f"{e['folder']}: {key} {o.get(key)!r} -> {e.get(key)!r}")
        old_roles = {f["path"]: f["role"] for f in o.get("files", [])}
        for f in e.get("files", []):
            if old_roles.get(f["path"]) != f["role"]:
                changes.append(f"{f['path']}: {old_roles.get(f['path'])!r} -> {f['role']!r}")
    return changes


@router.get("/{run_id}/file")
def get_claims_file(run_id: str, path: str, page: int = 1, highlight: str = "",
                    full: bool = False):
    """A page of one of the run's files as a PNG (or the file itself for a
    workbook). `highlight` = left/middle/right shades the named third of the
    page so a receipt's cited position is obvious. `full` renders at full
    resolution (map pages)."""
    from .evidence import render_page

    db = SessionLocal()
    try:
        run = db.get(ClaimsRun, run_id)
        if not run:
            raise HTTPException(404, "No such claims run.")
    finally:
        db.close()
    base = runner.files_dir(run_id)
    target = (base / path).resolve()
    if base.resolve() not in target.parents or not target.is_file():
        raise HTTPException(404, "No such file in this run.")
    if target.suffix.lower() in (".xlsx", ".xlsm"):
        return FileResponse(target, filename=target.name)
    try:
        png = render_page(target, page, highlight=highlight, full=full)
    except IndexError:
        raise HTTPException(404, "No such page.")
    return Response(content=png, media_type="image/png")


# ---- Map & Group actions (H6) -------------------------------------------------------
# Every mutation takes the revision the screen last saw (expected_revision);
# a stale one is refused with 409 so two browser actions never overwrite
# each other. Each action is audited, refreshes the grouping controls
# (conflicts, unknown claimants, case roles) and bumps the revision.

def _revision_check(run: ClaimsRun, body: dict) -> None:
    expected = body.get("expected_revision")
    if expected is None:
        return
    try:
        expected = int(expected)
    except (TypeError, ValueError):
        raise HTTPException(400, "expected_revision must be a number.")
    if expected != int(run.revision or 0):
        raise HTTPException(409, f"This run changed since your screen loaded (revision {run.revision}, you had "
                                 f"{expected}). Reload and try again.")


def _grouping_action(run_id: str, body: dict, action: str, fn, statuses: tuple = ("map_ready",)):
    """Common frame for a Map & Group action: run at the map (or, for the
    claimant, at review too), revision checked, fn(db, run) applied,
    controls refreshed, audited, committed; returns the gate and the new
    revision."""
    from . import grouping

    db = SessionLocal()
    try:
        run = db.get(ClaimsRun, run_id)
        if not run:
            raise HTTPException(404, "No such claims run.")
        if run.status not in statuses:
            raise HTTPException(400, f"This can be changed while the run is {' or '.join(statuses)} (it is {run.status}).")
        _revision_check(run, body)
        try:
            detail = fn(db, run)
        except cases_mod.GroupingError as exc:
            db.rollback()
            raise HTTPException(400, str(exc))
        gate = grouping.refresh(db, run)
        cases_mod.bump_revision(run)
        db.add(AuditEvent(run_id=run_id, actor="reviewer", action=action, detail=str(detail)[:2000]))
        db.commit()
        return {"ok": True, "revision": run.revision, "grouping": gate}
    finally:
        db.close()


@router.post("/{run_id}/cases")
def create_case(run_id: str, body: dict) -> dict:
    """body = {label, artifact_ids: [...], expected_revision}"""
    ids = [str(a) for a in (body.get("artifact_ids") or [])]

    def fn(db, run):
        c = cases_mod.create_case(db, run, str(body.get("label", "")), ids)
        return f"case {c.label} created with {len(ids)} file(s)"
    return _grouping_action(run_id, body, "case_created", fn)


@router.put("/{run_id}/cases/{case_id}")
def update_case(run_id: str, case_id: str, body: dict) -> dict:
    """body = {label?, roles?: {report_file, report_tab, mileage_tab, no_report}, state?: excluded|proposed, expected_revision}"""
    def fn(db, run):
        c = cases_mod.update_case(db, run, case_id, body.get("label"), body.get("roles"), body.get("state"))
        return f"case {c.label}: " + ", ".join(k for k in ("label", "roles", "state") if body.get(k) is not None)
    return _grouping_action(run_id, body, "case_updated", fn)


@router.put("/{run_id}/cases/{case_id}/claimant")
def set_case_claimant(run_id: str, case_id: str, body: dict) -> dict:
    """body = {name, identifier, expected_revision} — or {confirm: true} to
    confirm the proposed name as it stands."""
    def fn(db, run):
        if body.get("confirm"):
            c = cases_mod.confirm_claimant(db, run, case_id)
            detail = f"case {c.label}: claimant {c.claimant_name!r} confirmed"
        else:
            c = cases_mod.set_claimant(db, run, case_id, str(body.get("name", "")), str(body.get("identifier", "")))
            detail = f"case {c.label}: claimant set to {c.claimant_name!r} {c.claimant_identifier!r}"
        # At review time the case already has its employee record: keep the
        # delivered fields in step and withdraw the output for a rebuild.
        if c.legacy_employee_id:
            emp = db.get(ClaimEmployee, c.legacy_employee_id)
            if emp is not None:
                emp.name, emp.er_code = c.claimant_name, c.claimant_identifier
        run.outputs = {}
        return detail
    return _grouping_action(run_id, body, "claimant_set", fn, statuses=("map_ready", "ready"))


@router.post("/{run_id}/cases/{case_id}/merge")
def merge_case(run_id: str, case_id: str, body: dict) -> dict:
    """body = {into: case_id, expected_revision}"""
    def fn(db, run):
        c = cases_mod.merge_cases(db, run, case_id, str(body.get("into", "")))
        return f"case {case_id} merged into {c.label}"
    return _grouping_action(run_id, body, "cases_merged", fn)


@router.post("/{run_id}/cases/{case_id}/split")
def split_case(run_id: str, case_id: str, body: dict) -> dict:
    """body = {artifact_ids: [...], label, expected_revision}"""
    def fn(db, run):
        c = cases_mod.split_case(db, run, case_id, [str(a) for a in (body.get("artifact_ids") or [])],
                                 str(body.get("label", "")))
        return f"case {case_id}: {len(body.get('artifact_ids') or [])} file(s) split into {c.label}"
    return _grouping_action(run_id, body, "case_split", fn)


@router.post("/{run_id}/artifacts/{artifact_id}/move")
def move_artifact(run_id: str, artifact_id: str, body: dict) -> dict:
    """body = {case_id ("" = out of every case), expected_revision}"""
    def fn(db, run):
        cases_mod.move_artifact(db, run, artifact_id, str(body.get("case_id", "")))
        return f"file {artifact_id} moved to case {body.get('case_id') or '(none)'}"
    return _grouping_action(run_id, body, "artifact_moved", fn)


ARTIFACT_ROLES = ("report", "receipts", "approval", "report_copy", "listing", "roster", "policy", "other", "unknown")


@router.put("/{run_id}/artifacts/{artifact_id}/role")
def set_artifact_role(run_id: str, artifact_id: str, body: dict) -> dict:
    """The reviewer says what a file IS (its role); with remember=true the
    file-name pattern → role goes into the client profile (the delivered
    'remember for <client>'). body = {role, remember?, expected_revision}"""
    role = str(body.get("role", "")).strip()
    if role not in ARTIFACT_ROLES:
        raise HTTPException(400, f"role must be one of {', '.join(ARTIFACT_ROLES)}.")

    def fn(db, run):
        art = cases_mod._artifact(db, run.id, artifact_id)
        art.proposed_role, art.role_reason = role, "set by the reviewer at the map"
        if role in ("approval", "report_copy", "listing", "roster", "policy") and art.disposition == "unresolved":
            art.disposition, art.disposition_by, art.needs_confirmation = "irrelevant", "reviewer", 0
            art.disposition_reason = f"a {role.replace('_', ' ')}: nothing to verify in it"
        if body.get("remember"):
            from . import mapping

            legacy_role = {"report": "report", "receipts": "receipts"}.get(role, "ignore")
            pattern = _pattern_for(art.path)
            client = (run.snapshot or {}).get("client_name") or run.client
            prof = profile_mod.get_profile(client)
            pats = list(prof.get("file_role_patterns") or [])
            if legacy_role in mapping.ROLES and not any(p["pattern"] == pattern and p["role"] == legacy_role for p in pats):
                pats.append({"pattern": pattern, "role": legacy_role})
                prof["file_role_patterns"] = pats
                profile_mod.save_profile(client, prof, evidence=f"map correction on run {run.id}")
        return f"file {art.path}: role {role}" + (" (remembered)" if body.get("remember") else "")
    return _grouping_action(run_id, body, "artifact_role_set", fn)


def _pattern_for(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    i = name.rfind("_")
    return "*" + name[i:] if i > 0 else name


@router.post("/{run_id}/confirm-grouping")
async def confirm_grouping(run_id: str, body: dict) -> dict:
    """The one primary action of Map & Group: confirm the grouping and
    start verification. body = {expected_revision}. Refused (400) with the
    reasons while the gate is shut."""
    db = SessionLocal()
    try:
        run = db.get(ClaimsRun, run_id)
        if not run:
            raise HTTPException(404, "No such claims run.")
        if run.status != "map_ready":
            raise HTTPException(400, f"The grouping can be confirmed while the run waits at the map (it is {run.status}).")
        _revision_check(run, body)
        try:
            n, gate = cases_mod.confirm_grouping(db, run)
        except cases_mod.GroupingError as exc:
            db.rollback()
            raise HTTPException(400, str(exc))
        run.map = {**(run.map or {}), "confirmed": True}
        db.add(AuditEvent(run_id=run_id, actor="reviewer", action="grouping_confirmed",
                          detail=f"{n} case(s) to verify; {gate['counts']}"[:2000]))
        db.commit()
        telemetry.record(db, run_id, "map", telemetry.INFO, "GROUPING_CONFIRMED",
                         f"Grouping confirmed by the reviewer; {n} case(s) to verify.")
        runner.start_background(runner.start_verification(run_id))
        return {"ok": True, "cases": n, "revision": run.revision}
    finally:
        db.close()


# ---- review actions -----------------------------------------------------------

@router.post("/{run_id}/employees/{employee_id}/retry")
async def retry_employee(run_id: str, employee_id: str, body: dict | None = None) -> dict:
    """Re-run one worker: Retry on a failed employee, or Re-verify."""
    from . import worker

    db = SessionLocal()
    try:
        run = db.get(ClaimsRun, run_id)
        emp = db.get(ClaimEmployee, employee_id)
        if not run or not emp or emp.run_id != run_id:
            raise HTTPException(404, "No such employee in this run.")
        _revision_check(run, body or {})
        if run.status not in ("ready", "verifying"):
            raise HTTPException(400, f"Employees can be re-verified once the run is verifying or ready (it is {run.status}).")
        if emp.status == "verifying":
            raise HTTPException(400, "This employee is being verified right now.")
        if emp.status == "pending" and run.status == "verifying":
            raise HTTPException(400, "This employee is already queued; the run will get to them.")
        emp.status, emp.error = "pending", ""
        cases_mod.sync_case_from_employee(db, emp)
        cases_mod.bump_revision(run)
        db.add(AuditEvent(run_id=run_id, actor="reviewer", action="employee_reverify",
                          detail=f"{emp.name or emp.folder}: re-verify requested"))
        db.commit()
    finally:
        db.close()
    runner.start_background(worker.retry_employee(run_id, employee_id))
    return {"ok": True}


@router.post("/{run_id}/cases/{case_id}/retry")
async def retry_case(run_id: str, case_id: str, body: dict | None = None) -> dict:
    """Re-run one case's worker (H7): the case-keyed twin of the employee retry."""
    db = SessionLocal()
    try:
        case = db.get(ClaimCase, case_id)
        if not case or case.run_id != run_id or not case.legacy_employee_id:
            raise HTTPException(404, "No such case in this run.")
        emp_id = case.legacy_employee_id
    finally:
        db.close()
    return await retry_employee(run_id, emp_id, body)


@router.post("/{run_id}/flags/{flag_id}/decide")
async def decide_claim_flag(run_id: str, flag_id: str, body: dict) -> dict:
    """Record a decision on one flag. body = {decision, note}.

    accepted  — it is a real problem: the flag's ROW is excluded from the
                batch (an employee-level or run-level flag is acknowledged)
    dismissed — the flag is set aside with a note; the row stays
    """
    decision = body.get("decision")
    if decision not in ("accepted", "dismissed"):
        raise HTTPException(400, "decision must be 'accepted' or 'dismissed'.")
    note = str(body.get("note", "")).strip()[:500]
    if decision == "dismissed" and not note:
        raise HTTPException(400, "A short note is required when dismissing a flag — it goes in the audit trail.")
    db = SessionLocal()
    try:
        flag = db.get(ClaimFlag, flag_id)
        if not flag or flag.run_id != run_id:
            raise HTTPException(404, "No such flag.")
        if flag.status not in ("open", "info"):
            raise HTTPException(400, f"This flag is already {flag.status}.")
        _revision_check(db.get(ClaimsRun, run_id), body)
        if flag.code in ("CLAIMANT_UNKNOWN", "OWNERSHIP_CONFLICT"):
            raise HTTPException(400, "This is settled by an action, not a note: set or confirm the claimant on the case "
                                     "(CLAIMANT_UNKNOWN), or split / move the files at the map (OWNERSHIP_CONFLICT).")
        if flag.code == "ARTIFACT_UNRESOLVED":
            # A file is not "dismissed": it gets a disposition, which is
            # what releases the control (H3). The decision route accepts
            # the disposition inline so the flag card can offer it.
            disposition = str(body.get("disposition", "")).strip()
            if disposition not in ("irrelevant", "unreadable", "duplicate"):
                raise HTTPException(400, "Say what this file is: disposition must be irrelevant, unreadable "
                                         "or duplicate (with a note) — that is what settles an unplaced file.")
            _set_disposition(db, run_id, flag.artifact_id, disposition, note or decision, actor="reviewer")
            db.commit()
            return {"ok": True, "disposition": disposition}
        flag.status, flag.resolution = decision, note
        run = db.get(ClaimsRun, run_id)
        run.outputs = {}  # withdrawn; rebuilt from the reviewed state on next read
        cases_mod.bump_revision(run)
        db.add(AuditEvent(run_id=run_id, actor="reviewer", action=f"flag_{decision}",
                          detail=f"[{flag.code}] {flag.reason[:200]} — note: {note or 'none'}"))
        db.commit()
        return {"ok": True}
    finally:
        db.close()


DISPOSITIONS = ("used", "duplicate", "irrelevant", "unreadable")


def _set_disposition(db, run_id: str, artifact_id: str, disposition: str, reason: str, actor: str) -> ClaimSourceArtifact:
    """The reviewer settles a Source Artifact (H3): its disposition is
    recorded as theirs (never overwritten by an adapter afterwards), the
    open ARTIFACT_UNRESOLVED flag for it is resolved, the output is
    withdrawn for a rebuild, and the change is audited."""
    art = db.query(ClaimSourceArtifact).filter(ClaimSourceArtifact.run_id == run_id,
                                               ClaimSourceArtifact.artifact_id == artifact_id).first()
    if art is None:
        raise HTTPException(404, "No such file in this run.")
    if disposition not in DISPOSITIONS:
        raise HTTPException(400, f"disposition must be one of {', '.join(DISPOSITIONS)}.")
    if disposition == "used" and not art.case_id:
        raise HTTPException(400, "A file is 'used' only inside a case — move it into a case at the map first.")
    if disposition != "used" and not reason.strip():
        raise HTTPException(400, "A short reason is required — it goes in the audit trail.")
    old = art.disposition
    art.disposition, art.disposition_reason, art.disposition_by = disposition, reason.strip()[:400], actor
    art.needs_confirmation = 0
    for fl in db.query(ClaimFlag).filter(ClaimFlag.run_id == run_id, ClaimFlag.code == "ARTIFACT_UNRESOLVED",
                                          ClaimFlag.artifact_id == artifact_id, ClaimFlag.status.in_(("open", "info"))):
        fl.status, fl.resolution = "resolved_by_correction", f"file marked {disposition} — {reason.strip() or 'no reason given'}"
    run = db.get(ClaimsRun, run_id)
    run.outputs = {}
    cases_mod.bump_revision(run)
    db.add(AuditEvent(run_id=run_id, actor=actor, action="artifact_disposition",
                      detail=f"{art.path}: {old} -> {disposition} — {reason.strip() or 'no reason given'}"[:2000]))
    return art


@router.post("/{run_id}/artifacts/{artifact_id}/disposition")
def set_artifact_disposition(run_id: str, artifact_id: str, body: dict) -> dict:
    """The reviewer says what a file is. body = {disposition, reason}."""
    db = SessionLocal()
    try:
        run = db.get(ClaimsRun, run_id)
        if not run:
            raise HTTPException(404, "No such claims run.")
        if run.status not in ("map_ready", "verifying", "ready"):
            raise HTTPException(400, f"Files can be settled once the map is proposed (the run is {run.status}).")
        art = _set_disposition(db, run_id, artifact_id, str(body.get("disposition", "")).strip(),
                               str(body.get("reason", "")).strip(), actor="reviewer")
        db.commit()
        return {"ok": True, "artifact": cases_mod.artifact_dict(art)}
    finally:
        db.close()


CORRECTABLE_ROW_FIELDS = {"date", "item", "reason", "receipt_included", "amount", "currency", "rate",
                          "total", "km"}


def _validate_row_value(field: str, value):
    from decimal import Decimal, InvalidOperation

    from ..schemas_ai import CURRENCY_PATTERN, DATE_PATTERN
    from .report_reader import gl_of, item_name

    text = str(value).strip()
    if field in ("amount", "total", "rate", "km"):
        try:
            d = Decimal(text)
            if not d.is_finite() or d < 0:
                raise InvalidOperation
        except InvalidOperation:
            raise HTTPException(400, f"{field} must be a number.")
        return str(d.quantize(Decimal("0.01"))) if field in ("amount", "total") else str(d)
    if field == "date":
        if not re.match(DATE_PATTERN, text):
            raise HTTPException(400, "Date must be YYYY-MM-DD.")
        return text
    if field == "currency":
        if not re.match(CURRENCY_PATTERN, text.upper()):
            raise HTTPException(400, "Currency must be a 3-letter code, e.g. MYR.")
        return text.upper()
    if field == "receipt_included":
        if text.upper()[:1] not in ("Y", "N", ""):
            raise HTTPException(400, "Receipt included must be Y or N.")
        return text.upper()[:1]
    return text[:300]


@router.post("/{run_id}/rows/{row_id}/correct")
async def correct_claim_row(run_id: str, row_id: str, body: dict) -> dict:
    """Fix a value on one row (audited), then re-check that employee at once:
    flags that no longer apply resolve themselves, new ones are raised,
    decided ones stay decided. body = {fields: {name: value}, reason}."""
    from . import worker
    from .report_reader import gl_of, item_name

    reason = str(body.get("reason", "")).strip()
    if not reason:
        raise HTTPException(400, "A short reason is required — it goes in the audit trail.")
    submitted = body.get("fields")
    if not isinstance(submitted, dict) or not submitted:
        raise HTTPException(400, "fields must map field names to corrected values.")
    db = SessionLocal()
    try:
        row = db.get(ClaimRow, row_id)
        run = db.get(ClaimsRun, run_id)
        if not row or not run or row.run_id != run_id:
            raise HTTPException(404, "No such row.")
        if run.status != "ready":
            raise HTTPException(400, "Corrections are possible once the run is ready.")
        _revision_check(run, body)
        emp = db.get(ClaimEmployee, row.employee_id)
        validated = {}
        for field, raw in submitted.items():
            if field not in CORRECTABLE_ROW_FIELDS:
                raise HTTPException(400, f"Field {field!r} cannot be corrected.")
            validated[field] = _validate_row_value(field, raw)
        changed = {f: v for f, v in validated.items() if str(row.values.get(f) or "") != v}
        if changed:
            values = {**row.values, **changed}
            if "item" in changed:
                values["item_name"], values["gl"] = item_name(changed["item"]), gl_of(changed["item"])
            if "amount" in changed and (values.get("currency") or "MYR") == "MYR" and "total" not in changed:
                values["total"] = changed["amount"]
            corrections = dict(row.corrections or {})
            for f, v in changed.items():
                corrections[f] = {"from": row.values.get(f), "to": v, "reason": reason}
                db.add(AuditEvent(run_id=run_id, actor="reviewer", action="row_corrected",
                                  detail=f"{emp.name or emp.folder} {row.sheet} row {row.row}.{f}: "
                                         f"{row.values.get(f)!r} -> {v!r} — {reason}"))
            row.values, row.corrections = values, corrections
            run.outputs = {}
            db.commit()
        # Instant re-check for this employee — code only over stored
        # evidence (plus, at most, an AI tie-break).
        profile = profile_mod.profile_of(run.snapshot)
        summary = emp.summary or {}
        result = await worker.run_checks_for(db, run, emp, profile,
                                             searched=(int(summary.get("pages", 0)), len(emp.roles.get("receipt_files") or [])))
        existing = db.query(ClaimFlag).filter(ClaimFlag.employee_id == emp.id).all()
        new_keys = {(f["code"], f["row_id"], f["evidence_id"]): f for f in result["flags"]}
        open_now = {}
        for fl in existing:
            key = (fl.code, fl.row_id, fl.evidence_id)
            if fl.status in ("open", "info") and key not in new_keys:
                fl.status = "resolved_by_correction"
                fl.resolution = f"No longer applies after correcting {', '.join(sorted(changed)) or 'values'} — {reason}"
            elif fl.status in ("open", "info"):
                open_now[key] = fl
        decided = {(fl.code, fl.row_id, fl.evidence_id) for fl in existing
                   if fl.status in ("accepted", "dismissed") and fl.row_id != row_id}
        for key, fd in new_keys.items():
            if key in open_now:
                open_now[key].reason, open_now[key].basis, open_now[key].cite = fd["reason"], fd["basis"], fd["cite"]
            elif key not in decided:
                db.add(ClaimFlag(run_id=run_id, employee_id=emp.id, case_id=cases_mod.case_id_for_employee(db, emp.id), **fd))
        cases_mod.bump_revision(run)
        # The run-wide controls see the corrected values too (H7): a receipt
        # that stops being shared resolves; one that starts being shared is raised.
        worker.rerun_global_controls(db, run_id, profile)
        db.add(AuditEvent(run_id=run_id, actor="system", action="employee_rechecked",
                          detail=f"{emp.name or emp.folder} after correcting "
                                 f"{', '.join(sorted(changed)) or 'nothing (retry)'}: "
                                 f"{len(result['flags'])} rule(s) now apply"))
        db.commit()
        return {"ok": True, "flags_now": len(result["flags"])}
    finally:
        db.close()


@router.put("/{run_id}/employees/{employee_id}/category")
def set_employee_category(run_id: str, employee_id: str, body: dict) -> dict:
    """The reviewer sets the listing category (CATEGORY_UNCLEAR, or a
    correction). Audited; the open CATEGORY_UNCLEAR flag is resolved."""
    category = str(body.get("category", "")).strip()[:80]
    gl = str(body.get("gl", "")).strip()[:20]
    reason = str(body.get("reason", "")).strip()[:300]
    if not category:
        raise HTTPException(400, "Choose a category.")
    db = SessionLocal()
    try:
        emp = db.get(ClaimEmployee, employee_id)
        run = db.get(ClaimsRun, run_id)
        if not emp or not run or emp.run_id != run_id:
            raise HTTPException(404, "No such employee in this run.")
        _revision_check(run, body)
        old = (emp.category, emp.gl)
        emp.category, emp.gl = category, gl
        emp.category_basis = f"set by the reviewer: {reason or 'no reason given'}"
        for fl in db.query(ClaimFlag).filter(ClaimFlag.employee_id == emp.id, ClaimFlag.code == "CATEGORY_UNCLEAR",
                                              ClaimFlag.status == "open"):
            fl.status, fl.resolution = "resolved_by_correction", f"category set to {category} — {reason}"
        db.add(AuditEvent(run_id=run_id, actor="reviewer", action="category_set",
                          detail=f"{emp.name or emp.folder}: {old!r} -> {(category, gl)!r} — {reason}"))
        run.outputs = {}
        cases_mod.sync_case_from_employee(db, emp)
        cases_mod.bump_revision(run)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ---- serialisation -------------------------------------------------------

def _employee_dict(e: ClaimEmployee) -> dict:
    return {"id": e.id, "folder": e.folder, "name": e.name, "er_code": e.er_code,
            "roles": e.roles, "status": e.status, "error": e.error,
            "report_total": e.report_total, "category": e.category, "gl": e.gl,
            "category_basis": e.category_basis, "summary": e.summary}


def _row_dict(r: ClaimRow) -> dict:
    # origin (H7): where the amount comes from — reported (read from a claim
    # summary), evidence_derived (built from a receipt), reviewer_entered
    # (a value the reviewer corrected).
    origin = "evidence_derived" if r.kind == "derived" else "reported"
    if r.corrections:
        origin = "reviewer_entered"
    return {"id": r.id, "employee_id": r.employee_id, "case_id": r.case_id, "origin": origin, "kind": r.kind, "sheet": r.sheet,
            "row": r.row, "values": r.values, "corrections": r.corrections,
            "matched_evidence_id": r.matched_evidence_id, "verdict": r.verdict}


def _evidence_dict(e: ClaimEvidence) -> dict:
    return {"id": e.id, "employee_id": e.employee_id, "case_id": e.case_id, "kind": e.kind, "file": e.file,
            "page": e.page, "position": e.position, "values": e.values,
            "confidence": e.confidence, "matched_row_id": e.matched_row_id}


def _flag_dict(f: ClaimFlag) -> dict:
    return {"id": f.id, "employee_id": f.employee_id, "case_id": f.case_id, "row_id": f.row_id,
            "evidence_id": f.evidence_id, "artifact_id": f.artifact_id, "code": f.code, "reason": f.reason,
            "basis": f.basis, "cite": f.cite, "status": f.status, "resolution": f.resolution}


def _tallies(db, run_ids: list[str]) -> dict[str, dict]:
    if not run_ids:
        return {}
    tally = {rid: {"employees": 0, "open_flags": 0, "notes": 0, "errors": 0, "warnings": 0,
                   "employees_done": 0} for rid in run_ids}
    for rid, n in (db.query(ClaimEmployee.run_id, func.count(ClaimEmployee.id))
                   .filter(ClaimEmployee.run_id.in_(run_ids)).group_by(ClaimEmployee.run_id)):
        tally[rid]["employees"] = n
    for rid, n in (db.query(ClaimEmployee.run_id, func.count(ClaimEmployee.id))
                   .filter(ClaimEmployee.run_id.in_(run_ids),
                           ClaimEmployee.status.in_(("verified", "failed", "skipped")))
                   .group_by(ClaimEmployee.run_id)):
        tally[rid]["employees_done"] = n
    for rid, status, n in (db.query(ClaimFlag.run_id, ClaimFlag.status, func.count(ClaimFlag.id))
                           .filter(ClaimFlag.run_id.in_(run_ids), ClaimFlag.status.in_(("open", "info")))
                           .group_by(ClaimFlag.run_id, ClaimFlag.status)):
        tally[rid]["open_flags" if status == "open" else "notes"] = n
    for rid, level, n in (db.query(RunEvent.run_id, RunEvent.level, func.count(RunEvent.id))
                          .filter(RunEvent.run_id.in_(run_ids),
                                  RunEvent.level.in_(("warning", "error")))
                          .group_by(RunEvent.run_id, RunEvent.level)):
        tally[rid]["errors" if level == "error" else "warnings"] = n
    return tally


def _summary(run: ClaimsRun, counts: dict) -> dict:
    n_map = sum(1 for e in (run.map or {}).get("employees", []) if e.get("is_employee"))
    return {"id": run.id, "client": run.client, "status": run.status, "error": run.error,
            "progress": run.progress, "folder": run.folder_url or "zip upload",
            "employee_count": counts.get("employees") or n_map,
            "employees_done": counts.get("employees_done", 0),
            "open_flags": counts.get("open_flags", 0), "notes": counts.get("notes", 0),
            "errors": counts.get("errors", 0), "warnings": counts.get("warnings", 0),
            "created_at": run.created_at.isoformat()}


# ---- per-client steering: /api/claims-settings ------------------------------
# The few values code needs (rates, receipt-optional items, tolerances), the
# playbook paragraph, and the last confirmed map. Stored per client name.

settings_router = APIRouter(prefix="/claims-settings")


def _settings_payload(client: str) -> dict:
    return {"client": client, "local_mode": _local_mode(),
            "profile": profile_mod.get_profile(client),
            "playbook": profile_mod.get_playbook(client),
            "last_map": profile_mod.get_last_map(client)}


@settings_router.get("")
def get_claims_settings() -> dict:
    return _settings_payload(settings_store.get_setting("client_name"))


def _catalogue_payload() -> dict:
    return {"codes": {code: profile_mod.describe(code) for code in profile_mod.CATALOGUE},
            "kinds": list(profile_mod.FLAG_KINDS),
            "toggleable": list(profile_mod.CHECK_CODES)}


@settings_router.get("/catalogue")
def get_flag_catalogue() -> dict:
    """Every flag code with its title, meaning, what to do, kind and default
    — the words the Review screen and the Settings toggles show."""
    return _catalogue_payload()


@settings_router.put("")
def update_claims_settings(body: dict) -> dict:
    """body = {profile?: {...fields...}, playbook?: str, forget_last_map?: bool}.
    Every value is validated before any is saved; the change is audited."""
    from decimal import Decimal, InvalidOperation

    client = settings_store.get_setting("client_name")
    profile_in = body.get("profile")
    if profile_in is not None:
        if not isinstance(profile_in, dict):
            raise HTTPException(400, "profile must be an object.")
        current = profile_mod.get_profile(client)
        merged = {**current}
        if "mileage_rates" in profile_in:
            rates = profile_in["mileage_rates"]
            if not isinstance(rates, dict):
                raise HTTPException(400, "mileage_rates must map vehicle type to a rate.")
            clean = {}
            for vehicle, rate in rates.items():
                vehicle = str(vehicle).strip()
                try:
                    value = Decimal(str(rate).strip())
                    if not value.is_finite() or value <= 0 or value > 100:
                        raise InvalidOperation
                except InvalidOperation:
                    raise HTTPException(400, f"Rate for {vehicle!r} must be a number per km, e.g. 0.64.")
                if vehicle:
                    clean[vehicle] = f"{value.normalize():f}"
            merged["mileage_rates"] = clean
        if "km_tolerance" in profile_in:
            try:
                tol = Decimal(str(profile_in["km_tolerance"]).strip() or "0")
                if not tol.is_finite() or tol < 0 or tol > 100:
                    raise InvalidOperation
            except InvalidOperation:
                raise HTTPException(400, "km tolerance must be a number of km, e.g. 0 or 0.5.")
            merged["km_tolerance"] = f"{tol.normalize():f}"
        if "receipt_date_window_days" in profile_in:
            try:
                days = int(profile_in["receipt_date_window_days"])
            except (TypeError, ValueError):
                raise HTTPException(400, "receipt date window must be a whole number of days.")
            if days < 0 or days > 31:
                raise HTTPException(400, "receipt date window must be between 0 and 31 days.")
            merged["receipt_date_window_days"] = days
        for key in ("receipt_optional_items",):
            if key in profile_in:
                items = profile_in[key]
                if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
                    raise HTTPException(400, f"{key} must be a list of expense item names.")
                merged[key] = [i.strip() for i in items if i.strip()][:200]
        if "unclaimed_receipt_threshold" in profile_in:
            try:
                thr = Decimal(str(profile_in["unclaimed_receipt_threshold"]).strip() or "0")
                if not thr.is_finite() or thr < 0 or thr > 1_000_000:
                    raise InvalidOperation
            except InvalidOperation:
                raise HTTPException(400, "unclaimed receipt threshold must be an amount in MYR, e.g. 100.")
            merged["unclaimed_receipt_threshold"] = f"{thr.normalize():f}"
        if "mileage_item_pattern" in profile_in:
            pat = str(profile_in["mileage_item_pattern"]).strip()
            if not pat or len(pat) > 60:
                raise HTTPException(400, "mileage item pattern must be 1–60 characters.")
            merged["mileage_item_pattern"] = pat
        if "category_rule" in profile_in:
            merged["category_rule"] = str(profile_in["category_rule"]).strip()[:1000]
        if "categories" in profile_in:
            cats = profile_in["categories"]
            if not isinstance(cats, list):
                raise HTTPException(400, "categories must be a list of {item, gl}.")
            merged["categories"] = [{"item": str(c.get("item", "")).strip()[:80],
                                     "gl": str(c.get("gl", "")).strip()[:20]}
                                    for c in cats if isinstance(c, dict) and c.get("item")][:300]
        if "file_role_patterns" in profile_in:
            pats = profile_in["file_role_patterns"]
            if not isinstance(pats, list):
                raise HTTPException(400, "file_role_patterns must be a list of {pattern, role}.")
            from . import mapping

            merged["file_role_patterns"] = [
                {"pattern": str(p.get("pattern", "")).strip()[:120], "role": str(p.get("role", ""))}
                for p in pats if isinstance(p, dict) and p.get("pattern")
                and p.get("role") in mapping.ROLES][:100]
        if "listing_columns" in profile_in:
            from . import listing as listing_mod

            pins = profile_in["listing_columns"]
            if not isinstance(pins, dict):
                raise HTTPException(400, "listing_columns must map a header text to a role, 'blank' or '=text'.")
            clean_pins = {}
            for k, v in pins.items():
                k, v = str(k).strip()[:80], str(v).strip()[:120]
                if not k:
                    continue
                if not (v == "blank" or v.startswith("=") or v in listing_mod.ROLES):
                    raise HTTPException(400, f"listing column {k!r}: {v!r} must be a role "
                                             f"({', '.join(listing_mod.ROLES)}), 'blank', or '=text'.")
                clean_pins[k] = v
            merged["listing_columns"] = clean_pins
        if "checks" in profile_in:
            checks = profile_in["checks"]
            if not isinstance(checks, dict):
                raise HTTPException(400, "checks must map a check code to on/off.")
            merged["checks"] = {str(k): bool(v) for k, v in checks.items()
                                if str(k) in profile_mod.CHECK_CODES}
        profile_mod.save_profile(client, merged)
    if "playbook" in body:
        text = body["playbook"]
        if not isinstance(text, str) or len(text) > MAX_INSTRUCTIONS:
            raise HTTPException(400, f"The playbook must be text, at most {MAX_INSTRUCTIONS} characters.")
        profile_mod.save_playbook(client, text.strip())
    if body.get("forget_last_map"):
        profile_mod.forget_last_map(client)
    return _settings_payload(client)
