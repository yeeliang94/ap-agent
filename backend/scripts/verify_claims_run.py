"""End-to-end verification of the claims module on the synthetic sample.

Runs the whole flow through the API against a live server (real AI), as
a competent reviewer would, and asserts what the plan promises:

  1. the map: 10 employees, report + KM tabs found, approvals and report
     prints ignored, the stray file unplaced, the no-report employee marked
  2. verification finishes; time and AI cost printed
  3. every planted error is flagged with the expected code; false (open)
     flags ≤ 1 per employee; every flag cites a place
  4. the gate: no output while any flag is open
  5. the reviewer: fixes the RM 10 row (its NO_RECEIPT resolves by
     correction), acknowledges the run-level and employee-level flags,
     accepts the real problems (their rows are left out) and dismisses
     anything else with a note
  6. the output: one row per included employee, header order = the sample
     listing's, totals reconcile, amounts = report total minus excluded rows

Usage: python scripts/verify_claims_run.py   (server on :8002; set
       AP_API=http://127.0.0.1:<port>/api for another port)
"""
from __future__ import annotations

import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

import httpx

BASE = os.environ.get("AP_API", "http://127.0.0.1:8002/api")
GEN = Path(__file__).resolve().parents[2] / "samples" / "generated" / "claims"
LISTING = "Summary of Invoices JUL26.xlsx"

problems: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("  ok   " if ok else "  FAIL ") + what)
    if not ok:
        problems.append(what)


def wait_for(run_id: str, statuses: set[str], timeout: int) -> dict:
    started = time.time()
    while time.time() - started < timeout:
        r = httpx.get(f"{BASE}/claims-runs/{run_id}", timeout=30).json()
        if r["status"] in statuses:
            return r
        time.sleep(5)
    raise SystemExit(f"timed out waiting for {statuses}")


def main() -> int:
    truth = json.loads((GEN / "ground_truth_claims.json").read_text())
    by_name = {e["name"]: e for e in truth["employees"]}

    # The sample's client values go in through Settings — never the code.
    prof = truth["profile"]
    r = httpx.put(f"{BASE}/claims-settings", json={"profile": {
        "mileage_rates": {k: str(v) for k, v in prof["mileage_rates"].items()},
        "receipt_optional_items": prof["receipt_optional_items"],
        "category_rule": ("The listing category follows the report's overall purpose: the Business "
                          "Reason header plus the line reasons (an offsite or team retreat is Company "
                          "Event). A report whose lines are all one item takes that item."),
        "file_role_patterns": [],
    }}, timeout=30)
    r.raise_for_status()
    print("profile set:", r.json()["profile"]["mileage_rates"], r.json()["profile"]["receipt_optional_items"])

    with open(GEN / "demo_claims_batch.zip", "rb") as z, open(GEN / LISTING, "rb") as l:
        r = httpx.post(f"{BASE}/claims-runs", data={"received_date": "2026-08-03"},
                       files={"batch": ("demo_claims_batch.zip", z, "application/zip"),
                              "listing": (LISTING, l, "application/octet-stream")}, timeout=120)
    r.raise_for_status()
    run_id = r.json()["run_id"]
    print("run", run_id)

    # ---- 1. the map --------------------------------------------------------
    t0 = time.time()
    run = wait_for(run_id, {"map_ready", "failed"}, 600)
    print(f"map ready in {time.time() - t0:.0f}s; rounds {run['map'].get('rounds')}; warnings {run['map_warnings']}")
    check(run["status"] == "map_ready", f"map ready (status {run['status']}: {run['error'][:120]})")
    m = run["map"]
    emps = {e["name"]: e for e in m["employees"] if e["is_employee"]}
    check(len(emps) == 10, f"10 employees mapped ({len(emps)})")
    for name, t in by_name.items():
        e = emps.get(name)
        if not e:
            check(False, f"{name}: mapped as an employee")
            continue
        roles = {f["path"].split("/", 1)[1]: f["role"] for f in e["files"]}
        if t["files"]["report"]:
            check(e["report_file"] == f"{t['folder']}/{t['files']['report']}" and e["report_tab"] == "Expense Report",
                  f"{name}: report file + tab")
            check(roles.get(t["files"]["report_print"]) == "ignore", f"{name}: report print ignored")
            check(e["er_code"] == t["er_code"], f"{name}: ER code {e['er_code']}")
        else:
            check(e["no_report"], f"{name}: marked no report (rows from receipts)")
            if not e["er_code"]:
                e["er_code"] = t["er_code"]  # the reviewer types it
        check(roles.get(t["files"]["approval"]) == "ignore", f"{name}: approval ignored")
        check(all(roles.get(rf) == "receipts" for rf in t["files"]["receipts"]), f"{name}: receipts files")
        if t["files"]["stray"]:
            check(roles.get(t["files"]["stray"]) == "unplaced", f"{name}: stray {t['files']['stray']} unplaced")
    r = httpx.post(f"{BASE}/claims-runs/{run_id}/confirm-map", json={"map": m, "remember": []}, timeout=60)
    check(r.status_code == 200, f"confirm map ({r.text[:120]})")

    # ---- 2. verification ------------------------------------------------------
    t0 = time.time()
    run = wait_for(run_id, {"ready", "failed"}, 1200)
    took = time.time() - t0
    check(run["status"] == "ready", f"run ready (status {run['status']}: {run['error'][:120]})")
    check(took < 300, f"verification under 5 minutes ({took:.0f}s)")
    reqs = sum(int(e["summary"].get("requests", 0)) for e in run["employees"])
    toks = sum(int(e["summary"].get("tokens", 0)) for e in run["employees"])
    print(f"AI cost: {reqs} requests, {toks} tokens; per employee: "
          + ", ".join(f"{e['name']} {e['summary'].get('seconds')}s/{e['summary'].get('requests')}req" for e in run["employees"]))
    for e in run["employees"]:
        check(e["status"] == "verified", f"{e['name']}: verified ({e['status']} {e['error'][:80]})")

    # ---- 3. flags vs ground truth ------------------------------------------------
    emp_by_id = {e["id"]: e for e in run["employees"]}
    flags = run["flags"]
    for f in flags:
        check(bool(f["cite"]) or f["code"] in ("NO_REPORT", "CATEGORY_UNCLEAR"), f"flag {f['code']} cites a place")
    total_false = 0
    for e in run["employees"]:
        t = by_name[e["name"]]
        mine = [f for f in flags if f["employee_id"] == e["id"]]
        open_codes = [f["code"] for f in mine if f["status"] == "open"]
        info_codes = [f["code"] for f in mine if f["status"] == "info"]
        expected = [p["code"] for p in t["expected_flags"]]
        for code in expected:
            check(code in open_codes + info_codes, f"{e['name']}: planted {code} found")
        extra = list(open_codes)
        for code in expected:
            if code in extra:
                extra.remove(code)
        extra = [c for c in extra if c != "CATEGORY_UNCLEAR"] + [c for c in extra if c == "CATEGORY_UNCLEAR"]
        total_false += len(extra)
        check(len(extra) <= 1, f"{e['name']}: false open flags ≤ 1 ({extra})")
        for m_ in t["must_not_flag"]:
            if "return trip" in m_["what"]:
                check(not any(f["code"] == "MILEAGE_DISCREPANCY" and "2026-07-06" in f["reason"] for f in mine),
                      f"{e['name']}: return trip not flagged")
            if "Mobile Allowance" in m_["what"]:
                check(not any(f["code"] == "NO_RECEIPT" and f["status"] == "open" and "Mobile Allowance" in f["reason"] for f in mine),
                      f"{e['name']}: Mobile Allowance N not flagged")
        if t["category"] and e["category"]:
            check(e["category"] == t["category"], f"{e['name']}: category {e['category']} (expected {t['category']})")
    print(f"false open flags in total: {total_false} across {len(run['employees'])} employees")

    # ---- 4. the gate ------------------------------------------------------------
    open_flags = [f for f in flags if f["status"] == "open"]
    check(open_flags and run["outputs"] == {}, "gate: no output while flags are open")

    # ---- 5. the reviewer ---------------------------------------------------------
    rows = run["rows"]
    aeg = next(e for e in run["employees"] if e["name"] == "Aegene Ong")
    rm10 = next((r for r in rows if r["employee_id"] == aeg["id"] and r["values"].get("amount") == "45.00"), None)
    if rm10:
        r = httpx.post(f"{BASE}/claims-runs/{run_id}/rows/{rm10['id']}/correct",
                       json={"fields": {"amount": "35.00"}, "reason": "receipt shows 35.00 — row overstated"}, timeout=120)
        check(r.status_code == 200, f"fix the RM 10 row ({r.text[:100]})")
        run = httpx.get(f"{BASE}/claims-runs/{run_id}", timeout=30).json()
        after = [f for f in run["flags"] if f["row_id"] == rm10["id"]]
        check(any(f["status"] == "resolved_by_correction" for f in after) and not any(f["status"] == "open" for f in after),
              "the RM 10 NO_RECEIPT resolved by correction")
    else:
        check(False, "found the RM 10 row (amount 45.00)")
    excluded: dict[str, Decimal] = {}
    for f in run["flags"]:
        if f["status"] != "open":
            continue
        emp = emp_by_id.get(f["employee_id"])
        t = by_name[emp["name"]] if emp else None
        planted = t and f["code"] in [p["code"] for p in t["expected_flags"]]
        if f["code"] == "CATEGORY_UNCLEAR" and emp:
            httpx.put(f"{BASE}/claims-runs/{run_id}/employees/{emp['id']}/category",
                      json={"category": t["category"], "gl": t["gl"], "reason": "verifier: ground truth"}, timeout=30)
        elif not f["row_id"] or not planted:
            decision = "accepted" if not f["row_id"] else "dismissed"
            httpx.post(f"{BASE}/claims-runs/{run_id}/flags/{f['id']}/decide",
                       json={"decision": decision, "note": "verifier: acknowledged / kept the row"}, timeout=30)
        else:
            httpx.post(f"{BASE}/claims-runs/{run_id}/flags/{f['id']}/decide",
                       json={"decision": "accepted", "note": "verifier: real problem, row left out"}, timeout=30)
            row = next((r for r in run["rows"] if r["id"] == f["row_id"]), None)
            if row and row["kind"] != "mileage":
                excluded.setdefault(row["id"], Decimal(str(row["values"].get("total") or row["values"].get("amount") or "0")))
    run = httpx.get(f"{BASE}/claims-runs/{run_id}", timeout=30).json()
    still_open = [f for f in run["flags"] if f["status"] == "open"]
    check(not still_open, f"all flags decided ({[f['code'] for f in still_open]})")

    # ---- 6. the output -----------------------------------------------------------
    out = run["outputs"]
    check(bool(out) and "rows" in out, "output present after review")
    if out and "rows" in out:
        check(out["header"] == truth["listing"]["header"], f"header order = the sample listing's ({out['header'][:4]}…)")
        check(not out["header_fallback"], "no header fallback")
        included = [e for e in run["employees"] if e["status"] == "verified"]
        check(len(out["rows"]) == len(included), f"one row per included employee ({len(out['rows'])}/{len(included)})")
        check(out["totals"]["match"], f"reconciliation green (emitted {out['totals']['total_myr']} vs source {out['totals']['source_total']})")
        rows_by_id: dict[str, dict] = {r["id"]: r for r in run["rows"]}
        excluded_by_emp: dict[str, Decimal] = {}
        for rid, amt in excluded.items():
            excluded_by_emp[rows_by_id[rid]["employee_id"]] = excluded_by_emp.get(rows_by_id[rid]["employee_id"], Decimal("0")) + amt
        for inc in out["included"]:
            emp = next(e for e in run["employees"] if e["name"] == inc["name"])
            want = Decimal(str(by_name[inc["name"]]["expected_listing"]["amount"])) - excluded_by_emp.get(emp["id"], Decimal("0"))
            check(Decimal(inc["amount"]) == want, f"{inc['name']}: amount {inc['amount']} (expected {want})")
            check(inc["category"] == by_name[inc["name"]]["category"], f"{inc['name']}: listing category {inc['category']}")
        amount_col = out["header"].index("Amount (MYR)")
        emitted = sum(Decimal(r[amount_col]) for r in out["rows"])
        check(str(emitted) == out["totals"]["total_myr"], "TSV amounts add up to the stated total (independent recount)")

    print()
    if problems:
        print(f"{len(problems)} CHECK(S) FAILED:")
        for p in problems:
            print(" -", p)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
