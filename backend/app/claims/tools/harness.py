"""ToolHarness: the production InvestigationTools (H4).

One object per investigation. It resolves MANIFEST IDS only (a path the
model types is not a handle to anything), enforces the run's tool budget
(calls, bytes, pages) before every call, times every call, hashes inputs
and outputs into the tool-execution record, writes large or binary
results (page renders) to the run's temporary output area and hands back
a handle, redacts absolute paths from every error, and stops dead once
cancelled. Tools never write domain records: `record_proposal` keeps the
agent's proposals in memory for the audit step.

    tools = ToolHarness(workspace, manifest, budget, sandbox=None)
    r = await tools.read_cells("a1b2c3d4e5f6", "Expense Report", "A1:H20")
    r.data / r.citations / r.provenance / r.truncated / r.error_code
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from ..investigator.contracts import Budget, Citation, ManifestEntry, ToolExecution
from . import calculator as calc_mod
from . import documents as docs_mod
from . import files as files_mod
from . import tables as tables_mod
from . import workbook as wb_mod
from .contracts import (MAX_LIST, MAX_SEARCH_HITS, TOOL_NAMES, SandboxLimits, SandboxPort,
                        ToolResult)

log = logging.getLogger("claims.tools")

PROPOSAL_KINDS = ("case", "assignment", "line", "assumption", "artifact_role", "question", "flag")
MAX_PROPOSALS = 2000
OUTPUT_DIR = "tool_output"


def _hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


class ToolHarness:
    def __init__(self, workspace: Path | str, manifest: list[ManifestEntry], budget: Budget | None = None,
                 sandbox: SandboxPort | None = None, python_enabled: bool = False,
                 sandbox_limits: SandboxLimits | None = None):
        self.workspace = Path(workspace)
        self.files_dir = self.workspace / "files"
        self.out_dir = self.workspace / OUTPUT_DIR
        self.manifest = list(manifest)
        self._by_id = {m.id: m for m in manifest}
        self.budget = budget or Budget()
        self.sandbox = sandbox
        self.python_enabled = python_enabled
        self.sandbox_limits = sandbox_limits or SandboxLimits()
        self._executions: list[ToolExecution] = []
        self._proposals: list[dict] = []
        self._cancelled = False
        self._bytes_read = 0
        self._pages_read = 0
        self._index = files_mod.TextIndex(self.files_dir, self.manifest)
        self.handles: dict[str, Path] = {}

    # ---- bookkeeping ------------------------------------------------------------

    def executions(self) -> list[ToolExecution]:
        return list(self._executions)

    def proposals(self) -> list[dict]:
        return list(self._proposals)

    def budget_remaining(self) -> dict[str, int]:
        return {"tool_calls": max(0, self.budget.tool_calls - len(self._executions)),
                "bytes_read": max(0, self.budget.bytes_read - self._bytes_read),
                "pages_read": max(0, self.budget.pages_read - self._pages_read)}

    def cancel(self) -> None:
        self._cancelled = True

    def _redact(self, text: str) -> str:
        """No absolute paths leave the harness."""
        text = str(text).replace(str(self.workspace.resolve()), "<run>").replace(str(self.workspace), "<run>")
        return re.sub(r"(/Users/[^\s'\"]+|/home/[^\s'\"]+|[A-Za-z]:\\\\[^\s'\"]+)", "<path>", text)[:500]

    def _entry(self, artifact_id: str) -> ManifestEntry | None:
        return self._by_id.get(str(artifact_id or ""))

    def _path(self, m: ManifestEntry) -> Path:
        p = (self.files_dir / m.path).resolve()
        if self.files_dir.resolve() not in p.parents:
            raise PermissionError("path escapes the snapshot")
        return p

    def _prov(self, *ms: ManifestEntry) -> dict:
        return {"artifact_ids": [m.id for m in ms], "hashes": [m.sha256 for m in ms]}

    async def _call(self, tool: str, args: tuple, fn, *, pages: int = 0) -> ToolResult:
        """Guard → run fn (sync, in a thread) → record. fn returns a ToolResult."""
        assert tool in TOOL_NAMES, tool
        started = time.monotonic()
        result: ToolResult
        if self._cancelled:
            result = ToolResult.failure("TOOL_FAILED", "the investigation was cancelled")
        elif len(self._executions) >= self.budget.tool_calls:
            result = ToolResult.failure("BUDGET", f"tool-call budget of {self.budget.tool_calls} used up")
        elif self._pages_read + pages > self.budget.pages_read:
            result = ToolResult.failure("BUDGET", f"page budget of {self.budget.pages_read} would be exceeded")
        elif self._bytes_read > self.budget.bytes_read:
            result = ToolResult.failure("BUDGET", f"byte budget of {self.budget.bytes_read} used up")
        else:
            try:
                result = await asyncio.to_thread(fn) if not asyncio.iscoroutinefunction(fn) else await fn()
                self._pages_read += pages
            except FileNotFoundError:
                result = ToolResult.failure("NOT_FOUND", "the file is missing from the snapshot")
            except (KeyError, ValueError, tables_mod.TableError, calc_mod.CalculationError) as exc:
                result = ToolResult.failure("BAD_INPUT", self._redact(str(exc)))
            except PermissionError as exc:
                result = ToolResult.failure("TOOL_FAILED", self._redact(str(exc)))
            except Exception as exc:  # a broken file, a library error: named, never a stack trace
                log.warning("tool %s failed: %s", tool, self._redact(repr(exc)))
                result = ToolResult.failure("TOOL_FAILED", f"{type(exc).__name__}: {self._redact(str(exc))}")
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        result.provenance.setdefault("artifact_ids", [])
        result.provenance.setdefault("hashes", [])
        self._executions.append(ToolExecution(
            id=f"t{len(self._executions) + 1:04d}", tool=tool, elapsed_ms=result.elapsed_ms,
            input_hashes=[_hash(args)], output_hash=_hash(result.data) if result.ok else "",
            truncated=result.truncated, error_code=result.error_code,
            note=(result.error or "")[:300]))
        return result

    def _write_handle(self, data: bytes, suffix: str) -> str:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        name = f"h{len(self.handles) + 1:04d}{suffix}"
        target = self.out_dir / name
        target.write_bytes(data)
        self.handles[name] = target
        return name

    def handle_bytes(self, handle: str) -> bytes:
        p = self.handles.get(handle)
        if p is None or not p.is_file():
            raise KeyError(handle)
        return p.read_bytes()

    # ---- the tools ----------------------------------------------------------------

    async def list_artifacts(self, query: str = "", media_type: str = "", limit: int = MAX_LIST) -> ToolResult:
        limit = max(1, min(int(limit or MAX_LIST), MAX_LIST))

        def run():
            data, truncated = files_mod.list_artifacts(self.manifest, query, media_type, limit)
            return ToolResult(data=data, truncated=truncated,
                              provenance={"artifact_ids": [d["id"] for d in data], "hashes": [d["sha256"] for d in data]})
        return await self._call("list_artifacts", (query, media_type, limit), run)

    async def inspect_workbook(self, artifact_id: str) -> ToolResult:
        m = self._entry(artifact_id)

        def run():
            if m is None:
                return ToolResult.failure("NOT_FOUND", "no such artifact id in the manifest")
            if m.media_type != "workbook":
                return ToolResult.failure("BAD_INPUT", f"{m.path} is a {m.media_type}, not a workbook")
            path = self._path(m)
            self._bytes_read += m.size
            data = wb_mod.inspect_workbook(path)
            data["path"] = m.path
            return ToolResult(data=data, provenance=self._prov(m), citations=[Citation(artifact_id=m.id, path=m.path)])
        return await self._call("inspect_workbook", (artifact_id,), run)

    async def read_cells(self, artifact_id: str, sheet: str, cell_range: str) -> ToolResult:
        m = self._entry(artifact_id)

        def run():
            if m is None:
                return ToolResult.failure("NOT_FOUND", "no such artifact id in the manifest")
            if m.media_type != "workbook":
                return ToolResult.failure("BAD_INPUT", f"{m.path} is a {m.media_type}, not a workbook")
            data = wb_mod.read_cells(self._path(m), sheet, cell_range)
            return ToolResult(data=data, provenance=self._prov(m),
                              citations=[Citation(artifact_id=m.id, path=m.path, sheet=sheet, cell=data["range"])])
        return await self._call("read_cells", (artifact_id, sheet, cell_range), run)

    async def inspect_document(self, artifact_id: str) -> ToolResult:
        m = self._entry(artifact_id)

        def run():
            if m is None:
                return ToolResult.failure("NOT_FOUND", "no such artifact id in the manifest")
            path = self._path(m)
            self._bytes_read += m.size
            data = docs_mod.inspect_document(path)
            data["path"] = m.path
            return ToolResult(data=data, provenance=self._prov(m), truncated=bool(data.get("text_truncated")),
                              citations=[Citation(artifact_id=m.id, path=m.path)])
        return await self._call("inspect_document", (artifact_id,), run, pages=min(m.pages or 1, 200) if m else 0)

    async def render_page(self, artifact_id: str, page: int) -> ToolResult:
        m = self._entry(artifact_id)

        def run():
            if m is None:
                return ToolResult.failure("NOT_FOUND", "no such artifact id in the manifest")
            if m.media_type not in ("pdf", "image"):
                return ToolResult.failure("BAD_INPUT", f"{m.path} is a {m.media_type}; only PDFs and images render")
            try:
                png, w, h = docs_mod.render_page(self._path(m), int(page))
            except IndexError:
                return ToolResult.failure("BAD_INPUT", f"{m.path} has no page {page}")
            handle = self._write_handle(png, ".png")
            return ToolResult(data={"handle": handle, "width": w, "height": h, "page": int(page)}, handle=handle,
                              provenance=self._prov(m), citations=[Citation(artifact_id=m.id, path=m.path, page=int(page))])
        return await self._call("render_page", (artifact_id, page), run, pages=1)

    async def crop_page(self, artifact_id: str, page: int, region: list[int]) -> ToolResult:
        m = self._entry(artifact_id)

        def run():
            if m is None:
                return ToolResult.failure("NOT_FOUND", "no such artifact id in the manifest")
            if not isinstance(region, (list, tuple)) or len(region) != 4:
                return ToolResult.failure("BAD_INPUT", "region must be [x0, y0, x1, y1]")
            try:
                png, w, h = docs_mod.crop_page(self._path(m), int(page), list(region))
            except IndexError:
                return ToolResult.failure("BAD_INPUT", f"{m.path} has no page {page}")
            handle = self._write_handle(png, ".png")
            return ToolResult(data={"handle": handle, "width": w, "height": h}, handle=handle, provenance=self._prov(m),
                              citations=[Citation(artifact_id=m.id, path=m.path, page=int(page), region=[int(v) for v in region])])
        return await self._call("crop_page", (artifact_id, page, tuple(region) if isinstance(region, (list, tuple)) else region), run, pages=1)

    async def search_artifacts(self, query: str, limit: int = MAX_SEARCH_HITS) -> ToolResult:
        limit = max(1, min(int(limit or MAX_SEARCH_HITS), MAX_SEARCH_HITS))

        def run():
            if not (query or "").strip():
                return ToolResult.failure("BAD_INPUT", "empty query")
            before = self._index.bytes_read
            hits, cites, truncated = files_mod.search_artifacts(self._index, query, limit)
            self._bytes_read += self._index.bytes_read - before
            ids = sorted({h["artifact_id"] for h in hits})
            return ToolResult(data={"hits": hits, "unindexed": [self._by_id[i].path for i in self._index.failures if i in self._by_id]},
                              citations=cites, truncated=truncated,
                              provenance={"artifact_ids": ids, "hashes": [self._by_id[i].sha256 for i in ids]})
        return await self._call("search_artifacts", (query, limit), run)

    async def calculate(self, expression: str) -> ToolResult:
        def run():
            value = calc_mod.calculate(expression)
            return ToolResult(data={"expression": expression, "value": str(value)})
        return await self._call("calculate", (expression,), run)

    async def compare_tables(self, spec: dict[str, Any]) -> ToolResult:
        def run():
            data = tables_mod.compare_tables(spec)
            return ToolResult(data=data, truncated=bool(data.get("truncated")))
        return await self._call("compare_tables", (_hash(spec),), run)

    async def run_python(self, code: str, input_artifact_ids: list[str]) -> ToolResult:
        ids = tuple(str(i) for i in (input_artifact_ids or []))

        async def run():
            if not self.python_enabled or self.sandbox is None:
                return ToolResult.failure("TOOL_UNAVAILABLE", "run_python is not enabled for this run")
            ok, why = self.sandbox.available()
            if not ok:
                return ToolResult.failure("TOOL_UNAVAILABLE", f"the sandbox cannot isolate here: {self._redact(why)}")
            inputs: dict[str, Path] = {}
            for i in ids:
                m = self._entry(i)
                if m is None:
                    return ToolResult.failure("NOT_FOUND", f"no such artifact id {i}")
                inputs[m.path] = self._path(m)
            self.out_dir.mkdir(parents=True, exist_ok=True)
            out = self.out_dir / f"py{len(self._executions) + 1:04d}"
            out.mkdir(parents=True, exist_ok=True)
            res = await self.sandbox.run(code, inputs, out, self.sandbox_limits)
            if not res.ok:
                return ToolResult.failure(res.error_code or "TOOL_FAILED", self._redact(res.error or res.stderr[-300:]))
            return ToolResult(data={"stdout": res.stdout, "output_files": res.output_files, "output_hash": res.output_hash,
                                    "versions": res.versions, "elapsed_ms": res.elapsed_ms},
                              provenance={"artifact_ids": list(ids), "hashes": [self._by_id[i].sha256 for i in ids if i in self._by_id]})
        return await self._call("run_python", (_hash(code), ids), run)

    def record_proposal(self, kind: str, payload: dict[str, Any]) -> ToolResult:
        started = time.monotonic()
        if self._cancelled:
            result = ToolResult.failure("TOOL_FAILED", "the investigation was cancelled")
        elif kind not in PROPOSAL_KINDS:
            result = ToolResult.failure("BAD_INPUT", f"unknown proposal kind {kind!r} (one of {', '.join(PROPOSAL_KINDS)})")
        elif not isinstance(payload, dict):
            result = ToolResult.failure("BAD_INPUT", "payload must be an object")
        elif len(self._proposals) >= MAX_PROPOSALS:
            result = ToolResult.failure("BUDGET", f"more than {MAX_PROPOSALS} proposals")
        else:
            self._proposals.append({"kind": kind, **payload})
            result = ToolResult(data={"recorded": len(self._proposals)})
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        result.provenance = {"artifact_ids": [], "hashes": []}
        self._executions.append(ToolExecution(id=f"t{len(self._executions) + 1:04d}", tool="record_proposal",
                                              elapsed_ms=result.elapsed_ms, input_hashes=[_hash((kind, payload))],
                                              output_hash=_hash(result.data) if result.ok else "",
                                              error_code=result.error_code, note=(result.error or "")[:300]))
        return result
