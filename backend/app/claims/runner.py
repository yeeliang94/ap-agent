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
from .investigator.contracts import DEFAULT_OBJECTIVE as OBJECTIVE  # noqa: E402


async def _shadow_investigation(db, run: ClaimsRun, request, primary) -> None:
    """Shadow mode (H12): the tool-using investigator runs beside the
    mapper on the same request; its result is compared and recorded, never
    used. Its failure is a diary line, not a run failure."""
    if not config.CLAIMS_SHADOW_INVESTIGATION or config.CLAIMS_AGENTIC_INVESTIGATION:
        return
    from . import cases as cases_mod
    from .investigator import investigator as agentic

    started = time.monotonic()
    try:
        shadow = await agentic.investigate(request)
    except Exception as exc:
        telemetry.record_failure(db, run.id, "map", "SHADOW_FAILED", "The shadow investigation failed", exc)
        return
    comparison = cases_mod.compare_results(primary, shadow)
    cases_mod.store_shadow(db, run, shadow, comparison)
    db.commit()
    telemetry.record(db, run.id, "map", telemetry.INFO if comparison["agrees"] else telemetry.WARNING, "SHADOW_RESULT",
                     f"Shadow investigation in {_secs(started)}: "
                     + ("agrees with the map." if comparison["agrees"] else
                        f"{len(comparison['differences'])} difference(s) — " + "; ".join(comparison["differences"])[:600]))


def store_investigation(db, run: ClaimsRun, result) -> None:
    """Persist the normalized result: artifacts, proposed cases and
    assignments, the plan and the tool record (H2 tables)."""
    from . import cases as cases_mod
    from . import grouping

    cases_mod.store_result(db, run, result, confirmed=False)
    grouping.refresh(db, run)
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


class RunCancelled(Exception):
    """The run left its in-progress status under the conductor's feet (the
    reviewer cancelled it): the stage stops and writes nothing more."""


def _advance(db, run: ClaimsRun, expect: tuple[str, ...], **values) -> None:
    """Move the run to a new status ONLY if it is still in one of `expect`
    — a conditional UPDATE, not a write through the object loaded at the
    start of the job. A cancel lands in another session; the stale object
    must never resurrect a failed run as mapping or map_ready."""
    n = (db.query(ClaimsRun)
         .filter(ClaimsRun.id == run.id, ClaimsRun.status.in_(expect))
         .update(values, synchronize_session=False))
    db.commit()
    if n != 1:
        db.refresh(run)
        raise RunCancelled(f"the run is {run.status}, not {' or '.join(expect)}")
    for k, v in values.items():
        setattr(run, k, v)


def _still_running(db, run: ClaimsRun) -> None:
    """Raise RunCancelled if the run was failed meanwhile (checked between
    long stages, so a cancelled run stores nothing more)."""
    db.refresh(run)
    if run.status == "failed":
        raise RunCancelled("the run was cancelled")


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
        _advance(db, run, ("queued",), status="surveying", progress={})
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
        _still_running(db, run)
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

        _advance(db, run, ("surveying",), status="mapping", progress={})
        started = time.monotonic()
        try:
            manifest = await asyncio.to_thread(manifest_mod.build_manifest, dest, files)
            await asyncio.to_thread(manifest_mod.lock_snapshot, dest)  # read-only from here (H11)
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
        _still_running(db, run)
        claim_map, warnings, notes = result.map, list(result.warnings), list(result.notes)
        run.manifest = manifest_mod.to_dicts(manifest)
        store_investigation(db, run, result)
        await _shadow_investigation(db, run, request, result)
        for level, text in notes:
            telemetry.record(db, run_id, "map",
                             telemetry.WARNING if level == "WARNING" else telemetry.INFO,
                             "MAP_ROUND", text)
        for text in warnings:
            telemetry.record(db, run_id, "map", telemetry.WARNING, "MAP_WARNING", text)
        n_emp = sum(1 for e in claim_map.get("employees", []) if e.get("is_employee", True))
        _advance(db, run, ("mapping",), map=claim_map, map_warnings=warnings, status="map_ready",
                 progress={"employees": n_emp})
        telemetry.record(db, run_id, "map", telemetry.INFO, "STAGE_DONE",
                         f"Map proposed in {_secs(started)}: {n_emp} employee(s), "
                         f"{len(warnings)} warning(s). Waiting for the reviewer to confirm.")
        db.commit()
    except RunCancelled as exc:
        # Cancelled by the reviewer while this stage ran: the run is already
        # failed with their reason; this stage writes nothing more.
        db.rollback()
        log.info("claims run %s stopped: %s", run_id, exc)
        telemetry.record(db, run_id, "run", telemetry.INFO, "RUN_STOPPED",
                         f"Stage stopped: {exc}.")
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

    def on_progress(done: int, total: int, current: str | None) -> None:
        progress = {"done": done, "total": total, "what": "downloading"}
        if current:
            progress["file"] = current
        _set(db, run, progress=progress)

    def on_retry(what: str, attempt: int, total: int, error: Exception) -> None:
        # SourceUnavailable is this app's reviewer-facing failure and is
        # already written for one; anything else is engineer text and is
        # named rather than quoted. The raw text goes to detail either way.
        reason = (str(error) if isinstance(error, SourceUnavailable)
                  else telemetry.describe_failure(error))
        telemetry.record(
            db, run.id, "source", telemetry.WARNING, "SOURCE_RETRY",
            f"Copying the folder: {what} failed on attempt {attempt} of "
            f"{total} ({reason}); retrying.",
            detail=str(error))

    # Every source offers batch(); for the real SharePoint one it holds a
    # single session and a single site lookup open across the walk and
    # every download that follows.
    with source.batch(run.folder_url):
        entries = batch_source.walk_folder(source, run.folder_url, on_retry)
        telemetry.record(db, run.id, "source", telemetry.INFO, "FOLDER_LISTED",
                         f"Folder listed: {sum(1 for e in entries if e['kind'] == 'folder')} "
                         f"subfolder(s), {sum(1 for e in entries if e['kind'] == 'file')} file(s).")
        return batch_source.download_all(
            source, run.folder_url, entries, dest, on_progress, on_retry)


async def start_verification(run_id: str) -> None:
    """Confirmed map → workers → ready. Step 10 fills in the workers."""
    db = SessionLocal()
    try:
        run = db.get(ClaimsRun, run_id)
        if run is None or run.status != "verifying":
            # The confirm route moves the run to verifying in its own commit;
            # anything else asking for verification is out of order (H6/H9).
            log.warning("claims run %s: verification requested while %s — ignored",
                        run_id, run.status if run else "missing")
            return
        _set(db, run, progress={"done": 0, "total": 0})
        from . import worker

        await worker.verify_run(db, run)
    except Exception as exc:
        log.exception("claims run %s failed while verifying: %s", run_id, exc)
        db.rollback()
        _fail(db, run_id, str(exc), "RUN_FAILED")
    finally:
        db.close()


def _fail(db, run_id: str, error: str, code: str) -> None:
    # A failing run stops its investigation's outstanding tool calls (H11).
    from .investigator import investigator as agentic

    agentic.cancel_run(run_id)
    run = db.get(ClaimsRun, run_id)
    if run is None:
        return
    db.refresh(run)
    if run.status == "failed":
        # Already failed (cancelled by the reviewer meanwhile): their reason
        # stands; the stage's own failure is a diary line, not an overwrite.
        telemetry.record(db, run_id, "run", telemetry.INFO, "RUN_STOPPED",
                         f"Stage ended after the run was stopped: {error[:300]}")
        return
    _set(db, run, status="failed", error=error[:1000])
    telemetry.record(db, run_id, "run", telemetry.ERROR, code, f"Run stopped: {error}")


# ---- background stages ------------------------------------------------------------
# Stages run as tasks on the application's ONE event loop (uvicorn's). A
# sync route runs in a worker thread, where there is no running loop, so
# the loop is captured once at startup (main.py → set_loop) and tasks are
# handed to it thread-safely. Every task is kept referenced until it ends
# (the loop keeps only weak references) and watched by a done-callback, so
# a stage that dies unobserved still fails its run instead of leaving it
# "verifying" until the next restart.

_loop: asyncio.AbstractEventLoop | None = None
_tasks: set[asyncio.Task] = set()


def set_loop(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Remember the application's event loop (call from startup, on the
    loop thread; `None` = the loop running right now)."""
    global _loop
    _loop = loop or asyncio.get_running_loop()


def _run_id_of(coro) -> str | None:
    """Every stage coroutine takes the run id as a parameter named run_id;
    read it off the not-yet-started coroutine's frame so the done-callback
    knows which run to fail."""
    frame = getattr(coro, "cr_frame", None)
    value = frame.f_locals.get("run_id") if frame is not None else None
    return value if isinstance(value, str) else None


def start_background(coro, run_id: str | None = None) -> None:
    """Fire a stage without blocking the HTTP response — from the loop
    thread (an async route) or from a worker thread (a sync route) alike.
    Raises RuntimeError (and closes the coroutine) only when no loop was
    ever registered and none is running: a programming error, not a
    silently dropped stage."""
    run_id = run_id or _run_id_of(coro)
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    loop = _loop if _loop is not None and not _loop.is_closed() else running
    if loop is None:
        coro.close()
        raise RuntimeError("no event loop to run the background stage on — "
                           "runner.set_loop() must be called at application startup")
    if running is loop:
        _track(loop.create_task(coro), run_id)
    else:
        loop.call_soon_threadsafe(lambda: _track(loop.create_task(coro), run_id))


def _track(task: asyncio.Task, run_id: str | None) -> None:
    _tasks.add(task)
    task.add_done_callback(lambda t: _on_done(t, run_id))


def _on_done(task: asyncio.Task, run_id: str | None) -> None:
    _tasks.discard(task)
    if task.cancelled():
        log.warning("claims background stage for run %s was cancelled", run_id)
        return
    exc = task.exception()
    if exc is None:
        return
    log.error("claims background stage for run %s died: %r", run_id, exc, exc_info=exc)
    if not run_id:
        return
    db = SessionLocal()
    try:
        run = db.get(ClaimsRun, run_id)
        if run is None:
            return
        if run.status in IN_PROGRESS_STATUSES:
            _fail(db, run_id, f"a background stage died: {exc}", "RUN_FAILED")
        else:
            # A resting run (map_ready / ready) is left as it is — a stage
            # that died after the run came to rest is a diary error, not a
            # reason to fail a reviewed run.
            telemetry.record(db, run_id, "run", telemetry.ERROR, "BACKGROUND_FAILED",
                             f"A background stage died after the run was {run.status}: {str(exc)[:300]}")
    except Exception:
        log.exception("claims run %s: could not record the background failure", run_id)
    finally:
        db.close()


def pending_background() -> int:
    """How many background stages are still running (operators, tests)."""
    return sum(1 for t in _tasks if not t.done())


def wait_background(timeout: float = 30.0) -> bool:
    """Block (from a thread that is NOT the loop) until every tracked stage
    has ended; True when they all did within the timeout. Tests use it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(not t.done() for t in list(_tasks)):
            return True
        time.sleep(0.02)
    return False


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
