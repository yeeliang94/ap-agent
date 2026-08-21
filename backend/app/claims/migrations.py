"""Idempotent, additive claims schema migrations (hardening H2).

The startup `create_all` makes missing TABLES; it never adds a column to a
table that already exists, and it cannot backfill one table from another.
This runner does both, once, and records what it applied in claims_schema:

  version 1  the case model — case_id on rows/evidence/flags, manifest and
             revision on runs, artifact_id on flags; one ClaimCase per
             existing ClaimEmployee; case_id backfilled from employee_id;
             the H1 manifest moved from survey["manifest"] to its column;
             artifacts and cases materialised from the H1 investigation
             record when a run has one

Rules: additive only (no rename, no drop, no destructive rewrite); every
step checks before it acts, so running twice — or against a database that
already has the shape — changes nothing; migrations run unconditionally,
whatever the feature switches say (they gate what is read and shown, never
what is stored). No migration deletes user data.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import sessionmaker

log = logging.getLogger("claims.migrations")

# (version, name, function(session, connection))
MIGRATIONS: list[tuple[int, str, object]] = []


def migration(version: int, name: str):
    def wrap(fn):
        MIGRATIONS.append((version, name, fn))
        return fn
    return wrap


def _columns(conn, table: str) -> list[str]:
    return [r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({table})")]


def _tables(conn) -> set[str]:
    return set(sa_inspect(conn).get_table_names())


def _add_column(conn, table: str, column: str, ddl: str) -> bool:
    if column in _columns(conn, table):
        return False
    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    return True


def _session(engine):
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()


def current_version(engine) -> int:
    from .models import ClaimsSchema

    with engine.connect() as conn:
        if "claims_schema" not in _tables(conn):
            return 0
    s = _session(engine)
    try:
        return max((v for (v,) in s.query(ClaimsSchema.version).all()), default=0)
    finally:
        s.close()


def _is_applied(engine, version: int) -> bool:
    from .models import ClaimsSchema

    s = _session(engine)
    try:
        return s.get(ClaimsSchema, version) is not None
    finally:
        s.close()


def _record(engine, version: int, name: str) -> None:
    from .models import ClaimsSchema

    s = _session(engine)
    try:
        s.add(ClaimsSchema(version=version, name=name, applied_at=datetime.now(timezone.utc)))
        s.commit()
    finally:
        s.close()


def run_migrations(engine) -> list[int]:
    """Create missing tables, then apply every migration not yet recorded.
    Returns the versions applied this call ([] when up to date). Each
    migration owns its connections/sessions and closes them, so DDL and
    ORM backfill never hold two locks on the same SQLite file at once."""
    from ..db import Base
    from . import models as _models  # noqa: F401 — registers the claims tables

    Base.metadata.create_all(engine)
    applied: list[int] = []
    for version, name, fn in sorted(MIGRATIONS, key=lambda m: m[0]):
        if _is_applied(engine, version):
            continue
        try:
            fn(engine)
        except Exception:
            log.exception("claims migration %d (%s) failed", version, name)
            raise
        _record(engine, version, name)
        applied.append(version)
        log.info("claims migration %d (%s) applied", version, name)
    return applied


@migration(1, "case model: cases, artifacts, assignments, investigations, tool executions; case ids on rows/evidence/flags")
def _v1_case_model(engine) -> None:
    from .models import ClaimEmployee, ClaimEvidence, ClaimFlag, ClaimRow, ClaimsRun

    # 1. columns on the delivered tables (create_all made the new tables)
    with engine.begin() as conn:
        _add_column(conn, "claims_runs", "manifest", "JSON DEFAULT '[]'")
        _add_column(conn, "claims_runs", "revision", "INTEGER DEFAULT 0")
        # The current ORM shape must be queryable while v1 backfills. v2
        # records ownership of this additive field for databases that had
        # already completed v1 before live worker progress existed.
        _add_column(conn, "claim_employees", "progress", "JSON DEFAULT '{}'")
        for table in ("claim_rows", "claim_evidence", "claim_flags"):
            _add_column(conn, table, "case_id", "VARCHAR DEFAULT ''")
        _add_column(conn, "claim_flags", "artifact_id", "VARCHAR DEFAULT ''")
    # 2. one Claim Case per existing employee; case ids on their rows,
    #    evidence and flags.
    from . import cases as cases_mod

    s = _session(engine)
    try:
        for emp in s.query(ClaimEmployee).all():
            case = cases_mod.sync_case_from_employee(s, emp)
            for model in (ClaimRow, ClaimEvidence, ClaimFlag):
                for rec in s.query(model).filter(model.employee_id == emp.id, model.case_id == "").all():
                    rec.case_id = case.id
        s.commit()
        # 3. the H1 manifest and investigation record, if a run has one
        for run in s.query(ClaimsRun).all():
            survey = dict(run.survey or {})
            if survey.get("manifest") and not run.manifest:
                run.manifest = survey.pop("manifest")
                run.survey = survey
            inv = (run.survey or {}).get("investigation")
            if inv:
                cases_mod.materialise(s, run, inv)
        s.commit()
    finally:
        s.close()


@migration(2, "live progress for concurrent claim workers")
def _v2_worker_progress(engine) -> None:
    with engine.begin() as conn:
        _add_column(conn, "claim_employees", "progress", "JSON DEFAULT '{}'")
