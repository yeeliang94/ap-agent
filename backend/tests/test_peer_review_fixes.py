"""Regression tests for the peer-review findings (Aug 2026).

Each test pins one confirmed defect: the reconciliation that compared a
number with itself; a retry that closed a run while another employee was
still verifying; a match on an undated receipt that no person saw; a
mileage row with no rate (or wrong arithmetic) that no check caught; the
run-level flag that grew a twin on every re-verify; the map that let one
employee be given another's file; the AI budget that was checked only
after a whole bundle; and the upload that was read whole before its size
was looked at.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.claims import checks, evidence, listing, mapping, source, worker
from app.claims.models import ClaimEmployee, ClaimFlag, ClaimRow, ClaimsRun
from app.db import Base

PROFILE = {"mileage_rates": {"Car": "0.64", "Motorcycle": "0.35"}, "km_tolerance": "0",
           "receipt_date_window_days": 0, "receipt_optional_items": [],
           "mileage_item_pattern": "mileage", "categories": [], "category_rule": "",
           "file_role_patterns": [], "checks": {}, "set_by": {}}


@pytest.fixture()
def Session(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 't.sqlite3'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(worker, "SessionLocal", S)
    return S


def _row(rid, date, amount, item="Taxi", **extra):
    return {"id": rid, "kind": "expense", "sheet": "S", "row": int(rid[1:]) + 6,
            "values": {"date": date, "item": item, "item_name": item, "reason": "x",
                       "receipt_included": "Y", "amount": amount, "currency": "MYR",
                       "rate": "1", "total": amount, **extra}}


def _receipt(eid, date, amount, conf=None, **extra):
    return {"id": eid, "kind": "receipt", "file": "r.pdf", "page": 1, "position": "left",
            "values": {"vendor": "V", "date": date, "amount": amount, "currency": "MYR", **extra},
            "confidence": conf or {}}


def _km(rid, date, km, rate, amount):
    return {"id": rid, "kind": "mileage", "sheet": "KM", "row": int(rid[1:]) + 4,
            "values": {"date": date, "from": "A", "to": "B", "km": km, "rate": rate, "amount": amount}}


def _trip(eid, date, km_printed, conf=None):
    return {"id": eid, "kind": "map_trip", "file": "r.pdf", "page": 3, "position": "",
            "values": {"date": date, "purpose": "meet", "from": "A", "to": "B",
                       "return_trip": False, "km_printed": km_printed},
            "confidence": conf or {}}


# ---- 1. reconciliation is against the report totals -----------------------

def test_reconciliation_disagrees_when_rows_and_report_total_differ(Session):
    s = Session()
    run = ClaimsRun(client="c", folder_url="", listing_url="", received_date="2026-08-19",
                    instructions="", snapshot={}, status="ready", listing_headers={"state": "missing"})
    s.add(run)
    s.commit()
    e = ClaimEmployee(run_id=run.id, folder="A_1", name="A", er_code="ER(1)", roles={},
                      status="verified", report_total="999.00", category="Travel", gl="6100")
    s.add(e)
    s.commit()
    s.add(ClaimRow(run_id=run.id, employee_id=e.id, kind="expense", sheet="S", row=7,
                   values={"amount": "10.00", "total": "10.00"}, verdict="matched"))
    s.commit()
    out = listing.build_outputs(s, run)
    assert out["totals"]["total_myr"] == "10.00"
    assert out["totals"]["source_total"] == "999.00"
    assert out["totals"]["match"] is False
    assert out["totals"]["differences"] and "999.00" in out["totals"]["differences"][0]["why"]
    # when they agree, they agree — and an excluded row is subtracted from the report side
    e.report_total = "10.00"
    s.commit()
    assert listing.build_outputs(s, run)["totals"]["match"] is True
    r2 = ClaimRow(run_id=run.id, employee_id=e.id, kind="expense", sheet="S", row=8,
                  values={"amount": "5.00", "total": "5.00"}, verdict="no_evidence")
    s.add(r2)
    s.commit()
    s.add(ClaimFlag(run_id=run.id, employee_id=e.id, code="NO_RECEIPT", reason="", basis="", cite={},
                    row_id=r2.id, status="accepted"))
    e.report_total = "15.00"
    s.commit()
    out = listing.build_outputs(s, run)
    assert out["totals"]["match"] is True and out["totals"]["total_myr"] == "10.00"
    s.close()


# ---- 2. the run closes only when every employee is done --------------------

def test_finish_run_waits_for_employees_still_verifying(Session):
    s = Session()
    run = ClaimsRun(client="c", folder_url="", listing_url="", received_date="2026-08-19",
                    instructions="", snapshot={}, status="verifying")
    s.add(run)
    s.commit()
    a = ClaimEmployee(run_id=run.id, folder="A", name="A", er_code="", roles={}, status="verified")
    b = ClaimEmployee(run_id=run.id, folder="B", name="B", er_code="", roles={}, status="verifying")
    s.add_all([a, b])
    s.commit()
    assert worker._finish_run(run.id, 0.0) is False
    assert s.get(ClaimsRun, run.id).status == "verifying"
    b.status = "failed"
    s.commit()
    assert worker._finish_run(run.id, 0.0) is True
    s.expire_all()
    assert s.get(ClaimsRun, run.id).status == "ready"
    s.close()


# ---- 9. run-level flags are raised once ---------------------------------------

def test_missing_reference_flags_are_idempotent(Session):
    s = Session()
    run = ClaimsRun(client="c", folder_url="", listing_url="", received_date="2026-08-19",
                    instructions="", snapshot={"profile": {**PROFILE, "mileage_rates": {}}},
                    status="verifying", listing_headers={"state": "missing"})
    s.add(run)
    s.commit()
    e = ClaimEmployee(run_id=run.id, folder="A", name="A", er_code="", roles={}, status="verified")
    s.add(e)
    s.commit()
    s.add(ClaimRow(run_id=run.id, employee_id=e.id, kind="mileage", sheet="KM", row=5, values={}))
    s.commit()
    for _ in range(3):
        assert worker._finish_run(run.id, 0.0) is True
    flags = s.query(ClaimFlag).filter(ClaimFlag.run_id == run.id, ClaimFlag.employee_id == "").all()
    whats = sorted((f.cite or {}).get("what") for f in flags)
    assert whats == ["listing", "rates"], whats
    s.close()


# ---- 3. an uncertain match goes to a person ------------------------------------

def _run(rows, ev, profile=PROFILE):
    return asyncio.run(checks.run_checks(rows, ev, profile, {}, (1, 1), tie_break=None))


def test_undated_receipt_match_is_flagged_not_silent():
    rows = [_row("r1", "2026-07-03", "45.00")]
    ev = [_receipt("e1", "", "45.00", conf={"date": "date not legible"})]
    res = _run(rows, ev)
    assert res["verdicts"]["r1"][0] == "matched"
    unc = [f for f in res["flags"] if f["code"] == "EVIDENCE_UNCERTAIN"]
    assert len(unc) == 1 and unc[0]["status"] == "open" and unc[0]["row_id"] == "r1"
    assert "no readable date" in unc[0]["reason"]
    # a clean same-day receipt raises nothing
    res = _run(rows, [_receipt("e1", "2026-07-03", "45.00")])
    assert not [f for f in res["flags"] if f["code"] == "EVIDENCE_UNCERTAIN"]
    # a day/month-swapped date is matched but confirmed by a person
    res = _run(rows, [_receipt("e1", "2026-03-07", "45.00")])
    assert res["verdicts"]["r1"][0] == "matched"
    assert any(f["code"] == "EVIDENCE_UNCERTAIN" and "swapping" in f["reason"] for f in res["flags"])
    # the check can be switched off per client
    res = _run(rows, ev, {**PROFILE, "checks": {"EVIDENCE_UNCERTAIN": False}})
    assert not [f for f in res["flags"] if f["code"] == "EVIDENCE_UNCERTAIN"]


def test_uncertain_km_match_is_flagged():
    rows = [_km("k1", "2026-07-03", "12.0", "0.64", "7.68")]
    res = _run(rows, [_trip("t1", "2026-07-03", "12.0", conf={"km_printed": "read at normal resolution only"})])
    assert res["verdicts"]["k1"][0] == "matched"
    assert any(f["code"] == "EVIDENCE_UNCERTAIN" and f["row_id"] == "k1" for f in res["flags"])
    res = _run(rows, [_trip("t1", "2026-07-03", "12.0")])
    assert not [f for f in res["flags"] if f["code"] == "EVIDENCE_UNCERTAIN"]


# ---- 4. mileage: no rate typed, and per-row arithmetic ---------------------------

def test_mileage_rate_missing_is_judged_by_amount_over_km():
    trip = [_trip("t1", "2026-07-03", "10.0")]
    # implied 0.64 — one of the client's rates: nothing to raise
    res = _run([_km("k1", "2026-07-03", "10.0", None, "6.40")], trip)
    assert not [f for f in res["flags"] if f["code"] == "MILEAGE_RATE"]
    # implied 0.90 — not a client rate: raised, not silently skipped
    res = _run([_km("k1", "2026-07-03", "10.0", None, "9.00")], trip)
    rate = [f for f in res["flags"] if f["code"] == "MILEAGE_RATE"]
    assert len(rate) == 1 and "no rate typed" in rate[0]["reason"] and "0.9000" in rate[0]["reason"]


def test_mileage_arithmetic_is_checked_per_row():
    trip = [_trip("t1", "2026-07-03", "10.0")]
    res = _run([_km("k1", "2026-07-03", "10.0", "0.64", "7.00")], trip)
    ar = [f for f in res["flags"] if f["code"] == "MILEAGE_ARITHMETIC"]
    assert len(ar) == 1 and "6.40" in ar[0]["reason"] and ar[0]["status"] == "open"
    res = _run([_km("k1", "2026-07-03", "10.0", "0.64", "6.40")], trip)
    assert not [f for f in res["flags"] if f["code"] == "MILEAGE_ARITHMETIC"]


# ---- 5. a failed full-resolution re-read is marked, not silently reused ---------

def test_failed_map_reread_marks_km_low_confidence(monkeypatch):
    class R:
        def __init__(self, out):
            self.output = out

        def usage(self):
            class U:
                total_tokens = 1
                requests = 1
            return U()

    trip = evidence.TripRead(date="2026-07-03", purpose="p", from_="A", to="B", return_trip=False,
                             km_printed=12.0)
    page = evidence.PageRead(kind="map", trips=[trip], why="map")

    class Agent:
        async def run(self, prompt, **kw):
            return R(page)

    async def boom(*a, **k):
        raise RuntimeError("renderer down")

    monkeypatch.setattr(evidence, "_read_map_bands", boom)
    usage = evidence.Usage()
    kind, _why, _rec, trips, notes = asyncio.run(
        evidence._read_page(Agent(), evidence.Path("m.pdf"), 1, b"png", usage))
    assert kind == "map" and trips[0]["km_printed"] == "12.0"
    assert "normal resolution only" in trips[0]["confidence"]["km_printed"]
    assert any("full-resolution re-read failed" in n for n in notes)


# ---- 6. the AI budget is reserved before every request ---------------------------

def test_budget_is_checked_before_each_page_read(monkeypatch):
    calls = {"n": 0}

    class R:
        output = evidence.PageRead(kind="other", why="blank")

        def usage(self):
            class U:
                total_tokens = 1
                requests = 1
            return U()

    class Agent:
        async def run(self, prompt, **kw):
            calls["n"] += 1
            return R()

    monkeypatch.setattr(evidence, "create_agent", lambda *a, **k: Agent())
    monkeypatch.setattr(evidence, "page_pngs", lambda path: [b"p"] * 50)
    usage = evidence.Usage(cap=10)
    with pytest.raises(evidence.BudgetExceeded):
        asyncio.run(evidence.read_bundle(evidence.Path("big.pdf"), "big.pdf", usage))
    assert calls["n"] == 10, calls  # not 50


def test_usage_counts_provider_requests_not_agent_runs():
    class R:
        def usage(self):
            class U:
                total_tokens = 5
                requests = 3
            return U()

    u = evidence.Usage()
    u.add(R())
    assert u.requests == 3 and u.tokens == 5


# ---- 7. the confirmed map keeps files with their own employee -------------------

def test_confirmed_map_rejects_cross_folder_and_missing_files():
    survey = {"files": [{"path": "A_1/r.xlsx", "type": "workbook", "peek": {"tabs": {"Expense Report": []}}},
                        {"path": "A_1/rec.pdf", "type": "pdf", "peek": {}},
                        {"path": "B_2/r.xlsx", "type": "workbook", "peek": {"tabs": {"Expense Report": []}}},
                        {"path": "B_2/rec.pdf", "type": "pdf", "peek": {}}],
              "folders": [{"path": "A_1", "files": ["A_1/r.xlsx", "A_1/rec.pdf"]},
                          {"path": "B_2", "files": ["B_2/r.xlsx", "B_2/rec.pdf"]}]}
    good = {"employees": [
        {"folder": "A_1", "is_employee": True, "name": "A", "er_code": "ER(1)",
         "report_file": "A_1/r.xlsx", "report_tab": "Expense Report",
         "files": [{"path": "A_1/r.xlsx", "role": "report"}, {"path": "A_1/rec.pdf", "role": "receipts"}]},
        {"folder": "B_2", "is_employee": True, "name": "B", "er_code": "ER(2)",
         "report_file": "B_2/r.xlsx", "report_tab": "Expense Report",
         "files": [{"path": "B_2/r.xlsx", "role": "report"}, {"path": "B_2/rec.pdf", "role": "receipts"}]}],
        "root_files": []}
    assert mapping.validate_confirmed_map(good, survey) == []
    # A given B's report, and B's receipts
    bad = {"employees": [
        {**good["employees"][0], "report_file": "B_2/r.xlsx",
         "files": [{"path": "A_1/r.xlsx", "role": "ignore"}, {"path": "A_1/rec.pdf", "role": "receipts"},
                   {"path": "B_2/rec.pdf", "role": "receipts"}]},
        good["employees"][1]], "root_files": []}
    problems = mapping.validate_confirmed_map(bad, survey)
    assert any("B_2/r.xlsx is not in the folder 'A_1'" in p for p in problems), problems
    assert any("B_2/rec.pdf is not in the folder 'A_1'" in p for p in problems), problems
    assert any("B_2/rec.pdf: listed under" in p for p in problems), problems
    # a file dropped from the map is named, not lost
    dropped = {"employees": [good["employees"][0],
                             {**good["employees"][1], "files": [{"path": "B_2/r.xlsx", "role": "report"}]}],
               "root_files": []}
    problems = mapping.validate_confirmed_map(dropped, survey)
    assert any("B_2/rec.pdf: this file is in the batch but nowhere on the map" in p for p in problems), problems


# ---- 8. batch-wide quotas ----------------------------------------------------------

def test_batch_wide_file_count_and_size_caps():
    many = [{"kind": "file", "path": f"f{i}.pdf", "size": 10, "depth": 1}
            for i in range(source.MAX_TOTAL_FILES + 1)]
    with pytest.raises(source.QuotaExceeded, match="files a run may have"):
        source._check_listing_quotas(many)
    big = [{"kind": "file", "path": f"F{i}/x.pdf", "size": 20 * 1024 * 1024, "depth": 2}
           for i in range((source.MAX_TOTAL_MB // 20) + 1)]
    with pytest.raises(source.QuotaExceeded, match="MB limit for a run"):
        source._check_listing_quotas(big)
