"""The universal result controls over an InvestigationProposal (H5) — code,
deterministic, run after every round and before anything is normalized:

  coverage      every manifest artifact once, ids real, dispositions with
                reasons; 'used' means inside a case
  cases         artifacts real and not in two cases; a named report is a
                workbook of the case with an existing sheet that LOOKS like
                a claim summary (dated rows with amounts adding to a total);
                no report ⇒ no_summary
  identity      a claimant name / identifier must be verifiable at a cited
                place — the audit looks there with the tools; a name with no
                verifiable place is sent back (weak signals never confirm)
  conflicts     one identifier per case; a case whose files carry two
                different identifiers is a conflict the agent must explain
                or split

Problems come back in the agent's own terms (artifact ids, sheets, cells)
so the next round can fix them. Whatever remains after the last round is
NOT discarded: normalize() turns it into unresolved artifacts, unknown
claimants and visible warnings.
"""
from __future__ import annotations

from pathlib import Path

from .. import survey as survey_mod
from ..tools.contracts import InvestigationTools
from .contracts import InvestigationRequest
from .proposal import CaseProposal, CiteProposal, InvestigationProposal

MAX_IDENTITY_CHECKS = 60


def _fold(text: str) -> str:
    return " ".join((text or "").lower().replace("(", " ").replace(")", " ").split())


async def audit_proposal(proposal: InvestigationProposal, request: InvestigationRequest,
                         tools: InvestigationTools) -> list[str]:
    problems: list[str] = []
    by_id = {m.id: m for m in request.manifest}
    files_dir = Path(request.workspace) / "files"

    # ---- coverage -----------------------------------------------------------------
    seen: dict[str, int] = {}
    for a in proposal.artifacts:
        seen[a.artifact_id] = seen.get(a.artifact_id, 0) + 1
    for aid, n in seen.items():
        if aid not in by_id:
            problems.append(f"artifact {aid!r} is not in the manifest — use ids from list_artifacts")
        elif n > 1:
            problems.append(f"artifact {aid} ({by_id[aid].path}) is listed {n} times; once")
    for m in request.manifest:
        if m.id not in seen:
            problems.append(f"artifact {m.id} ({m.path}) has no role/disposition — every file gets one")
    in_case: dict[str, str] = {}
    for c in proposal.cases:
        for aid in c.artifact_ids:
            if aid in in_case and in_case[aid] != c.key:
                problems.append(f"artifact {aid} is in two cases ({in_case[aid]} and {c.key}); a file belongs to one case")
            in_case.setdefault(aid, c.key)
    for a in proposal.artifacts:
        if a.artifact_id not in by_id:
            continue
        if a.disposition in ("irrelevant", "unreadable", "duplicate") and not a.reason.strip():
            problems.append(f"artifact {a.artifact_id}: disposition {a.disposition} needs a reason")
        if a.disposition == "used" and a.artifact_id not in in_case:
            problems.append(f"artifact {a.artifact_id} ({by_id[a.artifact_id].path}) is 'used' but in no case — "
                            "put it in a case, or set another disposition (irrelevant/duplicate/unreadable/unresolved)")
        if a.disposition != "used" and a.artifact_id in in_case:
            problems.append(f"artifact {a.artifact_id} is inside case {in_case[a.artifact_id]} but its disposition is "
                            f"{a.disposition}; a file in a case is 'used'")

    # ---- cases --------------------------------------------------------------------
    keys: dict[str, int] = {}
    ids_seen: dict[str, str] = {}
    for c in proposal.cases:
        keys[c.key] = keys.get(c.key, 0) + 1
        if keys[c.key] > 1:
            problems.append(f"case key {c.key!r} is used twice")
        for aid in c.artifact_ids:
            if aid not in by_id:
                problems.append(f"case {c.key}: artifact {aid!r} is not in the manifest")
        if c.report_artifact_id:
            m = by_id.get(c.report_artifact_id)
            if m is None:
                problems.append(f"case {c.key}: report_artifact_id {c.report_artifact_id!r} is not in the manifest")
            elif m.media_type != "workbook":
                problems.append(f"case {c.key}: the report must be a workbook; {m.path} is a {m.media_type}")
            elif c.report_artifact_id not in c.artifact_ids:
                problems.append(f"case {c.key}: the report {m.path} must also be in the case's artifact_ids")
            elif not c.report_sheet:
                problems.append(f"case {c.key}: name the report_sheet of {m.path} (sheets: {m.sheets})")
            elif m.sheets and c.report_sheet not in m.sheets:
                problems.append(f"case {c.key}: {m.path} has no sheet {c.report_sheet!r} (sheets: {m.sheets})")
            else:
                from .. import mapping

                ok, why = mapping.report_tab_plausible(files_dir / m.path, c.report_sheet)
                if not ok:
                    problems.append(f"case {c.key}: sheet {c.report_sheet!r} of {m.path} does not look like a claim "
                                    f"summary: {why} — name the right sheet, or set no_summary=true")
                if c.mileage_sheet and m.sheets and c.mileage_sheet not in m.sheets:
                    problems.append(f"case {c.key}: {m.path} has no sheet {c.mileage_sheet!r}")
            if c.no_summary:
                problems.append(f"case {c.key}: no_summary is true but a report is named")
        elif not c.no_summary:
            problems.append(f"case {c.key}: no report named — name report_artifact_id + report_sheet, or set "
                            "no_summary=true if the case has evidence but no claim summary")
        if not c.artifact_ids:
            problems.append(f"case {c.key}: a case needs at least one file")
        ident = _fold(c.claimant_identifier)
        if ident:
            if ident in ids_seen and ids_seen[ident] != c.key:
                problems.append(f"identifier {c.claimant_identifier!r} is used by cases {ids_seen[ident]} and {c.key} — "
                                "one identifier per case; merge them or fix the identifier")
            ids_seen.setdefault(ident, c.key)

    # ---- identity: every claimed name / identifier verifiable at a cited place ------
    checks = 0
    for c in proposal.cases:
        for what, value in (("claimant_name", c.claimant_name), ("claimant_identifier", c.claimant_identifier)):
            if not value.strip():
                continue
            if checks >= MAX_IDENTITY_CHECKS:
                break
            checks += 1
            found, why = await _verify_identity(value, c, request, tools, by_id)
            if not found:
                problems.append(f"case {c.key}: {what} {value!r} {why} — cite where it is written (a cell or page "
                                "of a file in the case, or a file NAME), or leave it empty")
    return problems


async def _verify_identity(value: str, c: CaseProposal, request: InvestigationRequest, tools, by_id) -> tuple[bool, str]:
    """True when the value is written at one of the case's citations or in
    the name of one of the case's files. Filenames and cells/pages of the
    case's own artifacts only — a name found in someone else's file is no
    basis for this case."""
    want = _fold(value)
    if not want:
        return True, ""
    # file names of the case
    for aid in c.artifact_ids:
        m = by_id.get(aid)
        if m and want in _fold(m.path):
            return True, ""
        if m and _fold(survey_mod.er_code_of(Path(m.path).name)) == want:
            return True, ""
    # cited cells / pages, read with the tools
    for cite in c.identity_citations[:8]:
        if cite.artifact_id not in c.artifact_ids or cite.artifact_id not in by_id:
            continue
        text = await _text_at(cite, tools)
        if want in _fold(text):
            return True, ""
    # a bounded search inside the case's own files
    r = await tools.search_artifacts(value, limit=50)
    if r.ok:
        for hit in r.data.get("hits", []):
            if hit.get("artifact_id") in c.artifact_ids:
                return True, ""
    if not c.identity_citations:
        return False, "is not written in any file name of the case and no citation was given"
    return False, "is not at the cited place(s) nor in the case's file names"


async def _text_at(cite: CiteProposal, tools) -> str:
    m_id = cite.artifact_id
    if cite.sheet and cite.cell:
        r = await tools.read_cells(m_id, cite.sheet, cite.cell)
        if r.ok:
            return " ".join(str(c.get("value") or "") for c in r.data.get("cells", []))
        return ""
    if cite.page:
        r = await tools.inspect_document(m_id)
        if r.ok:
            return " ".join(b.get("text", "") for b in r.data.get("text_blocks", []) if b.get("page") == cite.page)
        return ""
    r = await tools.inspect_document(m_id)
    if r.ok and r.data.get("text_blocks"):
        return " ".join(b.get("text", "") for b in r.data["text_blocks"])
    return ""
