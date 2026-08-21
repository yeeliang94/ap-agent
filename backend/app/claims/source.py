"""Bringing a claims batch into the run's own workspace.

Three ways in, one result. Uploaded files (the primary way: a folder
picked in the browser, a zip, or loose files) are laid out with their
relative paths kept; a SharePoint folder link is walked (the folder and
its subfolders, up to three levels deep) and every file is downloaded; a
zip is unpacked with its folder tree kept. Either way
the run ends up with a private copy under runs/<id>/claims/files/,
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
# A relative path longer than this is a crafted input, not a batch: real
# paths are a few folder names and a file name. Checked BEFORE wrapper
# detection walks the paths.
MAX_PATH_CHARS = 1000
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


def _retry(what: str, fn, on_retry=None):
    """Call fn() up to RETRIES times, retrying on a source failure.

    on_retry(what, attempt, total, error) is told about every attempt that
    is going to be tried again, so a run that is sitting out a throttled
    gateway can say so instead of looking frozen. `what` is this call's
    own description, so listing and downloading need no separate shapes.
    """
    last: Exception | None = None
    for attempt in range(1, RETRIES + 1):
        try:
            return fn()
        except SourceUnavailable as exc:
            last = exc
            log.warning("%s attempt %d failed: %s", what, attempt, exc)
            if attempt < RETRIES:
                if on_retry:
                    on_retry(what, attempt, RETRIES, exc)
                time.sleep(0.2 * attempt)
    raise SourceUnavailable(f"{what} failed after {RETRIES} attempts: {last}")


def walk_folder(source, folder_url: str, on_retry=None) -> list[dict]:
    """Every entry under the batch folder, up to MAX_DEPTH levels deep.

    Returns entries as the source lists them ({name, kind, size, id, path})
    with `depth` added (1 = directly under the batch folder). Folder-count
    and file-count quotas are enforced from the listing alone, before a
    single byte is downloaded.
    """
    entries: list[dict] = []

    def visit(rel: str, depth: int) -> None:
        listing = _retry(f"listing {rel or 'the batch folder'}",
                         lambda: source.list_folder(folder_url, rel),
                         on_retry)
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


def _check_raw_paths(rels) -> None:
    """The cheapest checks, on the RAW paths before wrapper detection or
    listing building walks them: a run's worth of files at most, and no
    absurdly long path (a crafted zip can name a file in 65 KB of nested
    folders; nothing legitimate comes close)."""
    n = 0
    for rel in rels:
        n += 1
        if len(rel) > MAX_PATH_CHARS:
            raise QuotaExceeded(f"A path in the batch is {len(rel)} characters long — over the "
                                f"{MAX_PATH_CHARS} character limit ({rel[:80]!r}…).")
    if n > MAX_TOTAL_FILES:
        raise QuotaExceeded(f"The batch holds {n} files — more than the "
                            f"{MAX_TOTAL_FILES} files a run may have.")


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
                 on_progress=None, on_retry=None) -> list[dict]:
    """Copy every file entry into dest, keeping the folder tree.

    Returns the file entries with `local` (path under dest) added. A file
    over the size limit is refused here too, for sources whose listing
    does not report sizes.

    on_progress(done, total, current) names the file being fetched, and is
    called once more at the end with current=None. on_retry has _retry's
    shape.
    """
    files = [e for e in entries if e["kind"] == "file"]
    done = []
    for i, entry in enumerate(files, 1):
        if on_progress:
            on_progress(i - 1, len(files), entry["path"])
        data = _retry(f"downloading {entry['path']}",
                      lambda e=entry: source.download(folder_url, e),
                      on_retry)
        if len(data) > MAX_FILE_MB * 1024 * 1024:
            raise QuotaExceeded(
                f"{entry['path']} is {len(data) / 1024 / 1024:.0f} MB — over the "
                f"{MAX_FILE_MB} MB limit per file.")
        target = _safe_join(dest, entry["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        done.append({**entry, "size": len(data), "local": entry["path"]})
    if on_progress:
        on_progress(len(files), len(files), None)   # done: no file in hand
    return done


def _listing_entries(files: list[tuple[str, int | None, int]]) -> list[dict]:
    """Folder and file entries in the walker's shape, derived from
    (relative path, size, depth) triples. Folder entries come from the
    file paths, so the same quota check serves every way in — the zip
    and the uploaded set share this."""
    entries: list[dict] = []
    folders: set[tuple[str, int]] = set()
    for rel, _size, n in files:
        parts = rel.split("/")
        for depth in range(1, n):
            folders.add(("/".join(parts[:depth]), depth))
    for path_str, depth in sorted(folders):
        entries.append({"name": path_str.rsplit("/", 1)[-1], "kind": "folder",
                        "size": None, "id": path_str, "path": path_str, "depth": depth})
    for rel, size, n in files:
        entries.append({"name": rel.rsplit("/", 1)[-1], "kind": "file",
                        "size": size, "id": rel, "path": rel, "depth": n})
    return entries


def _with_local(entries: list[dict]) -> list[dict]:
    """The ingestion result shape: files first (each knowing its local
    copy's path), folders after."""
    return [{**e, "local": e["path"]} for e in entries if e["kind"] == "file"] + \
        [e for e in entries if e["kind"] == "folder"]


def unpack_zip(zip_path: Path, dest: Path) -> list[dict]:
    """The local-development way in: a zip of the batch folder tree.

    Same quotas as the SharePoint walk, checked BEFORE each entry is
    decompressed (a zip bomb stops here, not in memory). Returns file
    entries in the walker's shape. Wrapper folders around the batch
    ("batch/Aegene Ong_1/...") are peeled off (strip_wrapper_roots), so
    zipping the batch folder itself or its contents both work.
    """
    if zip_path.stat().st_size > MAX_ZIP_MB * 1024 * 1024:
        raise QuotaExceeded(f"The zip is over the {MAX_ZIP_MB} MB limit.")
    entries: list[dict] = []
    try:
        with zipfile.ZipFile(zip_path) as z:
            infos = [i for i in z.infolist()
                     if not i.is_dir() and not Path(i.filename).name.startswith(".")
                     and "__MACOSX" not in i.filename]
            _check_raw_paths(i.filename for i in infos)
            prefix = strip_wrapper_roots([i.filename for i in infos])
            listing = []
            for info in infos:
                rel = info.filename[len(prefix):] if prefix else info.filename
                parts = [p for p in rel.split("/") if p and p not in ("..",)]
                if not parts:
                    continue
                if len(parts) > MAX_DEPTH + 1:
                    parts = parts[:MAX_DEPTH] + ["/".join(parts[MAX_DEPTH:])]
                listing.append((info, "/".join(parts), len(parts)))
            entries = _listing_entries([(rel, info.file_size, n) for info, rel, n in listing])
            _check_listing_quotas(entries)
            run_limit = MAX_TOTAL_MB * 1024 * 1024
            run_written = 0
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
                        run_written += len(chunk)
                        if written > limit:
                            out.close()
                            target.unlink(missing_ok=True)
                            raise QuotaExceeded(f"{rel} unpacks to more than the {MAX_FILE_MB} MB "
                                                "limit per file (its header said less).")
                        if run_written > run_limit:
                            out.close()
                            target.unlink(missing_ok=True)
                            raise QuotaExceeded(f"The zip's files unpack to more than the {MAX_TOTAL_MB} MB "
                                                "limit for a run (their headers said less).")
                        out.write(chunk)
    except zipfile.BadZipFile as exc:
        raise SourceUnavailable("The uploaded file is not a valid zip.") from exc
    return _with_local(entries)


def ingest_uploaded(staged: Path, dest: Path) -> list[dict]:
    """The uploads-first way in: files the reviewer uploaded, already laid
    out under the run's staging folder (routes wrote them there, one per
    relative path). Same quotas and the same entry shape as the SharePoint
    walk and the zip, checked BEFORE anything is copied into the run's
    read-only snapshot. Sizes come from the filesystem, so the check is
    exact. Wrapper folders around the batch are peeled off
    (strip_wrapper_roots), so uploading the batch folder itself or its
    contents both work.
    """
    paths = [p for p in sorted(staged.rglob("*"))
             if p.is_file() and not p.name.startswith(".")]
    _check_raw_paths(p.relative_to(staged).as_posix() for p in paths)
    prefix = strip_wrapper_roots([p.relative_to(staged).as_posix() for p in paths])
    listing: list[tuple[Path, str, int]] = []   # (source file, rel path, depth)
    for path in paths:
        rel = path.relative_to(staged).as_posix()[len(prefix):]
        parts = rel.split("/")
        if len(parts) > MAX_DEPTH + 1:
            # The zip walker's rule: anything deeper counts as one level.
            parts = parts[:MAX_DEPTH] + ["/".join(parts[MAX_DEPTH:])]
        listing.append((path, "/".join(parts), len(parts)))
    entries = _listing_entries([(rel, src.stat().st_size, n) for src, rel, n in listing])
    _check_listing_quotas(entries)
    for src, rel, _n in listing:
        target = _safe_join(dest, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(src.read_bytes())
    return _with_local(entries)


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


# How many wrapper levels strip_wrapper_roots will ever peel. Real
# wrappers are one or two folders deep; the cap also bounds the work a
# crafted deeply-nested path can cause.
MAX_WRAPPER_LEVELS = 10

# 'Aug 2026', 'July', 'Q1', '2026': a time bucket, never a person.
_PERIOD_WORDS = {"jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "sept",
                 "oct", "nov", "dec", "january", "february", "march", "april", "june",
                 "july", "august", "september", "october", "november", "december",
                 "q1", "q2", "q3", "q4"}


def strip_wrapper_roots(rels: list[str]) -> str:
    """The prefix of wrapper folders to peel off the batch ('' if none).

    A reviewer often uploads (or zips) the folder AROUND the employee
    folders rather than its contents. As long as every path sits inside
    one common folder, that folder MAY be a wrapper — but it is only
    peeled on affirmative evidence, never on a guess:

    - a sole folder holding only loose files is never peeled — that is
      one employee's folder, the batch itself;
    - a folder titled with document or period words ('Claims', 'batch',
      'Aug 2026') is always a wrapper;
    - a folder named like a person ('Aegene Ong') is never a wrapper;
    - anything ambiguous ('Emp B1 Test', 'EMP001') is a wrapper only when
      what it holds affirms a batch: at least one subfolder that is NOT a
      category of a single employee ('Receipts', 'July' do not count —
      person-named or coded subfolders do).

    Loops, so a double wrapper ('Claims/Aug 2026/batch/…') unwraps too,
    capped at MAX_WRAPPER_LEVELS.
    """
    # Imported here, not at the top: strategies imports survey, which
    # imports this module.
    from .grouping import _fold, _is_document_title
    from .investigator.strategies import folder_looks_like_a_person

    def is_category(name: str) -> bool:
        tokens = _fold(name).split()
        return _is_document_title(name) or (
            bool(tokens) and all(t in _PERIOD_WORDS or t.isdigit() for t in tokens))

    prefix = ""
    current = list(rels)
    for _ in range(MAX_WRAPPER_LEVELS):
        root = _common_root(current)
        if not root:
            break
        inner = [r[len(root):] for r in current]
        if not any("/" in r for r in inner):
            break
        name = root[:-1]
        if is_category(name):
            pass
        elif folder_looks_like_a_person(name):
            break
        else:
            tops = {r.split("/", 1)[0] for r in inner if "/" in r}
            if all(is_category(t) for t in tops):
                break
        prefix += root
        current = inner
    return prefix


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
