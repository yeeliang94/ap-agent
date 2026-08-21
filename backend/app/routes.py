"""API routes: upload a batch, poll a run, review flags, fetch outputs."""
from __future__ import annotations

import logging
import os
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func

from . import config, settings_store, switches
from .db import SessionLocal
from .models import AuditEvent, Document, Flag, Run, RunEvent
from .pipeline import output as output_builder
from .pipeline import reference
from .pipeline.runner import start_background

router = APIRouter()


def _refs_for(run: Run) -> Path:
    """The run's private copy of the reference files. A run created before
    snapshots existed has none yet; it is taken now, once, from the folder
    the run recorded at start — never from whatever Settings says today."""
    refs = reference.run_refs(run.id)
    reference.ensure_snapshot(refs, run.snapshot.get("sharepoint_folder_url"))
    return refs


log = logging.getLogger("routes")


@router.get("/settings")
def get_settings() -> dict:
    """The app-level settings shown on the main screen. Secrets stay in .env."""
    return {
        "client_name": settings_store.get_setting("client_name"),
        "sharepoint_folder_url": settings_store.get_setting("sharepoint_folder_url"),
        "draft_prepared_by": settings_store.get_setting("draft_prepared_by"),
        "draft_reviewed_by": settings_store.get_setting("draft_reviewed_by"),
        "draft_bank_charge": settings_store.get_setting("draft_bank_charge"),
    }


@router.put("/settings")
def update_settings(body: dict) -> dict:
    """Save the on-screen settings. body = {client_name, sharepoint_folder_url,
    draft_prepared_by, draft_reviewed_by, draft_bank_charge}; the draft
    fields are optional and keep their current value when absent."""
    from decimal import Decimal, InvalidOperation

    client_name = body.get("client_name")
    folder_url = body.get("sharepoint_folder_url")
    if not isinstance(client_name, str) or not client_name.strip():
        raise HTTPException(400, "Client name cannot be empty.")
    if len(client_name.strip()) > 120:
        raise HTTPException(400, "Client name is too long (max 120 characters).")
    if not isinstance(folder_url, str) or not folder_url.strip().startswith("https://"):
        raise HTTPException(400, "SharePoint folder URL must start with https:// — "
                                 "copy it from the browser's address bar.")
    if len(folder_url.strip()) > 500:
        raise HTTPException(400, "SharePoint folder URL is too long (max 500 characters).")
    values = {
        "client_name": client_name.strip(),
        "sharepoint_folder_url": folder_url.strip(),
    }
    for key in ("draft_prepared_by", "draft_reviewed_by"):
        if key in body:
            if not isinstance(body[key], str) or len(body[key].strip()) > 80:
                raise HTTPException(400, f"{key.replace('_', ' ')} must be text, at most 80 characters.")
            values[key] = body[key].strip()
    if "draft_bank_charge" in body:
        try:
            charge = Decimal(str(body["draft_bank_charge"]).strip() or "0")
            if not charge.is_finite():
                raise InvalidOperation
        except InvalidOperation:
            raise HTTPException(400, "Bank charge per payment must be a number, e.g. 0.10.")
        if charge < 0 or charge > 1000:
            raise HTTPException(400, "Bank charge per payment must be between 0 and 1000.")
        values["draft_bank_charge"] = f"{charge:.2f}"
    settings_store.set_settings(values)
    return get_settings()


@router.get("/settings/switches")
def get_switches() -> dict:
    """Every feature switch with its words and value, plus the read-only
    .env deployment facts (whether a thing is set — never a secret)."""
    return {"switches": switches.listed(), "deployment": switches.deployment_facts()}


@router.put("/settings/switches")
def update_switches(body: dict) -> dict:
    """Flip switches. body = {switch_key: true/false, ...}. A change
    applies to new claims runs only — a run keeps the switches it started
    with — and is recorded in the audit trail like any settings change."""
    values: dict[str, str] = {}
    for key, value in body.items():
        if key not in switches.SWITCHES:
            raise HTTPException(400, f"{key} is not a switch.")
        if not isinstance(value, bool):
            raise HTTPException(400, f"{key} must be true or false.")
        values[key] = "1" if value else "0"
    if values:
        settings_store.set_settings(values)
    return get_switches()


# Only these file types may come out of an uploaded zip. Anything else is
# skipped and reported — never silently processed.
ALLOWED = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}


@router.post("/runs")
async def create_run(client: str = Form(...), batch: UploadFile = File(...)) -> dict:
    # Single-client MVP: applying the configured client's policy to a
    # different client's documents would be silent nonsense — refuse.
    configured = settings_store.get_setting("client_name")
    if client.strip() != configured:
        raise HTTPException(
            400, f"This installation is configured for {configured!r} only "
                 "(changeable in Settings on the main screen)."
        )
    payload = await batch.read()
    if len(payload) > config.MAX_ZIP_MB * 1024 * 1024:
        raise HTTPException(400, f"Zip exceeds the {config.MAX_ZIP_MB} MB limit.")

    db = SessionLocal()
    try:
        # Snapshot the settings this run is judged under: switching clients
        # in Settings later must never change how THIS run is checked.
        run = Run(client=client, snapshot={
            "client_name": configured,
            "sharepoint_folder_url": settings_store.get_setting("sharepoint_folder_url"),
            # The listing draft's signatures and bank charge, as they stood
            # when this run started: editing them "for the next run" must
            # not change an older run's fund request on its next rebuild.
            "draft_settings": settings_store.draft_settings_raw(),
        })
        db.add(run)
        db.commit()

        workspace = config.RUNS_DIR / run.id
        workspace.mkdir(parents=True)
        zip_path = workspace / "upload.zip"
        zip_path.write_bytes(payload)

        skipped: list[str] = []
        n_files = 0
        try:
            with zipfile.ZipFile(zip_path) as z:
                for info in z.infolist():
                    name = Path(info.filename).name  # flatten: no folders, no path tricks
                    if info.is_dir() or name.startswith("."):
                        continue
                    if Path(name).suffix.lower() not in ALLOWED:
                        skipped.append(name)
                        continue
                    # Quotas checked BEFORE decompressing each entry — a zip
                    # bomb or runaway batch stops here, not in memory.
                    if info.file_size > config.MAX_FILE_MB * 1024 * 1024:
                        skipped.append(f"{name} (over {config.MAX_FILE_MB} MB)")
                        continue
                    n_files += 1
                    if n_files > config.MAX_BATCH_FILES:
                        raise HTTPException(
                            400, f"Batch exceeds {config.MAX_BATCH_FILES} documents."
                        )
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
        tallies = _tallies(db, [r.id for r in runs])
        return [_run_summary(db, r, tallies.get(r.id, {})) for r in runs]
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
        open_flags = [f for f in flags if f.status == "open"]
        return {
            **_run_summary(db, run),
            "documents": [
                {"id": d.id, "filename": d.filename, "kind": d.kind,
                 "fields": d.fields, "confidence": d.confidence,
                 "corrections": d.corrections,
                 "status": d.status, "error": d.error, "page_count": d.page_count}
                for d in docs
            ],
            "flags": [
                {"id": f.id, "document_id": f.document_id, "code": f.code,
                 "reason": f.reason, "basis": f.basis, "status": f.status,
                 "resolution": f.resolution}
                for f in flags
            ],
            # The human gate, enforced server-side: no copy-ready output
            # leaves the API while any flag is undecided. The frontend lock
            # is a convenience; this is the rule.
            "outputs": run.outputs if (run.status == "ready" and not open_flags) else {},
        }
    finally:
        db.close()


@router.get("/sharepoint/status")
def sharepoint_status() -> dict:
    """Whether a delegated SharePoint sign-in is needed, and whether we have one."""
    from . import sharepoint_auth

    required = os.getenv("MCP_OAUTH", "").strip().lower() in ("1", "true", "on", "yes")
    return {"required": required,
            "connected": required and sharepoint_auth.storage().is_signed_in()}


@router.post("/sharepoint/connect")
async def sharepoint_connect() -> dict:
    """Sign in to SharePoint, at the reviewer's explicit request.

    The ONLY place a browser window may open. Background pipeline runs
    reuse whatever this saved; they never ask, because a sign-in prompt
    appearing during an unattended run is how a run waits forever.
    """
    from . import sharepoint_auth

    url = os.getenv("MCP_URL", "").strip()
    if not url:
        raise HTTPException(400, "No SharePoint MCP address is configured "
                                 "(MCP_URL in .env).")
    header = os.getenv("MCP_AUTH_HEADER", "").strip()
    value = os.getenv("MCP_AUTH_VALUE", "").strip()
    headers = {header: value} if header and value else {}
    try:
        who = await sharepoint_auth.connect(url, headers)
    except sharepoint_auth.SignInRequired as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        # The reason matters more than the class name, and the full
        # detail is already in the server log.
        from .telemetry import describe_failure

        log.warning("SharePoint sign-in failed: %s", exc, exc_info=True)
        # Which half broke is the difference between "the gateway is
        # flaky" and "the sign-in never landed", and a reviewer reading
        # only "could not connect" cannot tell those apart — nor could
        # anyone reading the log afterwards.
        phase = getattr(exc, "ap_phase", "")
        detail = f"Could not connect to SharePoint: {describe_failure(exc)}"
        if phase:
            detail += f" This happened while {phase}."
        raise HTTPException(502, detail) from exc
    return {"connected": True, "signed_in_as": who}


@router.post("/sharepoint/disconnect")
def sharepoint_disconnect() -> dict:
    """Forget the saved sign-in. Used when switching Windows accounts."""
    from . import sharepoint_auth

    sharepoint_auth.storage().forget()
    return {"connected": False}


@router.get("/runs/{run_id}/events")
def get_run_events(run_id: str, level: str = "") -> list[dict]:
    """The run's diary: what each stage did, and where it struggled.

    Served separately from the run itself because the review screen polls
    the run every second and does not need the whole history each time.

    level=problems returns only warnings and errors — what the reviewer
    opens the panel to see.
    """
    db = SessionLocal()
    try:
        if not db.get(Run, run_id):
            raise HTTPException(404, "No such run.")
        query = db.query(RunEvent).filter(RunEvent.run_id == run_id)
        if level == "problems":
            query = query.filter(RunEvent.level.in_(("warning", "error")))
        return [
            {"id": e.id, "at": e.at.isoformat(), "stage": e.stage,
             "level": e.level, "code": e.code, "message": e.message,
             "detail": e.detail, "document_id": e.document_id}
            for e in query.order_by(RunEvent.id).all()
        ]
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


@router.get("/runs/{run_id}/draft")
def get_listing_draft(run_id: str):
    """The drafted payment-listing workbook (a copy of the client's file with
    one new tab). Gated like every other output: nothing leaves while a
    flag is still open."""
    db = SessionLocal()
    try:
        run = db.get(Run, run_id)
        if not run:
            raise HTTPException(404, "No such run.")
        open_flags = db.query(Flag).filter(Flag.run_id == run_id, Flag.status == "open").count()
        if run.status != "ready" or open_flags:
            raise HTTPException(409, "The draft is available once every flag is decided.")
        draft = (run.outputs or {}).get("listing_draft") or {}
        name = draft.get("file")
        if not name:
            raise HTTPException(404, draft.get("skipped") or "No listing draft for this run.")
        path = output_builder.draft_dir(reference.run_refs(run_id)) / name
        if not path.is_file():
            raise HTTPException(404, "Draft file missing from workspace — rebuild by deciding any flag again.")
        return FileResponse(path, filename=name,
                            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    finally:
        db.close()


@router.get("/runs/{run_id}/documents/{doc_id}/preview")
def get_document_preview(run_id: str, doc_id: str, page: int = Query(1, ge=1)):
    """One page as PNG. Page numbers are one-based and bounds checked."""
    from fastapi.responses import Response

    from .pipeline.images import document_page_count, document_page_png

    db = SessionLocal()
    try:
        doc = db.get(Document, doc_id)
        if not doc or doc.run_id != run_id:
            raise HTTPException(404, "No such document.")
        path = config.RUNS_DIR / run_id / doc.filename
        if not path.exists():
            raise HTTPException(404, "File missing from workspace.")
        count = doc.page_count
        if count is None:
            try:
                count = document_page_count(path)
            except ValueError as exc:
                raise HTTPException(415, str(exc)) from exc
        if page > count:
            raise HTTPException(400, f"Page {page} is outside this document (1–{count}).")
        try:
            rendered = document_page_png(path, page)
        except IndexError as exc:
            raise HTTPException(400, f"Page {page} is outside this document (1–{count}).") from exc
        except ValueError as exc:
            raise HTTPException(415, str(exc)) from exc
        return Response(content=rendered, media_type="image/png")
    finally:
        db.close()


# Which fields a human may correct, per document kind. Computed fields
# (category, clause) are re-derived, not edited.
CORRECTABLE = {
    "invoice": {"vendor", "invoice_number", "date", "amount", "currency"},
    "claim": {"claimant", "description", "amount", "currency"},
}


def _validate_correction(field: str, value):
    """Corrections face the same strictness as AI answers — the reviewer is
    the authority on what the document says, but not exempt from typos."""
    import math
    import re as _re

    from .schemas_ai import CURRENCY_PATTERN, DATE_PATTERN

    if field == "amount":
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise HTTPException(400, "Amount must be a number.")
        if not math.isfinite(v) or v <= 0:
            raise HTTPException(400, "Amount must be a positive number.")
        return v
    value = str(value).strip()
    if field == "date" and not _re.match(DATE_PATTERN, value):
        raise HTTPException(400, "Date must be YYYY-MM-DD.")
    if field == "currency" and not _re.match(CURRENCY_PATTERN, value.upper()):
        raise HTTPException(400, "Currency must be a 3-letter code, e.g. MYR.")
    if field == "currency":
        return value.upper()
    if not value:
        raise HTTPException(400, "Value cannot be empty.")
    return value[:300]


@router.post("/runs/{run_id}/documents/{doc_id}/correct")
async def correct_fields(run_id: str, doc_id: str, body: dict) -> dict:
    """Record audited human corrections of extracted values (one request per
    document, however many fields changed), then re-check the affected
    documents and rebuild the outputs.

    body = {fields: {name: value, ...}, reason}. Every value is validated
    before any is applied. Flags that no longer apply are marked
    resolved_by_correction (kept, never deleted); newly triggered rules
    raise fresh open flags; codes a human already decided stay decided.
    """
    from .pipeline.checks import RULE_FIELDS, run_checks

    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise HTTPException(400, "A short reason is required — it goes in the audit trail.")
    reason = reason.strip()[:300]
    submitted = body.get("fields")
    if not isinstance(submitted, dict) or not submitted:
        raise HTTPException(400, "fields must map field names to corrected values.")

    db = SessionLocal()
    try:
        doc = db.get(Document, doc_id)
        run = db.get(Run, run_id)
        if not doc or not run or doc.run_id != run_id:
            raise HTTPException(404, "No such document.")
        if run.status != "ready":
            raise HTTPException(400, "Corrections are only possible once the run is ready.")

        # Validate every field before applying any — a typo in one field
        # must not leave the document half-corrected.
        validated = {}
        for field, raw in submitted.items():
            field = str(field)
            if field not in CORRECTABLE.get(doc.kind, set()):
                raise HTTPException(400, f"Field {field!r} is not correctable on a {doc.kind}.")
            validated[field] = _validate_correction(field, raw)

        # An invoice-number change can create or clear a DUPLICATE flag on
        # the OTHER document carrying that number, so remember both numbers.
        affected_numbers = set()
        if doc.kind == "invoice":
            affected_numbers.add(str(doc.fields.get("invoice_number", "")).strip())

        changed = {f: v for f, v in validated.items() if doc.fields.get(f) != v}
        if changed:
            # Apply: new values in, human verified so the doubt notes go,
            # the derived claim fields are cleared so judgment re-runs fresh.
            new_fields = {**doc.fields, **changed}
            new_fields.pop("category", None)
            new_fields.pop("clause", None)
            corrections = dict(doc.corrections)
            for field, value in changed.items():
                old = doc.fields.get(field)
                corrections[field] = {"from": old, "to": value, "reason": reason}
                db.add(AuditEvent(
                    run_id=run_id, actor="reviewer", action="field_corrected",
                    detail=f"{doc.filename}.{field}: {old!r} -> {value!r} — {reason}",
                ))
            doc.fields = new_fields
            doc.confidence = {k: v for k, v in doc.confidence.items() if k not in changed}
            doc.corrections = corrections
            # Outputs were built from the old value — withdraw them in the
            # same commit, so a failed re-check can never leave stale copy
            # blocks published. They are rebuilt below on success.
            run.outputs = {}
            db.commit()
        # No early return when nothing changed: a retry after "correction
        # saved, but re-check failed" must repair the stale flags rather
        # than short-circuit on the already-applied value.

        docs = db.query(Document).filter(Document.run_id == run_id).all()
        if doc.kind == "invoice":
            affected_numbers.add(str(doc.fields.get("invoice_number", "")).strip())
        scope = {doc_id} | {
            d.id for d in docs
            if d.id != doc_id and d.kind == "invoice"
            and str(d.fields.get("invoice_number", "")).strip() in affected_numbers
        }
        try:
            new_flag_dicts = await run_checks(
                docs, only_doc_ids=scope, refs=_refs_for(run))
        except Exception as exc:
            raise HTTPException(
                500, f"Correction saved, but re-check failed: {exc} — "
                     "save again to retry the re-check.")

        new_by_doc: dict[str, set[str]] = {}
        for fd in new_flag_dicts:
            new_by_doc.setdefault(fd["document_id"], set()).add(fd["code"])
        existing = db.query(Flag).filter(
            Flag.run_id == run_id, Flag.document_id.in_(scope)
        ).all()
        changed_fields = set(changed)
        open_now = {}
        for fl in existing:
            if fl.status == "open" and fl.code not in new_by_doc.get(fl.document_id, set()):
                fl.status = "resolved_by_correction"
                fl.resolution = (f"No longer applies after correcting "
                                 f"{', '.join(sorted(changed)) or 'values'} — {reason}")
            if fl.status == "open":
                open_now[(fl.document_id, fl.code)] = fl
            # A rejection judged the OLD value. Once that value is
            # corrected, the rejection no longer stands — supersede it so
            # it stops excluding the document. If the rule still fires on
            # the new value, the fresh open flag below demands a new
            # decision before anything reaches the output.
            if (fl.status == "rejected"
                    and RULE_FIELDS.get(fl.code, changed_fields) & changed_fields):
                fl.status = "superseded_by_correction"
                fl.resolution = (f"Rejection superseded by correcting "
                                 f"{', '.join(sorted(changed))} — {reason}")
        # A decided flag stays decided ONLY while the values its rule rests
        # on are unchanged. Correcting one of those values makes any
        # re-triggered rule a NEW incident the human has not seen — e.g. a
        # decided duplicate of number OLD must not hide a fresh duplicate
        # of number NEW.
        decided_covers = {
            (fl.document_id, fl.code) for fl in existing
            if fl.status in ("accepted", "rejected")
            and not (RULE_FIELDS.get(fl.code, changed_fields) & changed_fields)
        }
        for fd in new_flag_dicts:
            key = (fd["document_id"], fd["code"])
            if key in open_now:
                # Rule still applies — refresh the message so it describes
                # the corrected values, not the old ones.
                open_now[key].reason = fd["reason"]
                open_now[key].basis = fd["basis"]
            elif key not in decided_covers:
                db.add(Flag(run_id=run_id, **fd))
        db.add(AuditEvent(
            run_id=run_id, actor="system", action="document_rechecked",
            detail=f"{doc.filename} after correcting "
                   f"{', '.join(sorted(changed)) or 'nothing (retry)'}: "
                   f"{len(new_flag_dicts)} rule(s) now apply",
        ))

        # Rebuild the copy blocks from the corrected state. Flush first:
        # the session doesn't autoflush, so without it the exclusion query
        # would still see the pre-correction flag statuses.
        db.flush()
        excluded = {
            fl.document_id
            for fl in db.query(Flag).filter(Flag.run_id == run_id, Flag.status == "rejected")
        } | {d.id for d in docs if d.kind == "unknown"}
        run.outputs = await output_builder.build_outputs(
            docs, excluded, refs=_refs_for(run),
            draft_settings=settings_store.draft_settings(run.snapshot))
        db.commit()
        return {"ok": True}
    finally:
        db.close()


@router.post("/runs/{run_id}/flags/{flag_id}/decide")
async def decide_flag(run_id: str, flag_id: str, body: dict) -> dict:
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
        # Flush first — the session doesn't autoflush, so without it the
        # exclusion query would not see THIS decision yet and the rebuilt
        # outputs would lag one decision behind.
        db.flush()
        run = db.get(Run, run_id)
        docs = db.query(Document).filter(Document.run_id == run_id).all()
        excluded = {
            f.document_id
            for f in db.query(Flag).filter(Flag.run_id == run_id, Flag.status == "rejected")
        }
        # Documents the pipeline couldn't place are never in the output,
        # whatever the reviewer decided about their flag.
        excluded |= {d.id for d in docs if d.kind == "unknown"}
        # Withdraw the old outputs BEFORE rebuilding. If this was the last
        # open flag and the rebuild fails, the gate would otherwise open on
        # outputs built before the decision — bank rows and a listing draft
        # still holding a document the reviewer just rejected.
        run.outputs = {}
        try:
            run.outputs = await output_builder.build_outputs(
                docs, excluded, refs=_refs_for(run),
                draft_settings=settings_store.draft_settings(run.snapshot))
        except Exception as exc:
            db.commit()  # the decision stands; the outputs stay withdrawn
            raise HTTPException(500, f"Decision recorded, but output rebuild failed: {exc}")
        db.commit()
        return {"ok": True}
    finally:
        db.close()


def _tallies(db, run_ids: list[str]) -> dict[str, dict]:
    """Per-run counts for the summary, in three queries however many runs.

    The runs screen polls every three seconds. Counting in SQL (and for
    every run at once) keeps that poll cheap; counting in Python meant
    loading every flag, document and diary entry in the database each
    time, which grows with the pilot rather than staying flat.
    """
    if not run_ids:
        return {}
    tally: dict[str, dict] = {
        run_id: {"documents_total": 0, "open_flags": 0, "errors": 0, "warnings": 0}
        for run_id in run_ids
    }
    docs = (db.query(Document.run_id, func.count(Document.id))
            .filter(Document.run_id.in_(run_ids)).group_by(Document.run_id))
    for run_id, n in docs:
        tally[run_id]["documents_total"] = n

    flags = (db.query(Flag.run_id, func.count(Flag.id))
             .filter(Flag.run_id.in_(run_ids), Flag.status == "open")
             .group_by(Flag.run_id))
    for run_id, n in flags:
        tally[run_id]["open_flags"] = n

    # Diary counts, so every screen can show that something went wrong
    # without fetching the diary itself. A run can reach "ready" with
    # errors recorded — that combination is precisely what used to pass
    # unnoticed, so it is carried on every summary.
    events = (db.query(RunEvent.run_id, RunEvent.level, func.count(RunEvent.id))
              .filter(RunEvent.run_id.in_(run_ids),
                      RunEvent.level.in_(("warning", "error")))
              .group_by(RunEvent.run_id, RunEvent.level))
    for run_id, level, n in events:
        tally[run_id]["errors" if level == "error" else "warnings"] = n
    return tally


def _run_summary(db, run: Run, tally: dict | None = None) -> dict:
    counts = (tally if tally is not None
              else _tallies(db, [run.id]).get(run.id, {}))
    return {
        "id": run.id, "client": run.client, "status": run.status,
        "error": run.error, "progress": run.progress,
        "documents_total": counts.get("documents_total", 0),
        "open_flags": counts.get("open_flags", 0),
        "errors": counts.get("errors", 0),
        "warnings": counts.get("warnings", 0),
        "created_at": run.created_at.isoformat(),
    }
