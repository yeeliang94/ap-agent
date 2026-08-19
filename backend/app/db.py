"""Database setup.

SQLite for the pilot: a whole database in one local file, zero servers to run.
SQLAlchemy is the translation layer — if production later wants PostgreSQL,
the models stay the same and only the connection line changes.
"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from . import config


class Base(DeclarativeBase):
    pass


# check_same_thread=False lets FastAPI's worker threads share the connection
# safely under SQLAlchemy's own locking. timeout is how long a writer waits
# for another writer before giving up ("database is locked") — 30 s, so
# five claims workers committing at once queue rather than fail.
engine = create_engine(
    f"sqlite:///{config.DB_PATH}",
    connect_args={"check_same_thread": False, "timeout": 30},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record) -> None:
    """WAL mode: readers never block writers and writers never block
    readers, which is what several background workers plus a polling
    screen need. Applies to the file once and persists; harmless if
    already set. busy_timeout is the same 30 s at the SQLite level."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
    finally:
        cursor.close()


def init_db() -> None:
    """Create tables that don't exist yet. Safe to call on every startup."""
    from . import models  # imported here so table classes are registered
    from .claims import models as _claims_models  # noqa: F401 — claims_* tables

    config.ensure_dirs()
    Base.metadata.create_all(engine)
    # The claims module's additive, idempotent migrations (versioned; the
    # ALTER-TABLE pattern below cannot backfill related tables).
    from .claims.migrations import run_migrations

    run_migrations(engine)

    # Tiny migration: databases created before the runs.snapshot column
    # need it added (create_all only creates missing TABLES, not columns).
    with engine.connect() as conn:
        cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(runs)")]
        if "snapshot" not in cols:
            conn.exec_driver_sql("ALTER TABLE runs ADD COLUMN snapshot JSON DEFAULT '{}'")
            conn.commit()
