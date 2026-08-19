"""Binding the harness to an agent (H5): the allowlist as plain async
functions with typed arguments and one-paragraph docstrings — exactly what
pydantic-ai turns into tool definitions. Each function calls the harness
and returns the ToolResult as a plain dict; the harness does the guarding,
so the model sees the same budget/NOT_FOUND/BAD_INPUT answers as the
audit does. `run_python` is bound ONLY when the sandbox switch is on: off
means the tool does not exist for the model at all.
"""
from __future__ import annotations

from typing import Any

from .contracts import InvestigationTools


def bind_tools(tools: InvestigationTools, python_enabled: bool = False) -> list:
    async def list_artifacts(query: str = "", media_type: str = "", limit: int = 200) -> dict:
        """List the files of the batch (the immutable manifest). Filter by a
        path substring and/or media_type (workbook, pdf, image, other). Each
        entry has the artifact id every other tool needs, path, size, pages,
        sheet names and content hash."""
        return (await tools.list_artifacts(query, media_type, limit)).model_dump()

    async def inspect_workbook(artifact_id: str) -> dict:
        """The structure of one workbook: sheets with used range, cell and
        formula counts, hidden state, merged ranges. No values."""
        return (await tools.inspect_workbook(artifact_id)).model_dump()

    async def read_cells(artifact_id: str, sheet: str, cell_range: str) -> dict:
        """The exact saved values of a bounded range (like 'A1:H30') of one
        sheet, with the formula text where a cell holds one. Formulas are
        never computed. Read headers and totals with this."""
        return (await tools.read_cells(artifact_id, sheet, cell_range)).model_dump()

    async def inspect_document(artifact_id: str) -> dict:
        """A PDF's or image's page count, extracted text per page (when the
        PDF has a text layer), metadata, and counts of links / scripts /
        embedded files (never opened)."""
        return (await tools.inspect_document(artifact_id)).model_dump()

    async def render_page(artifact_id: str, page: int) -> dict:
        """Render one page (1-based) of a PDF or image to a PNG and return a
        handle plus its size — for a scanned page with no text layer."""
        return (await tools.render_page(artifact_id, page)).model_dump()

    async def crop_page(artifact_id: str, page: int, region: list[int]) -> dict:
        """A crop of one page at full resolution: region = [x0, y0, x1, y1]
        in the full render's pixels. Returns a handle."""
        return (await tools.crop_page(artifact_id, page, region)).model_dump()

    async def search_artifacts(query: str, limit: int = 50) -> dict:
        """Search names, workbook cell text and PDF text for a name, code,
        total or reference. Hits carry the artifact id and the cell or page."""
        return (await tools.search_artifacts(query, limit)).model_dump()

    async def calculate(expression: str) -> dict:
        """Exact decimal arithmetic: numbers, + - * /, parentheses, sum([...]),
        abs, min, max, round(x, places). Use it for every total."""
        return (await tools.calculate(expression)).model_dump()

    async def compare_tables(spec: dict[str, Any]) -> dict:
        """Deterministic sum / group / join / diff over small tables of row
        objects, e.g. {"op": "diff", "left": [...], "right": [...], "on": ["date", "amount"]}."""
        return (await tools.compare_tables(spec)).model_dump()

    async def run_python(code: str, input_artifact_ids: list[str]) -> dict:
        """Run read-only Python in an isolated sandbox over the named files
        (mounted read-only under their paths); print the result. No network,
        no writes outside the sandbox output folder, hard time/memory limits."""
        return (await tools.run_python(code, input_artifact_ids)).model_dump()

    def record_proposal(kind: str, payload: dict[str, Any]) -> dict:
        """Record an intermediate proposal for the audit (kind: case,
        assignment, line, assumption, artifact_role, question, flag). Nothing
        is written to the database; the final answer is what counts."""
        return tools.record_proposal(kind, payload).model_dump()

    bound = [list_artifacts, inspect_workbook, read_cells, inspect_document, render_page, crop_page,
             search_artifacts, calculate, compare_tables, record_proposal]
    if python_enabled:
        bound.append(run_python)
    return bound
