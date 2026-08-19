"""The claims conductor.

Takes a claims run through its statuses:

  queued → surveying → mapping → map_ready      (process_run, at start)
  map_ready → verifying → ready / failed         (start_verification, on
                                                  the reviewer's Confirm)

Runs as a background task; the frontend polls status/progress. Every
stage writes to the run diary (telemetry.record) and any stage failure
marks the run failed with a readable reason — never a silent stall.

Restart-safe from day one (lesson from the MVP peer review): a run found
in an in-progress status when the server starts was orphaned by the old
process and is marked failed with a plain reason. map_ready and ready are
resting states — a run waiting for a click survives a restart.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from .. import config, telemetry
from ..db import SessionLocal
from ..docsource import SourceUnavailable, get_source
from . import source as batch_source
from .models import IN_PROGRESS_STATUSES, ClaimsRun

log = logging.getLogger("claims.runner")


# The reviewer's standing objective for every investigation; the run's
# instructions are added to it, never replace it.
OBJECTIVE = ("Check the expense records and all supporting evidence, group what belongs "
             "together, reconcile every line and total, and show anything that does not agree.")


def store_investigation(db, run: ClaimsRun, result) -> None:
    """Persist the normalized result: artifacts, proposed cases and
    assignments, the plan and the tool record (H2 tables)."""
    from . import cases as cases_mod

    cases_mod.store_result(db, run, result, confirmed=False)
    db.commit()


def workspace_for(run_id: str) -> Path:
    """runs/<id>/claims/ — the run's private copy of everything."""
    return config.RUNS_DIR / run_id / "claims"


def files_dir(run_id: str) -> Path:
    return workspace_for(run_id) / "files"


def _set(db, run: ClaimsRun, **values) -> None:
    for k, v in values.items():
        setattr(run, k, v)
    db.commit()


def _secs(started: float) -> str:
    return f"{time.monotonic() - started:.1f}s"


async def process_run(run_id: str) -> None:
    """Survey → peek → map. Ends at map_ready (v1 always pauses there)."""
    db = SessionLocal()
    try:
        run = db.get(ClaimsRun, run_id)
        telemetry.record(db, run_id, "run", telemetry.INFO, "RUN_STARTED",
                         f"Claims run started for {run.client}"
                         + (" from a SharePoint folder." if run.folder_url else " from a zip."))

        # ---- bring the files in ----------------------------------------
        _set(db, run, status="surveying", progress={})
        started = time.monotonic()
        dest = files_dir(run_id)
        dest.mkdir(parents=True, exist_ok=True)
        try:
            files = await asyncio.to_thread(_fetch_batch, db, run, dest)
        except batch_source.QuotaExceeded as exc:
            telemetry.record(db, run_id, "source", telemetry.ERROR, "QUOTA_EXCEEDED", str(exc))
            raise
        except SourceUnavailable as exc:
            telemetry.record(db, run_id, "source", telemetry.ERROR, "SOURCE_UNAVAILABLE",
                             f"Could not read the batch folder: {exc}")
            raise
        n_folders = len({f["path"].split("/", 1)[0] for f in files if "/" in f["path"]})
        telemetry.record(db, run_id, "source", telemetry.INFO, "STAGE_DONE",
                         f"Batch copied in {_secs(started)}: {n_folders} subfolder(s), "
                         f"{len(files)} file(s).")

        # ---- survey + peek (code only) ---------------------------------
        from . import survey as survey_mod

        started = time.monotonic()
        try:
            survey = await asyncio.to_thread(survey_mod.survey_batch, dest, files)
        except batch_source.QuotaExceeded as exc:
            telemetry.record(db, run_id, "source", telemetry.ERROR, "QUOTA_EXCEEDED", str(exc))
            raise
        _set(db, run, survey=survey)
        n_peeked = sum(1 for f in survey["files"] if f.get("peek"))
        telemetry.record(db, run_id, "survey", telemetry.INFO, "STAGE_DONE",
                         f"Survey done in {_secs(started)}: {len(survey['folders'])} "
                         f"folder(s), {len(survey['files'])} file(s), {n_peeked} peeked inside.")
        if not survey["files"]:
            raise RuntimeError("The folder holds no files at all — nothing to investigate.")
        if not survey["folders"]:
            telemetry.record(db, run_id, "survey", telemetry.INFO, "FLAT_FOLDER",
                             f"No subfolders: {len(survey['files'])} file(s) sit directly in the batch folder. "
                             "Every file is inventoried; grouping is proposed at the map, not assumed.")

        # ---- investigate: manifest, then the adapter behind the seam --------
        # (H1) Every file is hashed into the immutable manifest first; the
        # investigator (legacy structured-folder mapper, or the tool-using
        # agent when CLAIMS_AGENTIC_INVESTIGATION is on) proposes the map /
        # cases; code audits; a person confirms.
        from . import investigator
        from . import manifest as manifest_mod

        _set(db, run, status="mapping", progress={})
        started = time.monotonic()
        try:
            manifest = await asyncio.to_thread(manifest_mod.build_manifest, dest, files)
            request = investigator.InvestigationRequest(
                run_id=run_id, workspace=str(workspace_for(run_id)), manifest=manifest,
                instructions=run.instructions or "", objective=OBJECTIVE,
                profile_snapshot=run.snapshot or {}, survey=survey)
            result = await investigator.investigate(request)
        except Exception as exc:
            telemetry.record_failure(db, run_id, "map", "MAP_FAILED",
                                     "Could not map the folder", exc)
            raise RuntimeError(
                "could not map folder — the survey listing is shown so you can add "
                "instructions and start again") from exc
        claim_map, warnings, notes = result.map, list(result.warnings), list(result.notes)
        run.manifest = manifest_mod.to_dicts(manifest)
        store_investigation(db, run, result)
        for level, text in notes:
            telemetry.record(db, run_id, "map",
                             telemetry.WARNING if level == "WARNING" else telemetry.INFO,
                             "MAP_ROUND", text)
        for text in warnings:
            telemetry.record(db, run_id, "map", telemetry.WARNING, "MAP_WARNING", text)
        n_emp = sum(1 for e in claim_map.get("employees", []) if e.get("is_employee", True))
        _set(db, run, map=claim_map, map_warnings=warnings, status="map_ready",
             progress={"employees": n_emp})
        telemetry.record(db, run_id, "map", telemetry.INFO, "STAGE_DONE",
                         f"Map proposed in {_secs(started)}: {n_emp} employee(s), "
                         f"{len(warnings)} warning(s). Waiting for the reviewer to confirm.")
        db.commit()
    except Exception as exc:
        log.exception("claims run %s failed: %s", run_id, exc)
        db.rollback()
        _fail(db, run_id, str(exc), "RUN_FAILED")
    finally:
        db.close()


def _fetch_batch(db, run: ClaimsRun, dest: Path) -> list[dict]:
    """Copy the batch into the workspace: unpack the zip, or walk SharePoint."""
    zip_path = workspace_for(run.id) / "upload.zip"
    if zip_path.is_file():
        entries = batch_source.unpack_zip(zip_path, dest)
        return [e for e in entries if e["kind"] == "file"]
    source = get_source(run.folder_url)
    entries = batch_source.walk_folder(source, run.folder_url)
    telemetry.record(db, run.id, "source", telemetry.INFO, "FOLDER_LISTED",
                     f"Folder listed: {sum(1 for e in entries if e['kind'] == 'folder')} "
                     f"subfolder(s), {sum(1 for e in entries if e['kind'] == 'file')} file(s).")

    def on_progress(done: int, total: int) -> None:
        _set(db, run, progress={"done": done, "total": total, "what": "downloading"})

    return batch_source.download_all(source, run.folder_url, entries, dest, on_progress)


async def start_verification(run_id: str) -> None:
    """Confirmed map → workers → ready. Step 10 fills in the workers."""
    db = SessionLocal()
    try:
        run = db.get(ClaimsRun, run_id)
        _set(db, run, status="verifying", progress={"done": 0, "total": 0})
        from . import worker

        await worker.verify_run(db, run)
    except Exception as exc:
        log.exception("claims run %s failed while verifying: %s", run_id, exc)
        db.rollback()
        _fail(db, run_id, str(exc), "RUN_FAILED")
    finally:
        db.close()


def _fail(db, run_id: str, error: str, code: str) -> None:
    run = db.get(ClaimsRun, run_id)
    if run:
        _set(db, run, status="failed", error=error[:1000])
        telemetry.record(db, run_id, "run", telemetry.ERROR, code, f"Run stopped: {error}")


def start_background(coro) -> None:
    """Fire a stage without blocking the HTTP response."""
    asyncio.get_event_loop().create_task(coro)


INTERRUPTED_ERROR = ("The server restarted before this run finished, so it "
                     "could not be completed. Start a new run.")


def fail_interrupted_runs() -> int:
    """Mark claims runs left mid-stage by a restart as failed. Called at
    startup, beside the invoice pipeline's own reconciliation. A run at
    map_ready (waiting for a click) or ready is untouched."""
    db = SessionLocal()
    try:
        stuck = db.query(ClaimsRun).filter(ClaimsRun.status.in_(IN_PROGRESS_STATUSES)).all()
        for run in stuck:
            _set(db, run, status="failed", error=INTERRUPTED_ERROR)
            telemetry.record(db, run.id, "run", telemetry.ERROR, "RUN_INTERRUPTED",
                             INTERRUPTED_ERROR)
        if stuck:
            log.warning("%d claims run(s) were in progress at startup and were "
                        "marked failed (interrupted by restart)", len(stuck))
        return len(stuck)
    finally:
        db.close()
