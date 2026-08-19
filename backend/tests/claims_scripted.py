"""A scripted stand-in for every AI call the claims module makes, driven by
the sample's ground truth — so a WHOLE run (survey → map → confirm →
verify → output) can be exercised end to end for nothing, deterministically.

Shared by the H0 baseline pin (test_claims_baseline.py), the H1 investigator
conformance tests and the H12 scenario suites. Not a test module itself.

What is scripted, and how:
  - the map AI answers the correct map for the run's own survey (built from
    the ground truth), so mapping.audit_map is exercised for real
  - the report / KM readers keep their real audit loop; only their agent is
    scripted with the reading a competent AI gives (columns A..H, from the
    sheet's own shape)
  - the evidence page reads are replaced at read_bundle: receipts and map
    trips come from the ground truth for that file
  - the category judge answers the ground-truth category, sure
  - the listing is read from the sample workbook by code (header row 1)
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from app.claims import category as category_mod
from app.claims import evidence as evidence_mod
from app.claims import listing as listing_mod
from app.claims import mapping, report_reader
from app.claims import source as batch_source
from app.claims.mapping import ClaimMap, FileRole, FolderMap
from app.claims.report_reader import KMColumns, KMReading, ReportColumns, ReportReading

GEN = Path(__file__).resolve().parents[2] / "samples" / "generated" / "claims"
LISTING = Path(__file__).resolve().parents[2] / "samples" / "generated" / "Summary of Invoices JUL26.xlsx"


def truth() -> dict:
    return json.loads((GEN / "ground_truth_claims.json").read_text())


def profile_from_truth(t: dict | None = None) -> dict:
    t = t or truth()
    p = t["profile"]
    return {"mileage_rates": {k: str(v) for k, v in p["mileage_rates"].items()},
            "receipt_optional_items": list(p["receipt_optional_items"]),
            "categories": [{"item": i, "gl": g} for i, g in p["categories"]],
            "category_rule": p["category_rule"], "km_tolerance": "0",
            "receipt_date_window_days": 0, "unclaimed_receipt_threshold": "100",
            "mileage_item_pattern": "mileage", "file_role_patterns": [], "checks": {}, "set_by": {}}


class ScriptedAgent:
    """Answers a fixed list of outputs, in order; records the prompts."""

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.prompts = []

    async def run(self, prompt, **kwargs):
        self.prompts.append(prompt)

        class R:
            output = self._outputs.pop(0)

            def usage(self):
                class U:
                    total_tokens = 10
                    requests = 1
                return U()
        return R()


def good_map(survey: dict, t: dict | None = None) -> ClaimMap:
    """The correct map for the sample, from the ground truth."""
    by_folder = {e["folder"]: e for e in (t or truth())["employees"]}
    employees = []
    for fo in survey["folders"]:
        e = by_folder[fo["path"]]
        files = []
        for path in fo["files"]:
            name = path.split("/", 1)[1]
            if name == e["files"]["report"]:
                role, why = "report", "tab 'Expense Report' has a name header and dated rows"
            elif name in e["files"]["receipts"]:
                role, why = "receipts", "page 1 shows till receipts side by side"
            elif name in (e["files"]["report_print"], e["files"]["approval"]):
                role, why = "ignore", "a print of the report / an approval e-mail"
            else:
                role, why = "unplaced", "cannot tell what this is"
            files.append(FileRole(path=path, role=role, reason=why))
        report = e["files"]["report"]
        employees.append(FolderMap(
            folder=fo["path"], is_employee=True, name=e["name"],
            er_code=e["er_code"] if report else "",
            report_file=f"{fo['path']}/{report}" if report else None,
            report_tab="Expense Report" if report else None,
            mileage_tab="KM" if e["mileage_tab"] else None,
            no_report=report is None, files=files,
            reason="folder named after one person; report and receipts inside"))
    return ClaimMap(employees=employees, root_files=[], notes=[])


def good_report_reading(ws) -> ReportReading:
    last = max(r for r in range(7, ws.max_row + 1) if ws.cell(row=r, column=1).value is not None)
    total_row = next(r for r in range(last, ws.max_row + 1) if ws.cell(row=r, column=7).value == "Total (MYR)")
    return ReportReading(
        columns=ReportColumns(date="A", item="B", reason="C", receipt_included="D", amount="E",
                              currency="F", rate="G", total="H"),
        header_row=6, first_row=7, last_row=last, total_cell=f"H{total_row}", name_cell="B1",
        period_cell="B2", purpose_cell="B3", why="header block, dated lines, total")


def good_km_reading(ws) -> KMReading:
    rows = [r for r in range(5, ws.max_row + 1) if ws.cell(row=r, column=1).value is not None
            and str(ws.cell(row=r, column=1).value).strip().lower() != "total"]
    if not rows:
        return KMReading(has_trips=False, why="headings only")
    total_row = next((r for r in range(rows[-1] + 1, ws.max_row + 2)
                      if str(ws.cell(row=r, column=7).value or "").strip().lower().startswith("total")), None)
    return KMReading(has_trips=True, columns=KMColumns(date="A", **{"from": "B"}, to="C", purpose="D",
                                                       vehicle="E", km="F", rate="G", amount="H"),
                     header_row=4, first_row=rows[0], last_row=rows[-1],
                     total_cell=f"H{total_row}" if total_row else None, why="trips")


def install(monkeypatch, survey_of, t: dict | None = None) -> dict:
    """Patch every AI seam. survey_of() returns the run's survey (the map
    answer is built lazily from it, once the run has produced it).
    Returns a dict of the scripted agents/hooks for assertions."""
    t = t or truth()
    holder: dict = {"map_prompts": []}

    class MapAgent:
        async def run(self, prompt, **kw):
            holder["map_prompts"].append(prompt)

            class R:
                output = good_map(survey_of(), t)

                def usage(self):
                    class U:
                        total_tokens = 10
                        requests = 1
                    return U()
            return R()
    monkeypatch.setattr(mapping, "create_agent", lambda *a, **k: MapAgent())

    real_read_report, real_read_km = report_reader.read_report, report_reader.read_km

    async def fake_read_report(ws, name, er, usage=None, context=""):
        monkeypatch.setattr(report_reader, "create_agent",
                            lambda *a, **k: ScriptedAgent([good_report_reading(ws)] * report_reader.MAX_ROUNDS))
        return await real_read_report(ws, name, er, usage, context=context)

    async def fake_read_km(ws, usage=None, context=""):
        monkeypatch.setattr(report_reader, "create_agent",
                            lambda *a, **k: ScriptedAgent([good_km_reading(ws)] * report_reader.MAX_ROUNDS))
        return await real_read_km(ws, usage, context=context)
    monkeypatch.setattr(report_reader, "read_report", fake_read_report)
    monkeypatch.setattr(report_reader, "read_km", fake_read_km)

    by_file: dict[str, dict] = {}
    for e in t["employees"]:
        for r in e["receipts"]:
            by_file.setdefault(f"{e['folder']}/{r['file']}", {"receipts": [], "trips": []})["receipts"].append(r)
        for tr in e["map_trips"]:
            by_file.setdefault(f"{e['folder']}/{tr['file']}", {"receipts": [], "trips": []})["trips"].append(tr)

    async def fake_read_bundle(path, rel_path, usage, sem=None, context=""):
        holder.setdefault("page_contexts", []).append(context)
        usage.requests += 1
        n_pages = batch_source.page_count(path) or 1
        found = by_file.get(rel_path, {"receipts": [], "trips": []})
        receipts = [{"file": rel_path, "page": r["page"], "position": r["position"], "vendor": r["vendor"],
                     "date": r["date"], "amount": f"{Decimal(str(r['amount'])):.2f}", "currency": r["currency"],
                     "confidence": {}} for r in found["receipts"]]
        trips = [{"file": rel_path, "page": tr["page"], "date": tr["date"], "purpose": tr["purpose"],
                  "from": "", "to": "", "return_trip": bool(tr["return_trip"]),
                  "km_printed": None if tr["km_printed"] is None else str(Decimal(str(tr["km_printed"])).quantize(Decimal("0.1"))),
                  "confidence": {}} for tr in found["trips"]]
        pages = []
        for p in range(1, n_pages + 1):
            kind = "receipts" if any(r["page"] == p for r in receipts) else (
                "map" if any(tr["page"] == p for tr in trips) else "other")
            pages.append({"file": rel_path, "page": p, "kind": kind, "why": "scripted"})
        return receipts, trips, pages, []
    monkeypatch.setattr(evidence_mod, "read_bundle", fake_read_bundle)

    cat_by_purpose = {e["purpose"]: (e["category"], e["gl"]) for e in t["employees"]}
    cat_by_name = {e["name"]: (e["category"], e["gl"]) for e in t["employees"]}

    async def fake_judge(categories, purpose, rows, rule, examples, usage=None, context=""):
        holder.setdefault("judge_contexts", []).append(context)
        cat, gl = cat_by_purpose.get(purpose, ("", ""))
        if not cat:
            # a no-report employee has no purpose; the sample's judge would
            # be unsure — mirror that so CATEGORY_UNCLEAR is exercised
            return category_mod.CategoryJudgment(category="", quoted_text="", sure=False,
                                                 why="no purpose stated"), ""
        return category_mod.CategoryJudgment(category=cat, quoted_text=purpose, sure=True,
                                             why="the stated purpose"), gl
    monkeypatch.setattr(category_mod, "judge_category", fake_judge)
    holder["categories"] = cat_by_name

    header = t["listing"]["header"]

    async def fake_prepare_listing(db, run):
        run.listing_headers = {"state": "ok", "tab": t["listing"]["current_tab"], "header_row": 1,
                               "header": list(header),
                               "roles": {"serial": 0, "processed_by": 1, "received_date": 2, "p2p_ref": 3,
                                         "po_number": 4, "cost_center": 5, "category": 6, "gl_account": 7,
                                         "vendor_name": 8, "invoice_number": 9, "amount": 10, "remarks": 11},
                               "why": "scripted", "past_examples": []}
        db.commit()
    monkeypatch.setattr(listing_mod, "prepare_listing", fake_prepare_listing)
    return holder
