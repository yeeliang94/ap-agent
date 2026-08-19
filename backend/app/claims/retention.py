"""Retention controls (hardening H11): what a run keeps on disk and for
how long, and the one deliberate pruning step.

A run's workspace (runs/<id>/claims/) holds the immutable snapshot
(files/), the survey thumbnails (peeks/), and the tool harness's temporary
output (tool_output/: page renders and sandbox output handed back to the
agent during the investigation). The snapshot and peeks are what
Citations and the replay bundle resolve to: they are kept for the run's
life. tool_output is scratch: it is pruned once a run has closed (ready or
failed) and again whenever the operator asks.

Nothing here deletes a run, a database row, or anything in SharePoint.
Run deletion is an operator decision taken outside this module.
"""
from __future__ import annotations

import logging
import shutil

from .. import config

log = logging.getLogger("claims.retention")

TOOL_OUTPUT_DIR = "tool_output"


def prune_tool_output(run_id: str) -> int:
    """Delete the run's tool_output scratch; returns bytes freed."""
    d = config.RUNS_DIR / run_id / "claims" / TOOL_OUTPUT_DIR
    if not d.is_dir():
        return 0
    freed = sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
    shutil.rmtree(d, ignore_errors=True)
    log.info("claims run %s: tool_output pruned (%d bytes)", run_id, freed)
    return freed


def workspace_size(run_id: str) -> dict[str, int]:
    """Bytes per area of a run's workspace — for the operator's view."""
    base = config.RUNS_DIR / run_id / "claims"
    out: dict[str, int] = {}
    if not base.is_dir():
        return out
    for child in base.iterdir():
        if child.is_dir():
            out[child.name] = sum(p.stat().st_size for p in child.rglob("*") if p.is_file())
        else:
            out[child.name] = child.stat().st_size
    return out


def prune_closed_runs(db) -> int:
    """tool_output of every closed run (ready / failed); returns bytes freed.
    Safe to call at startup and on demand."""
    from .models import ClaimsRun

    freed = 0
    for run in db.query(ClaimsRun).filter(ClaimsRun.status.in_(("ready", "failed"))).all():
        freed += prune_tool_output(run.id)
    return freed
