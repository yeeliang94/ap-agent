"""File tools (H4): `list_artifacts` over the manifest and
`search_artifacts` over a lazily built text index of the snapshot
(workbook cell text, PDF page text, file names). Results carry the
artifact id and hash of every hit, and a Citation to the cell or page.
"""
from __future__ import annotations

from pathlib import Path

from ..investigator.contracts import Citation, ManifestEntry
from . import documents as docs_mod
from . import workbook as wb_mod

MAX_INDEX_ENTRIES_PER_FILE = 20000


def snapshot_path(files_dir: Path, rel: str) -> Path:
    """THE containment rule for a manifest path. Every place that turns a
    manifest entry into a filesystem path goes through here — the harness,
    the text index, the code audit — so the rule is one rule:

      * the path must resolve under ``files/`` (``..`` and absolute paths
        are refused), and
      * no component of it, the file included, may be a SYMLINK. The
        snapshot holds copies of the client's files and nothing else; a
        link is either an escape or a way to make the host open a file
        that is not in the manifest, and either way it is not a claim
        file. (Resolving alone is not enough: a link INTO the snapshot
        would still let one manifest entry stand for another's bytes.)

    Refusal is always the same PermissionError, and the file is never
    opened, stat'ed for size or hashed first."""
    root = Path(files_dir).resolve()
    p = root / str(rel)
    resolved = p.resolve()
    if root not in resolved.parents:
        raise PermissionError("path escapes the snapshot")
    # root is already resolved, so every component of the path AS WRITTEN
    # is checked below it — a link at any level is refused before anything
    # is opened. (Checking the RESOLVED path instead would see no link at
    # all, which is the whole point of following one.)
    walked = root
    try:
        parts = p.relative_to(root).parts
    except ValueError:  # not lexically under root; the resolve check above stands
        parts = ()
    for part in parts:
        walked = walked / part
        if walked.is_symlink():
            raise PermissionError("path is a symbolic link")
    return resolved


class TextIndex:
    """(artifact id, locator, text) triples per artifact, built on first
    use, one file at a time — never re-reads a file it has indexed."""

    def __init__(self, files_dir: Path, manifest: list[ManifestEntry]):
        self.files_dir = files_dir
        self.manifest = manifest
        self._entries: dict[str, list[tuple[dict, str]]] = {}
        self.failures: dict[str, str] = {}
        self.bytes_read = 0

    def has(self, artifact_id: str) -> bool:
        return artifact_id in self._entries

    def entries_for(self, m: ManifestEntry) -> list[tuple[dict, str]]:
        if m.id in self._entries:
            return self._entries[m.id]
        out: list[tuple[dict, str]] = [({"filename": True}, m.path)]
        try:
            path = snapshot_path(self.files_dir, m.path)
            if path.is_file():
                self.bytes_read += path.stat().st_size
                if m.media_type == "workbook":
                    for sheet, cell, text in wb_mod.iter_text(path):
                        out.append(({"sheet": sheet, "cell": cell}, text))
                        if len(out) > MAX_INDEX_ENTRIES_PER_FILE:
                            break
                elif m.media_type == "pdf":
                    for page, text in docs_mod.page_text(path):
                        out.append(({"page": page}, text))
        except Exception as exc:  # a broken file is indexed by name only, and said so
            self.failures[m.id] = f"{type(exc).__name__}"
        self._entries[m.id] = out
        return out


def list_artifacts(manifest: list[ManifestEntry], query: str = "", media_type: str = "",
                   limit: int = 500) -> tuple[list[dict], bool]:
    q = (query or "").lower().strip()
    hits = [m for m in manifest if (not q or q in m.path.lower()) and (not media_type or m.media_type == media_type)]
    data = [{"id": m.id, "path": m.path, "media_type": m.media_type, "size": m.size, "pages": m.pages,
             "sheets": list(m.sheets), "sha256": m.sha256} for m in hits[:limit]]
    return data, len(hits) > limit


def search_artifacts(index: TextIndex, query: str, limit: int,
                     artifact_ids: list[str] | None = None) -> tuple[list[dict], list[Citation], bool]:
    """Hits over the index; `artifact_ids` (when given) restricts the
    search to those artifacts — the audit scopes a name check to the
    case's own files — and nothing outside them is even indexed."""
    q = (query or "").lower().strip()
    hits: list[dict] = []
    cites: list[Citation] = []
    only = set(artifact_ids) if artifact_ids is not None else None
    for m in index.manifest:
        if only is not None and m.id not in only:
            continue
        for where, text in index.entries_for(m):
            low = text.lower()
            pos = low.find(q)
            if pos < 0:
                continue
            snippet = text[max(0, pos - 60): pos + len(q) + 140].replace("\n", " ")
            hits.append({"artifact_id": m.id, "path": m.path, **where, "text": snippet})
            cites.append(Citation(artifact_id=m.id, path=m.path, sheet=where.get("sheet", ""),
                                  cell=where.get("cell", ""), page=where.get("page")))
            if len(hits) > limit:
                break
        if len(hits) > limit:
            break
    return hits[:limit], cites[:limit], len(hits) > limit
