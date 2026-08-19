"""SandboxPort adapters (H8). See the full implementation notes there; this
module ships the port's production adapter DISABLED unless the host can
isolate — the deterministic tools never depend on it.
"""
from __future__ import annotations

from pathlib import Path

from .contracts import SandboxLimits, SandboxResult


class UnavailableSandbox:
    """The adapter used when no OS-level isolation is configured: it never
    runs anything and says why."""

    def __init__(self, why: str = "no OS-level isolation is configured on this host (CLAIMS_SANDBOX_RUNNER unset)"):
        self.why = why

    def available(self) -> tuple[bool, str]:
        return False, self.why

    async def run(self, code: str, inputs: dict[str, Path], output_dir: Path, limits: SandboxLimits) -> SandboxResult:
        return SandboxResult(ok=False, error_code="TOOL_UNAVAILABLE", error=self.why)


def production_sandbox():
    from . import sandbox_impl

    return sandbox_impl.production_sandbox()
