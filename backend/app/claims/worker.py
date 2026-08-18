"""One worker per employee, five at a time, failure isolated, retryable.

A worker = steps 7–9 for ONE employee, seeing only that employee's files:
read the report (+ KM tab) → inventory the evidence pages → match and
check → decide the category → write rows, evidence and flags. It runs
inside its own database session and its own try/except: one employee
failing (model error, request cap, unreadable file) marks that employee
failed with the reason and the others carry on. Per-employee timing and
AI cost go to the run diary and the employee summary.

The run turns ready when every employee is verified, failed or skipped.
"""
from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal
from pathlib import Path

from openpyxl import load_workbook

from .. import config, telemetry
from ..db import SessionLocal
from ..model_layer import USAGE_LIMITS, create_agent
from . import category as category_mod
from . import checks as checks_mod
from . import evidence as evidence_mod
from . import profile as profile_mod
from . import report_reader
from .models import ClaimEmployee, ClaimEvidence, ClaimFlag, ClaimRow, ClaimsRun

log = logging.getLogger("claims.worker")

POOL_SIZE = 5
# A worker's whole budget of AI requests. Each page read is its own tiny
# conversation (well inside the enterprise per-agent cap, which
# USAGE_LIMITS enforces on every call); this cap bounds the WORKER, so a
# runaway employee folder cannot multiply spend unnoticed.
WORKER_REQUEST_CAP = config.MAX_AGENT_REQUESTS * 4


class WorkerBudgetExceeded(Exception):
    pass


def _files_dir(run_id: str) -> Path:
    from .runner import files_dir

    return files_dir(run_id)


async def verify_run(db, run: ClaimsRun) -> None:
    """Run the pool over every pending employee, then close the run."""
    from .runner import _set

    started = time.monotonic()
    employees = db.query(ClaimEmployee).filter(ClaimEmployee.run_id == run.id,
                                               ClaimEmployee.status == "pending").all()
    ids = [e.id for e in employees]
    total = db.query(ClaimEmployee).filter(ClaimEmployee.run_id == run.id).count()
    _set(db, run, progress={"done": total - len(ids), "total": total})
    telemetry.record(db, run.id, "verify", telemetry.INFO, "STAGE_STARTED",
                     f"Verifying {len(ids)} employee(s), {POOL_SIZE} at a time.")
    # The listing header map (Step 12) is read once, before the workers,
    # so the category judge can see past examples and the output has its
    # column order ready.
    try:
        from . import listing as listing_mod

        await listing_mod.prepare_listing(db, run)
    except Exception as exc:
        telemetry.record_failure(db, run.id, "output", "LISTING_UNREADABLE",
                                 "Could not read the linked listing", exc)
    # The client's category list, once per run: from the first report in
    # the batch that carries an Expense Types tab (they share a template),
    # so an employee with no report is still judged against the client's
    # own list. Code only.
    if not (profile_mod.profile_of(run.snapshot).get("categories") or []):
        cats = _batch_categories(run)
        if cats:
            run.survey = {**(run.survey or {}), "categories": cats}
            db.commit()
    sem = asyncio.Semaphore(POOL_SIZE)

    async def one(eid: str) -> None:
        async with sem:
            await verify_employee(run.id, eid)
            _bump(run.id)

    await asyncio.gather(*(one(eid) for eid in ids))
    _finish_run(run.id, started)


def _batch_categories(run: ClaimsRun) -> list[dict]:
    files = _files_dir(run.id)
    for e in (run.map or {}).get("employees", []):
        rf = e.get("report_file")
        if e.get("is_employee") and rf and (files / rf).is_file():
            try:
                cats = report_reader.read_categories(load_workbook(files / rf, read_only=True, data_only=True))
            except Exception:
                continue
            if cats:
                return cats
    return []


def _bump(run_id: str) -> None:
    s = SessionLocal()
    try:
        run = s.get(ClaimsRun, run_id)
        total = s.query(ClaimEmployee).filter(ClaimEmployee.run_id == run_id).count()
        done = s.query(ClaimEmployee).filter(
            ClaimEmployee.run_id == run_id,
            ClaimEmployee.status.in_(("verified", "failed", "skipped"))).count()
        run.progress = {"done": done, "total": total}
        s.commit()
    finally:
        s.close()


def _finish_run(run_id: str, started: float) -> None:
    """Close the run: run-level flags for controls the batch needed and
    could not find, then ready — with a diary line summing it up."""
    s = SessionLocal()
    try:
        run = s.get(ClaimsRun, run_id)
        employees = s.query(ClaimEmployee).filter(ClaimEmployee.run_id == run_id).all()
        profile = profile_mod.profile_of(run.snapshot)
        rows = s.query(ClaimRow).filter(ClaimRow.run_id == run_id).all()
        row_dicts = [{"kind": r.kind} for r in rows]
        existing = {f.code for f in s.query(ClaimFlag).filter(ClaimFlag.run_id == run_id,
                                                                ClaimFlag.employee_id == "").all()}
        why = checks_mod.needs_missing_reference(row_dicts, profile)
        if why and "MISSING_REFERENCE:rates" not in existing:
            s.add(ClaimFlag(run_id=run_id, employee_id="", code="MISSING_REFERENCE", reason=why,
                            basis="client profile: mileage_rates (not set)", cite={"what": "rates"}))
        listing_state = (run.listing_headers or {}).get("state")
        if listing_state in (None, "missing", "unreadable") and "MISSING_REFERENCE" not in existing:
            s.add(ClaimFlag(run_id=run_id, employee_id="", code="MISSING_REFERENCE",
                            reason=("No listing workbook could be read for this run"
                                    + (f" ({(run.listing_headers or {}).get('why')})" if (run.listing_headers or {}).get("why") else "")
                                    + ", so the output columns will follow the fallback set (Name, ER code, "
                                      "Category, GL, Amount MYR) instead of the client's own listing. Link "
                                      "the listing and start a new run, or acknowledge to proceed with the "
                                      "fallback."),
                            basis="run input: this month's listing link", cite={"what": "listing"}))
        n_flags = s.query(ClaimFlag).filter(ClaimFlag.run_id == run_id, ClaimFlag.status == "open").count()
        failed = [e for e in employees if e.status == "failed"]
        cost = sum(int((e.summary or {}).get("requests", 0)) for e in employees)
        tokens = sum(int((e.summary or {}).get("tokens", 0)) for e in employees)
        run.status = "ready"
        run.progress = {"done": len(employees), "total": len(employees)}
        s.commit()
        telemetry.record(s, run_id, "verify", telemetry.INFO, "STAGE_DONE",
                         f"Verification finished in {time.monotonic() - started:.0f}s: "
                         f"{len(employees) - len(failed)} of {len(employees)} employee(s) verified"
                         + (f", {len(failed)} failed" if failed else "")
                         + f"; {n_flags} open flag(s); AI cost {cost} request(s), {tokens} tokens.")
        telemetry.record(s, run_id, "run", telemetry.INFO, "RUN_READY",
                         "Run is ready for review.")
    finally:
        s.close()


async def verify_employee(run_id: str, employee_id: str) -> None:
    """The whole worker for one employee, in its own session, never raising."""
    s = SessionLocal()
    started = time.monotonic()
    usage = evidence_mod.Usage()
    try:
        run = s.get(ClaimsRun, run_id)
        emp = s.get(ClaimEmployee, employee_id)
        if emp is None or emp.status == "skipped":
            return
        emp.status, emp.error = "verifying", ""
        s.commit()
        # A retry starts clean: the previous attempt's rows, evidence and
        # open flags go; decided flags are kept for the record.
        s.query(ClaimRow).filter(ClaimRow.employee_id == employee_id).delete()
        s.query(ClaimEvidence).filter(ClaimEvidence.employee_id == employee_id).delete()
        s.query(ClaimFlag).filter(ClaimFlag.employee_id == employee_id,
                                  ClaimFlag.status.in_(("open", "info"))).delete()
        s.commit()
        try:
            await _work(s, run, emp, usage)
        except WorkerBudgetExceeded as exc:
            _fail_employee(s, run_id, emp, f"AI request cap reached ({exc})", started, usage)
            return
        except Exception as exc:
            reason = telemetry.record_failure(s, run_id, "verify", "EMPLOYEE_FAILED",
                                              f"Could not verify {emp.name or emp.folder}", exc)
            _fail_employee(s, run_id, emp, reason, started, usage)
            return
        emp.status = "verified"
        emp.summary = {**(emp.summary or {}), "seconds": round(time.monotonic() - started, 1),
                       "requests": usage.requests, "tokens": usage.tokens}
        s.commit()
        n_flags = s.query(ClaimFlag).filter(ClaimFlag.employee_id == employee_id,
                                            ClaimFlag.status == "open").count()
        telemetry.record(s, run_id, "verify", telemetry.INFO, "EMPLOYEE_DONE",
                         f"{emp.name or emp.folder}: verified in {time.monotonic() - started:.0f}s, "
                         f"{(emp.summary or {}).get('rows', 0)} row(s), {n_flags} open flag(s), "
                         f"{usage.requests} AI request(s), {usage.tokens} tokens.")
    finally:
        s.close()


def _fail_employee(s, run_id: str, emp: ClaimEmployee, reason: str, started: float, usage) -> None:
    emp.status, emp.error = "failed", reason
    emp.summary = {**(emp.summary or {}), "seconds": round(time.monotonic() - started, 1),
                   "requests": usage.requests, "tokens": usage.tokens}
    s.commit()


async def _work(s, run: ClaimsRun, emp: ClaimEmployee, usage: evidence_mod.Usage) -> None:
    profile = profile_mod.profile_of(run.snapshot)
    files = _files_dir(run.id)
    roles = emp.roles or {}
    notes: list[tuple[str, str]] = []
    rows: list[dict] = []
    header: dict = {}
    categories: list[dict] = []
    flags: list[dict] = []

    def budget() -> None:
        if usage.requests > WORKER_REQUEST_CAP:
            raise WorkerBudgetExceeded(f"{usage.requests} > {WORKER_REQUEST_CAP}")

    # ---- 7. the report (+ KM tab) --------------------------------------------------
    report_ok = False
    if not roles.get("no_report") and roles.get("report_file"):
        path = files / roles["report_file"]
        try:
            wb = load_workbook(path, data_only=True)
        except Exception as exc:
            flags.append(checks_mod._flag("REPORT_UNREADABLE",
                                          f"{roles['report_file']} could not be opened as a workbook "
                                          f"({type(exc).__name__}). Continuing with receipts only.",
                                          "universal rule: a report that cannot be read is said so, never guessed",
                                          {"file": roles["report_file"], "page": 0}))
            wb = None
        if wb is not None:
            categories = report_reader.read_categories(wb) or list(profile.get("categories") or [])
            tab = roles.get("report_tab")
            if tab in wb.sheetnames:
                try:
                    r_rows, header, r_notes = await report_reader.read_report(
                        wb[tab], emp.name, emp.er_code, usage)
                    notes += r_notes
                    for r in r_rows:
                        rows.append({"kind": "expense", "sheet": tab, "row": r["row"], "values": r})
                    report_ok = True
                except report_reader.ReportUnreadable as exc:
                    flags.append(checks_mod._flag("REPORT_UNREADABLE",
                                                  f"The report tab {tab!r} of {roles['report_file']} could not be "
                                                  f"read reliably: {exc} Continuing with receipts only.",
                                                  "universal rule: three attempts, then a person",
                                                  {"sheet": tab, "row": 0}))
                    notes.append(("WARNING", f"Report tab {tab!r}: unreadable — {exc}"))
            else:
                flags.append(checks_mod._flag("REPORT_UNREADABLE",
                                              f"Tab {tab!r} is not in {roles['report_file']} (tabs: "
                                              f"{wb.sheetnames}). Continuing with receipts only.",
                                              "the confirmed map", {"sheet": tab or "", "row": 0}))
            budget()
            km_tab = roles.get("mileage_tab")
            if km_tab and km_tab in wb.sheetnames and km_tab != tab:
                try:
                    trips_rows, k_notes = await report_reader.read_km(wb[km_tab], usage)
                    notes += k_notes
                    for t in trips_rows:
                        rows.append({"kind": "mileage", "sheet": km_tab, "row": t["row"], "values": t})
                except report_reader.ReportUnreadable as exc:
                    flags.append(checks_mod._flag("REPORT_UNREADABLE",
                                                  f"The mileage tab {km_tab!r} could not be read reliably: {exc}",
                                                  "universal rule: three attempts, then a person",
                                                  {"sheet": km_tab, "row": 0}))
                    notes.append(("WARNING", f"KM tab {km_tab!r}: unreadable — {exc}"))
            budget()
    if not categories:
        categories = list(profile.get("categories") or (run.survey or {}).get("categories") or [])

    # ---- 8. the evidence pages ---------------------------------------------------------
    receipts: list[dict] = []
    trips: list[dict] = []
    pages_read = 0
    files_read = 0
    page_sem = evidence_mod.page_semaphore()  # shared by every worker
    for rel in roles.get("receipt_files") or []:
        path = files / rel
        if not path.is_file():
            notes.append(("WARNING", f"{rel}: file missing from the workspace"))
            continue
        try:
            r, t, pages, n = await evidence_mod.read_bundle(path, rel, usage, page_sem)
        except Exception as exc:
            reason = telemetry.describe_failure(exc)
            notes.append(("WARNING", f"{rel}: could not be read ({reason})"))
            raise
        receipts += r
        trips += t
        pages_read += len(pages)
        files_read += 1
        notes += [("INFO", x) for x in n]
        kinds = {}
        for p in pages:
            kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
        notes.append(("INFO", f"{rel}: {len(pages)} page(s) — "
                              + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
                              + f"; {len(r)} receipt(s), {len(t)} map trip(s)."))
        budget()

    # ---- no report: the receipts become the row list ------------------------------------
    if not report_ok and not roles.get("no_report") and not any(f["code"] == "REPORT_UNREADABLE" for f in flags):
        # a report was named but nothing came of it — say so
        flags.append(checks_mod._flag("REPORT_UNREADABLE", "No report tab could be read.",
                                      "the confirmed map", {}))
    derived = False
    if not report_ok:
        derived = True
        for i, r in enumerate(receipts, 1):
            rows.append({"kind": "derived", "sheet": "", "row": i,
                         "values": {"date": r.get("date", ""), "item": "", "item_name": "", "gl": "",
                                    "reason": f"from receipt: {r.get('vendor', '')}", "receipt_included": "Y",
                                    "amount": r["amount"], "currency": r["currency"], "rate": "1",
                                    "total": r["amount"] if r["currency"] == "MYR" else None,
                                    "vendor": r.get("vendor", "")}})
        if roles.get("no_report"):
            flags.append(checks_mod._flag("NO_REPORT",
                                          f"{emp.name or emp.folder} has no expense report in the folder; "
                                          f"{len(receipts)} row(s) were built from the receipts found. Confirm "
                                          "the derived list is what should be paid.",
                                          "universal rule: receipts but no report → rows from receipts, "
                                          "flagged for a person", {}))

    # ---- 9. checks — in memory, so no database lock is held while the
    # tie-break may call the AI; then everything is written in one commit.
    from .models import _id as new_id

    row_dicts = []
    for r in rows:
        r["id"] = new_id()
        row_dicts.append({"id": r["id"], "kind": r["kind"], "sheet": r["sheet"], "row": r["row"],
                          "values": dict(r["values"])})
    ev_dicts = []
    for r in receipts:
        r["id"] = new_id()
        ev_dicts.append({"id": r["id"], "kind": "receipt", "file": r["file"], "page": r["page"],
                         "position": r["position"],
                         "values": {"vendor": r["vendor"], "date": r["date"], "amount": r["amount"],
                                    "currency": r["currency"],
                                    **{k: r[k] for k in ("date_alt", "amount_alt") if r.get(k)}},
                         "confidence": dict(r.get("confidence") or {})})
    for t in trips:
        t["id"] = new_id()
        ev_dicts.append({"id": t["id"], "kind": "map_trip", "file": t["file"], "page": t["page"],
                         "position": "",
                         "values": {"date": t["date"], "purpose": t["purpose"], "from": t["from"],
                                    "to": t["to"], "return_trip": t["return_trip"],
                                    "km_printed": t["km_printed"]},
                         "confidence": dict(t.get("confidence") or {})})
    if derived:
        # each derived row IS its receipt (rows were built from receipts in order)
        derived_rows = [r for r in rows if r["kind"] == "derived"]
        for row, rec in zip(derived_rows, receipts):
            row["matched_evidence_id"] = rec["id"]
            rec["matched_row_id"] = row["id"]
    result = await checks_mod.run_checks(row_dicts, ev_dicts, profile,
                                         {"name": emp.name, "er_code": emp.er_code},
                                         (pages_read, files_read), TieBreak(usage))
    for r, rd in zip(rows, row_dicts):
        verdict, eid = result["verdicts"].get(r["id"], ("unchecked", ""))
        s.add(ClaimRow(id=r["id"], run_id=run.id, employee_id=emp.id, kind=r["kind"], sheet=r["sheet"],
                       row=r["row"], values=rd["values"],
                       verdict="matched" if r["kind"] == "derived" else verdict,
                       matched_evidence_id=r.get("matched_evidence_id") or eid))
    for ed in ev_dicts:
        s.add(ClaimEvidence(id=ed["id"], run_id=run.id, employee_id=emp.id, kind=ed["kind"], file=ed["file"],
                            page=ed["page"], position=ed["position"], values=ed["values"],
                            confidence=ed["confidence"],
                            matched_row_id=result["matches"].get(ed["id"], "")
                            or next((r["id"] for r in rows if r.get("matched_evidence_id") == ed["id"]), "")))
    for f in flags:
        s.add(ClaimFlag(run_id=run.id, employee_id=emp.id, **f))
    for f in result["flags"]:
        s.add(ClaimFlag(run_id=run.id, employee_id=emp.id, **f))
    for level, text in notes + [("INFO", n) for n in result["notes"]]:
        telemetry.record(s, run.id, "verify", telemetry.WARNING if level == "WARNING" else telemetry.INFO,
                         "EMPLOYEE_NOTE", f"{emp.name or emp.folder}: {text}")

    # ---- category ---------------------------------------------------------------------
    emp.category, emp.gl, emp.category_basis = "", "", ""
    if categories and profile_mod.check_enabled(profile, "CATEGORY_UNCLEAR"):
        row_values = [r["values"] for r in rows if r["kind"] in ("expense", "derived")]
        examples = list((run.listing_headers or {}).get("past_examples") or [])
        try:
            judgment, gl = await category_mod.judge_category(
                categories, header.get("purpose", "") if header else "", row_values,
                profile.get("category_rule") or "", examples, usage)
        except Exception as exc:
            judgment, gl = None, ""
            notes.append(("WARNING", f"category judgement failed: {telemetry.describe_failure(exc)}"))
        if judgment is not None and judgment.sure:
            emp.category, emp.gl = judgment.category, gl
            emp.category_basis = (f"{judgment.category}: quoted \"{judgment.quoted_text}\" — {judgment.why}"
                                  + (f" (rule: {profile.get('category_rule')})" if profile.get("category_rule") else ""))
        else:
            s.add(ClaimFlag(run_id=run.id, employee_id=emp.id, code="CATEGORY_UNCLEAR",
                            reason=("The listing category for this employee could not be settled"
                                    + (f": {judgment.why}" if judgment else " (the judge failed)")
                                    + (f" (best guess: {judgment.category})" if judgment and judgment.category else "")
                                    + ". Choose the category on the employee's summary."),
                            basis=("client profile: category rule — " + (profile.get("category_rule") or "none confirmed yet")
                                   + f"; list from {'the report' if categories else 'nowhere'}"),
                            cite={"sheet": roles.get("report_tab") or "", "row": 0}))
            if judgment and judgment.category:
                emp.category_basis = f"unsure — best guess {judgment.category}: {judgment.why}"
    elif not categories:
        s.add(ClaimFlag(run_id=run.id, employee_id=emp.id, code="CATEGORY_UNCLEAR",
                        reason="No category list is known for this client (no Expense Types tab in the "
                               "report and none in the profile). Choose the category on the employee's "
                               "summary, or add the client's list in Settings → Claims.",
                        basis="client profile: categories (not set)", cite={}))

    # ---- summary -----------------------------------------------------------------------
    total = header.get("total") if header else None
    if total is None:
        total = str(sum((Decimal(r["values"]["total"] or r["values"]["amount"]) for r in rows
                         if r["kind"] in ("expense", "derived") and (r["values"].get("total") or r["values"].get("amount"))),
                        Decimal("0")).quantize(Decimal("0.01")))
    emp.report_total = total
    n_rows = sum(1 for r in rows if r["kind"] != "mileage")
    open_flags = sum(1 for f in flags + result["flags"] if f["status"] == "open")
    emp.summary = {**(emp.summary or {}), "rows": n_rows, "km_rows": sum(1 for r in rows if r["kind"] == "mileage"),
                   "receipts": len(receipts), "map_trips": len(trips), "pages": pages_read,
                   "flagged": len({f["row_id"] for f in result["flags"] if f["status"] == "open" and f["row_id"]}),
                   "open_flags": open_flags, "purpose": (header or {}).get("purpose", ""),
                   "categories_from": "report" if categories and not profile.get("categories") else ("profile" if categories else "")}
    s.commit()


class TieBreak:
    """The AI tie-break among candidate receipts — one call per tie."""

    def __init__(self, usage: evidence_mod.Usage) -> None:
        self.usage = usage

    async def __call__(self, row: dict, candidates: list[dict]) -> str:
        from pydantic import BaseModel, Field

        class Pick(BaseModel):
            index: int = Field(ge=0, description="which candidate (0-based), or -1 if unsure")
            why: str = Field(max_length=200)

        v = row["values"]
        text = (f"A claim row: {v.get('date')} | {v.get('item_name') or v.get('item')} | "
                f"{v.get('reason')} | {v.get('currency', 'MYR')} {v.get('amount')}\n"
                "Candidate receipts (same day, amount and currency):\n"
                + "\n".join(f"{i}. vendor {c['values'].get('vendor')} at {c.get('file')} p.{c.get('page')} "
                            f"{c.get('position')}" for i, c in enumerate(candidates))
                + "\nWhich candidate is this row's receipt, judging by the row's reason and the "
                  "vendors? Answer -1 if you cannot tell.")
        agent = create_agent("judge", Pick, "Pick the receipt that supports the row, or -1 if unsure.",
                             temperature=0)
        result = await evidence_mod.ai_call(agent.run(text, usage_limits=USAGE_LIMITS), "the receipt tie-break")
        self.usage.add(result)
        i = result.output.index
        return candidates[i]["id"] if 0 <= i < len(candidates) else ""


async def run_checks_for(s, run: ClaimsRun, emp: ClaimEmployee, profile: dict,
                         searched: tuple[int, int], usage=None) -> dict:
    """Run the checks over the employee's STORED rows and evidence, and
    write the verdicts and matches back. Used by the worker and by every
    correction (instant re-check)."""
    row_objs = s.query(ClaimRow).filter(ClaimRow.employee_id == emp.id).all()
    ev_objs = s.query(ClaimEvidence).filter(ClaimEvidence.employee_id == emp.id).all()
    rows = [{"id": r.id, "kind": r.kind, "sheet": r.sheet, "row": r.row, "values": dict(r.values)}
            for r in row_objs]
    evidence = [{"id": e.id, "kind": e.kind, "file": e.file, "page": e.page, "position": e.position,
                 "values": dict(e.values), "confidence": dict(e.confidence or {})} for e in ev_objs]
    tie = TieBreak(usage or evidence_mod.Usage())
    result = await checks_mod.run_checks(rows, evidence, profile,
                                         {"name": emp.name, "er_code": emp.er_code}, searched, tie)
    by_id = {r.id: r for r in row_objs}
    for rid, (verdict, eid) in result["verdicts"].items():
        if rid in by_id and by_id[rid].kind != "derived":
            by_id[rid].verdict = verdict
            by_id[rid].matched_evidence_id = eid
    for e in ev_objs:
        if e.kind == "receipt" or e.kind == "map_trip":
            if e.matched_row_id and by_id.get(e.matched_row_id) and by_id[e.matched_row_id].kind == "derived":
                continue
            e.matched_row_id = result["matches"].get(e.id, "")
    s.flush()
    return result


async def retry_employee(run_id: str, employee_id: str) -> None:
    """Re-run one worker (Retry on a failed employee, or Re-verify)."""
    s = SessionLocal()
    try:
        run = s.get(ClaimsRun, run_id)
        was_ready = run.status == "ready"
        if was_ready:
            run.status = "verifying"
            s.commit()
    finally:
        s.close()
    started = time.monotonic()
    await verify_employee(run_id, employee_id)
    _bump(run_id)
    if was_ready or True:
        _finish_run(run_id, started)
