"""The pipeline conductor.

Takes a run through: sorting → extracting → checking → ready. Runs as a
background task after upload; the frontend polls the run's status/progress.
Any stage failure marks the run failed with a readable error — never a
silent stall.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..db import SessionLocal
from ..models import Document, Flag, Run
from . import output, reference
from .checks import run_checks
from .extract import extract_all
from .images import document_to_pngs
from .sort import attach_receipts, sort_document

log = logging.getLogger("runner")


async def process_run(run_id: str, workspace: Path) -> None:
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        docs = db.query(Document).filter(Document.run_id == run_id).all()
        folder_url = run.snapshot.get("sharepoint_folder_url")

        # Resolve the reference files FIRST. An unreachable folder or an
        # ambiguous file name is fatal, and finding that out before the AI
        # reads a single page avoids paying for a run that cannot finish.
        # Recorded on the run so its record shows what it was judged
        # against — a None role means that check did not happen.
        # Reassigned, not mutated: SQLAlchemy only persists JSON on rebind.
        run.snapshot = {**run.snapshot,
                        "reference_files": reference.resolve_reference_files(folder_url)}
        db.commit()

        # ---- sort -------------------------------------------------------
        _set(db, run, status="sorting", progress={"done": 0, "total": len(docs)})
        for i, doc in enumerate(docs, 1):
            try:
                first_page = document_to_pngs(workspace / doc.filename)[0]
                result = await sort_document(workspace / doc.filename, first_page)
                doc.kind = result.kind
                doc.status = "sorted"
            except Exception as exc:
                doc.kind, doc.status, doc.error = "unknown", "error", str(exc)
            _set(db, run, progress={"done": i, "total": len(docs)})
        attach_receipts(docs)
        db.commit()

        # ---- extract ----------------------------------------------------
        to_read = [d for d in docs if d.kind in ("invoice", "claim")]
        counter = {"done": 0}

        def on_progress() -> None:
            counter["done"] += 1
            _set(db, run, progress={"done": counter["done"], "total": len(to_read)})

        _set(db, run, status="extracting", progress={"done": 0, "total": len(to_read)})
        await extract_all(docs, workspace, on_progress)
        db.commit()

        # ---- check ------------------------------------------------------
        _set(db, run, status="checking", progress={})
        flag_dicts = await run_checks(docs, folder_url=folder_url)
        for fd in flag_dicts:
            db.add(Flag(run_id=run_id, **fd))
        for d in docs:
            if d.status == "extracted":
                d.status = "checked"
        db.commit()

        # ---- draft outputs (regenerated after review decisions) --------
        run.outputs = await output.build_outputs(docs, excluded_doc_ids=set(),
                                                 folder_url=folder_url)
        _set(db, run, status="ready")
        db.commit()
    except Exception as exc:
        # The run's error field carries a one-line reason for the UI; the
        # full traceback belongs in the server log, or diagnosing a failed
        # run means guessing.
        log.exception("run %s failed: %s", run_id, exc)
        db.rollback()
        run = db.get(Run, run_id)
        if run:
            _set(db, run, status="failed", error=str(exc))
    finally:
        db.close()


def _set(db, run: Run, **values) -> None:
    for k, v in values.items():
        setattr(run, k, v)
    db.commit()


def start_background(run_id: str, workspace: Path) -> None:
    """Fire the pipeline without blocking the upload response."""
    asyncio.get_event_loop().create_task(process_run(run_id, workspace))
