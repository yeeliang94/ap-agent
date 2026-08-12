"""API routes: upload a batch, poll a run, review flags, fetch outputs."""
from __future__ import annotations

import zipfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from . import config
from .db import SessionLocal
from .models import AuditEvent, Document, Flag, Run
from .pipeline import output as output_builder
from .pipeline.runner import start_background

router = APIRouter()

# Only these file types may come out of an uploaded zip. Anything else is
# skipped and reported — never silently processed.
ALLOWED = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}


@router.post("/runs")
async def create_run(client: str = Form(...), batch: UploadFile = File(...)) -> dict:
    db = SessionLocal()
    try:
        run = Run(client=client)
        db.add(run)
        db.commit()

        workspace = config.RUNS_DIR / run.id
        workspace.mkdir(parents=True)
        zip_path = workspace / "upload.zip"
        zip_path.write_bytes(await batch.read())

        skipped: list[str] = []
        try:
            with zipfile.ZipFile(zip_path) as z:
                for info in z.infolist():
                    name = Path(info.filename).name  # flatten: no folders, no path tricks
                    if info.is_dir() or name.startswith("."):
                        continue
                    if Path(name).suffix.lower() not in ALLOWED:
                        skipped.append(name)
                        continue
                    (workspace / name).write_bytes(z.read(info))
                    db.add(Document(run_id=run.id, filename=name))
        except zipfile.BadZipFile:
            run.status, run.error = "failed", "The uploaded file is not a valid zip."
            db.commit()
            raise HTTPException(400, "Not a valid zip file.")
        db.commit()

        n_docs = db.query(Document).filter(Document.run_id == run.id).count()
        if n_docs == 0:
            run.status, run.error = "failed", "The zip contained no readable documents."
            db.commit()
            raise HTTPException(400, "Zip contained no supported documents.")

        start_background(run.id, workspace)
        return {"run_id": run.id, "documents": n_docs, "skipped": skipped}
    finally:
        db.close()


@router.get("/runs")
def list_runs() -> list[dict]:
    db = SessionLocal()
    try:
        runs = db.query(Run).order_by(Run.created_at.desc()).all()
        return [_run_summary(db, r) for r in runs]
    finally:
        db.close()


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        if not run:
            raise HTTPException(404, "No such run.")
        docs = db.query(Document).filter(Document.run_id == run_id).all()
        flags = db.query(Flag).filter(Flag.run_id == run_id).all()
        return {
            **_run_summary(db, run),
            "documents": [
                {"id": d.id, "filename": d.filename, "kind": d.kind,
                 "fields": d.fields, "confidence": d.confidence,
                 "status": d.status, "error": d.error}
                for d in docs
            ],
            "flags": [
                {"id": f.id, "document_id": f.document_id, "code": f.code,
                 "reason": f.reason, "basis": f.basis, "status": f.status,
                 "resolution": f.resolution}
                for f in flags
            ],
            "outputs": run.outputs,
        }
    finally:
        db.close()


@router.get("/runs/{run_id}/documents/{doc_id}/file")
def get_document_file(run_id: str, doc_id: str):
    """Serve the original document so the review screen can show the source."""
    db = SessionLocal()
    try:
        doc = db.get(Document, doc_id)
        if not doc or doc.run_id != run_id:
            raise HTTPException(404, "No such document.")
        path = config.RUNS_DIR / run_id / doc.filename
        if not path.exists():
            raise HTTPException(404, "File missing from workspace.")
        return FileResponse(path)
    finally:
        db.close()


@router.get("/runs/{run_id}/documents/{doc_id}/preview")
def get_document_preview(run_id: str, doc_id: str):
    """First page as a PNG — browsers render images reliably, PDF plugins not."""
    from fastapi.responses import Response

    from .pipeline.images import document_to_pngs

    db = SessionLocal()
    try:
        doc = db.get(Document, doc_id)
        if not doc or doc.run_id != run_id:
            raise HTTPException(404, "No such document.")
        path = config.RUNS_DIR / run_id / doc.filename
        if not path.exists():
            raise HTTPException(404, "File missing from workspace.")
        return Response(content=document_to_pngs(path)[0], media_type="image/png")
    finally:
        db.close()


@router.post("/runs/{run_id}/flags/{flag_id}/decide")
def decide_flag(run_id: str, flag_id: str, body: dict) -> dict:
    """Record a human decision on one flag. body = {decision, note, exclude_document}."""
    decision = body.get("decision")
    if decision not in ("accepted", "rejected"):
        raise HTTPException(400, "decision must be 'accepted' or 'rejected'.")
    db = SessionLocal()
    try:
        flag = db.get(Flag, flag_id)
        if not flag or flag.run_id != run_id:
            raise HTTPException(404, "No such flag.")
        flag.status = decision
        flag.resolution = str(body.get("note", ""))
        db.add(AuditEvent(
            run_id=run_id, actor="reviewer",
            action=f"flag_{decision}",
            detail=f"[{flag.code}] {flag.reason} — note: {flag.resolution or 'none'}",
        ))
        # Rebuild the copy blocks: rejected flags exclude their document.
        run = db.get(Run, run_id)
        docs = db.query(Document).filter(Document.run_id == run_id).all()
        excluded = {
            f.document_id
            for f in db.query(Flag).filter(Flag.run_id == run_id, Flag.status == "rejected")
        }
        run.outputs = output_builder.build_outputs(docs, excluded)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


def _run_summary(db, run: Run) -> dict:
    open_flags = (
        db.query(Flag).filter(Flag.run_id == run.id, Flag.status == "open").count()
    )
    n_docs = db.query(Document).filter(Document.run_id == run.id).count()
    return {
        "id": run.id, "client": run.client, "status": run.status,
        "error": run.error, "progress": run.progress,
        "documents_total": n_docs, "open_flags": open_flags,
        "created_at": run.created_at.isoformat(),
    }
