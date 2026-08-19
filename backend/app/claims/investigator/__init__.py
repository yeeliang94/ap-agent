"""The claims investigator — one deep module, one entry point:

    result = await investigate(request, tools)

Behind it: the legacy structured-folder adapter (the delivered mapper,
the rollback path) or the tool-using investigator (H5), chosen by the
CLAIMS_AGENTIC_INVESTIGATION switch. Callers never pick a strategy: a
structured folder is a full dump that arrives with strong folder-based
grouping signals.
"""
from __future__ import annotations

from ... import config
from .contracts import InvestigationRequest, InvestigationResult


def adapter_name() -> str:
    return "investigator" if config.CLAIMS_AGENTIC_INVESTIGATION else "legacy"


async def investigate(request: InvestigationRequest, tools=None) -> InvestigationResult:
    if adapter_name() == "investigator":
        from . import investigator as agentic

        return await agentic.investigate(request, tools)
    from . import legacy

    return await legacy.investigate(request, tools)


__all__ = ["investigate", "adapter_name", "InvestigationRequest", "InvestigationResult"]
