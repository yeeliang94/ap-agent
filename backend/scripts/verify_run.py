"""Phase 2 verification: run the demo batch end-to-end and score it.

Uploads demo_batch.zip to the running server, waits for the pipeline to
finish, then compares results against ground_truth.json:
  1. every document classified correctly
  2. key fields extracted correctly
  3. every planted anomaly flagged, with no false passes
  4. output blocks reconcile

Usage: python scripts/verify_run.py   (server must be running on :8002)
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8002/api"
GEN = Path(__file__).resolve().parents[2] / "samples" / "generated"


def main() -> int:
    truth = json.loads((GEN / "ground_truth.json").read_text())

    with open(GEN / "demo_batch.zip", "rb") as f:
        r = httpx.post(f"{BASE}/runs", data={"client": "Client ABC"},
                       files={"batch": ("demo_batch.zip", f, "application/zip")},
                       timeout=60)
    r.raise_for_status()
    run_id = r.json()["run_id"]
    print(f"run {run_id}: {r.json()['documents']} documents uploaded")

    while True:
        run = httpx.get(f"{BASE}/runs/{run_id}", timeout=30).json()
        status, progress = run["status"], run.get("progress") or {}
        print(f"  {status} {progress.get('done', '')}/{progress.get('total', '')}", flush=True)
        if status in ("ready", "failed"):
            break
        time.sleep(3)

    if status == "failed":
        print(f"RUN FAILED: {run['error']}")
        return 1

    ok = True

    # ---- 1. classification --------------------------------------------
    kinds = {d["filename"]: d["kind"] for d in run["documents"]}
    wrong = {n: (kinds.get(n), spec["kind"])
             for n, spec in truth["documents"].items()
             if kinds.get(n) != spec["kind"]}
    print(f"\nclassification: {len(truth['documents']) - len(wrong)}/{len(truth['documents'])} correct")
    if wrong:
        ok = False
        for n, (got, want) in wrong.items():
            print(f"  WRONG {n}: got {got}, wanted {want}")

    # ---- 2. extraction --------------------------------------------------
    n_checked = n_correct = 0
    for d in run["documents"]:
        spec = truth["documents"].get(d["filename"])
        if not spec or spec["kind"] == "receipt":
            continue
        for key in ("invoice_number", "amount", "claimant"):
            if key in spec["fields"]:
                n_checked += 1
                got, want = d["fields"].get(key), spec["fields"][key]
                if isinstance(want, float):
                    good = got is not None and abs(float(got) - want) < 0.01
                else:
                    good = str(got).strip() == str(want).strip()
                if good:
                    n_correct += 1
                else:
                    print(f"  extraction miss {d['filename']}.{key}: got {got!r}, wanted {want!r}")
    print(f"extraction: {n_correct}/{n_checked} key fields correct")
    ok = ok and (n_correct == n_checked)

    # ---- 3. flags --------------------------------------------------------
    print("\nplanted anomalies:")
    for expected in truth["expected_flags"]:
        hit = any(
            f["code"] == expected["code"] and expected["match"] in (f["reason"] + f["basis"])
            for f in run["flags"]
        )
        print(f"  {'FOUND' if hit else 'MISSED'}  {expected['code']} ({expected['match']})")
        ok = ok and hit
    extra = [f for f in run["flags"]
             if not any(e["code"] == f["code"] and e["match"] in (f["reason"] + f["basis"])
                        for e in truth["expected_flags"])]
    print(f"other flags raised: {len(extra)}")
    for f in extra:
        print(f"  [{f['code']}] {f['reason'][:100]}")

    # ---- 4. outputs ------------------------------------------------------
    out = run["outputs"]
    print(f"\noutputs: {len(out['listing_rows'])} listing rows, "
          f"{len(out['bank_rows'])} bank rows, totals match={out['totals']['match']}, "
          f"new vendors={out['new_vendors']}")
    ok = ok and out["totals"]["match"]

    print(f"\n{'ALL CHECKS PASSED' if ok else 'SOME CHECKS FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
