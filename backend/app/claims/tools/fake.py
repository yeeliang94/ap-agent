"""InMemoryTools: the InvestigationTools test double (H1).

Scripted artifacts and results, no disk, no model. Built from a manifest
plus, per artifact id, its "contents":

    {"sheets": {"Expense Report": {"A1": "Name:", "B1": "Aegene Ong", ...}},
     "pages": ["text of page 1", "text of page 2"]}

Behaves like the real harness where it matters to a test: it enforces the
tool-call budget, refuses ids not in the manifest, records one
ToolExecution per call, hands large text back by handle, and keeps the
proposals the agent records. Calculator and table comparison are the real
pure implementations. `scripts` lets a test pin the answer of any call:
{("read_cells", "a1", "KM", "A1:H9"): ToolResult(...)}.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..investigator.contracts import Budget, Citation, ManifestEntry, ToolExecution
from . import calculator as calc_mod
from . import tables as tables_mod
from .contracts import (MAX_CELLS, MAX_LIST, MAX_SEARCH_HITS, MAX_TEXT_CHARS, TOOL_NAMES,
                        ToolResult)

_CELL = re.compile(r"^([A-Za-z]{1,3})(\d{1,7})$")


def _col_num(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def _hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


class BudgetExhausted(Exception):
    pass


class InMemoryTools:
    def __init__(self, manifest: list[ManifestEntry], contents: dict[str, dict] | None = None,
                 budget: Budget | None = None, scripts: dict[tuple, ToolResult] | None = None,
                 python_available: bool = False):
        self.manifest = list(manifest)
        self._by_id = {m.id: m for m in manifest}
        self.contents = contents or {}
        self.budget = budget or Budget()
        self.scripts = scripts or {}
        self.python_available = python_available
        self.calls: list[tuple] = []
        self._executions: list[ToolExecution] = []
        self._proposals: list[dict] = []
        self.handles: dict[str, str] = {}
        self._cancelled = False
        self._python_runs: list[str] = []

    # ---- bookkeeping ------------------------------------------------------------

    def _record(self, tool: str, args: tuple, result: ToolResult) -> ToolResult:
        assert tool in TOOL_NAMES, tool
        self.calls.append((tool, *args))
        result.provenance.setdefault("artifact_ids", [])
        result.provenance.setdefault("hashes", [])
        self._executions.append(ToolExecution(
            id=f"t{len(self._executions) + 1:04d}", tool=tool, elapsed_ms=0,
            input_hashes=[_hash(args)], output_hash=_hash(result.data) if result.ok else "",
            truncated=result.truncated, error_code=result.error_code))
        return result

    def _guard(self, tool: str, args: tuple) -> ToolResult | None:
        if self._cancelled:
            return self._record(tool, args, ToolResult.failure("TOOL_FAILED", "cancelled"))
        used = sum(1 for e in self._executions if e.error_code != "BUDGET")
        if used >= self.budget.tool_calls:
            return self._record(tool, args, ToolResult.failure(
                "BUDGET", f"tool-call budget of {self.budget.tool_calls} used up"))
        scripted = self.scripts.get((tool, *args))
        if scripted is not None:
            return self._record(tool, args, scripted.model_copy(deep=True))
        return None

    def _entry(self, artifact_id: str) -> ManifestEntry | None:
        return self._by_id.get(artifact_id)

    def _prov(self, m: ManifestEntry) -> dict:
        return {"artifact_ids": [m.id], "hashes": [m.sha256]}

    def executions(self) -> list[ToolExecution]:
        return list(self._executions)

    def proposals(self) -> list[dict]:
        return list(self._proposals)

    def budget_remaining(self) -> dict[str, int]:
        return {"tool_calls": max(0, self.budget.tool_calls - len(self._executions))}

    def cancel(self) -> None:
        self._cancelled = True

    # ---- the tools ----------------------------------------------------------------

    async def list_artifacts(self, query: str = "", media_type: str = "", limit: int = MAX_LIST) -> ToolResult:
        args = (query, media_type, limit)
        if (g := self._guard("list_artifacts", args)) is not None:
            return g
        limit = max(1, min(int(limit), MAX_LIST))
        q = (query or "").lower()
        hits = [m for m in self.manifest
                if (not q or q in m.path.lower()) and (not media_type or m.media_type == media_type)]
        data = [{"id": m.id, "path": m.path, "media_type": m.media_type, "size": m.size,
                 "pages": m.pages, "sheets": m.sheets, "sha256": m.sha256} for m in hits[:limit]]
        return self._record("list_artifacts", args, ToolResult(
            data=data, truncated=len(hits) > limit,
            provenance={"artifact_ids": [d["id"] for d in data], "hashes": [d["sha256"] for d in data]}))

    async def inspect_workbook(self, artifact_id: str) -> ToolResult:
        args = (artifact_id,)
        if (g := self._guard("inspect_workbook", args)) is not None:
            return g
        m = self._entry(artifact_id)
        if m is None:
            return self._record("inspect_workbook", args, ToolResult.failure("NOT_FOUND", "no such artifact"))
        sheets = (self.contents.get(artifact_id) or {}).get("sheets") or {}
        data = {"path": m.path, "sheets": []}
        for name, cells in sheets.items():
            rows = [int(_CELL.match(c).group(2)) for c in cells if _CELL.match(c)]
            cols = [_col_num(_CELL.match(c).group(1)) for c in cells if _CELL.match(c)]
            data["sheets"].append({"name": name, "max_row": max(rows or [0]), "max_col": max(cols or [0]),
                                   "cells": len(cells), "formulas": 0, "hidden": False, "merged": []})
        for name in m.sheets:
            if name not in sheets:
                data["sheets"].append({"name": name, "max_row": 0, "max_col": 0, "cells": 0,
                                       "formulas": 0, "hidden": False, "merged": []})
        return self._record("inspect_workbook", args, ToolResult(
            data=data, provenance=self._prov(m), citations=[Citation(artifact_id=m.id, path=m.path)]))

    async def read_cells(self, artifact_id: str, sheet: str, cell_range: str) -> ToolResult:
        args = (artifact_id, sheet, cell_range)
        if (g := self._guard("read_cells", args)) is not None:
            return g
        m = self._entry(artifact_id)
        if m is None:
            return self._record("read_cells", args, ToolResult.failure("NOT_FOUND", "no such artifact"))
        cells = ((self.contents.get(artifact_id) or {}).get("sheets") or {}).get(sheet)
        if cells is None:
            return self._record("read_cells", args, ToolResult.failure("NOT_FOUND", f"no sheet {sheet!r}"))
        try:
            a, b = cell_range.split(":") if ":" in cell_range else (cell_range, cell_range)
            ma, mb = _CELL.match(a), _CELL.match(b)
            c0, r0, c1, r1 = _col_num(ma.group(1)), int(ma.group(2)), _col_num(mb.group(1)), int(mb.group(2))
        except Exception:
            return self._record("read_cells", args, ToolResult.failure("BAD_INPUT", "range must look like A1:H20"))
        if (r1 - r0 + 1) * (c1 - c0 + 1) > MAX_CELLS:
            return self._record("read_cells", args, ToolResult.failure("BAD_INPUT", f"range over {MAX_CELLS} cells"))
        out = []
        for ref, value in cells.items():
            mm = _CELL.match(ref)
            if not mm:
                continue
            c, r = _col_num(mm.group(1)), int(mm.group(2))
            if c0 <= c <= c1 and r0 <= r <= r1:
                out.append({"cell": ref.upper(), "row": r, "col": c, "value": value})
        out.sort(key=lambda x: (x["row"], x["col"]))
        return self._record("read_cells", args, ToolResult(
            data={"sheet": sheet, "range": cell_range, "cells": out}, provenance=self._prov(m),
            citations=[Citation(artifact_id=m.id, path=m.path, sheet=sheet, cell=cell_range)]))

    async def inspect_document(self, artifact_id: str) -> ToolResult:
        args = (artifact_id,)
        if (g := self._guard("inspect_document", args)) is not None:
            return g
        m = self._entry(artifact_id)
        if m is None:
            return self._record("inspect_document", args, ToolResult.failure("NOT_FOUND", "no such artifact"))
        pages = (self.contents.get(artifact_id) or {}).get("pages") or []
        blocks = [{"page": i, "text": t[:MAX_TEXT_CHARS]} for i, t in enumerate(pages, 1)]
        return self._record("inspect_document", args, ToolResult(
            data={"path": m.path, "pages": len(pages) or (m.pages or 0), "text_blocks": blocks},
            provenance=self._prov(m), citations=[Citation(artifact_id=m.id, path=m.path)]))

    async def render_page(self, artifact_id: str, page: int) -> ToolResult:
        args = (artifact_id, page)
        if (g := self._guard("render_page", args)) is not None:
            return g
        m = self._entry(artifact_id)
        if m is None:
            return self._record("render_page", args, ToolResult.failure("NOT_FOUND", "no such artifact"))
        handle = f"h{len(self.handles) + 1:04d}.png"
        self.handles[handle] = f"<png of {m.path} page {page}>"
        return self._record("render_page", args, ToolResult(
            data={"handle": handle, "width": 800, "height": 1100}, handle=handle, provenance=self._prov(m),
            citations=[Citation(artifact_id=m.id, path=m.path, page=page)]))

    async def crop_page(self, artifact_id: str, page: int, region: list[int]) -> ToolResult:
        args = (artifact_id, page, tuple(region))
        if (g := self._guard("crop_page", args)) is not None:
            return g
        m = self._entry(artifact_id)
        if m is None:
            return self._record("crop_page", args, ToolResult.failure("NOT_FOUND", "no such artifact"))
        handle = f"h{len(self.handles) + 1:04d}.png"
        self.handles[handle] = f"<crop of {m.path} page {page} {region}>"
        return self._record("crop_page", args, ToolResult(
            data={"handle": handle}, handle=handle, provenance=self._prov(m),
            citations=[Citation(artifact_id=m.id, path=m.path, page=page, region=list(region))]))

    async def search_artifacts(self, query: str, limit: int = MAX_SEARCH_HITS) -> ToolResult:
        args = (query, limit)
        if (g := self._guard("search_artifacts", args)) is not None:
            return g
        q = (query or "").lower().strip()
        if not q:
            return self._record("search_artifacts", args, ToolResult.failure("BAD_INPUT", "empty query"))
        hits, cites = [], []
        for m in self.manifest:
            c = self.contents.get(m.id) or {}
            for sheet, cells in (c.get("sheets") or {}).items():
                for ref, value in cells.items():
                    if q in str(value).lower():
                        hits.append({"artifact_id": m.id, "path": m.path, "sheet": sheet, "cell": ref, "text": str(value)[:200]})
                        cites.append(Citation(artifact_id=m.id, path=m.path, sheet=sheet, cell=ref))
            for i, text in enumerate(c.get("pages") or [], 1):
                if q in text.lower():
                    hits.append({"artifact_id": m.id, "path": m.path, "page": i,
                                 "text": text[max(0, text.lower().index(q) - 40):][:200]})
                    cites.append(Citation(artifact_id=m.id, path=m.path, page=i))
            if q in m.path.lower():
                hits.append({"artifact_id": m.id, "path": m.path, "filename": True, "text": m.path})
                cites.append(Citation(artifact_id=m.id, path=m.path))
        limit = max(1, min(int(limit), MAX_SEARCH_HITS))
        ids = sorted({h["artifact_id"] for h in hits[:limit]})
        return self._record("search_artifacts", args, ToolResult(
            data={"hits": hits[:limit]}, truncated=len(hits) > limit, citations=cites[:limit],
            provenance={"artifact_ids": ids, "hashes": [self._by_id[i].sha256 for i in ids]}))

    async def calculate(self, expression: str) -> ToolResult:
        args = (expression,)
        if (g := self._guard("calculate", args)) is not None:
            return g
        try:
            value = calc_mod.calculate(expression)
        except calc_mod.CalculationError as exc:
            return self._record("calculate", args, ToolResult.failure("BAD_INPUT", str(exc)))
        return self._record("calculate", args, ToolResult(data={"expression": expression, "value": str(value)}))

    async def compare_tables(self, spec: dict[str, Any]) -> ToolResult:
        args = (_hash(spec),)
        if (g := self._guard("compare_tables", args)) is not None:
            return g
        try:
            data = tables_mod.compare_tables(spec)
        except tables_mod.TableError as exc:
            return self._record("compare_tables", args, ToolResult.failure("BAD_INPUT", str(exc)))
        return self._record("compare_tables", args, ToolResult(data=data, truncated=bool(data.get("truncated"))))

    async def run_python(self, code: str, input_artifact_ids: list[str]) -> ToolResult:
        args = (_hash(code), tuple(input_artifact_ids))
        if (g := self._guard("run_python", args)) is not None:
            return g
        if not self.python_available:
            return self._record("run_python", args, ToolResult.failure(
                "TOOL_UNAVAILABLE", "run_python is not enabled for this run"))
        self._python_runs.append(code)
        return self._record("run_python", args, ToolResult(data={"stdout": "", "output_files": {}}))

    def record_proposal(self, kind: str, payload: dict[str, Any]) -> ToolResult:
        args = (kind, _hash(payload))
        if (g := self._guard("record_proposal", args)) is not None:
            return g
        if kind not in ("case", "assignment", "line", "assumption", "artifact_role", "question"):
            return self._record("record_proposal", args, ToolResult.failure("BAD_INPUT", f"unknown proposal kind {kind!r}"))
        self._proposals.append({"kind": kind, **payload})
        return self._record("record_proposal", args, ToolResult(data={"recorded": len(self._proposals)}))
