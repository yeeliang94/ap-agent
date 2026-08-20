"""The claims investigator — one deep module, one entry point:

    result = await investigate(request, tools)

Behind it: the legacy structured-folder adapter (the delivered mapper,
the rollback path) or the tool-using investigator (H5), chosen by the
agentic-investigation switch. Callers never pick a strategy: a
structured folder is a full dump that arrives with strong folder-based
grouping signals.
"""
from __future__ import annotations

from ... import switches
from .contracts import InvestigationRequest, InvestigationResult


def adapter_name(run_snapshot: dict | None = None) -> str:
    """The strategy for this run: the switch stamped on its snapshot when
    it has one (a run keeps the switches it started with), otherwise the
    live switch — the same fallback every other per-run read uses."""
    if switches.for_run(run_snapshot, "claims_agentic_investigation"):
        return "investigator"
    return "legacy"


async def investigate(request: InvestigationRequest, tools=None) -> InvestigationResult:
    if adapter_name(request.profile_snapshot) == "investigator":
        from . import investigator as agentic

        return await agentic.investigate(request, tools)
    from . import legacy

    return await legacy.investigate(request, tools)


__all__ = ["investigate", "adapter_name", "InvestigationRequest", "InvestigationResult"]
