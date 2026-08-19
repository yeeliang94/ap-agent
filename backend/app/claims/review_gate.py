"""The human gate on the Payment Listing, and the outputs behind it.

Universal control 11 (hardening H9): nothing leaves this module while a
person still has something to decide. What blocks is NAMED (so a screen can
say why) and recomputed from the reviewed state on the server, whatever a
screen asks for — the gate is here, not in the browser.

Building the listing is code only (Decimal, no AI), so it can be done as
often as it is asked for. STORING it is a different act: `run.outputs` is
the record of what was released, and the replay bundle re-derives the money
from it. So a GET builds and returns; the paths that CHANGE the reviewed
state — every review action, and the end of verification — store.
"""
from __future__ import annotations

from fastapi import HTTPException

from .. import telemetry
from ..models import AuditEvent
from . import cases as cases_mod
from .investigator.contracts import REVIEWER_DISPOSITIONS
from .models import (ClaimCase, ClaimEmployee, ClaimFlag, ClaimSourceArtifact, ClaimsRun)


def bump_revision(run: ClaimsRun) -> int:
    """The revision the screen will see next.

    A request that carried an expected_revision already claimed the next
    number when the route compared and set it (routes._revision_check
    leaves `_revision_claimed` on the run to say so); one that did not
    bumps here."""
    if getattr(run, "_revision_claimed", False):
        return int(run.revision or 0)
    return cases_mod.bump_revision(run)


def output_blockers(db, run: ClaimsRun, open_flags: list) -> list[str]:
    """What keeps the Payment Listing locked, in plain sentences."""
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


def outputs_if_unlocked(db, run: ClaimsRun, blockers: list[str]) -> dict:
    """The Payment Listing, or {} while anything blocks it. Pure: the
    blockers are passed in (the caller has usually just computed them, and
    computing them twice per read is two more passes over the run)."""
    from . import listing as listing_mod

    if blockers:
        return {}
    return listing_mod.build_outputs(db, run)


def store_outputs(db, run: ClaimsRun) -> dict:
    """Rebuild the outputs and keep them on the run when they changed.

    Called where the reviewed state changed — the end of verification and
    every review action — and never from a read. A GET that commits is a
    surprise: two browsers polling a run would fight over the same row, and
    a reader would be writing the record of a release nobody asked for."""
    open_flags = db.query(ClaimFlag).filter(ClaimFlag.run_id == run.id, ClaimFlag.status == "open").all()
    try:
        outputs = outputs_if_unlocked(db, run, output_blockers(db, run, open_flags))
    except Exception as exc:
        # Storing is a side errand of closing a run or recording a review
        # action: a listing that cannot be built is a diary line, never the
        # reason a verified run fails to close. The read path still shows
        # the failure to whoever asks for the output.
        db.rollback()
        telemetry.record_failure(db, run.id, "output", "OUTPUT_NOT_BUILT",
                                 "The payment listing could not be built yet", exc)
        return run.outputs or {}
    if outputs == (run.outputs or {}):
        return outputs
    run.outputs = outputs
    db.commit()
    if outputs and not outputs["totals"]["match"]:
        telemetry.record(db, run.id, "output", telemetry.WARNING, "RECONCILIATION_MISMATCH",
                         f"Emitted total {outputs['totals']['total_myr']} differs from the source "
                         f"total {outputs['totals']['source_total']} by {outputs['totals']['difference']}.")
    return outputs


DISPOSITIONS = ("used", *REVIEWER_DISPOSITIONS)


def set_disposition(db, run_id: str, artifact_id: str, disposition: str, reason: str, actor: str) -> ClaimSourceArtifact:
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
    bump_revision(run)
    note = ""
    reverify = _reverify_case_after_disposition(db, run, art, old, disposition)
    if reverify:
        note = f"; {reverify[1]} will be re-verified"
    db.add(AuditEvent(run_id=run_id, actor=actor, action="artifact_disposition",
                      detail=f"{art.path}: {old} -> {disposition} — {reason.strip() or 'no reason given'}{note}"[:2000]))
    return art


def _reverify_case_after_disposition(db, run: ClaimsRun, art: ClaimSourceArtifact, old: str,
                                     new: str) -> tuple[str, str] | None:
    """A file inside a case whose worker already ran (the run is verifying
    or ready) cannot change what it is without the case being verified
    again: its rows, evidence and flags were derived from that file.

    verifying → refused (the worker may be reading it this moment).
    ready     → the case's roles are recomputed from the dispositions,
                mirrored onto its employee record, the employee is set
                pending, and the caller starts the retry worker. Returns
                (employee id, case label) when a re-verification is due."""
    if old == new or not art.case_id or run.status not in ("verifying", "ready"):
        return None
    case = db.get(ClaimCase, art.case_id)
    if case is None or not case.legacy_employee_id:
        return None
    emp = db.get(ClaimEmployee, case.legacy_employee_id)
    if emp is None or emp.status in ("pending", "skipped"):
        return None
    from . import grouping

    def read_by_worker(roles: dict) -> bool:
        return art.path == roles.get("report_file") or art.path in (roles.get("receipt_files") or [])

    artifacts = db.query(ClaimSourceArtifact).filter(ClaimSourceArtifact.run_id == run.id).all()
    new_roles = grouping.roles_for_case(case, artifacts, run.survey or {})
    if read_by_worker(case.roles or {}) == read_by_worker(new_roles):
        return None  # the worker never read this file, and still would not: nothing derived changes
    if run.status == "verifying":
        # nothing is committed on this path: the route closes its session
        raise HTTPException(400, f"{art.path} sits in case {case.label}, which is being verified right now. "
                                 "Settle it once the run is ready — the case is then verified again.")
    case.roles = new_roles
    emp.roles = dict(new_roles)
    emp.status, emp.error = "pending", ""
    cases_mod.sync_case_from_employee(db, emp)
    telemetry.record(db, run.id, "review", telemetry.INFO, "CASE_REVERIFY",
                     f"{case.label}: {art.path} is now {new}; the case is verified again on the remaining files.")
    return emp.id, case.label


def pending_retry(db, run_id: str, art: ClaimSourceArtifact) -> str | None:
    """The employee `_reverify_case_after_disposition` set pending for this
    file's case, if any (looked up after the commit, so the retry worker
    sees the committed state)."""
    if not art.case_id:
        return None
    case = db.get(ClaimCase, art.case_id)
    if case is None or not case.legacy_employee_id:
        return None
    emp = db.get(ClaimEmployee, case.legacy_employee_id)
    run = db.get(ClaimsRun, run_id)
    return emp.id if emp is not None and emp.status == "pending" and run.status == "ready" else None
