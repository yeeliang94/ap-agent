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
from .investigator.contracts import IGNORABLE_ROLES
from .models import ClaimCase, ClaimFlag, ClaimSourceArtifact, ClaimsRun

_NAME_PREFIX = re.compile(r"^([A-Z][A-Za-z.'\-]+(?: [A-Z][A-Za-z.'\-]+){1,3})_")
_LABELS = ("name", "employee", "claimant", "staff", "requestor", "requester")
# 'Expense Report_July.pdf' is titled like 'Aegene Ong_receipts.pdf' and
# means nothing about who owns it. A prefix holding one of these words is
# a document title, never a person.
_DOC_WORDS = {"expense", "expenses", "report", "reports", "claim", "claims", "form", "forms",
              "mileage", "summary", "travel", "receipt", "receipts", "invoice", "invoices",
              "statement", "statements", "listing", "scan", "scans", "copy", "attachment",
              "supporting", "document", "documents", "bundle", "approval", "approvals",
              "reimbursement", "batch", "folder"}


def _fold(text: str) -> str:
    return " ".join((text or "").lower().replace("(", " ").replace(")", " ").split())


def _is_document_title(text: str) -> bool:
    return any(word in _DOC_WORDS for word in _fold(text).split())


def _same_person(a: str, b: str) -> bool:
    """Two folded names that differ only by initials or a middle name."""
    return bool(a) and bool(b) and (a == b or a in b or b in a)


def _stated_names(run: ClaimsRun) -> set[str]:
    """Folded names the run's OWN files state in a header cell ('Name:
    Aegene Ong'). A person-like file-name prefix is ownership only when
    one of these corroborates it; alone it is a hint."""
    return {_fold(name) for f in (run.survey or {}).get("files", [])
            for _cite, name in _peek_names(((f.get("peek") or {}).get("tabs") or {}))}


def _peek_names(tabs: dict) -> list[tuple[dict, str]]:
    """(cite, name) for every value cell beside a 'Name'-like label cell."""
    found: list[tuple[dict, str]] = []
    for tab, rows in (tabs or {}).items():
        for row in (rows or [])[:6]:
            cells = [c.strip() for c in row.split(" | ")]
            for i, cell in enumerate(cells):
                ref, _, text = cell.partition(": ")
                if text.rstrip(":").strip().lower() in _LABELS and i + 1 < len(cells):
                    nref, _, ntext = cells[i + 1].partition(": ")
                    if ntext.strip():
                        found.append(({"sheet": tab, "row": int(re.sub(r"\D", "", nref) or 0),
                                       "note": f"beside {text!r} in {ref}"}, ntext.strip()))
    return found


def signals_for(run: ClaimsRun, artifacts: list[ClaimSourceArtifact]) -> dict[str, list[dict]]:
    """artifact_id → [{kind: er_code|name|folder, value, strength, cite}]."""
    peeks = {f["path"]: f for f in (run.survey or {}).get("files", [])}
    stated = _stated_names(run)
    out: dict[str, list[dict]] = {}
    for a in artifacts:
        sig: list[dict] = []
        name = Path(a.path).name
        code = survey_mod.er_code_of(name)
        if code:
            sig.append({"kind": "er_code", "value": code, "strength": "strong",
                        "cite": {"file": a.path, "page": 0, "note": "in the file name"}})
        m = _NAME_PREFIX.match(name)
        if m and not _is_document_title(m.group(1)):
            value = m.group(1).strip()
            said = next((s for s in stated if _same_person(_fold(value), s)), "")
            sig.append({"kind": "name", "value": value, "strength": "strong" if said else "weak",
                        "cite": {"file": a.path, "page": 0,
                                 "note": "prefix of the file name" if said else
                                         "prefix of the file name — no header cell of this run names them"}})
        top = a.path.split("/", 1)[0] if "/" in a.path else ""
        if top:
            sig.append({"kind": "folder", "value": top, "strength": "weak",
                        "cite": {"file": a.path, "page": 0, "note": "top-level folder"}})
        tabs = ((peeks.get(a.path) or {}).get("peek") or {}).get("tabs") or {}
        for cite, stated_name in _peek_names(tabs):
            sig.append({"kind": "name", "value": stated_name, "strength": "strong", "cite": cite})
        for tab, rows in tabs.items():
            for row in rows[:6]:
                cells = [c.strip() for c in row.split(" | ")]
                for i, cell in enumerate(cells):
                    ref, _, text = cell.partition(": ")
                    c2 = survey_mod.er_code_of(text)
                    if c2:
                        sig.append({"kind": "er_code", "value": c2, "strength": "strong",
                                    "cite": {"sheet": tab, "row": int(re.sub(r"\D", "", ref) or 0)}})
        out[a.artifact_id] = sig
    return out


def conflict_in(case: ClaimCase, artifacts: list[ClaimSourceArtifact], signals: dict[str, list[dict]],
                shared: set[str] | None = None) -> str:
    """A sentence naming the conflict, or "" — strong signals of the case's
    files that point at two different identifiers or two different names.
    `shared` = artifact ids of master workbooks that are the report of
    several cases: they carry every claimant's name by design and are left
    out of the comparison."""
    codes: dict[str, str] = {}
    names: dict[str, str] = {}
    for a in artifacts:
        if a.case_id != case.id or (shared and a.artifact_id in shared):
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
        # A name that is a prefix of another (initials, middle names) is
        # the same person — so the names are CLUSTERED by that relation
        # and a conflict is more than one cluster. Eliminating every name
        # that relates to another instead would silently drop a real
        # third name whenever two of the three happened to be one person.
        clusters = _clusters(list(names))
        if len(clusters) > 1:
            return "the files carry different names: " + "; ".join(names[c[0]] for c in clusters[:4])
    return ""


def _clusters(keys: list[str]) -> list[list[str]]:
    """The folded names grouped by 'one contains the other', transitively.
    Order is the order the names were first seen."""
    parent = {k: k for k in keys}

    def find(k: str) -> str:
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            if _same_person(a, b):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb
    groups: dict[str, list[str]] = {}
    for k in keys:
        groups.setdefault(find(k), []).append(k)
    return list(groups.values())


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
        # the chosen report may be a MASTER workbook whose home is another
        # case (one sheet per claimant, scenario F): any used workbook of
        # the run that the case named keeps being its report
        report = next((a for a in workbooks if a.path == old.get("report_file")), None) \
            or next((a for a in artifacts if a.media_type == "workbook" and a.path == old.get("report_file")
                     and a.disposition == "used" and (case.artifact_ids and a.artifact_id in case.artifact_ids)), None) \
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
            "ignored": [a.path for a in mine if a.proposed_role in IGNORABLE_ROLES
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
    report_use: dict[str, int] = {}
    for c in cases:
        rf = (c.roles or {}).get("report_file")
        if rf:
            report_use[rf] = report_use.get(rf, 0) + 1
    shared = {a.artifact_id for a in artifacts if report_use.get(a.path, 0) > 1}
    for c in cases:
        own_report = [aid for aid in (c.artifact_ids or []) if aid != "" and any(
            a.artifact_id == aid and a.media_type == "workbook" and a.path == (c.roles or {}).get("report_file") for a in artifacts)]
        c.artifact_ids = sorted({*[a.artifact_id for a in artifacts if a.case_id == c.id], *own_report})
        if c.state == "excluded":
            continue
        why = conflict_in(c, artifacts, signals, shared)
        if why:
            key = ("OWNERSHIP_CONFLICT", c.id)
            live.add(key)
            f = existing.get(key)
            if c.claimant_state == "proposed":
                # conflicting strong signals force Claimant = unknown: the
                # proposed name stays visible as a suggestion, never as a state
                c.claimant_state = "unknown"
                c.claimant_basis = (c.claimant_basis + "; set to unknown: the files carry conflicting identity signals")[:400]
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
    open_conflicts = s.query(ClaimFlag).filter(ClaimFlag.run_id == run.id, ClaimFlag.code == "OWNERSHIP_CONFLICT",
                                               ClaimFlag.status == "open").all()
    for f in open_conflicts:
        problems.append(f.reason.split(". ")[0])
    # Every unresolved file shuts the gate: "potentially material" is not a
    # judgment code makes from a file type it could not read (an approval
    # e-mail is a .msg; an instruction can sit in a .txt).
    material_unresolved = [a for a in artifacts if a.disposition == "unresolved"]
    by_case: dict[str, list[str]] = {}
    for a in material_unresolved[:20]:
        problems.append(f"{a.path}: a file ({a.media_type}) nobody has placed — move it into a case, or mark it "
                        "irrelevant / duplicate / unreadable")
        if a.case_id:
            by_case.setdefault(a.case_id, []).append(problems[-1])
    to_verify = [c for c in cases if c.state != "excluded"]
    conflicts = len(open_conflicts)
    for f in open_conflicts:
        by_case.setdefault(f.case_id, []).append(f.reason.split(". ")[0])
    for c in to_verify:
        roles = c.roles or {}
        mine: list[str] = []
        if not roles.get("no_report") and not roles.get("report_file"):
            mine.append(f"{c.label}: choose the claim summary workbook and its sheet, or mark 'no summary'")
        elif not roles.get("no_report") and not roles.get("report_tab"):
            mine.append(f"{c.label}: choose the sheet of {Path(roles['report_file']).name} that holds the lines")
        if roles.get("no_report") and not roles.get("receipt_files"):
            mine.append(f"{c.label}: no summary and no receipt files — nothing to verify; move files in or exclude it")
        problems += mine
        if mine:
            by_case.setdefault(c.id, []).extend(mine)
    from . import source as source_mod

    if len(to_verify) > source_mod.MAX_CASES_PER_RUN:
        problems.append(f"{len(to_verify)} cases to verify — more than the {source_mod.MAX_CASES_PER_RUN} one run may hold")
    # A batch with files but no case would "verify" nothing and still end
    # green — the do-nothing run is refused here, not discovered later.
    if artifacts and not to_verify:
        problems.append("no case to verify — the batch holds files but nobody would be checked or paid; "
                        "create a case from the files, or cancel the run if this batch truly holds no claims")
    return {"problems": problems, "by_case": by_case,
            "counts": {"artifacts": len(artifacts),
                       "dispositioned": sum(1 for a in artifacts if a.disposition != "unresolved"),
                       "unresolved": sum(1 for a in artifacts if a.disposition == "unresolved"),
                       "material_unresolved": len(material_unresolved),
                       "cases": len(cases), "to_verify": len(to_verify),
                       "claimants_confirmed": sum(1 for c in to_verify if c.claimant_state == "confirmed"),
                       # what Confirm grouping is about to confirm, so the
                       # button can say it before the click
                       "claimants_to_confirm": sum(1 for c in to_verify if c.claimant_state == "proposed"
                                                   and (c.claimant_name or "").strip()),
                       "conflicts": conflicts},
            "ok": not problems}
