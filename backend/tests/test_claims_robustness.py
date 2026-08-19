"""Phase 4b: robustness of the checks + the flag catalogue.

R1  every flag code the code can raise has words in the catalogue (a new
    flag without a title / meaning / what-to-do fails here, not on screen)
R2  a report whose lines do not add up to its total cell is KEPT and
    flagged (REPORT_TOTAL_MISMATCH), not thrown away; a workbook whose
    formulas were never computed is named as such
"""
from __future__ import annotations

import asyncio
import re
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from app.claims import profile, report_reader, worker
from app.claims.report_reader import ReportColumns, ReportReading
from app.main import app

CLAIMS = Path(__file__).resolve().parents[1] / "app" / "claims"


class _Scripted:
    """An 'agent' that answers from a list, in order (like test_claims_checks)."""

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.prompts = []

    async def run(self, prompt, **kw):
        self.prompts.append(prompt)

        class R:
            output = self._outputs.pop(0)

            def usage(self):
                class U:
                    total_tokens = 10
                    requests = 1
                return U()
        return R()


def _report_sheet(lines, total):
    """A tiny expense report tab: header block, headings on row 6, lines
    from row 7, the typed total two rows under the last line."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Expense Report"
    ws["A1"], ws["B1"] = "Name:", "Aegene Ong"
    ws["A2"], ws["B2"] = "Period:", "01JUL26-21JUL26"
    ws["A3"], ws["B3"] = "Purpose:", "Client visits"
    for c, h in zip("ABCDEFGH", ["Date", "Expense Item", "Reason", "Receipt?", "Amount", "Curr", "Rate", "Total (MYR)"]):
        ws[f"{c}6"] = h
    r = 7
    for d, item, amount in lines:
        ws[f"A{r}"], ws[f"B{r}"], ws[f"C{r}"], ws[f"D{r}"] = d, item, "x", "Y"
        ws[f"E{r}"], ws[f"F{r}"], ws[f"G{r}"], ws[f"H{r}"] = amount, "MYR", 1, amount
        r += 1
    ws[f"G{r + 1}"], ws[f"H{r + 1}"] = "Total (MYR)", total
    return wb, ws, r - 1, r + 1


def _reading(last_row, total_row, **over):
    base = dict(columns=ReportColumns(date="A", item="B", reason="C", receipt_included="D", amount="E",
                                      currency="F", rate="G", total="H"),
                header_row=6, first_row=7, last_row=last_row, total_cell=f"H{total_row}", name_cell="B1",
                period_cell="B2", purpose_cell="B3", why="header block, dated lines, total")
    return ReportReading(**{**base, **over})


def _codes_in_source() -> set[str]:
    """Every literal flag code in the claims package: `_flag("CODE"`,
    `code="CODE"`, or `checks_mod._flag("CODE"`."""
    found: set[str] = set()
    for path in CLAIMS.glob("*.py"):
        text = path.read_text()
        found |= set(re.findall(r'_flag\(\s*"([A-Z][A-Z_]+)"', text))
        found |= set(re.findall(r'code="([A-Z][A-Z_]+)"', text))
    return found


# ---- R1: the catalogue ------------------------------------------------------------

def test_every_raised_code_is_catalogued():
    raised = _codes_in_source()
    assert raised, "the scan found no codes — the regex is broken"
    missing = sorted(raised - set(profile.CATALOGUE))
    assert not missing, f"flag codes with no catalogue words: {missing}"


def test_catalogue_entries_are_complete_and_shaped():
    for code, entry in profile.CATALOGUE.items():
        for key in ("title", "meaning", "what_to_do", "kind", "blocks"):
            assert entry.get(key), f"{code}: {key} missing"
        assert entry["kind"] in profile.FLAG_KINDS, code
        assert entry["blocks"] in ("open", "info"), code
        assert "_" not in entry["title"], f"{code}: title looks like a code"
    # run-level controls cannot be switched off; everything else can
    assert "MISSING_REFERENCE" not in profile.CHECK_CODES
    assert "NO_RECEIPT" in profile.CHECK_CODES
    assert profile.describe("NOT_A_CODE")["title"] == "Not a code"


def test_catalogue_endpoint_and_run_detail_carry_it(monkeypatch, tmp_path):
    client = TestClient(app)
    r = client.get("/api/claims-settings/catalogue")
    assert r.status_code == 200
    body = r.json()
    assert set(body["codes"]) == set(profile.CATALOGUE)
    assert body["codes"]["NO_RECEIPT"]["title"] == "No receipt for this row"
    assert body["kinds"] == list(profile.FLAG_KINDS)
    assert "MISSING_REFERENCE" not in body["toggleable"]


# ---- R2: a wrong total is a flag, not a lost report ---------------------------

LINES = [(date(2026, 7, 1), "Taxi (713070)", 45.00), (date(2026, 7, 3), "Coffee (713070)", 18.90),
         (date(2026, 7, 5), "Toll (713070)", 10.00)]  # sum 73.90


def test_total_typo_keeps_the_lines_and_marks_the_mismatch(monkeypatch):
    wb, ws, last, total_row = _report_sheet(LINES, 74.00)  # typed 10 cents high
    agent = _Scripted([_reading(last, total_row), _reading(last, total_row)])
    monkeypatch.setattr(report_reader, "create_agent", lambda *a, **k: agent)
    rows, header, notes = asyncio.run(report_reader.read_report(ws, "Aegene Ong", "ER(01JUL26-21JUL26)"))
    # fed back once (a missed line is the other cause), then the same
    # reading again is accepted — the lines are kept
    assert len(agent.prompts) == 2 and "answer the same reading again" in agent.prompts[1]
    assert len(rows) == 3
    assert header["total"] == "74.00" and header["total_cell"] == f"H{total_row}"
    assert header["total_check"] == {"lines": "73.90", "cell": "74.00", "column": "total"}
    assert any("same reading twice" in t for _, t in notes)
    # the worker turns it into a flag citing the total cell's row
    flags = worker._report_total_flags(header, ws.title)
    assert len(flags) == 1 and flags[0]["code"] == "REPORT_TOTAL_MISMATCH"
    assert flags[0]["cite"] == {"sheet": "Expense Report", "row": total_row}
    assert "73.90" in flags[0]["reason"] and "74.00" in flags[0]["reason"]


def test_a_correct_total_raises_nothing_and_a_moved_span_is_still_audited(monkeypatch):
    wb, ws, last, total_row = _report_sheet(LINES, 73.90)
    agent = _Scripted([_reading(last, total_row)])
    monkeypatch.setattr(report_reader, "create_agent", lambda *a, **k: agent)
    rows, header, _ = asyncio.run(report_reader.read_report(ws, "Aegene Ong", "ER(01JUL26-21JUL26)"))
    assert len(rows) == 3 and header["total_check"] is None and len(agent.prompts) == 1
    assert worker._report_total_flags(header, ws.title) == []
    # a span that misses the last line: fed back; the AI extends it; accepted with no mismatch
    agent = _Scripted([_reading(last - 1, total_row), _reading(last, total_row)])
    monkeypatch.setattr(report_reader, "create_agent", lambda *a, **k: agent)
    rows, header, _ = asyncio.run(report_reader.read_report(ws, "Aegene Ong", "ER(01JUL26-21JUL26)"))
    assert len(rows) == 3 and header["total_check"] is None and len(agent.prompts) == 2


def test_total_mismatch_with_other_structural_problems_stays_structural(monkeypatch):
    # a reading whose amount column is wrong AND whose sum is off: three
    # rounds of the same wrong answer → unreadable, not "accepted with a note"
    wb, ws, last, total_row = _report_sheet(LINES, 74.00)
    wrong = _reading(last, total_row, columns=ReportColumns(date="A", item="B", amount="C", total="H"))
    agent = _Scripted([wrong, wrong, wrong])
    monkeypatch.setattr(report_reader, "create_agent", lambda *a, **k: agent)
    with pytest.raises(report_reader.ReportUnreadable):
        asyncio.run(report_reader.read_report(ws, "Aegene Ong", "ER(01JUL26-21JUL26)"))


def test_uncomputed_formulas_are_named(tmp_path):
    wb, ws, last, total_row = _report_sheet(LINES, 0)
    ws[f"H{total_row}"] = f"=SUM(H7:H{last})"  # openpyxl saves the formula, no cached value
    path = tmp_path / "r.xlsx"
    wb.save(path)
    assert report_reader.uncomputed_formulas(path, "Expense Report") == 1
    assert report_reader.uncomputed_formulas(path, "No such tab") == 0
    # a values-only workbook has none
    wb2, _, _, _ = _report_sheet(LINES, 73.90)
    wb2.save(tmp_path / "v.xlsx")
    assert report_reader.uncomputed_formulas(tmp_path / "v.xlsx", "Expense Report") == 0
    # and data_only really does read the formula cell as empty (the reason this exists)
    assert load_workbook(path, data_only=True)["Expense Report"][f"H{total_row}"].value is None


def test_a_missed_line_is_told_apart_from_a_typo(monkeypatch):
    # the same three lines, correct total, but the reading drops the last
    # line: that is a short span, not a typo — structural, never "accepted
    # with a mismatch note", even when the AI insists three times
    wb, ws, last, total_row = _report_sheet(LINES, 73.90)
    short = _reading(last - 1, total_row)
    agent = _Scripted([short, short, short])
    monkeypatch.setattr(report_reader, "create_agent", lambda *a, **k: agent)
    with pytest.raises(report_reader.ReportUnreadable) as exc:
        asyncio.run(report_reader.read_report(ws, "Aegene Ong", "ER(01JUL26-21JUL26)"))
    assert "outside" in str(exc.value)


# ---- R3: rows the reader may skip ------------------------------------------------

def test_subtotal_row_inside_the_span_can_be_skipped(monkeypatch):
    wb, ws, last, total_row = _report_sheet(LINES, 73.90)
    # push a "Meals subtotal" row in between lines 2 and 3
    ws.insert_rows(9)
    ws["B9"], ws["H9"] = "Meals subtotal", 63.90  # no date: a subtotal, not a line
    last, total_row = last + 1, total_row + 1
    ws[f"H{total_row}"] = 73.90
    naive = _reading(last, total_row)                      # sum includes the subtotal → 137.80
    with_skip = _reading(last, total_row, skip_rows=[9])   # subtotal left out → 73.90
    agent = _Scripted([naive, with_skip])
    monkeypatch.setattr(report_reader, "create_agent", lambda *a, **k: agent)
    rows, header, _ = asyncio.run(report_reader.read_report(ws, "Aegene Ong", "ER(01JUL26-21JUL26)"))
    assert len(agent.prompts) == 2
    assert [r["row"] for r in rows] == [7, 8, 10] and header["total_check"] is None
    # skipping a REAL line is refused
    bad = _reading(last, total_row, skip_rows=[9, 10])
    agent = _Scripted([bad, bad, bad])
    monkeypatch.setattr(report_reader, "create_agent", lambda *a, **k: agent)
    with pytest.raises(report_reader.ReportUnreadable) as exc:
        asyncio.run(report_reader.read_report(ws, "Aegene Ong", "ER(01JUL26-21JUL26)"))
    assert "row 10 is in skip_rows" in str(exc.value)
