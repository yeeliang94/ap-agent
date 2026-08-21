"""Small shared contract for reviewer-facing live run progress."""
from __future__ import annotations

from datetime import datetime, timezone


def progress(phase: str, step: str, done: int = 0, total: int = 0,
             unit: str = "items", **compat) -> dict:
    """Build the additive contract while retaining legacy counter keys."""
    return {
        "phase": phase,
        "step": step,
        "done": done,
        "total": total,
        "unit": unit,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **compat,
    }


def terminal_progress(previous: dict | None, step: str, *, phase: str | None = None) -> dict:
    """Finish or stop a run without duplicating its last trustworthy counts."""
    old = previous or {}
    return progress(
        phase or old.get("phase", "finalizing"),
        step,
        old.get("done", 0),
        old.get("total", 0),
        old.get("unit", "items"),
    )
