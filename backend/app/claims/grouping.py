"""Identity signals, ownership conflicts, case roles and the grouping
gate (hardening H6) — code, deterministic, run after every proposal and
after every reviewer action at the Map & Group screen.

Signals come from three cheap places and each carries a Citation:
  - file names: an ER(...) code, and a person-like prefix before '_'
  - workbook peeks: the value beside a 'Name' label cell (B1 for the
    delivered template) and any ER(...) code in the first rows
  - the top-level folder name (a structured batch)
A signal is STRONG when it is an exact identifier (ER code) or an explicit
name in a header cell / file name; a folder name alone is a grouping
hint, not ownership. Two strong signals in one case that name different
people → OWNERSHIP_CONFLICT (blocks confirmation until split/moved). A
case with lines or evidence and no confirmed claimant → CLAIMANT_UNKNOWN
(blocks OUTPUT, not verification: useful work is still shown).
"""
from __future__ import annotations

import re
from pathlib import Path

from . import profile as profile_mod
from . import survey as survey_mod
from .models import ClaimCase, ClaimFlag, ClaimSourceArtifact, ClaimsRun

_NAME_PREFIX = re.compile(r"^([A-Z][A-Za-z.'\-]+(?: [A-Z][A-Za-z.'\-]+){1,3})_")
_LABELS = ("name", "employee", "claimant", "staff", "requestor", "requester")
MATERIAL_TYPES = ("workbook", "pdf", "image")


def _fold(text: str) -> str:
    return " ".join((text or "").lower().replace("(", " ").replace(")", " ").split())


def signals_for(run: ClaimsRun, artifacts: list[ClaimSourceArtifact]) -> dict[str, list[dict]]:
    """artifact_id → [{kind: er_code|name|folder, value, strength, cite}]."""
    peeks = {f["path"]: f for f in (run.survey or {}).get("files", [])}
    out: dict[str, list[dict]] = {}
    for a in artifacts:
        sig: list[dict] = []
        name = Path(a.path).name
        code = survey_mod.er_code_of(name)
        if code:
            sig.append({"kind": "er_code", "value": code, "strength": "strong",
                        "cite": {"file": a.path, "page": 0, "note": "in the file name"}})
        m = _NAME_PREFIX.match(name)
        if m:
            sig.append({"kind": "name", "value": m.group(1).strip(), "strength": "strong",
                        "cite": {"file": a.path, "page": 0, "note": "prefix of the file name"}})
        top = a.path.split("/", 1)[0] if "/" in a.path else ""
        if top:
            sig.append({"kind": "folder", "value": top, "strength": "weak",
                        "cite": {"file": a.path, "page": 0, "note": "top-level folder"}})
        tabs = ((peeks.get(a.path) or {}).get("peek") or {}).get("tabs") or {}
        for tab, rows in tabs.items():
            for row in rows[:6]:
                cells = [c.strip() for c in row.split(" | ")]
                for i, cell in enumerate(cells):
                    ref, _, text = cell.partition(": ")
                    if text.rstrip(":").strip().lower() in _LABELS and i + 1 < len(cells):
                        nref, _, ntext = cells[i + 1].partition(": ")
                        if ntext.strip():
                            sig.append({"kind": "name", "value": ntext.strip(), "strength": "strong",
                                        "cite": {"sheet": tab, "row": int(re.sub(r"\D", "", nref) or 0),
                                                 "note": f"beside {text!r} in {ref}"}})
                    c2 = survey_mod.er_code_of(text)
                    if c2:
                        sig.append({"kind": "er_code", "value": c2, "strength": "strong",
                                    "cite": {"sheet": tab, "row": int(re.sub(r"\D", "", ref) or 0)}})
        out[a.artifact_id] = sig
    return out


def conflict_in(case: ClaimCase, artifacts: list[ClaimSourceArtifact], signals: dict[str, list[dict]]) -> str:
    """A sentence naming the conflict, or "" — strong signals of the case's
    files that point at two different identifiers or two different names."""
    codes: dict[str, str] = {}
    names: dict[str, str] = {}
    for a in artifacts:
        if a.case_id != case.id:
            continue
        for s in signals.get(a.artifact_id, []):
            if s["strength"] != "strong":
                continue
            if s["kind"] == "er_code":
                codes.setdefault(_fold(s["value"]), f"{s['value']} in {a.path}")
            elif s["kind"] == "name":
                names.setdefault(_fold(s["value"]), f"{s['value']!r} in {a.path}")
    if len(codes) > 1:
        return "the files carry different ER codes: " + "; ".join(list(codes.values())[:4])
    if len(names) > 1:
        # a name that is a prefix of another (initials, middle names) is the same person
        keys = sorted(names, key=len)
        distinct = [k for k in keys if not any(k != o and (k in o or o in k) for o in keys)]
        if len(distinct) > 1:
            return "the files carry different names: " + "; ".join(names[k] for k in distinct[:4])
    return ""


def roles_for_case(case: ClaimCase, artifacts: list[ClaimSourceArtifact], survey: dict) -> dict:
    """The worker's file roles for a case, from its artifacts. Keeps the
    chosen report file/tab while the report is still in the case; else
    picks the workbook the investigation called a report, or the only
    workbook (its sheet is then the reviewer's to choose). A reviewer's
    explicit 'no summary' (no_report with no report file) is kept.
    receipts = the PDFs/images with role receipts / unknown / other;
    ignored = approvals, report copies, listings, irrelevant, duplicates."""
    mine = [a for a in artifacts if a.case_id == case.id]
    old = dict(case.roles or {})
    peeks = {f["path"]: f for f in (survey or {}).get("files", [])}
    workbooks = [a for a in mine if a.media_type == "workbook"]
    explicit_no = bool(old.get("no_report")) and not old.get("report_file")
    report = None
    if not explicit_no:
        report = next((a for a in workbooks if a.path == old.get("report_file")), None) \
            or next((a for a in workbooks if a.proposed_role == "report"), None) \
            or (workbooks[0] if len(workbooks) == 1 else None)
    tabs = list((((peeks.get(report.path) or {}).get("peek") or {}).get("tabs") or {}).keys()) if report else []
    same = report is not None and old.get("report_file") == report.path
    tab = old.get("report_tab") if same and (not tabs or old.get("report_tab") in tabs) else None
    mileage = old.get("mileage_tab") if same and (not tabs or old.get("mileage_tab") in tabs) else None
    return {"report_file": report.path if report else None,
            "report_tab": tab if report else None,
            "mileage_tab": mileage if report else None,
            "no_report": report is None,
            "receipt_files": [a.path for a in mine if a.media_type in ("pdf", "image")
                              and a.disposition in ("used", "unresolved")
                              and a.proposed_role in ("receipts", "unknown", "other")],
            "ignored": [a.path for a in mine if a.proposed_role in ("approval", "report_copy", "listing", "roster", "policy")
                        or a.disposition in ("irrelevant", "duplicate")],
            "unplaced": [a.path for a in mine if a.disposition == "unresolved"]}


def refresh(s, run: ClaimsRun) -> dict:
    """After any change to cases/artifacts at the map: recompute each
    case's roles, raise/resolve OWNERSHIP_CONFLICT and CLAIMANT_UNKNOWN
    (idempotent by case id), and return the gate's state."""
    cases = s.query(ClaimCase).filter(ClaimCase.run_id == run.id).all()
    artifacts = s.query(ClaimSourceArtifact).filter(ClaimSourceArtifact.run_id == run.id).all()
    signals = signals_for(run, artifacts)
    existing = {profile_mod.flag_key(f): f for f in s.query(ClaimFlag).filter(
        ClaimFlag.run_id == run.id, ClaimFlag.code.in_(("OWNERSHIP_CONFLICT", "CLAIMANT_UNKNOWN")))}
    live: set[tuple] = set()
    for c in cases:
        c.roles = roles_for_case(c, artifacts, run.survey or {})
        c.artifact_ids = [a.artifact_id for a in artifacts if a.case_id == c.id]
        if c.state == "excluded":
            continue
        why = conflict_in(c, artifacts, signals)
        if why:
            key = ("OWNERSHIP_CONFLICT", c.id)
            live.add(key)
            f = existing.get(key)
            reason = (f"Case {c.label or c.id}: {why}. Two people could own this — split the case, move the "
                      "odd file out, or set the claimant with a note.")
            if f is None:
                s.add(ClaimFlag(run_id=run.id, employee_id="", case_id=c.id, code="OWNERSHIP_CONFLICT", reason=reason,
                                basis="universal rule: conflicting strong identity signals never resolve themselves",
                                cite={"what": c.id, "file": next((a.path for a in artifacts if a.case_id == c.id), "")}))
            elif f.status in ("open", "info"):
                f.reason = reason
            elif f.status == "resolved_by_correction":
                f.status, f.reason, f.resolution = "open", reason, ""
        if c.claimant_state == "unknown":
            key = ("CLAIMANT_UNKNOWN", c.id)
            live.add(key)
            f = existing.get(key)
            reason = (f"Case {c.label or c.id} has no confirmed claimant"
                      + (f" (the investigation proposed {c.claimant_name!r}: {c.claimant_basis})" if c.claimant_name else
                         " (no name or code was found in its files)")
                      + ". Set or confirm the claimant before this case can be paid.")
            if f is None:
                s.add(ClaimFlag(run_id=run.id, employee_id="", case_id=c.id, code="CLAIMANT_UNKNOWN", reason=reason,
                                basis="universal rule: a payable case has a reviewer-confirmed claimant",
                                cite={"what": c.id}))
            elif f.status in ("open", "info"):
                f.reason = reason
            elif f.status == "resolved_by_correction":
                f.status, f.reason, f.resolution = "open", reason, ""
    for key, f in existing.items():
        if key not in live and f.status in ("open", "info"):
            f.status, f.resolution = "resolved_by_correction", "no longer applies after regrouping"
    s.flush()
    return gate(s, run)


def gate(s, run: ClaimsRun) -> dict:
    """What still stops Confirm grouping & verify, plus coverage counts."""
    cases = s.query(ClaimCase).filter(ClaimCase.run_id == run.id).all()
    artifacts = s.query(ClaimSourceArtifact).filter(ClaimSourceArtifact.run_id == run.id).all()
    problems: list[str] = []
    for f in s.query(ClaimFlag).filter(ClaimFlag.run_id == run.id, ClaimFlag.code == "OWNERSHIP_CONFLICT",
                                        ClaimFlag.status == "open"):
        problems.append(f.reason.split(". ")[0])
    material_unresolved = [a for a in artifacts if a.disposition == "unresolved" and a.media_type in MATERIAL_TYPES]
    for a in material_unresolved[:20]:
        problems.append(f"{a.path}: a {a.media_type} nobody has placed — move it into a case, or mark it "
                        "irrelevant / duplicate / unreadable")
    to_verify = [c for c in cases if c.state != "excluded"]
    for c in to_verify:
        roles = c.roles or {}
        if not roles.get("no_report") and not roles.get("report_file"):
            problems.append(f"{c.label}: choose the claim summary workbook and its sheet, or mark 'no summary'")
        elif not roles.get("no_report") and not roles.get("report_tab"):
            problems.append(f"{c.label}: choose the sheet of {Path(roles['report_file']).name} that holds the lines")
        if roles.get("no_report") and not roles.get("receipt_files"):
            problems.append(f"{c.label}: no summary and no receipt files — nothing to verify; move files in or exclude it")
    from . import source as source_mod

    if len(to_verify) > source_mod.MAX_CASES_PER_RUN:
        problems.append(f"{len(to_verify)} cases to verify — more than the {source_mod.MAX_CASES_PER_RUN} one run may hold")
    return {"problems": problems,
            "counts": {"artifacts": len(artifacts),
                       "dispositioned": sum(1 for a in artifacts if a.disposition != "unresolved"),
                       "unresolved": sum(1 for a in artifacts if a.disposition == "unresolved"),
                       "material_unresolved": len(material_unresolved),
                       "cases": len(cases), "to_verify": len(to_verify),
                       "claimants_confirmed": sum(1 for c in to_verify if c.claimant_state == "confirmed"),
                       "conflicts": sum(1 for p in problems if "Two people" in p or "different" in p)},
            "ok": not problems}
