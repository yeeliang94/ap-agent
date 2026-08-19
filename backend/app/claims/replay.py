"""Replay bundles (hardening H11): everything needed to show how a run's
money was arrived at, and a verifier that re-derives it.

    bundle = build_bundle(db, run)      # JSON-able
    report = verify_bundle(db, run)     # {"reproduces": bool, "problems": [...], ...}

The bundle holds: the manifest (every file with its hash), the versions
(adapter, prompt, tools, models), the Investigation Plan, the tool
executions (input/output hashes, and for `calculate` the expression and
value), every Flag with its decision, every correction, every reviewer
action from the audit trail, the cases with their totals, and the final
output with its totals. The verifier re-evaluates every recorded
calculation with the same Decimal calculator, re-sums the emitted TSV
independently, rebuilds the outputs from the stored state and compares
them to what was published, and checks that every cited file is in the
manifest with a hash. No model is called; nothing is written.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from ..models import AuditEvent
from . import cases as cases_mod
from . import listing as listing_mod
from .models import (ClaimCase, ClaimFlag, ClaimInvestigation, ClaimRow, ClaimSourceArtifact, ClaimToolExecution,
                     ClaimsRun)
from .tools import calculator

BUNDLE_VERSION = "h11.1"


def build_bundle(db, run: ClaimsRun) -> dict:
    inv = db.query(ClaimInvestigation).filter(ClaimInvestigation.run_id == run.id, ClaimInvestigation.status != "shadow") \
        .order_by(ClaimInvestigation.created_at.desc(), ClaimInvestigation.id.desc()).first()
    tools = db.query(ClaimToolExecution).filter(ClaimToolExecution.run_id == run.id).order_by(ClaimToolExecution.id).all()
    cases = db.query(ClaimCase).filter(ClaimCase.run_id == run.id).order_by(ClaimCase.label).all()
    flags = db.query(ClaimFlag).filter(ClaimFlag.run_id == run.id).all()
    rows = db.query(ClaimRow).filter(ClaimRow.run_id == run.id).all()
    audit = db.query(AuditEvent).filter(AuditEvent.run_id == run.id).order_by(AuditEvent.id).all()
    return {
        "bundle_version": BUNDLE_VERSION,
        "run": {"id": run.id, "client": run.client, "status": run.status, "received_date": run.received_date,
                "revision": run.revision, "created_at": run.created_at.isoformat() if run.created_at else "",
                "instructions": run.instructions},
        "manifest": list(run.manifest or []),
        "profile_snapshot": (run.snapshot or {}).get("profile") or {},
        "versions": ((inv.plan or {}).get("versions") if inv else {}) or {},
        "investigation": cases_mod.investigation_dict(inv),
        "tool_executions": [{"id": t.id, "tool": t.tool, "elapsed_ms": t.elapsed_ms, "input_hashes": t.input_hashes,
                             "output_hash": t.output_hash, "truncated": bool(t.truncated), "error_code": t.error_code,
                             "note": t.note} for t in tools],
        "calculations": [t.note for t in tools if t.tool == "calculate" and not t.error_code and " = " in (t.note or "")],
        "cases": [{"id": c.id, "label": c.label, "claimant": {"name": c.claimant_name, "identifier": c.claimant_identifier,
                                                               "state": c.claimant_state, "basis": c.claimant_basis},
                   "state": c.state, "status": c.status, "category": c.category, "gl": c.gl,
                   "reported_total": c.reported_total, "lines_total": c.lines_total, "roles": c.roles}
                  for c in cases],
        "lines": [{"id": r.id, "case_id": r.case_id, "kind": r.kind, "sheet": r.sheet, "row": r.row,
                   "values": r.values, "corrections": r.corrections, "verdict": r.verdict,
                   "matched_evidence_id": r.matched_evidence_id} for r in rows],
        "flags": [{"id": f.id, "code": f.code, "case_id": f.case_id, "row_id": f.row_id, "evidence_id": f.evidence_id,
                   "artifact_id": f.artifact_id, "status": f.status, "resolution": f.resolution, "cite": f.cite,
                   "basis": f.basis} for f in flags],
        "reviewer_decisions": [{"at": a.at.isoformat(), "actor": a.actor, "action": a.action, "detail": a.detail}
                               for a in audit],
        "listing_headers": {k: v for k, v in (run.listing_headers or {}).items() if k != "past_examples"},
        "output": run.outputs or {},
    }


def verify_bundle(db, run: ClaimsRun) -> dict:
    """Re-derive what the bundle claims and name every mismatch."""
    problems: list[str] = []
    bundle = build_bundle(db, run)
    by_path = {m.get("path"): m for m in bundle["manifest"]}
    by_id = {m.get("id"): m for m in bundle["manifest"]}
    # 1. every recorded calculation re-evaluates to the recorded value
    n_calc = 0
    for note in bundle["calculations"]:
        expr, _, value = note.rpartition(" = ")
        try:
            got = calculator.calculate(expr)
            n_calc += 1
            if str(got) != value.strip():
                problems.append(f"calculation {expr!r}: recorded {value!r}, re-evaluated {got}")
        except calculator.CalculationError as exc:
            problems.append(f"calculation {expr!r} no longer evaluates: {exc}")
    # 2. every cited file resolves to a manifest entry with a hash
    for f in bundle["flags"]:
        cite = f.get("cite") or {}
        path = cite.get("file")
        if path and path not in by_path:
            problems.append(f"flag {f['code']} cites {path!r}, which is not in the manifest")
        elif path and not by_path[path].get("sha256"):
            problems.append(f"flag {f['code']} cites {path!r}, which has no hash")
        aid = f.get("artifact_id")
        if aid and aid not in by_id:
            problems.append(f"flag {f['code']} names artifact {aid}, which is not in the manifest")
    for a in db.query(ClaimSourceArtifact).filter(ClaimSourceArtifact.run_id == run.id):
        m = by_id.get(a.artifact_id)
        if m is None or m.get("sha256") != a.sha256:
            problems.append(f"artifact {a.path}: stored hash differs from the manifest")
    # 3. the published output re-derives from the stored state, and its
    #    TSV re-sums to its total
    published = bundle["output"]
    rebuilt = listing_mod.build_outputs(db, run) if run.status == "ready" else {}
    if published and rebuilt:
        for key in ("rows", "header"):
            if published.get(key) != rebuilt.get(key):
                problems.append(f"published output {key} differs from a rebuild of the stored state")
        if (published.get("totals") or {}).get("total_myr") != (rebuilt.get("totals") or {}).get("total_myr"):
            problems.append("published emitted total differs from a rebuild")
        amount_idx = (bundle["listing_headers"].get("roles") or {}).get("amount")
        if amount_idx is None and published.get("header_fallback"):
            amount_idx = listing_mod.FALLBACK_ROLES.index("amount")
        if amount_idx is not None:
            total = Decimal("0")
            for line in published["tsv"].split("\n")[1:]:
                cells = line.split("\t")
                try:
                    total += Decimal(cells[amount_idx].lstrip("'") or "0") if amount_idx < len(cells) else Decimal("0")
                except InvalidOperation:
                    problems.append(f"emitted amount {cells[amount_idx]!r} is not a number")
            if f"{total:.2f}" != published["totals"]["total_myr"]:
                problems.append(f"the TSV re-sums to {total:.2f}, the published total says {published['totals']['total_myr']}")
        # the Calculated Lines Total per case re-sums from the lines
        for inc in published.get("included", []):
            cid = inc.get("case_id")
            if not cid:
                continue
            excluded = {f["row_id"] for f in bundle["flags"] if f["status"] == "accepted" and f["row_id"]}
            lines = [l for l in bundle["lines"] if l["case_id"] == cid and l["kind"] != "mileage" and l["id"] not in excluded]
            if lines:
                s = sum((Decimal(str((l["values"] or {}).get("total") or (l["values"] or {}).get("amount") or "0")) for l in lines), Decimal("0"))
                if f"{s:.2f}" != inc["amount"]:
                    problems.append(f"case {inc['name']}: lines re-sum to {s:.2f}, published {inc['amount']}")
    elif published and not rebuilt:
        problems.append("an output is published but the run is not ready")
    return {"reproduces": not problems, "problems": problems,
            "checked": {"calculations": n_calc, "flags": len(bundle["flags"]), "artifacts": len(bundle["manifest"]),
                        "output_rows": len((published or {}).get("rows", []))},
            "gates": acceptance_gates(db, run),
            "versions": bundle["versions"], "bundle_version": BUNDLE_VERSION}


def acceptance_gates(db, run: ClaimsRun) -> dict:
    """The live-model acceptance gates (H12), measured on one run: artifacts
    dispositioned, payable claimants confirmed, material values cited,
    arithmetic reconciled, evidence reuse, automatic owner confirmation."""
    from .models import ClaimEvidence

    arts = db.query(ClaimSourceArtifact).filter(ClaimSourceArtifact.run_id == run.id).all()
    cases = db.query(ClaimCase).filter(ClaimCase.run_id == run.id).all()
    flags = db.query(ClaimFlag).filter(ClaimFlag.run_id == run.id).all()
    rows = db.query(ClaimRow).filter(ClaimRow.run_id == run.id).all()
    evidence = db.query(ClaimEvidence).filter(ClaimEvidence.run_id == run.id).all()
    payable = [c for c in cases if c.state == "confirmed" and c.status == "verified"]
    open_flags = [f for f in flags if f.status == "open"]
    uncited = [f for f in flags if not f.cite and f.code not in ("NO_REPORT", "NO_SUMMARY", "CLAIM_AMOUNT_UNCONFIRMED",
                                                                  "PURPOSE_UNKNOWN", "CATEGORY_UNCLEAR", "REPORT_UNREADABLE")]
    rows_uncited = [r for r in rows if not r.sheet and r.kind != "derived"]
    ev_uncited = [e for e in evidence if not e.file]
    # Evidence reuse across cases is SILENT when two matched receipts with
    # the same value fingerprint sit in different cases and no SHARED_RECEIPT
    # flag (any status) names them.
    from . import checks as checks_mod

    groups: dict[tuple, set[str]] = {}
    for e in evidence:
        if e.kind == "receipt" and e.matched_row_id:
            groups.setdefault(checks_mod._receipt_key({"values": e.values or {}, "id": e.id}), set()).add(e.case_id or e.employee_id)
    shared_named = {f.evidence_id for f in flags if f.code == "SHARED_RECEIPT"}
    silent = [k for k, cases_ in groups.items() if len(cases_) > 1 and not any(
        e.id in shared_named for e in evidence if e.kind == "receipt" and e.matched_row_id
        and checks_mod._receipt_key({"values": e.values or {}, "id": e.id}) == k)]
    reuse = [f for f in flags if f.code in ("SHARED_RECEIPT", "DUPLICATE_RECEIPT") and f.status == "open"]
    ai_confirmed = [c for c in cases if c.claimant_state == "confirmed" and "reviewer" not in (c.claimant_basis or "")
                    and "confirmed map" not in (c.claimant_basis or "")]
    out = run.outputs or {}
    return {
        "artifacts_dispositioned_or_blocking": {
            "ok": all(a.disposition != "unresolved" or any(f.code == "ARTIFACT_UNRESOLVED" and f.artifact_id == a.artifact_id and f.status == "open" for f in flags) for a in arts),
            "total": len(arts), "unresolved": sum(1 for a in arts if a.disposition == "unresolved")},
        "payable_claimants_confirmed": {"ok": all(c.claimant_state == "confirmed" for c in payable),
                                        "payable": len(payable), "confirmed": sum(1 for c in payable if c.claimant_state == "confirmed")},
        "material_values_cited": {"ok": not uncited and not rows_uncited and not ev_uncited,
                                  "flags_uncited": len(uncited), "rows_uncited": len(rows_uncited), "evidence_uncited": len(ev_uncited)},
        "arithmetic_reconciles": {"ok": bool(out) and bool((out.get("totals") or {}).get("match")),
                                  "reported_missing_named": (out.get("totals") or {}).get("reported_missing")},
        "no_silent_evidence_reuse": {"ok": not silent, "silent_reuse_groups": len(silent), "open_reuse_flags": len(reuse)},
        "no_automatic_owner_confirmation": {"ok": not ai_confirmed, "cases": [c.label for c in ai_confirmed]},
        "open_flags": len(open_flags), "false_flag_budget": f"≤ 1 open false flag per confirmed case ({len(payable)} cases)",
    }
