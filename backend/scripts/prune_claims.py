"""Prune the scratch a claims investigation leaves behind.

Every run's workspace (backend/data/runs/<id>/claims/) holds three things:

  files/       the immutable snapshot the investigation read — KEPT, it is
               what Citations and the replay bundle resolve to
  peeks/       the survey thumbnails — KEPT, same reason
  tool_output/ page renders and sandbox output handed to the agent while it
               worked — SCRATCH, and the only thing this script deletes

`worker._finish_run` already prunes a run's tool_output when the run closes
normally. This script is the operator's copy of the same act, for the runs
that never got there: a run failed by a restart, a cancelled run, or a
machine where an older build never pruned at all. Nothing here deletes a
run, a database row, or anything in SharePoint.

Usage:
    python scripts/prune_claims.py            # say what would be freed
    python scripts/prune_claims.py --apply    # actually delete it
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.claims import retention                        # noqa: E402
from app.claims.models import ClaimsRun                 # noqa: E402
from app.db import SessionLocal, init_db                # noqa: E402


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="delete the scratch (without this, only report it)")
    args = parser.parse_args()

    init_db()
    db = SessionLocal()
    try:
        closed = db.query(ClaimsRun).filter(ClaimsRun.status.in_(("ready", "failed"))).all()
        if not args.apply:
            total = 0
            for run in closed:
                scratch = retention.workspace_size(run.id).get(retention.TOOL_OUTPUT_DIR, 0)
                if scratch:
                    print(f"  {run.id}  {run.status:7}  {_mb(scratch)} of tool_output")
                    total += scratch
            print(f"{len(closed)} closed run(s); {_mb(total)} would be freed. "
                  "Re-run with --apply to delete it.")
            return 0
        freed = retention.prune_closed_runs(db)
        print(f"{len(closed)} closed run(s) pruned; {_mb(freed)} freed.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
