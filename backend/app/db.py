"""Database setup.

SQLite for the pilot: a whole database in one local file, zero servers to run.
SQLAlchemy is the translation layer — if production later wants PostgreSQL,
the models stay the same and only the connection line changes.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from . import config


class Base(DeclarativeBase):
    pass


# check_same_thread=False lets FastAPI's worker threads share the connection
# safely under SQLAlchemy's own locking.
engine = create_engine(
    f"sqlite:///{config.DB_PATH}", connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    """Create tables that don't exist yet. Safe to call on every startup."""
    from . import models  # imported here so table classes are registered

    config.ensure_dirs()
    Base.metadata.create_all(engine)

    # Tiny migration: databases created before the runs.snapshot column
    # need it added (create_all only creates missing TABLES, not columns).
    with engine.connect() as conn:
        cols = [r[1] for r in conn.exec_driver_sql("PRAGMA table_info(runs)")]
        if "snapshot" not in cols:
            conn.exec_driver_sql("ALTER TABLE runs ADD COLUMN snapshot JSON DEFAULT '{}'")
            conn.commit()
