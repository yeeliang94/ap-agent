"""API routes for claims runs: /api/claims-runs.

Start a run, watch it, confirm the map, review flags, fetch the output.
Everything a reviewer does here is audited (AuditEvent) and everything the
system does is in the run diary (RunEvent, via telemetry).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from sqlalchemy import func

from .. import settings_store, switches, telemetry
from ..db import SessionLocal
from ..models import AuditEvent, RunEvent
from .. import config
from . import cases as cases_mod
from . import profile as profile_mod
from . import runner
from .review_gate import (bump_revision as _bump_revision, output_blockers, outputs_if_unlocked,
                          pending_retry as _pending_retry, set_disposition as _set_disposition,
                          store_outputs)
from . import schemas as S
from .investigator.contracts import IGNORABLE_ROLES, REVIEWER_DISPOSITIONS
from .models import (ClaimCase, ClaimEmployee, ClaimEvidence, ClaimEvidenceAssignment, ClaimFlag,
                     ClaimInvestigation, ClaimRow, ClaimSourceArtifact, ClaimToolExecution, ClaimsRun)

router = APIRouter(prefix="/claims-runs")
log = logging.getLogger("claims.routes")

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_INSTRUCTIONS = 4000


def _local_mode() -> bool:
    return config.DOC_SOURCE != "mcp"


async def _read_upload(upload: UploadFile | None, max_mb: int, what: str) -> bytes:
    """The upload's bytes, read a megabyte at a time and refused the moment
    it passes the limit, so at most `max_mb` is ever held — the body is
    never taken in whole first and only then measured."""
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


def _local_ingestion_path(value: str, kind: str) -> bool:
    """Is `value` a filesystem path this server may ingest from?

    A path on this machine stands in for a SharePoint link only in local
    mode AND only under the one tree an operator named
    (CLAIMS_LOCAL_ROOT). Without that root any folder the process can read
    could be copied into a run workspace and put on screen by anyone who
    can reach the API, so the answer is a plain no and the zip upload
    stays the local way in."""
    if not _local_mode():
        return False
    root = config.local_ingestion_root()
    if root is None:
        return False
    try:
        target = Path(value).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    if target != root and root not in target.parents:
        return False
    return target.is_dir() if kind == "dir" else target.is_file()


def _clean_rel_path(raw: str, filename: str) -> str:
    """A browser-reported relative path, made safe for the staging area.

    Backslashes become slashes (a Windows browser), empty and '.' segments
    are dropped, and anything absolute or escaping ('..', a drive letter)
    is refused — the fallback is the bare filename, never a guessed tree.
    """
    value = (raw or "").replace("\\", "/").strip()
    if not value:
        return Path(filename or "file").name
    parts = [p for p in value.split("/") if p not in ("", ".")]
    if not parts or value.startswith("/") or any(p == ".." for p in parts) \
            or any(":" in p for p in parts):
        raise HTTPException(400, f"The file path {raw!r} is not a plain relative path — "
                                 "re-pick the folder and try the upload again.")
    return "/".join(parts)


@router.post("")
async def create_claims_run(
    received_date: str = Form(...),
    folder_url: str = Form(""),
    listing_url: str = Form(""),
    instructions: str = Form(""),
    batch: list[UploadFile] = File(default=[]),
    batch_paths: str = Form(""),
    listing: UploadFile | None = File(None),
) -> dict:
    """Start a claims run.

    The primary way in: uploaded files — a folder picked in the browser
    (batch_paths carries each file's relative path, as JSON), a single zip
    of the folder tree, or loose files of the readable types. The optional
    listing workbook comes as a file too. A SharePoint folder link (the
    folder that CONTAINS the employee subfolders) plus a listing link is
    the alternative while the SharePoint source switch is on. The received
    date goes on every listing row. Instructions are the optional
    paragraph for this client.
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

    uploads = [u for u in (batch or []) if u and u.filename]
    names = [u.filename for u in uploads]
    zips = [n for n in names if n.lower().endswith(".zip")]
    if zips and len(uploads) > 1:
        raise HTTPException(400, "Upload the zip on its own, or the files without a zip.")
    for name in names:
        if name.lower().endswith(".zip"):
            continue
        if Path(name).suffix.lower() not in source_mod.READABLE:
            raise HTTPException(400, f"{Path(name).name} isn't a supported type. A batch may hold "
                                     "PDF, PNG, JPG, WEBP or Excel files — or one zip of the folder.")
    try:
        paths_list = json.loads(batch_paths) if batch_paths.strip() else []
        if not isinstance(paths_list, list) or not all(isinstance(p, str) for p in paths_list):
            raise ValueError
    except ValueError:
        raise HTTPException(400, "The upload's file paths did not come through as a list "
                                 "(batch_paths) — reload the page and try the upload again.")
    if paths_list and len(paths_list) != len(uploads):
        raise HTTPException(400, "The upload's file paths (batch_paths) must name every "
                                 "uploaded file, in order — reload the page and try again.")

    zip_bytes = b""
    batch_files: list[tuple[str, bytes]] = []
    if zips:
        zip_bytes = await _read_upload(uploads[0], source_mod.MAX_ZIP_MB, "The zip")
    else:
        total = 0
        for i, upload in enumerate(uploads):
            rel = _clean_rel_path(paths_list[i] if paths_list else "", upload.filename)
            data = await _read_upload(upload, source_mod.MAX_FILE_MB, Path(upload.filename).name)
            total += len(data)
            if total > source_mod.MAX_ZIP_MB * 1024 * 1024:
                raise HTTPException(413, f"The files add up to more than the "
                                         f"{source_mod.MAX_ZIP_MB} MB limit per upload.")
            batch_files.append((rel, data))
    listing_bytes = await _read_upload(listing, source_mod.MAX_FILE_MB, "The listing file")
    if not zip_bytes and not batch_files and not folder_url:
        raise HTTPException(400, "Upload the batch (a folder, a zip, or files), "
                                 "or give the batch folder link.")
    if (zip_bytes or batch_files) and folder_url:
        raise HTTPException(400, "Give either the folder link or an upload, not both.")
    if (folder_url.startswith("https://") or listing_url.startswith("https://")) \
            and not switches.on("claims_sharepoint_source"):
        raise HTTPException(400, "Starting from a SharePoint link is switched off — flip "
                                 "'SharePoint source' in Settings, or upload the files instead.")
    if folder_url and not folder_url.startswith("https://") and not _local_ingestion_path(folder_url, "dir"):
        raise HTTPException(400, "The folder link must start with https:// — copy it from "
                                 "the browser's address bar."
                                 + (" (In local mode a folder path under CLAIMS_LOCAL_ROOT also works.)"
                                    if _local_mode() else ""))
    if listing_url and not listing_url.startswith("https://") and not _local_ingestion_path(listing_url, "file"):
        raise HTTPException(400, "The listing link must start with https://.")
    if listing_bytes and listing_url:
        raise HTTPException(400, "Give either the listing link or a listing file, not both.")
    if listing_bytes and not (listing.filename or "").lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "The listing must be an Excel workbook (.xlsx).")

    client = settings_store.get_setting("client_name")
    # Creating the run is a workbook-sized file write plus several
    # synchronous database round trips. This handler is async — it has to
    # be, to read the upload — so that work goes to a thread instead of
    # stalling the one event loop every other request and every background
    # stage shares.
    run_id = await asyncio.to_thread(_store_new_run, client, folder_url, listing_url, received_date,
                                     instructions, zip_bytes, batch_files, listing_bytes)
    runner.start_background(runner.process_run(run_id))
    return {"run_id": run_id}


def _store_new_run(client: str, folder_url: str, listing_url: str, received_date: str,
                   instructions: str, zip_bytes: bytes,
                   batch_files: list[tuple[str, bytes]], listing_bytes: bytes) -> str:
    """The blocking half of starting a run: the row, the workspace and the
    uploaded bytes. Runs in a worker thread (see create_claims_run)."""
    source_label = (folder_url or ("zip upload" if zip_bytes else "file upload"))
    db = SessionLocal()
    try:
        run = ClaimsRun(client=client, folder_url=folder_url, listing_url=listing_url,
                        received_date=received_date, instructions=instructions,
                        snapshot={**profile_mod.snapshot(client), "source": source_label,
                                  # A run keeps the switches it started with.
                                  "switches": switches.snapshot()})
        db.add(run)
        db.commit()
        ws = runner.workspace_for(run.id)
        ws.mkdir(parents=True, exist_ok=True)
        if zip_bytes:
            (ws / "upload.zip").write_bytes(zip_bytes)
        for rel, data in batch_files:
            target = (ws / "upload" / rel).resolve()
            staging = (ws / "upload").resolve()
            if target != staging and staging not in target.parents:
                # _clean_rel_path already refused these; belt and braces.
                raise HTTPException(400, f"The file path {rel!r} points outside the upload.")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        if listing_bytes:
            (ws / "listing.xlsx").write_bytes(listing_bytes)
        db.add(AuditEvent(run_id=run.id, actor="reviewer", action="claims_run_started",
                          detail=f"client {client}; "
                                 + (f"folder {folder_url}" if folder_url else
                                    f"{source_label} ({len(batch_files) or 1} file(s))")
                                 + f"; received date {received_date}"
                                 + (f"; instructions: {instructions[:200]}" if instructions else "")))
        db.commit()
        return run.id
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
        # Computed ONCE and handed to both fields: the gate is two passes
        # over the run's cases and artifacts, and the screen polls this
        # route every three seconds.
        blockers = output_blockers(db, run, open_flags)
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
            **(_case_model_payload(db, run_id, evidence) if switches.on("claims_case_model") else {}),
            # The human gate, enforced server-side: no output leaves while
            # any flag is undecided. Built fresh from the reviewed state
            # (code only, Decimal) — and NOT stored: a read does not write.
            # The copy kept on the run is refreshed where the state changes
            # (every review action, and the end of verification).
            "outputs": outputs_if_unlocked(db, run, blockers),
            "output_blockers": blockers,
        }
    finally:
        db.close()


def _case_model_payload(db, run_id: str, evidence: list) -> dict:
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

    run = db.get(ClaimsRun, run_id)
    signals = grouping.signals_for(run, artifacts)
    gate = grouping.gate(db, run)
    # A capability gate, read LIVE (like the case routes): what a reviewer
    # may DO follows today's switch. What the pipeline DID follows the
    # run's snapshot (switches.for_run — see the runner's shadow gate).
    gate["actions_enabled"] = switches.on("claims_full_dump_grouping")
    return {"cases": [cases_mod.case_dict(c) for c in cases],
            "grouping": gate,
            "artifacts": [{**cases_mod.artifact_dict(a), "signals": signals.get(a.artifact_id, [])} for a in artifacts],
            # Evidence Items (the normalized name): the worker's page inventory
            # keyed by artifact, for callers that speak the new contract.
            "evidence_items": [_evidence_item_dict(e, {a.path: a.artifact_id for a in artifacts})
                               for e in evidence],
            "assignments": [cases_mod.assignment_dict(a) for a in assignments],
            "investigation": cases_mod.investigation_dict(inv),
            "shadow_investigation": cases_mod.investigation_dict(shadow),
            "tool_summary": tool_counts,
            "artifact_counts": {"total": len(artifacts),
                                "unresolved": sum(1 for a in artifacts if a.disposition == "unresolved"),
                                "needs_review": sum(1 for a in artifacts if a.needs_confirmation)}}


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
def cancel_claims_run(run_id: str, body: S.RevisionBody | None = None) -> dict:
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
        _revision_check(db, run, body or {}, required=False)
        cancelled_tools = agentic.cancel_run(run_id)
        run.status, run.error = "failed", "cancelled by the reviewer"
        _bump_revision(run)
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
async def confirm_map(run_id: str, body: S.ConfirmMapBody) -> dict:
    """Save the reviewer's (possibly corrected) map, audit what changed,
    remember it as the client's last confirmed map, start verification.

    body = {"map": {...same shape as run.map...},
            "remember": [{"pattern": "*_Approval.pdf", "role": "ignore"}, ...]}
    """
    from . import mapping

    new_map = body.map.model_dump()
    remember = body.remember
    db = SessionLocal()
    try:
        run = db.get(ClaimsRun, run_id)
        if not run:
            raise HTTPException(404, "No such claims run.")
        if run.status != "map_ready":
            raise HTTPException(400, f"The map can only be confirmed while the run is waiting "
                                     f"at the map (it is {run.status}).")
        _revision_check(db, run, body, required=False)
        problems = mapping.validate_confirmed_map(new_map, run.survey)
        if problems:
            raise HTTPException(400, "The map is not ready to confirm: " + "; ".join(problems))
        from . import source as source_mod

        n_cases = sum(1 for e in new_map["employees"] if e.get("is_employee") and not e.get("skip"))
        if n_cases > source_mod.MAX_CASES_PER_RUN:
            raise HTTPException(400, f"{n_cases} cases to verify — more than the {source_mod.MAX_CASES_PER_RUN} "
                                     "one run may hold. Skip some at the map and run them in a second batch.")
        changes = _map_changes(run.map, new_map)
        client = (run.snapshot or {}).get("client_name") or run.client
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
        # File-role patterns the reviewer ticked "remember" on go into the
        # client profile — the map AI is shown them next time and code
        # applies them over its guess. AFTER the commit, in the profile's
        # own transaction: the profile is a different store, and writing it
        # from inside the run's transaction both half-records a refused
        # confirm and deadlocks SQLite's single writer against ourselves.
        for r in remember:
            _remember_file_role(client, r.pattern.strip(), r.role.strip(),
                                f"map correction on run {run_id}")
        profile_mod.save_last_map(client, clean, run_id)
        telemetry.record(db, run_id, "map", telemetry.INFO, "MAP_CONFIRMED",
                         f"Map confirmed by the reviewer with {len(changes)} change(s); "
                         f"{n} employee(s) to verify.")
        runner.start_background(runner.start_verification(run_id))
        return {"ok": True, "employees": n, "changes": changes}
    finally:
        db.close()


def _remember_file_role(client: str, pattern: str, role: str, evidence: str) -> None:
    """Remember "files named like this are <role>" for the client.

    Called only AFTER the run's own transaction has committed. The profile
    lives in another table behind its own session: writing it while the
    run's transaction is open would record a preference for an action that
    may still be refused, and — SQLite having exactly one writer — makes
    the request wait on a lock it is holding itself."""
    from . import mapping

    if not pattern or role not in mapping.ROLES:
        return
    profile = profile_mod.get_profile(client)
    patterns = list(profile.get("file_role_patterns") or [])
    if any(p["pattern"] == pattern and p["role"] == role for p in patterns):
        return
    patterns.append({"pattern": pattern, "role": role})
    profile["file_role_patterns"] = patterns
    profile_mod.save_profile(client, profile, evidence=evidence)


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
    if page < 1:
        raise HTTPException(404, "No such page.")
    try:
        png = render_page(target, page, highlight=highlight, full=full)
    except IndexError:
        # The file is here, that page of it is not.
        raise HTTPException(404, "No such page.")
    except ValueError as exc:
        # A real file of a kind nothing can rasterise (.txt, .msg, .docx).
        # 415, not 500: the request is well formed and the answer is that
        # this type has no page image — the screen offers the download.
        raise HTTPException(415, f"{target.name} cannot be shown as a page image ({exc}).")
    return Response(content=png, media_type="image/png")


# ---- Map & Group actions (H6) -------------------------------------------------------
# Every mutation takes the revision the screen last saw (expected_revision);
# a stale one is refused with 409 so two browser actions never overwrite
# each other. Each action is audited, refreshes the grouping controls
# (conflicts, unknown claimants, case roles) and bumps the revision.

def _case_routes_on() -> None:
    """The case routes exist only while the case-model switch is on (off =
    the employee fields and routes stay authoritative; storage unchanged)."""
    if not switches.on("claims_case_model"):
        raise HTTPException(404, "The case model is switched off on this server.")


def _revision_check(db, run: ClaimsRun, body, required: bool = True) -> None:
    """Every mutation carries the revision the screen last saw, and is
    refused (400) without it, (409) with a stale one. The only routes that
    still accept its absence are the ones no current screen sends it for:
    confirm-map (the delivered MapView, the fallback while CLAIMS_CASE_MODEL
    is off) and cancel (API only).

    COMPARE-AND-SET, not read-compare-write. Reading the revision into
    Python and comparing it there is not a control: two reviewers' requests
    run in separate threadpool threads, both read revision 7, both pass,
    both commit — the second silently overwrites the first. The claim is
    made by the database instead, in one statement:

        UPDATE claims_runs SET revision = revision + 1
         WHERE id = ? AND revision = ?

    which takes the write lock; whoever loses it waits, re-reads and
    matches nothing (0 rows) → 409. Claiming the number here is also what
    makes the check a claim rather than a look, so the later bump is
    `_bump_revision`, which does not add a second one."""
    expected = _expected_revision(body)
    if expected is None:
        if required:
            raise HTTPException(400, "expected_revision is required: send the revision your screen loaded.")
        return
    claimed = (db.query(ClaimsRun)
               .filter(ClaimsRun.id == run.id, func.coalesce(ClaimsRun.revision, 0) == expected)
               .update({"revision": func.coalesce(ClaimsRun.revision, 0) + 1},
                       synchronize_session=False))
    if claimed != 1:
        db.rollback()
        current = db.query(func.coalesce(ClaimsRun.revision, 0)).filter(ClaimsRun.id == run.id).scalar()
        raise HTTPException(409, f"This run changed since your screen loaded (revision {current}, you had "
                                 f"{expected}). Reload and try again.")
    run.revision = expected + 1
    run._revision_claimed = True  # type: ignore[attr-defined]


def _expected_revision(body) -> int | None:
    """The revision the screen sent, as a number — from a request model or
    from a plain dict (the routes that still take one)."""
    expected = body.get("expected_revision") if isinstance(body, dict) else getattr(body, "expected_revision", None)
    if expected is None:
        return None
    try:
        return int(expected)
    except (TypeError, ValueError):
        raise HTTPException(400, "expected_revision must be a number.")


def _grouping_action(run_id: str, body: dict, action: str, fn, statuses: tuple = ("map_ready",)):
    """Common frame for a Map & Group action: run at the map (or, for the
    claimant, at review too), revision checked, fn(db, run) applied,
    controls refreshed, audited, committed; returns the gate and the new
    revision."""
    from . import grouping

    _case_routes_on()
    db = SessionLocal()
    try:
        run = db.get(ClaimsRun, run_id)
        if not run:
            raise HTTPException(404, "No such claims run.")
        if run.status not in statuses:
            raise HTTPException(400, f"This can be changed while the run is {' or '.join(statuses)} (it is {run.status}).")
        _revision_check(db, run, body, required=True)
        try:
            detail = fn(db, run)
        except cases_mod.GroupingError as exc:
            db.rollback()
            raise HTTPException(400, str(exc))
        gate = grouping.refresh(db, run)
        _bump_revision(run)
        db.add(AuditEvent(run_id=run_id, actor="reviewer", action=action, detail=str(detail)[:2000]))
        db.commit()
        if run.status == "ready":
            store_outputs(db, run)   # a claimant confirmed at review may open the gate
        return {"ok": True, "revision": run.revision, "grouping": gate}
    finally:
        db.close()


def _regrouping_on() -> None:
    """Create / merge / split / move / role are the full-dump grouping
    actions: off = a flat folder behaves as today (classified, not
    regrouped on screen); the gate, claimant and dispositions stay.

    The case model comes first: with it switched off these routes do not
    exist at all (404), rather than existing and refusing (400)."""
    _case_routes_on()
    if not switches.on("claims_full_dump_grouping"):
        raise HTTPException(400, "Regrouping at the map is switched off on this server "
                                 "(the full-dump grouping switch in Settings).")


@router.post("/{run_id}/cases")
def create_case(run_id: str, body: S.CreateCaseBody) -> dict:
    """body = {label, artifact_ids: [...], expected_revision}"""
    _regrouping_on()
    ids = list(body.artifact_ids)

    def fn(db, run):
        c = cases_mod.create_case(db, run, body.label, ids)
        return f"case {c.label} created with {len(ids)} file(s)"
    return _grouping_action(run_id, body, "case_created", fn)


@router.put("/{run_id}/cases/{case_id}")
def update_case(run_id: str, case_id: str, body: S.UpdateCaseBody) -> dict:
    """body = {label?, roles?: {report_file, report_tab, mileage_tab, no_report}, state?: excluded|proposed, expected_revision}"""
    def fn(db, run):
        c = cases_mod.update_case(db, run, case_id, body.label, body.roles, body.state)
        return f"case {c.label}: " + ", ".join(k for k in ("label", "roles", "state")
                                               if getattr(body, k) is not None)
    return _grouping_action(run_id, body, "case_updated", fn)


@router.put("/{run_id}/cases/{case_id}/claimant")
def set_case_claimant(run_id: str, case_id: str, body: S.ClaimantBody) -> dict:
    """body = {name, identifier, expected_revision} — or {confirm: true} to
    confirm the proposed name as it stands."""
    def fn(db, run):
        if body.confirm:
            c = cases_mod.confirm_claimant(db, run, case_id)
            detail = f"case {c.label}: claimant {c.claimant_name!r} confirmed"
        else:
            c = cases_mod.set_claimant(db, run, case_id, body.name, body.identifier)
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
def merge_case(run_id: str, case_id: str, body: S.MergeCaseBody) -> dict:
    """body = {into: case_id, expected_revision}"""
    _regrouping_on()

    def fn(db, run):
        c = cases_mod.merge_cases(db, run, case_id, body.into)
        return f"case {case_id} merged into {c.label}"
    return _grouping_action(run_id, body, "cases_merged", fn)


@router.post("/{run_id}/cases/{case_id}/split")
def split_case(run_id: str, case_id: str, body: S.SplitCaseBody) -> dict:
    """body = {artifact_ids: [...], label, expected_revision}"""
    _regrouping_on()

    def fn(db, run):
        c = cases_mod.split_case(db, run, case_id, list(body.artifact_ids), body.label)
        return f"case {case_id}: {len(body.artifact_ids)} file(s) split into {c.label}"
    return _grouping_action(run_id, body, "case_split", fn)


@router.post("/{run_id}/artifacts/{artifact_id}/move")
def move_artifact(run_id: str, artifact_id: str, body: S.MoveArtifactBody) -> dict:
    """body = {case_id ("" = out of every case), expected_revision}"""
    _regrouping_on()

    def fn(db, run):
        cases_mod.move_artifact(db, run, artifact_id, body.case_id)
        return f"file {artifact_id} moved to case {body.case_id or '(none)'}"
    return _grouping_action(run_id, body, "artifact_moved", fn)


ARTIFACT_ROLES = ("report", "receipts", "approval", "report_copy", "listing", "roster", "policy", "other", "unknown")


@router.put("/{run_id}/artifacts/{artifact_id}/role")
def set_artifact_role(run_id: str, artifact_id: str, body: S.ArtifactRoleBody) -> dict:
    """The reviewer says what a file IS (its role); with remember=true the
    file-name pattern → role goes into the client profile (the delivered
    'remember for <client>'). body = {role, remember?, expected_revision}"""
    _regrouping_on()
    role = body.role.strip()
    if role not in ARTIFACT_ROLES:
        raise HTTPException(400, f"role must be one of {', '.join(ARTIFACT_ROLES)}.")
    remember: list[tuple[str, str, str, str]] = []

    def fn(db, run):
        art = cases_mod._artifact(db, run.id, artifact_id)
        art.proposed_role, art.role_reason = role, "set by the reviewer at the map"
        if role in IGNORABLE_ROLES and art.disposition == "unresolved":
            art.disposition, art.disposition_by, art.needs_confirmation = "irrelevant", "reviewer", 0
            art.disposition_reason = f"a {role.replace('_', ' ')}: nothing to verify in it"
        if body.remember:
            # Staged, not written: the client profile is another store and
            # is only touched once this action has committed (see
            # _remember_file_role).
            remember.append(((run.snapshot or {}).get("client_name") or run.client,
                             _pattern_for(art.path),
                             {"report": "report", "receipts": "receipts"}.get(role, "ignore"),
                             f"map correction on run {run.id}"))
        return f"file {art.path}: role {role}" + (" (remembered)" if body.remember else "")
    out = _grouping_action(run_id, body, "artifact_role_set", fn)
    for args in remember:
        _remember_file_role(*args)
    return out


def _pattern_for(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    i = name.rfind("_")
    return "*" + name[i:] if i > 0 else name


@router.post("/{run_id}/confirm-grouping")
async def confirm_grouping(run_id: str, body: S.RevisionBody) -> dict:
    """The one primary action of Map & Group: confirm the grouping and
    start verification. body = {expected_revision}. Refused (400) with the
    reasons while the gate is shut."""
    _case_routes_on()
    db = SessionLocal()
    try:
        run = db.get(ClaimsRun, run_id)
        if not run:
            raise HTTPException(404, "No such claims run.")
        if run.status != "map_ready":
            raise HTTPException(400, f"The grouping can be confirmed while the run waits at the map (it is {run.status}).")
        _revision_check(db, run, body, required=True)
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
async def retry_employee(run_id: str, employee_id: str, body: S.RevisionBody | None = None) -> dict:
    """Re-run one worker: Retry on a failed employee, or Re-verify."""
    from . import worker

    db = SessionLocal()
    try:
        run = db.get(ClaimsRun, run_id)
        emp = db.get(ClaimEmployee, employee_id)
        if not run or not emp or emp.run_id != run_id:
            raise HTTPException(404, "No such employee in this run.")
        _revision_check(db, run, body or {})
        if run.status not in ("ready", "verifying"):
            raise HTTPException(400, f"Employees can be re-verified once the run is verifying or ready (it is {run.status}).")
        if emp.status == "skipped":
            raise HTTPException(400, "This employee was skipped at the map, so there is nothing to verify. "
                                     "Start a new run with them included.")
        if emp.status == "verifying":
            raise HTTPException(400, "This employee is being verified right now.")
        if emp.status == "pending" and run.status == "verifying":
            raise HTTPException(400, "This employee is already queued; the run will get to them.")
        emp.status, emp.error = "pending", ""
        cases_mod.sync_case_from_employee(db, emp)
        _bump_revision(run)
        db.add(AuditEvent(run_id=run_id, actor="reviewer", action="employee_reverify",
                          detail=f"{emp.name or emp.folder}: re-verify requested"))
        db.commit()
    finally:
        db.close()
    runner.start_background(worker.retry_employee(run_id, employee_id))
    return {"ok": True}


@router.post("/{run_id}/cases/{case_id}/retry")
async def retry_case(run_id: str, case_id: str, body: S.RevisionBody | None = None) -> dict:
    """Re-run one case's worker (H7): the case-keyed twin of the employee retry."""
    _case_routes_on()
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
async def decide_claim_flag(run_id: str, flag_id: str, body: S.DecideFlagBody) -> dict:
    """Record a decision on one flag. body = {decision, note}.

    accepted  — it is a real problem: the flag's ROW is excluded from the
                batch (an employee-level or run-level flag is acknowledged)
    dismissed — the flag is set aside with a note; the row stays
    """
    decision = body.decision
    if decision not in ("accepted", "dismissed"):
        raise HTTPException(400, "decision must be 'accepted' or 'dismissed'.")
    note = body.note.strip()[:500]
    if decision == "dismissed" and not note:
        raise HTTPException(400, "A short note is required when dismissing a flag — it goes in the audit trail.")
    db = SessionLocal()
    try:
        flag = db.get(ClaimFlag, flag_id)
        if not flag or flag.run_id != run_id:
            raise HTTPException(404, "No such flag.")
        if flag.status not in ("open", "info"):
            raise HTTPException(400, f"This flag is already {flag.status}.")
        run = db.get(ClaimsRun, run_id)
        if run is None:
            raise HTTPException(404, "No such claims run.")
        if run.status != "ready":
            # Same guard as a row correction: while a worker is running it
            # deletes and rewrites this employee's flags wholesale, so a
            # decision taken now is written onto rows that are about to go.
            raise HTTPException(400, f"Flags can be decided once the run is ready (it is {run.status}).")
        _revision_check(db, run, body)
        if flag.code in ("CLAIMANT_UNKNOWN", "OWNERSHIP_CONFLICT"):
            raise HTTPException(400, "This is settled by an action, not a note: set or confirm the claimant on the case "
                                     "(CLAIMANT_UNKNOWN), or split / move the files at the map (OWNERSHIP_CONFLICT).")
        if flag.code == "ARTIFACT_UNRESOLVED":
            # Settling a Source Artifact is a case-model act, and is behind
            # the same switch as the disposition route it stands in for.
            _case_routes_on()
            # A file is not "dismissed": it gets a disposition, which is
            # what releases the control (H3). The decision route accepts
            # the disposition inline so the flag card can offer it.
            disposition = body.disposition.strip()
            if disposition not in REVIEWER_DISPOSITIONS:
                raise HTTPException(400, "Say what this file is: disposition must be irrelevant, unreadable "
                                         "or duplicate (with a note) — that is what settles an unplaced file.")
            if not note:
                raise HTTPException(400, "A short note is required — it is the reason recorded with the disposition.")
            art = _set_disposition(db, run_id, flag.artifact_id, disposition, note, actor="reviewer")
            db.commit()
            pending = _pending_retry(db, run_id, art)
            if pending:
                from . import worker

                runner.start_background(worker.retry_employee(run_id, pending))
            return {"ok": True, "disposition": disposition, "reverify_employee_id": pending}
        flag.status, flag.resolution = decision, note
        run.outputs = {}  # withdrawn; rebuilt below, now the state is settled
        if flag.code == "CLAIM_AMOUNT_UNCONFIRMED" and decision == "accepted" and flag.case_id:
            # "It is a real problem" on derived amounts means: these amounts
            # are not payable as they stand — the case is left out of the
            # listing (named under not_included), never paid on a guess.
            case = db.get(ClaimCase, flag.case_id)
            if case is not None:
                case.state = "excluded"
                case.reason = (case.reason + "; excluded in review: derived amounts not confirmed")[:400]
        _bump_revision(run)
        db.add(AuditEvent(run_id=run_id, actor="reviewer", action=f"flag_{decision}",
                          detail=f"[{flag.code}] {flag.reason[:200]} — note: {note or 'none'}"))
        db.commit()
        store_outputs(db, run)   # this decision may have opened the gate
        return {"ok": True}
    finally:
        db.close()


@router.post("/{run_id}/artifacts/{artifact_id}/disposition")
def set_artifact_disposition(run_id: str, artifact_id: str, body: S.DispositionBody) -> dict:
    """The reviewer says what a file is. body = {disposition, reason}.
    After verification, a change on a file inside a case re-verifies that
    case (ready) or is refused until the run is ready (verifying)."""
    from . import worker

    db = SessionLocal()
    try:
        run = db.get(ClaimsRun, run_id)
        if not run:
            raise HTTPException(404, "No such claims run.")
        if run.status not in ("map_ready", "verifying", "ready"):
            raise HTTPException(400, f"Files can be settled once the map is proposed (the run is {run.status}).")
        _case_routes_on()
        _revision_check(db, run, body, required=True)
        art = _set_disposition(db, run_id, artifact_id, body.disposition.strip(),
                               body.reason.strip(), actor="reviewer")
        db.commit()
        pending = _pending_retry(db, run_id, art)
        out = {"ok": True, "artifact": cases_mod.artifact_dict(art), "revision": run.revision}
    finally:
        db.close()
    if pending:
        runner.start_background(worker.retry_employee(run_id, pending))
        out["reverify_employee_id"] = pending
    return out


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
async def correct_claim_row(run_id: str, row_id: str, body: S.CorrectRowBody) -> dict:
    """Fix a value on one row (audited), then re-check that employee at once:
    flags that no longer apply resolve themselves, new ones are raised,
    decided ones stay decided. body = {fields: {name: value}, reason}."""
    from . import worker
    from .report_reader import gl_of, item_name

    reason = body.reason.strip()
    if not reason:
        raise HTTPException(400, "A short reason is required — it goes in the audit trail.")
    submitted = body.fields
    if not submitted:
        raise HTTPException(400, "fields must map field names to corrected values.")
    db = SessionLocal()
    try:
        row = db.get(ClaimRow, row_id)
        run = db.get(ClaimsRun, run_id)
        if not row or not run or row.run_id != run_id:
            raise HTTPException(404, "No such row.")
        if run.status != "ready":
            raise HTTPException(400, "Corrections are possible once the run is ready.")
        _revision_check(db, run, body)
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
        # Instant re-check for this employee, and the flag reconciliation
        # that goes with it — the worker's own job (worker.py).
        n_flags = await worker.recheck_after_correction(db, run, emp, row_id, sorted(changed), reason)
        _bump_revision(run)
        db.add(AuditEvent(run_id=run_id, actor="system", action="employee_rechecked",
                          detail=f"{emp.name or emp.folder} after correcting "
                                 f"{', '.join(sorted(changed)) or 'nothing (retry)'}: "
                                 f"{n_flags} rule(s) now apply"))
        db.commit()
        store_outputs(db, run)
        return {"ok": True, "flags_now": n_flags}
    finally:
        db.close()


@router.put("/{run_id}/cases/{case_id}/category")
def set_case_category(run_id: str, case_id: str, body: S.CategoryBody) -> dict:
    """The case-keyed twin of the employee category route (H10)."""
    _case_routes_on()
    db = SessionLocal()
    try:
        case = db.get(ClaimCase, case_id)
        if not case or case.run_id != run_id or not case.legacy_employee_id:
            raise HTTPException(404, "No such case in this run.")
        emp_id = case.legacy_employee_id
    finally:
        db.close()
    return set_employee_category(run_id, emp_id, body)


@router.put("/{run_id}/employees/{employee_id}/category")
def set_employee_category(run_id: str, employee_id: str, body: S.CategoryBody) -> dict:
    """The reviewer sets the listing category (CATEGORY_UNCLEAR, or a
    correction). Audited; the open CATEGORY_UNCLEAR flag is resolved."""
    category = body.category.strip()[:80]
    gl = body.gl.strip()[:20]
    reason = body.reason.strip()[:300]
    if not category:
        raise HTTPException(400, "Choose a category.")
    db = SessionLocal()
    try:
        emp = db.get(ClaimEmployee, employee_id)
        run = db.get(ClaimsRun, run_id)
        if not emp or not run or emp.run_id != run_id:
            raise HTTPException(404, "No such employee in this run.")
        if run.status != "ready":
            raise HTTPException(400, f"The category can be set once the run is ready (it is {run.status}).")
        _revision_check(db, run, body)
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
        _bump_revision(run)
        db.commit()
        store_outputs(db, run)
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


def _evidence_item_dict(e: ClaimEvidence, artifact_of: dict) -> dict:
    return {"id": e.id, "artifact_id": artifact_of.get(e.file, ""), "case_id": e.case_id, "kind": e.kind,
            "values": e.values, "confidence": e.confidence, "extraction_method": "page read (AI, twice for receipts)",
            "citation": {"artifact_id": artifact_of.get(e.file, ""), "path": e.file, "page": e.page,
                         "position": e.position}, "line_id": e.matched_row_id}


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


# The target state names (CLAIMS-AGENT-HARDENING.md, "State machine"): the
# HTTP `stage` field speaks them while `status` keeps the delivered names
# the screens poll on during the migration.
STAGE_OF = {"mapping": "investigating", "map_ready": "group_ready"}


def _summary(run: ClaimsRun, counts: dict) -> dict:
    n_map = sum(1 for e in (run.map or {}).get("employees", []) if e.get("is_employee"))
    return {"id": run.id, "client": run.client, "status": run.status, "stage": STAGE_OF.get(run.status, run.status),
            "error": run.error,
            "progress": run.progress,
            "folder": run.folder_url or (run.snapshot or {}).get("source") or "zip upload",
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
            # Whether the New-run form offers SharePoint link fields.
            "sharepoint_source": switches.on("claims_sharepoint_source"),
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
def update_claims_settings(body: S.ClaimsSettingsBody) -> dict:
    """body = {profile?: {...fields...}, playbook?: str, forget_last_map?: bool}.
    Every value is validated before any is saved (settings_schema); the
    change is audited by the profile store itself."""
    from . import settings_schema

    client = settings_store.get_setting("client_name")
    if body.profile is not None:
        profile_mod.save_profile(client,
                                 settings_schema.merged_profile(profile_mod.get_profile(client), body.profile))
    if "playbook" in body.model_fields_set:
        profile_mod.save_playbook(client, settings_schema.clean_playbook(body.playbook))
    if body.forget_last_map:
        profile_mod.forget_last_map(client)
    return _settings_payload(client)
