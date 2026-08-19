"""The legacy structured-folder adapter (H1): the delivered survey + map
path, wrapped UNCHANGED behind the investigator interface.

It is the conformance baseline (Client A must come out identical through
the seam) and the rollback path while CLAIMS_AGENTIC_INVESTIGATION is off.
The map AI (mapping.propose_map) and its audit run exactly as before; this
module only translates the delivered map into the normalized result:

  subfolder judged an employee  → a proposed Claim Case whose Claimant is
                                  PROPOSED (a folder name is a strong
                                  signal, not a confirmation)
  file role report / receipts   → Source Artifact used, assigned to the case
  file role ignore              → irrelevant, with the AI's reason
  file role unplaced            → unresolved, needs the reviewer
  root files                    → classified only (no case), as delivered

Instructions reach the map AI as they always did.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .. import survey as survey_mod
from .contracts import (Citation, ClaimCase, Claimant, EvidenceAssignment, InvestigationPlan,
                        InvestigationRequest, InvestigationResult, ManifestEntry, SourceArtifact)

ADAPTER = "legacy"

# delivered file role → (proposed artifact role, disposition)
_ROLE_MAP = {
    "report": ("report", "used"),
    "receipts": ("receipts", "used"),
    "ignore": ("other", "irrelevant"),
    "unplaced": ("unknown", "unresolved"),
}


def _judge_model() -> str:
    from ... import config

    return config.JUDGE_MODEL


def case_id_for(run_id: str, label: str) -> str:
    """Deterministic per run and folder, so a re-run of the map (or a
    retry) does not mint a second id for the same case."""
    return "c" + hashlib.sha256(f"{run_id}\0{label}".encode()).hexdigest()[:10]


async def investigate(request: InvestigationRequest, tools=None) -> InvestigationResult:
    from .. import mapping

    files_dir = Path(request.workspace) / "files"
    survey = request.survey or _survey_from_manifest(files_dir, request.manifest)
    snapshot = {"profile": request.profile_snapshot.get("profile", request.profile_snapshot),
                **{k: v for k, v in request.profile_snapshot.items() if k != "profile"}}
    claim_map, warnings, notes = await mapping.propose_map(
        survey, files_dir, snapshot=snapshot, instructions=request.instructions)
    result = from_map(request, claim_map, warnings)
    result.notes = list(notes)
    return result


def _survey_from_manifest(files_dir: Path, manifest: list[ManifestEntry]) -> dict:
    files = [{"path": m.path, "size": m.size, "depth": len(m.path.split("/"))} for m in manifest]
    return survey_mod.survey_batch(files_dir, files)


def from_map(request: InvestigationRequest, claim_map: dict, warnings: list[str] | None = None,
             confirmed: bool = False) -> InvestigationResult:
    """The delivered map dict → normalized result. Used at map time (AI
    proposal) and again at confirm time (the reviewer's corrected map),
    when confirmed=True marks the cases and claimants confirmed."""
    by_path = {m.path: m for m in request.manifest}
    role_of: dict[str, tuple[str, str, str]] = {}   # path -> (role, reason, folder)
    for e in claim_map.get("employees", []):
        for fr in e.get("files", []):
            role_of[fr["path"]] = (fr.get("role", "unplaced"), fr.get("reason", ""), e["folder"])
    for fr in claim_map.get("root_files", []) or []:
        role_of[fr["path"]] = (fr.get("role", "unplaced"), fr.get("reason", ""), "")

    artifacts: list[SourceArtifact] = []
    for m in request.manifest:
        role, reason, _folder = role_of.get(m.path, ("unplaced", "not placed by the map", ""))
        proposed, disposition = _ROLE_MAP.get(role, ("unknown", "unresolved"))
        artifacts.append(SourceArtifact(
            id=m.id, path=m.path, sha256=m.sha256, media_type=m.media_type, size=m.size,
            pages=m.pages, sheets=m.sheets,
            inspection_state="inspected" if role != "unplaced" else "not_inspected",
            proposed_role=proposed, role_reason=reason[:400],
            role_citations=[Citation(artifact_id=m.id, path=m.path)],
            disposition=disposition,
            disposition_reason=(reason[:400] if disposition != "used" else ""),
            disposition_by="adapter" if disposition != "unresolved" else "",
            needs_confirmation=disposition == "unresolved"))

    cases: list[ClaimCase] = []
    assignments: list[EvidenceAssignment] = []
    for e in claim_map.get("employees", []):
        if not e.get("is_employee"):
            continue
        cid = case_id_for(request.run_id, e["folder"])
        files = e.get("files", [])
        roles = {
            "report_file": e.get("report_file"), "report_tab": e.get("report_tab"),
            "mileage_tab": e.get("mileage_tab"), "no_report": bool(e.get("no_report")),
            "receipt_files": [f["path"] for f in files if f.get("role") == "receipts"],
            "ignored": [f["path"] for f in files if f.get("role") == "ignore"],
            "unplaced": [f["path"] for f in files if f.get("role") == "unplaced"],
        }
        skipped = bool(e.get("skip"))
        name = (e.get("name") or "").strip()
        claimant = Claimant(
            name=name, identifier=(e.get("er_code") or "").strip(),
            state=("confirmed" if confirmed and name else ("proposed" if name else "unknown")),
            basis="folder structure: the subfolder is named after one person; ER code from a file name "
                  "or the report header" + ("; confirmed by the reviewer at the map" if confirmed else ""),
            citations=[Citation(artifact_id=by_path[f["path"]].id, path=f["path"])
                       for f in files if f["path"] in by_path][:5])
        cases.append(ClaimCase(
            id=cid, claimant=claimant,
            state="excluded" if skipped else ("confirmed" if confirmed else "proposed"),
            grouping_basis="folder_structure: one subfolder per claimant",
            citations=[Citation(path=e["folder"], note="subfolder")],
            artifact_ids=[by_path[f["path"]].id for f in files if f["path"] in by_path],
            roles=roles, label=e["folder"],
            confidence=0.9 if e.get("report_file") or e.get("no_report") else 0.6,
            reason=(e.get("reason") or "")[:400]))
        for f in files:
            m = by_path.get(f["path"])
            if m is None or f.get("role") not in ("report", "receipts"):
                continue
            assignments.append(EvidenceAssignment(
                id="s" + hashlib.sha256(f"{cid}\0{m.id}".encode()).hexdigest()[:10],
                artifact_id=m.id, case_id=cid, state="confirmed" if confirmed else "proposed",
                basis="folder_structure", confidence=0.9,
                reason=(f.get("reason") or "")[:400],
                citations=[Citation(artifact_id=m.id, path=m.path)]))
    plan = InvestigationPlan(strategy="structured", objective=request.objective or request.instructions,
                             steps=["survey the folder tree", "peek inside every file",
                                    "map subfolders to employees (AI proposes, code audits)"],
                             rounds=int(claim_map.get("rounds") or 0), adapter=ADAPTER,
                             versions={"adapter": ADAPTER, "judge_model": _judge_model()})
    return InvestigationResult(artifacts=artifacts, cases=cases, assignments=assignments,
                               plan=plan, map=claim_map, warnings=list(warnings or []))
