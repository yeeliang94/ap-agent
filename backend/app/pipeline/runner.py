"""The pipeline conductor.

Takes a run through: sorting → extracting → checking → ready. Runs as a
background task after upload; the frontend polls the run's status/progress.
Any stage failure marks the run failed with a readable error — never a
silent stall.

Every stage also writes to the run's diary (telemetry.record). Per-document
failures are caught so one bad file cannot sink a batch, and that
tolerance is exactly what used to hide a broken dependency: with the AI
proxy rejecting every request, all 22 documents "failed individually" and
the run still reached ready. Each stage therefore ends by looking at how
many failed, and says so.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from .. import telemetry
from ..db import SessionLocal
from ..models import Document, Flag, Run
from . import output, reference
from .checks import run_checks
from .extract import extract_all
from .images import document_to_pngs
from .sort import attach_receipts, sort_document

log = logging.getLogger("runner")


class PipelineHalted(Exception):
    """A stage failed for every single document, so the run is abandoned.

    Not a crash — a decision. When all 22 documents fail sorting with the
    same 401, the remaining stages will fail the same way, cost the same
    money, and produce output nobody may act on. Stopping here says so
    once, loudly, instead of producing a "ready" run built on nothing.
    """


async def process_run(run_id: str, workspace: Path) -> None:
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        docs = db.query(Document).filter(Document.run_id == run_id).all()
        folder_url = run.snapshot.get("sharepoint_folder_url")  # where the copy comes from

        telemetry.record(db, run_id, "run", telemetry.INFO, "RUN_STARTED",
                         f"Run started for {run.client} with {len(docs)} uploaded file(s).")

        # Take the run's own copy of the reference files FIRST. An
        # unreachable folder or an ambiguous file name is fatal, and finding
        # that out before the AI reads a single page avoids paying for a run
        # that cannot finish. From here on the run — and its review — reads
        # only this copy, never SharePoint again. Recorded on the run so its
        # record shows what it was judged against — a None role means that
        # check did not happen. Reassigned, not mutated: SQLAlchemy only
        # persists JSON on rebind.
        refs = reference.run_refs(run_id)
        started = time.monotonic()
        try:
            reference_files = reference.snapshot_references(folder_url, refs)
        except Exception as exc:
            telemetry.record_failure(db, run_id, "reference", "REFERENCE_UNAVAILABLE",
                                     "Could not read the SharePoint reference folder", exc)
            raise
        run.snapshot = {**run.snapshot, "reference_files": reference_files}
        db.commit()
        _record_reference_files(db, run_id, reference_files, started)

        # Read the payment listing NOW, before any invoice is read. It is
        # the whole point of the run (has this invoice been paid before?),
        # it costs AI calls, and it can fail — so find that out first, and
        # tell the diary how each tab was read (cached: replayed for free).
        started = time.monotonic()
        try:
            notes = await reference.load_listing_notes(refs)
        except Exception as exc:
            telemetry.record_failure(db, run_id, "reference", "LISTING_UNREADABLE",
                                     "Could not read the payment listing", exc)
            raise
        for level, text in notes:
            telemetry.record(db, run_id, "reference", _level(level), "LISTING_READ", text)
        telemetry.record(db, run_id, "reference", telemetry.INFO, "STAGE_DONE",
                         f"Payment listing ready in {_secs(started)}.")

        # ---- sort -------------------------------------------------------
        _set(db, run, status="sorting", progress={"done": 0, "total": len(docs)})
        started = time.monotonic()
        sort_failures: list[str] = []
        for i, doc in enumerate(docs, 1):
            try:
                first_page = document_to_pngs(workspace / doc.filename)[0]
                result = await sort_document(workspace / doc.filename, first_page)
                doc.kind = result.kind
                doc.status = "sorted"
            except Exception as exc:
                # One unreadable file must not sink the batch — but the
                # reason is named here rather than stored as a raw
                # exception, and counted so the stage can report a pattern.
                reason = telemetry.record_failure(
                    db, run_id, "sort", "DOCUMENT_FAILED",
                    f"Could not sort {doc.filename}", exc, document_id=doc.id)
                doc.kind, doc.status, doc.error = "unknown", "error", reason
                sort_failures.append(reason)
            _set(db, run, progress={"done": i, "total": len(docs)})
        attach_receipts(docs)
        db.commit()
        _halt_if_all_failed(
            _record_stage_end(db, run_id, "sort", len(docs), sort_failures, started))

        # ---- extract ----------------------------------------------------
        to_read = [d for d in docs if d.kind in ("invoice", "claim")]
        counter = {"done": 0}

        def on_progress() -> None:
            counter["done"] += 1
            _set(db, run, progress={"done": counter["done"], "total": len(to_read)})

        _set(db, run, status="extracting", progress={"done": 0, "total": len(to_read)})
        started = time.monotonic()
        await extract_all(docs, workspace, on_progress)
        db.commit()
        extract_failures = []
        for d in to_read:
            if d.status == "error":
                extract_failures.append(d.error)
                telemetry.record(db, run_id, "extract", telemetry.ERROR,
                                 "DOCUMENT_FAILED",
                                 f"Could not read {d.filename}: {d.error}",
                                 document_id=d.id)
        _halt_if_all_failed(
            _record_stage_end(db, run_id, "extract", len(to_read),
                              extract_failures, started))

        # ---- check ------------------------------------------------------
        _set(db, run, status="checking", progress={})
        started = time.monotonic()
        check_notes: list[tuple[str, str]] = []
        try:
            flag_dicts = await run_checks(docs, refs=refs, notes=check_notes)
        except Exception as exc:
            telemetry.record_failure(db, run_id, "check", "CHECKS_FAILED",
                                     "The checking stage could not complete", exc)
            raise
        for level, text in check_notes:
            telemetry.record(db, run_id, "check", _level(level), "LISTING_MATCHES", text)
        for fd in flag_dicts:
            db.add(Flag(run_id=run_id, **fd))
        for d in docs:
            if d.status == "extracted":
                d.status = "checked"
        db.commit()
        _record_flags(db, run_id, flag_dicts, started)

        # ---- draft outputs (regenerated after review decisions) --------
        started = time.monotonic()
        try:
            from ..settings_store import draft_settings
            run.outputs = await output.build_outputs(
                docs, excluded_doc_ids=set(), refs=refs,
                draft_settings=draft_settings(run.snapshot))
        except Exception as exc:
            telemetry.record_failure(db, run_id, "output", "OUTPUT_FAILED",
                                     "Could not build the copy-ready output", exc)
            raise
        telemetry.record(db, run_id, "output", telemetry.INFO, "STAGE_DONE",
                         f"Copy-ready output built in {_secs(started)}.")
        _set(db, run, status="ready")
        telemetry.record(db, run_id, "run", telemetry.INFO, "RUN_READY",
                         "Run finished and is ready for review.")
        db.commit()
    except PipelineHalted as exc:
        # A decision, not a crash. The reason is already recorded and
        # already readable, so no traceback is warranted here.
        log.error("run %s halted: %s", run_id, exc)
        db.rollback()
        _fail(db, run_id, str(exc), "RUN_HALTED")
    except Exception as exc:
        # The run's error field carries a one-line reason for the UI; the
        # full traceback belongs in the server log, or diagnosing a failed
        # run means guessing.
        log.exception("run %s failed: %s", run_id, exc)
        db.rollback()
        _fail(db, run_id, str(exc), "RUN_FAILED")
    finally:
        db.close()


def _fail(db, run_id: str, error: str, code: str) -> None:
    run = db.get(Run, run_id)
    if run:
        _set(db, run, status="failed", error=error)
        telemetry.record(db, run_id, "run", telemetry.ERROR, code,
                         f"Run stopped: {error}")


def _halt_if_all_failed(reason: str) -> None:
    """Abandon the run when a stage worked for nothing at all."""
    if reason:
        raise PipelineHalted(reason)


def _level(note_level: str) -> str:
    """Pipeline notes say "INFO"/"WARNING"; the diary has its own constants."""
    return telemetry.WARNING if note_level == "WARNING" else telemetry.INFO


def _secs(started: float) -> str:
    return f"{time.monotonic() - started:.1f}s"


def _record_reference_files(db, run_id: str, files: dict, started: float) -> None:
    """Which reference file played which role — and which role had none.

    A missing policy sheet or bank template does not stop a run: the checks
    that need the file do not happen. That is stated here for the diary,
    and — when the batch actually needed the file — raised as a run-level
    MISSING_REFERENCE flag by run_checks, so a reviewer must acknowledge
    it before any output leaves.
    """
    found = {role: name for role, name in files.items() if name}
    telemetry.record(db, run_id, "reference", telemetry.INFO, "REFERENCE_RESOLVED",
                     f"Reference folder read in {_secs(started)}: "
                     + (", ".join(f"{r.replace('_', ' ')} = {n}"
                                  for r, n in found.items()) or "no files matched"))
    for role, name in files.items():
        if not name:
            telemetry.record(
                db, run_id, "reference", telemetry.WARNING, "REFERENCE_MISSING",
                f"No {role.replace('_', ' ')} in the folder, so the checks that "
                f"rely on it did not run.")


def _record_stage_end(db, run_id: str, stage: str, total: int,
                      failures: list[str], started: float) -> str:
    """Close a stage by saying how much of it actually worked.

    Returns a reason to abandon the run, or "" to carry on. The important
    case is total failure: per-document tolerance means a broken
    dependency looks like many unrelated small problems, and counting them
    is what turns that back into one diagnosable event.
    """
    if not total:
        return ""
    failed = len(failures)
    if failed == 0:
        telemetry.record(db, run_id, stage, telemetry.INFO, "STAGE_DONE",
                         f"All {total} document(s) completed the {stage} stage "
                         f"in {_secs(started)}.")
        return ""

    # One reason shared by every failure is a dependency being down, not
    # a batch of coincidentally bad files. Say which it is.
    distinct = sorted(set(failures))
    shared = distinct[0] if len(distinct) == 1 else ""
    if failed == total:
        why = (f" for the same reason: {shared}. This is a setup or service "
               f"problem, not a problem with the uploaded files."
               if shared else f". Reasons: {'; '.join(distinct[:3])}.")
        telemetry.record(
            db, run_id, stage, telemetry.ERROR, "ALL_DOCUMENTS_FAILED",
            f"EVERY document ({total}) failed the {stage} stage" + why,
            detail=f"took {_secs(started)}")
        # The message the reviewer sees on the failed run itself. Every
        # later stage would fail the same way, so the run ends here.
        return (f"Every one of the {total} document(s) failed the {stage} "
                f"stage" + why)
    telemetry.record(
        db, run_id, stage, telemetry.WARNING, "SOME_DOCUMENTS_FAILED",
        f"{failed} of {total} document(s) failed the {stage} stage"
        + (f", all with the same reason: {shared}" if shared else "")
        + f". The other {total - failed} continued.",
        detail=f"took {_secs(started)}")
    return ""


def _record_flags(db, run_id: str, flag_dicts: list[dict], started: float) -> None:
    """A count per flag code, so a wall of flags reads as a pattern."""
    counts: dict[str, int] = {}
    for fd in flag_dicts:
        counts[fd["code"]] = counts.get(fd["code"], 0) + 1
    telemetry.record(
        db, run_id, "check", telemetry.INFO, "STAGE_DONE",
        f"Checks finished in {_secs(started)}: {len(flag_dicts)} flag(s) raised"
        + (" — " + ", ".join(f"{n}x {code}" for code, n in sorted(counts.items()))
           if counts else "."))


def _set(db, run: Run, **values) -> None:
    for k, v in values.items():
        setattr(run, k, v)
    db.commit()


def start_background(run_id: str, workspace: Path) -> None:
    """Fire the pipeline without blocking the upload response."""
    asyncio.get_event_loop().create_task(process_run(run_id, workspace))


# The statuses a run passes through while the pipeline is working on it.
# A run found in one of these when the server STARTS was interrupted: the
# task that owned it died with the old process and nothing will resume it.
IN_PROGRESS_STATUSES = ("queued", "sorting", "extracting", "checking")

INTERRUPTED_ERROR = ("The server restarted before this run finished, so it "
                     "could not be completed. Start a new run.")


def fail_interrupted_runs() -> int:
    """Mark runs left mid-stage by a restart as failed. Called at startup.

    Runs are in-process tasks, so a restart (crash, deploy, laptop lid) is
    fatal to any run that was under way. Without this, such a run would
    show "extracting" forever and the reviewer could not tell a stuck run
    from a slow one. Returns how many were reconciled.
    """
    db = SessionLocal()
    try:
        stuck = db.query(Run).filter(Run.status.in_(IN_PROGRESS_STATUSES)).all()
        for run in stuck:
            _set(db, run, status="failed", error=INTERRUPTED_ERROR)
            telemetry.record(db, run.id, "run", telemetry.ERROR, "RUN_INTERRUPTED",
                             INTERRUPTED_ERROR)
        if stuck:
            log.warning("%d run(s) were in progress at startup and were marked "
                        "failed (interrupted by restart)", len(stuck))
        return len(stuck)
    finally:
        db.close()
