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

def store_result(s, run: ClaimsRun, result: C.InvestigationResult, confirmed: bool = False,
                 replace_cases: bool | None = None, record: bool = True) -> ClaimInvestigation | None:
    """Write (or refresh) the artifacts, cases, assignments, plan and tool
    record of a result. Called at map time (the proposal) and, with
    record=False, when the reviewer's corrected map replaces the proposal
    (a reviewer edit is audited, not a second investigation — the plan and
    adapter on record stay the investigation's)."""
    inv = None
    if not record:
        upsert_artifacts(s, run.id, result.artifacts)
        upsert_cases(s, run.id, result.cases, confirmed, replace_cases)
        upsert_assignments(s, run.id, result.assignments)
        upsert_flags(s, run.id, result.flags)
        s.flush()
        return None
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
    upsert_cases(s, run.id, result.cases, confirmed, replace_cases)
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
    s.flush()  # the case each file belongs to is set by upsert_assignments, which queries these rows


def upsert_cases(s, run_id: str, cases: list[C.ClaimCase], confirmed: bool, replace: bool | None = None) -> None:
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
    if replace if replace is not None else confirmed:
        # cases the reviewer removed from the map are gone from the run
        keep = {c.id for c in cases}
        for cid, rec in existing.items():
            if cid not in keep:
                s.delete(rec)
    s.flush()


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


# ---- the Map & Group gate and actions (H6) -------------------------------------------

class GroupingError(ValueError):
    """A reviewer action that cannot be applied; the message says why."""


def _case(s, run_id: str, case_id: str) -> ClaimCase:
    c = s.get(ClaimCase, case_id)
    if c is None or c.run_id != run_id:
        raise GroupingError("No such case in this run.")
    return c


def _artifact(s, run_id: str, artifact_id: str) -> ClaimSourceArtifact:
    a = s.query(ClaimSourceArtifact).filter(ClaimSourceArtifact.run_id == run_id,
                                            ClaimSourceArtifact.artifact_id == artifact_id).first()
    if a is None:
        raise GroupingError("No such file in this run.")
    return a


def new_case_id(run_id: str, seed: str) -> str:
    return case_id_for(run_id, seed)


def _assign(s, run: ClaimsRun, art: ClaimSourceArtifact, case: ClaimCase | None, actor: str, why: str) -> None:
    """Move a file into a case (or out of every case): the artifact's case,
    its assignment record (reviewer decision, confirmed), its disposition
    (used inside a case; unresolved when moved out unless the reviewer set
    a disposition before)."""
    old_case = art.case_id
    art.case_id = case.id if case else ""
    key = "s" + __import__("hashlib").sha256(f"{art.case_id}\0{art.artifact_id}".encode()).hexdigest()[:10] if case else ""
    for rec in s.query(ClaimEvidenceAssignment).filter(ClaimEvidenceAssignment.run_id == run.id,
                                                       ClaimEvidenceAssignment.artifact_id == art.artifact_id):
        if not case or rec.case_id != case.id:
            rec.state, rec.basis, rec.reason = "rejected", "reviewer_decision", why[:400]
    if case:
        rec = s.query(ClaimEvidenceAssignment).filter(ClaimEvidenceAssignment.run_id == run.id,
                                                      ClaimEvidenceAssignment.key == key).first()
        if rec is None:
            rec = ClaimEvidenceAssignment(run_id=run.id, key=key, artifact_id=art.artifact_id, case_id=case.id)
            s.add(rec)
        rec.state, rec.basis, rec.confidence, rec.reason = "confirmed", "reviewer_decision", 1.0, why[:400]
        rec.citations = [{"artifact_id": art.artifact_id, "path": art.path}]
        art.disposition, art.disposition_by, art.needs_confirmation = "used", actor, 0
        art.disposition_reason = ""
    elif art.disposition == "used":
        art.disposition, art.disposition_by, art.needs_confirmation = "unresolved", "", 1
        art.disposition_reason = "moved out of its case by the reviewer; say what it is or move it into another case"
    return None if old_case == art.case_id else old_case


def create_case(s, run: ClaimsRun, label: str, artifact_ids: list[str], actor: str = "reviewer") -> ClaimCase:
    label = (label or "").strip()[:120]
    n = s.query(ClaimCase).filter(ClaimCase.run_id == run.id).count()
    label = label or f"Case {n + 1}"
    cid = new_case_id(run.id, f"{label}\0{n}\0{run.revision}")
    case = ClaimCase(id=cid, run_id=run.id, label=label, state="proposed", claimant_state="unknown",
                     claimant_basis="", grouping_basis="reviewer_decision: created by the reviewer at the map",
                     reason="created by the reviewer", confidence=1.0)
    s.add(case)
    s.flush()
    for aid in artifact_ids:
        _assign(s, run, _artifact(s, run.id, aid), case, actor, "moved into a new case by the reviewer")
    return case


def move_artifact(s, run: ClaimsRun, artifact_id: str, case_id: str, actor: str = "reviewer") -> None:
    art = _artifact(s, run.id, artifact_id)
    case = _case(s, run.id, case_id) if case_id else None
    _assign(s, run, art, case, actor, "moved by the reviewer" if case else "taken out of its case by the reviewer")


def merge_cases(s, run: ClaimsRun, case_id: str, into_id: str, actor: str = "reviewer") -> ClaimCase:
    if case_id == into_id:
        raise GroupingError("Choose a different case to merge into.")
    src, dst = _case(s, run.id, case_id), _case(s, run.id, into_id)
    for art in s.query(ClaimSourceArtifact).filter(ClaimSourceArtifact.run_id == run.id,
                                                   ClaimSourceArtifact.case_id == src.id).all():
        _assign(s, run, art, dst, actor, f"merged from case {src.label}")
    if not dst.claimant_name and src.claimant_name:
        dst.claimant_name, dst.claimant_identifier = src.claimant_name, src.claimant_identifier
        dst.claimant_state, dst.claimant_basis = ("proposed" if src.claimant_state != "confirmed" else "confirmed"), src.claimant_basis
    dst.grouping_basis = (dst.grouping_basis + f"; merged with {src.label} by the reviewer")[:400]
    for f in s.query(ClaimFlag).filter(ClaimFlag.run_id == run.id, ClaimFlag.case_id == src.id):
        f.case_id = dst.id
    s.delete(src)
    s.flush()
    return dst


def split_case(s, run: ClaimsRun, case_id: str, artifact_ids: list[str], label: str, actor: str = "reviewer") -> ClaimCase:
    src = _case(s, run.id, case_id)
    mine = {a.artifact_id for a in s.query(ClaimSourceArtifact).filter(ClaimSourceArtifact.run_id == run.id,
                                                                       ClaimSourceArtifact.case_id == src.id)}
    chosen = [a for a in artifact_ids if a in mine]
    if not chosen:
        raise GroupingError("Choose at least one file of this case to split off.")
    if len(chosen) == len(mine):
        raise GroupingError("That is every file of the case — rename it instead of splitting.")
    return create_case(s, run, label or f"{src.label} (split)", chosen, actor)


def set_claimant(s, run: ClaimsRun, case_id: str, name: str, identifier: str, actor: str = "reviewer") -> ClaimCase:
    case = _case(s, run.id, case_id)
    name, identifier = (name or "").strip()[:120], (identifier or "").strip()[:60]
    if not name and identifier:
        raise GroupingError("A claimant needs a name (the identifier alone is not a person).")
    case.claimant_name, case.claimant_identifier = name, identifier
    case.claimant_state = "confirmed" if name else "unknown"
    case.claimant_basis = ("set by the reviewer at the map" if name else "cleared by the reviewer")
    return case


def confirm_claimant(s, run: ClaimsRun, case_id: str) -> ClaimCase:
    case = _case(s, run.id, case_id)
    if not case.claimant_name.strip():
        raise GroupingError("There is no proposed name to confirm — set the claimant.")
    case.claimant_state = "confirmed"
    case.claimant_basis = (case.claimant_basis + "; confirmed by the reviewer")[:400]
    return case


def update_case(s, run: ClaimsRun, case_id: str, label: str | None = None, roles: dict | None = None,
                state: str | None = None) -> ClaimCase:
    case = _case(s, run.id, case_id)
    if label is not None and label.strip():
        case.label = label.strip()[:120]
    if roles is not None:
        allowed = {k: roles[k] for k in ("report_file", "report_tab", "mileage_tab", "no_report") if k in roles}
        mine = {a.path: a for a in s.query(ClaimSourceArtifact).filter(ClaimSourceArtifact.run_id == run.id,
                                                                       ClaimSourceArtifact.case_id == case.id)}
        if allowed.get("report_file") and allowed["report_file"] not in mine:
            raise GroupingError("The claim summary must be a workbook inside this case.")
        if allowed.get("report_file") and mine[allowed["report_file"]].media_type != "workbook":
            raise GroupingError("The claim summary must be a workbook.")
        new_roles = {**(case.roles or {}), **allowed}
        if allowed.get("no_report"):
            new_roles.update({"report_file": None, "report_tab": None, "mileage_tab": None})
        elif "report_file" in allowed and allowed["report_file"]:
            new_roles["no_report"] = False
            if mine[allowed["report_file"]].proposed_role != "report":
                mine[allowed["report_file"]].proposed_role = "report"
                mine[allowed["report_file"]].role_reason = "chosen as the claim summary by the reviewer"
        case.roles = new_roles
    if state in ("excluded", "proposed"):
        case.state = state
    return case


def confirm_grouping(s, run: ClaimsRun, actor: str = "reviewer") -> tuple[int, dict]:
    """The one gate (H6): validate; every case that is not excluded becomes
    confirmed, its proposed claimant confirmed (the reviewer's click IS
    the confirmation), its assignments confirmed; one ClaimEmployee per
    case for the delivered worker; the run moves to verifying. Returns
    (cases to verify, gate). Raises GroupingError when the gate is shut."""
    from . import grouping

    g = grouping.refresh(s, run)
    if not g["ok"]:
        raise GroupingError("The grouping is not ready to confirm: " + "; ".join(g["problems"][:8]))
    cases = s.query(ClaimCase).filter(ClaimCase.run_id == run.id).order_by(ClaimCase.label).all()
    s.query(ClaimEmployee).filter(ClaimEmployee.run_id == run.id).delete()
    n = 0
    for c in cases:
        if c.state != "excluded":
            c.state = "confirmed"
            if c.claimant_state == "proposed" and c.claimant_name.strip():
                c.claimant_state = "confirmed"
                c.claimant_basis = (c.claimant_basis + "; confirmed by the reviewer at the map")[:400]
        for a in s.query(ClaimEvidenceAssignment).filter(ClaimEvidenceAssignment.run_id == run.id,
                                                         ClaimEvidenceAssignment.case_id == c.id,
                                                         ClaimEvidenceAssignment.state == "proposed"):
            a.state = "confirmed"
        skipped = c.state == "excluded"
        emp = ClaimEmployee(run_id=run.id, folder=c.label, name=c.claimant_name, er_code=c.claimant_identifier,
                            roles=dict(c.roles or {}), status="skipped" if skipped else "pending",
                            error="skipped by the reviewer at the map" if skipped else "")
        s.add(emp)
        s.flush()
        c.legacy_employee_id = emp.id
        c.status, c.error = emp.status, emp.error
        if not skipped:
            n += 1
    for inv in s.query(ClaimInvestigation).filter(ClaimInvestigation.run_id == run.id,
                                                  ClaimInvestigation.status == "proposed"):
        inv.status = "confirmed"
    grouping.refresh(s, run)  # CLAIMANT_UNKNOWN for cases still without a claimant; conflicts none
    run.status = "verifying"
    run.progress = {"done": 0, "total": 0}
    bump_revision(run)
    s.flush()
    return n, g
