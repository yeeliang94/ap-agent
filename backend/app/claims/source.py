"""Bringing a claims batch into the run's own workspace.

Two ways in, one result. A SharePoint folder link is walked (the folder
and its subfolders, up to three levels deep) and every file is downloaded;
a zip (local development) is unpacked with its folder tree kept. Either
way the run ends up with a private copy under runs/<id>/claims/files/,
laid out exactly as the batch was, and from then on the run reads only
that copy — the run is judged against the files as they were when it
started, and SharePoint is never asked twice.

Quotas are enforced here, BEFORE any AI call, and a refusal names the
quota it hit. Since hardening H3 the ingestion quotas are RUN-WIDE — a
batch is a folder of files, structured or a flat dump, and an employee
folder is no longer the unit: 1500 files, 1500 MB and 6000 pages per run,
25 MB per file, three levels deep. The per-CASE budgets (60 files, 200
pages) apply after grouping, to each Claim Case as it is verified
(case_budget_problems); a case over one is failed with the reason and the
rest of the run carries on. MAX_CASES_PER_RUN caps what one run may
verify (30, the delivered 30-employee-folder default) at confirm time.
"""
from __future__ import annotations

import logging
import time
import zipfile
from pathlib import Path

from ..docsource import SourceUnavailable

log = logging.getLogger("claims.source")

MAX_DEPTH = 3
MAX_FILE_MB = 25
MAX_ZIP_MB = 200
# Run-wide caps (H3): a flat dump has no employee folders to count by.
MAX_TOTAL_FILES = 1500
MAX_TOTAL_MB = 1500      # every file's uncompressed size, added up
MAX_TOTAL_PAGES = 6000   # 30 cases × 200 pages, as one number
# Post-grouping budgets, per Claim Case (applied when a case is verified,
# and at confirm time for the case count).
MAX_CASES_PER_RUN = 30
MAX_FILES_PER_CASE = 60
MAX_PAGES_PER_CASE = 200

# Every transient source failure is retried this many times. The REST
# fake retries inside itself already; the real MCP source and the walk
# as a whole get the same courtesy here.
RETRIES = 3

# File types the batch may contain and the app knows how to read.
# Anything else is still copied and listed (nothing uploaded vanishes
# silently) — the map will show it as unplaced.
READABLE = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".xlsx", ".xlsm"}


class QuotaExceeded(Exception):
    """A batch is over one of the confirmed limits; the message names it."""


def _retry(what: str, fn):
    """Call fn() up to RETRIES times, retrying on a source failure."""
    last: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            return fn()
        except SourceUnavailable as exc:
            last = exc
            log.warning("%s attempt %d failed: %s", what, attempt, exc)
            if attempt < RETRIES:
                time.sleep(0.2 * attempt)
    raise SourceUnavailable(f"{what} failed after {RETRIES} attempts: {last}")


def walk_folder(source, folder_url: str) -> list[dict]:
    """Every entry under the batch folder, up to MAX_DEPTH levels deep.

    Returns entries as the source lists them ({name, kind, size, id, path})
    with `depth` added (1 = directly under the batch folder). Folder-count
    and file-count quotas are enforced from the listing alone, before a
    single byte is downloaded.
    """
    entries: list[dict] = []

    def visit(rel: str, depth: int) -> None:
        listing = _retry(f"listing {rel or 'the batch folder'}",
                         lambda: source.list_folder(folder_url, rel))
        for entry in listing:
            entry = {**entry, "depth": depth}
            entries.append(entry)
            # A folder up to MAX_DEPTH levels down is opened; its files
            # are listed. Anything deeper is left where it is.
            if entry["kind"] == "folder" and depth <= MAX_DEPTH:
                visit(entry["path"], depth + 1)

    visit("", 1)
    _check_listing_quotas(entries)
    return entries


def _check_listing_quotas(entries: list[dict]) -> None:
    """The run-wide file quotas, from the listing alone (before a byte is
    downloaded). Per-case budgets are a later, per-case matter."""
    total_files, total_bytes = 0, 0
    for e in entries:
        if e["kind"] != "file":
            continue
        total_files += 1
        total_bytes += int(e.get("size") or 0)
        if e.get("size") and e["size"] > MAX_FILE_MB * 1024 * 1024:
            raise QuotaExceeded(
                f"{e['path']} is {e['size'] / 1024 / 1024:.0f} MB — over the "
                f"{MAX_FILE_MB} MB limit per file.")
    if total_files > MAX_TOTAL_FILES:
        raise QuotaExceeded(f"The batch holds {total_files} files — more than the "
                            f"{MAX_TOTAL_FILES} files a run may have.")
    if total_bytes > MAX_TOTAL_MB * 1024 * 1024:
        raise QuotaExceeded(f"The batch's files add up to {total_bytes / 1024 / 1024:.0f} MB — over the "
                            f"{MAX_TOTAL_MB} MB limit for a run.")


def download_all(source, folder_url: str, entries: list[dict], dest: Path,
                 on_progress=None) -> list[dict]:
    """Copy every file entry into dest, keeping the folder tree.

    Returns the file entries with `local` (path under dest) added. A file
    over the size limit is refused here too, for sources whose listing
    does not report sizes.
    """
    files = [e for e in entries if e["kind"] == "file"]
    done = []
    for i, entry in enumerate(files, 1):
        data = _retry(f"downloading {entry['path']}",
                      lambda e=entry: source.download(folder_url, e))
        if len(data) > MAX_FILE_MB * 1024 * 1024:
            raise QuotaExceeded(
                f"{entry['path']} is {len(data) / 1024 / 1024:.0f} MB — over the "
                f"{MAX_FILE_MB} MB limit per file.")
        target = _safe_join(dest, entry["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        done.append({**entry, "size": len(data), "local": entry["path"]})
        if on_progress:
            on_progress(i, len(files))
    return done


def unpack_zip(zip_path: Path, dest: Path) -> list[dict]:
    """The local-development way in: a zip of the batch folder tree.

    Same quotas as the SharePoint walk, checked BEFORE each entry is
    decompressed (a zip bomb stops here, not in memory). Returns file
    entries in the walker's shape. A zip whose every path starts with one
    common folder ("batch/Aegene Ong_1/...") has that folder stripped, so
    zipping the folder itself or its contents both work.
    """
    if zip_path.stat().st_size > MAX_ZIP_MB * 1024 * 1024:
        raise QuotaExceeded(f"The zip is over the {MAX_ZIP_MB} MB limit.")
    entries: list[dict] = []
    try:
        with zipfile.ZipFile(zip_path) as z:
            infos = [i for i in z.infolist()
                     if not i.is_dir() and not Path(i.filename).name.startswith(".")
                     and "__MACOSX" not in i.filename]
            prefix = _common_root(i.filename for i in infos)
            listing = []
            for info in infos:
                rel = info.filename[len(prefix):] if prefix else info.filename
                parts = [p for p in rel.split("/") if p and p not in ("..",)]
                if not parts:
                    continue
                if len(parts) > MAX_DEPTH + 1:
                    parts = parts[:MAX_DEPTH] + ["/".join(parts[MAX_DEPTH:])]
                listing.append((info, "/".join(parts), len(parts)))
            # Folder entries, derived from the file paths, so the same
            # quota check serves both ways in.
            folders: set[tuple[str, int]] = set()
            for _info, rel, n in listing:
                parts = rel.split("/")
                for depth in range(1, n):
                    folders.add(("/".join(parts[:depth]), depth))
            for path, depth in sorted(folders):
                entries.append({"name": path.rsplit("/", 1)[-1], "kind": "folder",
                                "size": None, "id": path, "path": path, "depth": depth})
            for info, rel, n in listing:
                entries.append({"name": rel.rsplit("/", 1)[-1], "kind": "file",
                                "size": info.file_size, "id": rel, "path": rel, "depth": n})
            _check_listing_quotas(entries)
            for info, rel, _n in listing:
                target = _safe_join(dest, rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                # Streamed with a hard stop: the header's declared size was
                # checked above, but the bytes that actually come out are
                # what count.
                limit = MAX_FILE_MB * 1024 * 1024
                written = 0
                with z.open(info) as src, open(target, "wb") as out:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > limit:
                            out.close()
                            target.unlink(missing_ok=True)
                            raise QuotaExceeded(f"{rel} unpacks to more than the {MAX_FILE_MB} MB "
                                                "limit per file (its header said less).")
                        out.write(chunk)
    except zipfile.BadZipFile as exc:
        raise SourceUnavailable("The uploaded file is not a valid zip.") from exc
    return [{**e, "local": e["path"]} for e in entries if e["kind"] == "file"] + \
        [e for e in entries if e["kind"] == "folder"]


def _common_root(names) -> str:
    """'batch/' when every path starts with 'batch/', else ''."""
    names = list(names)
    if not names:
        return ""
    first = names[0].split("/", 1)
    if len(first) < 2:
        return ""
    root = first[0] + "/"
    return root if all(n.startswith(root) for n in names) else ""


def _safe_join(dest: Path, rel: str) -> Path:
    """dest/rel, refusing anything that would escape dest."""
    target = (dest / rel).resolve()
    if dest.resolve() != target and dest.resolve() not in target.parents:
        raise SourceUnavailable(f"Refusing path {rel!r}: it points outside the workspace.")
    return target


def page_count(path: Path) -> int | None:
    """Pages in a PDF; 1 for an image; None for anything else."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        import pymupdf

        try:
            with pymupdf.open(path) as pdf:
                return pdf.page_count
        except Exception:
            return None
    if suffix in (".png", ".jpg", ".jpeg", ".webp"):
        return 1
    return None


def check_page_quotas(files: list[dict]) -> None:
    """The run-wide page quota, once page counts are known."""
    n = sum(int(f.get("pages") or 0) for f in files)
    if n > MAX_TOTAL_PAGES:
        raise QuotaExceeded(f"The batch holds {n} pages — more than the {MAX_TOTAL_PAGES} pages a run may have.")


def case_budget_problems(n_files: int, n_pages: int, label: str = "this case") -> str:
    """The per-case budget (H3), as a sentence when it is exceeded, else ""."""
    if n_files > MAX_FILES_PER_CASE:
        return (f"{label} has {n_files} files to read — more than the {MAX_FILES_PER_CASE} files one case "
                "may have. Split it into two cases at the map, or leave some files out, then re-verify.")
    if n_pages > MAX_PAGES_PER_CASE:
        return (f"{label} has {n_pages} pages to read — more than the {MAX_PAGES_PER_CASE} pages one case "
                "may have. Split it into two cases at the map, or leave some files out, then re-verify.")
    return ""
