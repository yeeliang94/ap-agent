"""H2 — the additive case-model migration and the dual-write.

  - a pre-migration database (the delivered five tables, no case columns)
    opens, migrates ONCE (version 1 recorded; columns and tables added; one
    Claim Case per employee; case ids backfilled), opens again unchanged
  - a fresh database gets the shape and the version in one go
  - through the API, a Client A run writes cases beside employees: after
    confirm-map the cases and claimants are confirmed and tied to their
    employee; every row, evidence item and flag carries the case id; the
    case mirrors the employee's status, totals and category; the run
    detail carries cases / artifacts / assignments / investigation, and
    hides them when CLAIMS_CASE_MODEL is off while storage is unchanged
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import config
from app.claims import migrations
from app.claims.models import (ClaimCase, ClaimEmployee, ClaimEvidence, ClaimEvidenceAssignment, ClaimFlag,
                               ClaimInvestigation, ClaimRow, ClaimSourceArtifact, ClaimsRun, ClaimsSchema)

from . import claims_scripted as scripted
from .test_claims_baseline import client, db, rev, run_client_a  # noqa: F401

needs_sample = pytest.mark.skipif(not scripted.GEN.is_dir(), reason="run samples/generate_claims_sample.py first")

# The delivered shape (330b972), verbatim column lists.
OLD_DDL = [
    """CREATE TABLE claims_runs (id VARCHAR NOT NULL PRIMARY KEY, client VARCHAR NOT NULL, status VARCHAR NOT NULL,
       error TEXT NOT NULL, folder_url TEXT NOT NULL, listing_url TEXT NOT NULL, received_date VARCHAR NOT NULL,
       instructions TEXT NOT NULL, progress JSON NOT NULL, survey JSON NOT NULL, map JSON NOT NULL,
       map_warnings JSON NOT NULL, listing_headers JSON NOT NULL, outputs JSON NOT NULL, snapshot JSON NOT NULL,
       created_at DATETIME NOT NULL)""",
    """CREATE TABLE claim_employees (id VARCHAR NOT NULL PRIMARY KEY, run_id VARCHAR NOT NULL, folder VARCHAR NOT NULL,
       name VARCHAR NOT NULL, er_code VARCHAR NOT NULL, roles JSON NOT NULL, status VARCHAR NOT NULL, error TEXT NOT NULL,
       report_total VARCHAR NOT NULL, category VARCHAR NOT NULL, gl VARCHAR NOT NULL, category_basis TEXT NOT NULL,
       summary JSON NOT NULL)""",
    """CREATE TABLE claim_rows (id VARCHAR NOT NULL PRIMARY KEY, run_id VARCHAR NOT NULL, employee_id VARCHAR NOT NULL,
       kind VARCHAR NOT NULL, sheet VARCHAR NOT NULL, row INTEGER NOT NULL, "values" JSON NOT NULL,
       corrections JSON NOT NULL, matched_evidence_id VARCHAR NOT NULL, verdict VARCHAR NOT NULL)""",
    """CREATE TABLE claim_evidence (id VARCHAR NOT NULL PRIMARY KEY, run_id VARCHAR NOT NULL, employee_id VARCHAR NOT NULL,
       kind VARCHAR NOT NULL, file VARCHAR NOT NULL, page INTEGER NOT NULL, position VARCHAR NOT NULL,
       "values" JSON NOT NULL, confidence JSON NOT NULL, matched_row_id VARCHAR NOT NULL)""",
    """CREATE TABLE claim_flags (id VARCHAR NOT NULL PRIMARY KEY, run_id VARCHAR NOT NULL, employee_id VARCHAR NOT NULL,
       row_id VARCHAR NOT NULL, evidence_id VARCHAR NOT NULL, code VARCHAR NOT NULL, reason TEXT NOT NULL,
       basis TEXT NOT NULL, cite JSON NOT NULL, status VARCHAR NOT NULL, resolution TEXT NOT NULL)""",
]


def _old_database(path):
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        for ddl in OLD_DDL:
            conn.execute(text(ddl))
        conn.exec_driver_sql("""INSERT INTO claims_runs VALUES ('run1','Client A','ready','','','','2026-08-03','',
            '{}','{"folders":[]}','{"employees":[{"folder":"A_1","is_employee":true,"name":"A","er_code":"ER(1)","files":[]}],"confirmed":true}',
            '[]','{}','{}','{}','2026-08-19 00:00:00')""")
        conn.exec_driver_sql("""INSERT INTO claim_employees VALUES ('emp1','run1','A_1','A','ER(1)','{"report_file":"A_1/r.xlsx"}',
            'verified','','258.70','Taxi','713070','','{"rows":3,"rows_total":"258.70"}')""")
        conn.exec_driver_sql("""INSERT INTO claim_rows VALUES ('row1','run1','emp1','expense','S',7,'{"amount":"45.00"}','{}','','matched')""")
        conn.exec_driver_sql("""INSERT INTO claim_evidence VALUES ('ev1','run1','emp1','receipt','A_1/r.pdf',1,'left','{"vendor":"Grab"}','{}','row1')""")
        conn.exec_driver_sql("""INSERT INTO claim_flags VALUES ('fl1','run1','emp1','row1','','NO_RECEIPT','x','y','{}','open','')""")
        conn.exec_driver_sql("""INSERT INTO claim_flags VALUES ('fl2','run1','','','','MISSING_REFERENCE','x','y','{"what":"listing"}','open','')""")
    return engine


def _shape(engine) -> dict:
    S = sessionmaker(bind=engine)
    s = S()
    try:
        return {"cases": sorted((c.id, c.legacy_employee_id, c.label, c.status, c.reported_total, c.lines_total)
                                for c in s.query(ClaimCase)),
                "rows": sorted((r.id, r.case_id) for r in s.query(ClaimRow)),
                "evidence": sorted((e.id, e.case_id) for e in s.query(ClaimEvidence)),
                "flags": sorted((f.id, f.case_id) for f in s.query(ClaimFlag)),
                "versions": sorted(v.version for v in s.query(ClaimsSchema)),
                "run": [(r.id, r.revision, list(r.manifest or [])) for r in s.query(ClaimsRun)]}
    finally:
        s.close()


def test_pre_migration_database_migrates_once_and_reopens_unchanged(tmp_path):
    engine = _old_database(tmp_path / "old.sqlite3")
    assert migrations.current_version(engine) == 0
    assert migrations.run_migrations(engine) == [1, 2]
    first = _shape(engine)
    assert first["versions"] == [1, 2]
    # One case per employee, tied to it, mirroring its worker fields.
    assert len(first["cases"]) == 1
    cid, emp_id, label, status, reported, lines = first["cases"][0]
    assert emp_id == "emp1" and label == "A_1" and status == "verified"
    assert reported == "258.70" and lines == "258.70"
    # Case ids backfilled on rows / evidence / employee flags; the
    # run-level flag stays run-level.
    assert first["rows"] == [("row1", cid)] and first["evidence"] == [("ev1", cid)]
    assert dict(first["flags"]) == {"fl1": cid, "fl2": ""}
    assert first["run"] == [("run1", 0, [])]
    # Again: nothing to do, nothing changed.
    assert migrations.run_migrations(engine) == []
    assert _shape(engine) == first
    assert migrations.current_version(engine) == 2
    # A second engine on the same file (a restart) sees the same.
    engine2 = create_engine(f"sqlite:///{tmp_path / 'old.sqlite3'}", connect_args={"check_same_thread": False})
    assert migrations.run_migrations(engine2) == [] and _shape(engine2) == first


def test_fresh_database_gets_the_shape_and_the_version(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'new.sqlite3'}", connect_args={"check_same_thread": False})
    assert migrations.run_migrations(engine) == [1, 2]
    with engine.connect() as conn:
        cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(claim_rows)")]
        assert "case_id" in cols
        worker_cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(claim_employees)")]
        assert "progress" in worker_cols
        tables = {r[0] for r in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"claim_cases", "claim_source_artifacts", "claim_evidence_assignments", "claim_investigations",
            "claim_tool_executions", "claims_schema"} <= tables
    assert migrations.run_migrations(engine) == []


@needs_sample
@pytest.mark.asyncio
async def test_a_run_writes_cases_beside_employees(db, monkeypatch):
    run_id = await run_client_a(db, monkeypatch)
    got = client.get(f"/api/claims-runs/{run_id}").json()
    assert got["status"] == "ready"
    assert got["revision"] >= 1
    # Cases mirror the employees 1:1 and are tied to them.
    assert len(got["cases"]) == len(got["employees"]) == 10
    by_emp = {c["employee_id"]: c for c in got["cases"]}
    for e in got["employees"]:
        c = by_emp[e["id"]]
        assert c["state"] == "confirmed" and c["claimant"]["state"] == "confirmed"
        assert c["claimant"]["name"] == e["name"] and c["claimant"]["identifier"] == e["er_code"]
        assert c["status"] == e["status"] == "verified"
        assert c["reported_total"] == e["report_total"] and c["category"] == e["category"]
        assert c["lines_total"] == str(e["summary"].get("rows_total"))
        assert c["roles"] == e["roles"]
    # Every row / evidence item / employee flag carries its case id.
    for r in got["rows"]:
        assert r["case_id"] == by_emp[r["employee_id"]]["id"]
    for ev in got["evidence"]:
        assert ev["case_id"] == by_emp[ev["employee_id"]]["id"]
    for f in got["flags"]:
        assert f["case_id"] == (by_emp[f["employee_id"]]["id"] if f["employee_id"] else "")
    # Artifacts: every file of the batch, dispositioned — the stray file by the reviewer at the map.
    assert got["artifact_counts"]["total"] == 44 and got["artifact_counts"]["unresolved"] == 0
    stray = next(a for a in got["artifacts"] if a["path"].endswith("notes.txt"))
    assert stray["disposition"] == "irrelevant" and stray["disposition_by"] == "reviewer"
    used = [a for a in got["artifacts"] if a["disposition"] == "used"]
    assert all(a["case_id"] for a in used)
    # Assignments confirmed by the map confirmation; investigation record confirmed.
    assert got["assignments"] and all(a["state"] == "confirmed" for a in got["assignments"])
    assert got["investigation"]["status"] == "confirmed" and got["investigation"]["adapter"] == "legacy"
    # Storage: ONE investigation record (the proposal, confirmed by the
    # reviewer's click — a reviewer edit is not a second investigation),
    # one artifact row per file.
    s = db()
    assert s.query(ClaimInvestigation).filter(ClaimInvestigation.run_id == run_id).count() == 1
    assert s.query(ClaimSourceArtifact).filter(ClaimSourceArtifact.run_id == run_id).count() == 44
    assert s.query(ClaimEvidenceAssignment).filter(ClaimEvidenceAssignment.run_id == run_id).count() == len(got["assignments"])
    # The switch hides the fields; storage stays.
    monkeypatch.setattr(config, "CLAIMS_CASE_MODEL", False)
    hidden = client.get(f"/api/claims-runs/{run_id}").json()
    assert "cases" not in hidden and "artifacts" not in hidden and hidden["employees"]
    assert s.query(ClaimCase).filter(ClaimCase.run_id == run_id).count() == 10
    # A category set by the reviewer is mirrored on the case at once.
    monkeypatch.setattr(config, "CLAIMS_CASE_MODEL", True)
    emp = got["employees"][0]
    r = client.put(f"/api/claims-runs/{run_id}/employees/{emp['id']}/category",
                   json={"category": "Meals", "gl": "711010", "reason": "test", "expected_revision": rev(run_id)})
    assert r.status_code == 200
    again = client.get(f"/api/claims-runs/{run_id}").json()
    assert next(c for c in again["cases"] if c["employee_id"] == emp["id"])["category"] == "Meals"
    assert again["revision"] == got["revision"] + 1


def test_sync_creates_a_case_for_an_employee_made_directly(tmp_path):
    """Tests and old code paths that add a ClaimEmployee by hand still get a
    mirrored case on first sync (never a crash, never a missing case)."""
    from app.claims import cases as cases_mod

    engine = create_engine(f"sqlite:///{tmp_path / 't.sqlite3'}", connect_args={"check_same_thread": False})
    migrations.run_migrations(engine)
    s = sessionmaker(bind=engine)()
    run = ClaimsRun(id="r1", client="c")
    emp = ClaimEmployee(run_id="r1", folder="X_1", name="X", er_code="ER(9)", roles={"no_report": True}, status="pending")
    s.add_all([run, emp])
    s.commit()
    case = cases_mod.sync_case_from_employee(s, emp)
    assert case.legacy_employee_id == emp.id and case.label == "X_1" and case.claimant_name == "X"
    assert case.state == "confirmed" and case.roles == {"no_report": True}
    assert cases_mod.sync_case_from_employee(s, emp).id == case.id  # idempotent
    assert cases_mod.case_id_for_employee(s, emp.id) == case.id
