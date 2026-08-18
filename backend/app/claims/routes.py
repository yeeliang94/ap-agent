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
from . import profile as profile_mod
from . import runner
from .models import ClaimEmployee, ClaimEvidence, ClaimFlag, ClaimRow, ClaimsRun

router = APIRouter(prefix="/claims-runs")
log = logging.getLogger("claims.routes")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_INSTRUCTIONS = 4000


def _local_mode() -> bool:
    return os.getenv("DOC_SOURCE", "local").lower() != "mcp"


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
    zip_bytes = await batch.read() if batch is not None and batch.filename else b""
    listing_bytes = await listing.read() if listing is not None and listing.filename else b""
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
            # The human gate, enforced server-side: no output leaves while
            # any flag is undecided.
            "outputs": run.outputs if (run.status == "ready" and not open_flags) else {},
        }
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
        problems = mapping.validate_confirmed_map(new_map, run.survey)
        if problems:
            raise HTTPException(400, "The map is not ready to confirm: " + "; ".join(problems))
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
        # Status changes HERE, in the same commit as the confirmation, so
        # the screen sees "verifying" at once and a restart before the
        # workers start is reconciled as an interrupted run.
        run.status = "verifying"
        run.progress = {"done": 0, "total": 0}
        # One employee record per confirmed employee folder.
        db.query(ClaimEmployee).filter(ClaimEmployee.run_id == run_id).delete()
        n = 0
        for e in clean["employees"]:
            if not e.get("is_employee"):
                continue
            roles = {
                "report_file": e.get("report_file"), "report_tab": e.get("report_tab"),
                "mileage_tab": e.get("mileage_tab"), "no_report": bool(e.get("no_report")),
                "receipt_files": [f["path"] for f in e.get("files", []) if f["role"] == "receipts"],
                "ignored": [f["path"] for f in e.get("files", []) if f["role"] == "ignore"],
                "unplaced": [f["path"] for f in e.get("files", []) if f["role"] == "unplaced"],
            }
            db.add(ClaimEmployee(run_id=run_id, folder=e["folder"], name=e.get("name", ""),
                                 er_code=e.get("er_code", ""), roles=roles,
                                 status="skipped" if e.get("skip") else "pending",
                                 error="skipped by the reviewer at the map" if e.get("skip") else ""))
            n += 1
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


# ---- serialisation -------------------------------------------------------

def _employee_dict(e: ClaimEmployee) -> dict:
    return {"id": e.id, "folder": e.folder, "name": e.name, "er_code": e.er_code,
            "roles": e.roles, "status": e.status, "error": e.error,
            "report_total": e.report_total, "category": e.category, "gl": e.gl,
            "category_basis": e.category_basis, "summary": e.summary}


def _row_dict(r: ClaimRow) -> dict:
    return {"id": r.id, "employee_id": r.employee_id, "kind": r.kind, "sheet": r.sheet,
            "row": r.row, "values": r.values, "corrections": r.corrections,
            "matched_evidence_id": r.matched_evidence_id, "verdict": r.verdict}


def _evidence_dict(e: ClaimEvidence) -> dict:
    return {"id": e.id, "employee_id": e.employee_id, "kind": e.kind, "file": e.file,
            "page": e.page, "position": e.position, "values": e.values,
            "confidence": e.confidence, "matched_row_id": e.matched_row_id}


def _flag_dict(f: ClaimFlag) -> dict:
    return {"id": f.id, "employee_id": f.employee_id, "row_id": f.row_id,
            "evidence_id": f.evidence_id, "code": f.code, "reason": f.reason,
            "basis": f.basis, "cite": f.cite, "status": f.status, "resolution": f.resolution}


def _tallies(db, run_ids: list[str]) -> dict[str, dict]:
    if not run_ids:
        return {}
    tally = {rid: {"employees": 0, "open_flags": 0, "errors": 0, "warnings": 0,
                   "employees_done": 0} for rid in run_ids}
    for rid, n in (db.query(ClaimEmployee.run_id, func.count(ClaimEmployee.id))
                   .filter(ClaimEmployee.run_id.in_(run_ids)).group_by(ClaimEmployee.run_id)):
        tally[rid]["employees"] = n
    for rid, n in (db.query(ClaimEmployee.run_id, func.count(ClaimEmployee.id))
                   .filter(ClaimEmployee.run_id.in_(run_ids),
                           ClaimEmployee.status.in_(("verified", "failed", "skipped")))
                   .group_by(ClaimEmployee.run_id)):
        tally[rid]["employees_done"] = n
    for rid, n in (db.query(ClaimFlag.run_id, func.count(ClaimFlag.id))
                   .filter(ClaimFlag.run_id.in_(run_ids), ClaimFlag.status == "open")
                   .group_by(ClaimFlag.run_id)):
        tally[rid]["open_flags"] = n
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
            "employees": counts.get("employees") or n_map,
            "employees_done": counts.get("employees_done", 0),
            "open_flags": counts.get("open_flags", 0),
            "errors": counts.get("errors", 0), "warnings": counts.get("warnings", 0),
            "created_at": run.created_at.isoformat()}
