"""Post-inventory strategy (H5): the investigator never asks the caller
what shape the input is. It looks at the manifest and picks how to frame
the investigation — the same tools, the same audit, a different emphasis:

  structured     every file sits under a top-level folder and the folders
                 look like people (a name, an ER code): folder structure is
                 a strong grouping signal, proposed on that basis
  full_dump      files at the root, or folders that do not look like
                 people: identity must be found in the files themselves
  evidence_only  discovered AFTER the proposal: no case has a claim
                 summary — lines will be derived from evidence (H7)
"""
from __future__ import annotations

import math
import re
from pathlib import Path

from .. import survey as survey_mod
from .contracts import ManifestEntry

_PERSONISH = re.compile(r"^[A-Za-z][A-Za-z .'\-]{2,}(_\d+)?$")


def top_folder(path: str) -> str:
    return path.split("/", 1)[0] if "/" in path else ""


def folder_looks_like_a_person(name: str) -> bool:
    n = name.strip()
    if not n or n.lower() in ("maps", "receipts", "reports", "misc", "other", "scans", "images", "docs"):
        return False
    return bool(_PERSONISH.match(n)) or bool(survey_mod.er_code_of(n))


def choose_hint(manifest: list[ManifestEntry]) -> tuple[str, dict]:
    """('structured' | 'full_dump', facts) from the layout alone."""
    tops = {}
    root = 0
    for m in manifest:
        t = top_folder(m.path)
        if not t:
            root += 1
        else:
            tops[t] = tops.get(t, 0) + 1
    personish = [t for t in tops if folder_looks_like_a_person(t)]
    facts = {"root_files": root, "top_folders": len(tops), "person_like_folders": len(personish),
             "workbooks": sum(1 for m in manifest if m.media_type == "workbook"),
             "documents": sum(1 for m in manifest if m.media_type in ("pdf", "image")),
             "other": sum(1 for m in manifest if m.media_type == "other")}
    if tops and root == 0 and len(personish) >= max(1, math.ceil(0.8 * len(tops))):
        return "structured", facts
    return "full_dump", facts


def finalize(hint: str, cases) -> str:
    """The strategy as recorded, once the proposal is known."""
    if cases and all(getattr(c, "no_summary", False) or not getattr(c, "report_artifact_id", None) for c in cases):
        return "evidence_only"
    return hint


def describe(strategy: str, facts: dict) -> str:
    if strategy == "structured":
        return (f"Layout: {facts['top_folders']} top-level folder(s), {facts['person_like_folders']} named like a "
                "person, no files at the root. Folder structure is a STRONG grouping signal here: propose one "
                "case per person-folder (grouping_basis folder_structure) unless the files inside say otherwise, "
                "and still cite where each name / code is written.")
    return (f"Layout: {facts['root_files']} file(s) at the root, {facts['top_folders']} folder(s). Do NOT assume a "
            "folder is a person. Find identity in the files themselves — names and codes in report headers, "
            "file names, approval e-mails, cardholder lines — and cite each. Similar dates, merchants, amounts "
            "or nearby files never prove ownership. Leave a file unassigned rather than guess.")
