"""The InvestigationTools and SandboxPort contracts (hardening H1/H4/H8).

Every tool returns a ToolResult: data plus provenance (which artifacts, by
manifest id and hash, the answer came from), a truncation indicator and
an error code — so the tool-execution record can be replayed and every
value the agent cites traces to the snapshot. Tools resolve MANIFEST IDS,
never paths the model typed. Large results go to the run's temporary
output area and come back by handle.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ..investigator.contracts import Citation, ToolExecution

# The allowlist. A tool not named here is not callable, whatever the
# model asks for; run_python is present only when the sandbox switch is on
# AND the sandbox reports it can isolate.
TOOL_NAMES = ("list_artifacts", "inspect_workbook", "read_cells", "inspect_document",
              "render_page", "crop_page", "search_artifacts", "calculate", "compare_tables",
              "run_python", "record_proposal")

# Bounds every adapter enforces (the plan's "Required controls" column).
MAX_LIST = 500
MAX_CELLS = 4000          # cells per read_cells call
MAX_CELL_CHARS = 200
MAX_TEXT_CHARS = 20000    # text returned inline; beyond this → handle
MAX_SEARCH_HITS = 100
MAX_PAGE_PIXELS = 3000    # longest edge for render/crop
MAX_TABLE_ROWS = 5000
MAX_CALC_OPS = 200


class ToolResult(BaseModel):
    call_id: str = ""        # the ToolExecution id this result was recorded under
    ok: bool = True
    data: Any = None
    citations: list[Citation] = Field(default_factory=list)
    provenance: dict[str, Any] = Field(default_factory=dict)  # {"artifact_ids": [...], "hashes": [...]}
    truncated: bool = False
    error_code: str = ""     # TOOL_FAILED / TOOL_UNAVAILABLE / SANDBOX_LIMIT / BUDGET / NOT_FOUND / BAD_INPUT
    error: str = ""
    handle: str = ""         # a large result written to the run's temp output area
    elapsed_ms: int = 0

    @classmethod
    def failure(cls, code: str, message: str) -> "ToolResult":
        return cls(ok=False, error_code=code, error=message[:500])


class SandboxLimits(BaseModel):
    wall_seconds: int = Field(default=30, ge=1, le=600)
    cpu_seconds: int = Field(default=20, ge=1, le=600)
    memory_mb: int = Field(default=512, ge=16, le=8192)
    max_processes: int = Field(default=1, ge=1, le=8)
    max_open_files: int = Field(default=64, ge=8, le=1024)
    max_input_bytes: int = Field(default=50 * 1024 * 1024, ge=0)
    max_output_bytes: int = Field(default=2 * 1024 * 1024, ge=0)


class SandboxResult(BaseModel):
    ok: bool
    stdout: str = ""
    stderr: str = ""
    exit_status: int | None = None
    output_files: dict[str, str] = Field(default_factory=dict)  # name -> sha256
    output_hash: str = ""
    elapsed_ms: int = 0
    killed: bool = False
    limit_hit: str = ""       # wall / cpu / memory / output / input / processes
    error_code: str = ""      # SANDBOX_LIMIT / TOOL_FAILED / TOOL_UNAVAILABLE
    error: str = ""
    versions: dict[str, str] = Field(default_factory=dict)


@runtime_checkable
class SandboxPort(Protocol):
    """Runs model-written Python OUTSIDE the FastAPI process, read-only over
    the snapshot, one empty writable output directory, no network."""

    def available(self) -> tuple[bool, str]:
        """(True, "") when isolation is provided; else (False, why)."""
        ...

    async def run(self, code: str, inputs: dict[str, Path], output_dir: Path,
                  limits: SandboxLimits) -> SandboxResult:
        ...


@runtime_checkable
class InvestigationTools(Protocol):
    """The allowlisted capabilities. Async because the real ones do I/O."""

    async def list_artifacts(self, query: str = "", media_type: str = "", limit: int = MAX_LIST) -> ToolResult: ...
    async def inspect_workbook(self, artifact_id: str) -> ToolResult: ...
    async def read_cells(self, artifact_id: str, sheet: str, cell_range: str) -> ToolResult: ...
    async def inspect_document(self, artifact_id: str) -> ToolResult: ...
    async def render_page(self, artifact_id: str, page: int) -> ToolResult: ...
    async def crop_page(self, artifact_id: str, page: int, region: list[int]) -> ToolResult: ...
    async def search_artifacts(self, query: str, limit: int = MAX_SEARCH_HITS,
                               artifact_ids: list[str] | None = None) -> ToolResult: ...
    async def calculate(self, expression: str) -> ToolResult: ...
    async def compare_tables(self, spec: dict[str, Any]) -> ToolResult: ...
    async def run_python(self, code: str, input_artifact_ids: list[str]) -> ToolResult: ...
    def record_proposal(self, kind: str, payload: dict[str, Any]) -> ToolResult: ...
    def proposals(self) -> list[dict[str, Any]]: ...
    def executions(self) -> list[ToolExecution]: ...
    def budget_remaining(self) -> dict[str, int]: ...
    def cancel(self) -> None: ...
    @property
    def cancelled(self) -> bool: ...
