"""Phase 4b: robustness of the checks + the flag catalogue.

R1  every flag code the code can raise has words in the catalogue (a new
    flag without a title / meaning / what-to-do fails here, not on screen)
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.claims import profile
from app.main import app

CLAIMS = Path(__file__).resolve().parents[1] / "app" / "claims"


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
