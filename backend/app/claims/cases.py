"""The case model's storage helpers (H2): store an InvestigationResult on a
run, keep the ClaimCase mirror of every ClaimEmployee in step (dual-write
during the compatibility period), and serialise cases / artifacts /
assignments for HTTP.

Rules kept here so every caller gets them for free:
  - a reviewer's artifact disposition is never overwritten by an adapter
  - a case's worker fields (status, totals, category, summary) come from
    the employee record while both exist; its grouping fields (claimant,
    state, roles, artifacts) come from the confirmed result
  - Reported Total is written from the source's figure only; Calculated
    Lines Total is stored beside it, never in its place
"""
from __future__ import annotations

from typing import Any

from .investigator import contracts as C
from .investigator.legacy import case_id_for
from . import profile as profile_mod
from .models import (ClaimCase, ClaimEmployee, ClaimEvidenceAssignment, ClaimFlag, ClaimInvestigation,
                     ClaimSourceArtifact, ClaimToolExecution, ClaimsRun)


def bump_revision(run: ClaimsRun) -> int:
    run.revision = int(run.revision or 0) + 1
    return run.revision


# ---- storing a normalized result --------------------------------------------------

def store_result(s, run: ClaimsRun, result: C.InvestigationResult, confirmed: bool = False) -> ClaimInvestigation:
    """Write (or refresh) the artifacts, cases, assignments, plan and tool
    record of a result. Called at map time (proposal) and at confirm time
    (the reviewer's corrected map, confirmed=True — the confirmed record
    keeps the proposal's plan, adapter and strategy: confirmation is a
    reviewer step, not a second investigation)."""
    if confirmed:
        previous = s.query(ClaimInvestigation).filter(ClaimInvestigation.run_id == run.id) \
            .order_by(ClaimInvestigation.created_at.desc(), ClaimInvestigation.id.desc()).first()
        if previous is not None:
            result = result.model_copy(update={"plan": C.InvestigationPlan(**previous.plan)})
    inv = ClaimInvestigation(run_id=run.id, adapter=result.plan.adapter, strategy=result.plan.strategy or "",
                             status="confirmed" if confirmed else "proposed", plan=result.plan.model_dump(),
                             rounds=result.plan.rounds,
                             summary={"artifacts": len(result.artifacts), "cases": len(result.cases),
                                      "assignments": len(result.assignments), "flags": len(result.flags),
                                      "unresolved": len(result.unresolved_artifacts()),
                                      "warnings": list(result.warnings)[:50],
                                      "assumptions": list(result.assumptions)[:50],
                                      "questions": list(result.questions)[:50]})
    s.add(inv)
    s.flush()
    for t in result.tool_executions:
        s.add(ClaimToolExecution(run_id=run.id, investigation_id=inv.id, tool=t.tool, elapsed_ms=t.elapsed_ms,
                                 input_hashes=list(t.input_hashes), output_hash=t.output_hash,
                                 truncated=1 if t.truncated else 0, error_code=t.error_code, note=t.note))
    upsert_artifacts(s, run.id, result.artifacts)
    upsert_cases(s, run.id, result.cases, confirmed)
    upsert_assignments(s, run.id, result.assignments)
    upsert_flags(s, run.id, result.flags)
    s.flush()
    return inv


def upsert_flags(s, run_id: str, flags: list[C.FlagProposal]) -> int:
    """The investigation's own flags (TOOL_FAILED, TOOL_UNAVAILABLE,
    OWNERSHIP_CONFLICT, …) as ClaimFlag rows — once per identity key; a
    flag a person already decided is not raised again. Returns how many
    were added."""
    existing = {profile_mod.flag_key(f) for f in s.query(ClaimFlag).filter(ClaimFlag.run_id == run_id)}
    added = 0
    for f in flags:
        d = f.model_dump()
        if profile_mod.flag_key(d) in existing:
            continue
        s.add(ClaimFlag(run_id=run_id, employee_id="", case_id=f.case_id, artifact_id=f.artifact_id,
                        row_id=f.row_id, evidence_id=f.evidence_id, code=f.code, reason=f.reason,
                        basis=f.basis, cite=dict(f.cite), status=f.status))
        existing.add(profile_mod.flag_key(d))
        added += 1
    s.flush()
    return added


def upsert_artifacts(s, run_id: str, artifacts: list[C.SourceArtifact]) -> None:
    existing = {a.artifact_id: a for a in s.query(ClaimSourceArtifact).filter(ClaimSourceArtifact.run_id == run_id)}
    for a in artifacts:
        rec = existing.get(a.id)
        if rec is None:
            rec = ClaimSourceArtifact(run_id=run_id, artifact_id=a.id)
            s.add(rec)
        rec.path, rec.sha256, rec.media_type, rec.size, rec.pages = a.path, a.sha256, a.media_type, a.size, a.pages
        rec.sheets = list(a.sheets)
        rec.inspection_state, rec.failure_reason = a.inspection_state, a.failure_reason
        rec.proposed_role, rec.role_reason = a.proposed_role, a.role_reason
        rec.role_citations = [c.model_dump() for c in a.role_citations]
        if rec.disposition_by != "reviewer":
            rec.disposition, rec.disposition_reason, rec.disposition_by = a.disposition, a.disposition_reason, a.disposition_by
            rec.needs_confirmation = 1 if a.needs_confirmation else 0
    # the case each file belongs to, from the assignments (set by upsert_assignments)


def upsert_cases(s, run_id: str, cases: list[C.ClaimCase], confirmed: bool) -> None:
    existing = {c.id: c for c in s.query(ClaimCase).filter(ClaimCase.run_id == run_id)}
    for c in cases:
        rec = existing.get(c.id)
        if rec is None:
            rec = ClaimCase(id=c.id, run_id=run_id)
            s.add(rec)
        rec.label = c.label
        rec.claimant_name, rec.claimant_identifier = c.claimant.name, c.claimant.identifier
        rec.claimant_state, rec.claimant_basis = c.claimant.state, c.claimant.basis
        rec.claimant_citations = [x.model_dump() for x in c.claimant.citations]
        rec.state, rec.grouping_basis = c.state, c.grouping_basis
        rec.citations = [x.model_dump() for x in c.citations]
        rec.artifact_ids, rec.roles = list(c.artifact_ids), dict(c.roles)
        rec.confidence, rec.reason = c.confidence, c.reason
        if c.category and not rec.category:
            rec.category, rec.gl = c.category, c.gl
        if c.reported_total is not None:
            rec.reported_total = c.reported_total
            rec.reported_total_cite = c.reported_total_citation.model_dump() if c.reported_total_citation else {}
        if c.lines_total is not None:
            rec.lines_total = c.lines_total
        if confirmed and c.state == "excluded":
            rec.status, rec.error = "skipped", "skipped by the reviewer at the map"
    if confirmed:
        # cases the reviewer removed from the map are gone from the run
        keep = {c.id for c in cases}
        for cid, rec in existing.items():
            if cid not in keep:
                s.delete(rec)


def upsert_assignments(s, run_id: str, assignments: list[C.EvidenceAssignment]) -> None:
    existing = {a.key: a for a in s.query(ClaimEvidenceAssignment).filter(ClaimEvidenceAssignment.run_id == run_id)}
    seen = set()
    for a in assignments:
        rec = existing.get(a.id)
        if rec is None:
            rec = ClaimEvidenceAssignment(run_id=run_id, key=a.id)
            s.add(rec)
        seen.add(a.id)
        rec.evidence_id, rec.artifact_id, rec.case_id, rec.line_id = a.evidence_id, a.artifact_id, a.case_id, a.line_id
        # a reviewer's decision (confirmed/rejected by hand) is kept
        if rec.basis != "reviewer_decision":
            rec.state, rec.basis, rec.confidence, rec.reason = a.state, a.basis, a.confidence, a.reason
            rec.citations = [c.model_dump() for c in a.citations]
    for key, rec in existing.items():
        if key not in seen and rec.basis != "reviewer_decision" and rec.state == "proposed":
            s.delete(rec)
    # the case an artifact belongs to
    by_art: dict[str, str] = {}
    for a in assignments:
        if a.artifact_id and a.state != "rejected":
            by_art[a.artifact_id] = a.case_id
    for art in s.query(ClaimSourceArtifact).filter(ClaimSourceArtifact.run_id == run_id):
        art.case_id = by_art.get(art.artifact_id, "")


# ---- the employee ↔ case mirror ------------------------------------------------------

def link_employees(s, run: ClaimsRun) -> None:
    """After confirm-map: tie every ClaimEmployee to the case with the same
    folder label (deterministic ids), or create the case if the run
    predates the case model."""
    cases = {c.label: c for c in s.query(ClaimCase).filter(ClaimCase.run_id == run.id)}
    for emp in s.query(ClaimEmployee).filter(ClaimEmployee.run_id == run.id):
        case = cases.get(emp.folder)
        if case is None:
            case = sync_case_from_employee(s, emp)
        else:
            case.legacy_employee_id = emp.id
            _mirror(case, emp)
    s.flush()


def sync_case_from_employee(s, emp: ClaimEmployee) -> ClaimCase:
    """The case that mirrors this employee, created if missing (old runs,
    tests that build employees directly), fields mirrored."""
    case = s.query(ClaimCase).filter(ClaimCase.legacy_employee_id == emp.id).first()
    if case is None:
        cid = case_id_for(emp.run_id, emp.folder)
        case = s.get(ClaimCase, cid)
        if case is None:
            case = ClaimCase(id=cid, run_id=emp.run_id, label=emp.folder, state="confirmed",
                             claimant_state="confirmed" if emp.name else "unknown",
                             claimant_basis="the confirmed map (employee folder)",
                             grouping_basis="folder structure: one subfolder per claimant")
            s.add(case)
        case.legacy_employee_id = emp.id
    _mirror(case, emp)
    s.flush()
    return case


def _mirror(case: ClaimCase, emp: ClaimEmployee) -> None:
    case.claimant_name = case.claimant_name or emp.name
    case.claimant_identifier = case.claimant_identifier or emp.er_code
    if not case.roles:
        case.roles = dict(emp.roles or {})
    case.status, case.error = emp.status, emp.error
    case.category, case.gl, case.category_basis = emp.category, emp.gl, emp.category_basis
    case.reported_total = emp.report_total or ""
    case.lines_total = str((emp.summary or {}).get("rows_total") or "")
    case.summary = dict(emp.summary or {})


def case_for_employee(s, employee_id: str) -> ClaimCase | None:
    return s.query(ClaimCase).filter(ClaimCase.legacy_employee_id == employee_id).first()


def case_id_for_employee(s, employee_id: str) -> str:
    case = case_for_employee(s, employee_id)
    return case.id if case else ""


def materialise(s, run: ClaimsRun, inv: dict) -> None:
    """From an H1 investigation record kept on run.survey: artifacts,
    cases and assignments as rows (migration 1)."""
    try:
        result = C.InvestigationResult(
            artifacts=[C.SourceArtifact(**a) for a in inv.get("artifacts", [])],
            cases=[C.ClaimCase(**c) for c in inv.get("cases", [])],
            assignments=[C.EvidenceAssignment(**a) for a in inv.get("assignments", [])],
            plan=C.InvestigationPlan(**(inv.get("plan") or {})))
    except Exception:
        return
    confirmed = bool((run.map or {}).get("confirmed"))
    if not s.query(ClaimInvestigation).filter(ClaimInvestigation.run_id == run.id).first():
        store_result(s, run, result, confirmed=confirmed)
    link_employees(s, run)


# ---- serialisation ------------------------------------------------------------------

def case_dict(c: ClaimCase) -> dict[str, Any]:
    return {"id": c.id, "employee_id": c.legacy_employee_id, "label": c.label,
            "claimant": {"name": c.claimant_name, "identifier": c.claimant_identifier, "state": c.claimant_state,
                         "basis": c.claimant_basis, "citations": c.claimant_citations},
            "state": c.state, "grouping_basis": c.grouping_basis, "citations": c.citations,
            "artifact_ids": c.artifact_ids, "roles": c.roles, "status": c.status, "error": c.error,
            "category": c.category, "gl": c.gl, "category_basis": c.category_basis,
            "reported_total": c.reported_total, "reported_total_cite": c.reported_total_cite,
            "lines_total": c.lines_total, "summary": c.summary, "confidence": c.confidence, "reason": c.reason,
            # delivered aliases, so the employee-keyed screen renders a case
            "folder": c.label, "name": c.claimant_name, "er_code": c.claimant_identifier,
            "report_total": c.reported_total}


def artifact_dict(a: ClaimSourceArtifact) -> dict[str, Any]:
    return {"id": a.artifact_id, "path": a.path, "sha256": a.sha256, "media_type": a.media_type,
            "size": a.size, "pages": a.pages, "sheets": a.sheets, "inspection_state": a.inspection_state,
            "failure_reason": a.failure_reason, "proposed_role": a.proposed_role, "role_reason": a.role_reason,
            "role_citations": a.role_citations, "disposition": a.disposition,
            "disposition_reason": a.disposition_reason, "disposition_by": a.disposition_by,
            "needs_confirmation": bool(a.needs_confirmation), "case_id": a.case_id}


def assignment_dict(a: ClaimEvidenceAssignment) -> dict[str, Any]:
    return {"id": a.key or a.id, "evidence_id": a.evidence_id, "artifact_id": a.artifact_id,
            "case_id": a.case_id, "line_id": a.line_id, "state": a.state, "basis": a.basis,
            "confidence": a.confidence, "reason": a.reason, "citations": a.citations}


def investigation_dict(inv: ClaimInvestigation | None) -> dict[str, Any]:
    if inv is None:
        return {}
    return {"id": inv.id, "adapter": inv.adapter, "strategy": inv.strategy, "status": inv.status,
            "plan": inv.plan, "summary": inv.summary, "rounds": inv.rounds,
            "created_at": inv.created_at.isoformat() if inv.created_at else ""}
