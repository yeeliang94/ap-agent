"""The claims batch walker: nested folders, retries, quotas, zip unpacking.

Runs against the fake MCP server over the real protocol (server_url from
conftest) and against local folders. No AI anywhere here.
"""
from __future__ import annotations

import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import docsource
from app.claims import source as batch_source
from app.docsource import LocalFolderSource, RealMcpSource, SourceUnavailable

FOLDER = "https://example.sharepoint.com/sites/x/Shared%20Documents/Claims/JUL26"
GEN = Path(__file__).resolve().parents[2] / "samples" / "generated" / "claims"


def _tree(root: Path, folders: int, files_per: int = 2, depth_extra: bool = True) -> None:
    for i in range(folders):
        d = root / f"Person {i}_{i}"
        d.mkdir(parents=True)
        for j in range(files_per):
            (d / f"file{j}.pdf").write_bytes(b"%PDF-1.4 stub")
        if depth_extra and i == 0:
            (d / "Maps").mkdir()
            (d / "Maps" / "route.png").write_bytes(b"\x89PNG stub")
            (d / "Maps" / "deeper").mkdir()
            (d / "Maps" / "deeper" / "too_deep").mkdir()
            (d / "Maps" / "deeper" / "x.pdf").write_bytes(b"pdf")
            (d / "Maps" / "deeper" / "too_deep" / "y.pdf").write_bytes(b"pdf")


def test_local_walk_lists_subfolders_to_depth_three(tmp_path):
    _tree(tmp_path, 3)
    entries = batch_source.walk_folder(LocalFolderSource(), str(tmp_path))
    paths = {e["path"] for e in entries}
    assert "Person 0_0/file0.pdf" in paths
    assert "Person 0_0/Maps/route.png" in paths            # depth 2
    assert "Person 0_0/Maps/deeper/x.pdf" in paths         # depth 3
    assert "Person 0_0/Maps/deeper/too_deep/y.pdf" not in paths  # depth 4: not walked
    assert {e["kind"] for e in entries} == {"file", "folder"}


def test_quotas_are_run_wide_not_per_employee_folder(tmp_path, monkeypatch):
    """H3: a batch is a folder of files; 31 subfolders or 61 files in one
    folder are fine at ingestion. The run-wide caps still refuse and name
    themselves; the per-case budget applies later, per case."""
    _tree(tmp_path, 31, files_per=1, depth_extra=False)
    assert sum(1 for e in batch_source.walk_folder(LocalFolderSource(), str(tmp_path)) if e["kind"] == "file") == 31
    _tree(tmp_path / "wide", 1, files_per=61, depth_extra=False)
    assert batch_source.walk_folder(LocalFolderSource(), str(tmp_path / "wide"))
    monkeypatch.setattr(batch_source, "MAX_TOTAL_FILES", 10)
    with pytest.raises(batch_source.QuotaExceeded) as exc:
        batch_source.walk_folder(LocalFolderSource(), str(tmp_path))
    assert "10 files a run may have" in str(exc.value)
    assert "61 files" in batch_source.case_budget_problems(61, 10, "Case A")
    assert "201 pages" in batch_source.case_budget_problems(3, 201, "Case A")
    assert batch_source.case_budget_problems(60, 200) == ""


def test_download_all_copies_the_tree_and_refuses_oversized_files(tmp_path, monkeypatch):
    src, dest = tmp_path / "src", tmp_path / "dest"
    _tree(src, 2, depth_extra=False)
    entries = batch_source.walk_folder(LocalFolderSource(), str(src))
    files = batch_source.download_all(LocalFolderSource(), str(src), entries, dest)
    assert (dest / "Person 1_1" / "file1.pdf").read_bytes() == b"%PDF-1.4 stub"
    assert all("local" in f for f in files)
    monkeypatch.setattr(batch_source, "MAX_FILE_MB", 0)
    with pytest.raises(batch_source.QuotaExceeded) as exc:
        batch_source.download_all(LocalFolderSource(), str(src), entries, dest)
    assert "MB limit" in str(exc.value)


def test_download_progress_names_the_current_file_and_reports_retries(tmp_path):
    """A slow retry must look busy, not like a frozen counter."""
    class FlakySource:
        calls = 0

        def download(self, _folder_url, _entry):
            self.calls += 1
            if self.calls == 1:
                raise SourceUnavailable("SharePoint is rate-limiting us")
            return b"pdf"

    progress, retries = [], []
    files = batch_source.download_all(
        FlakySource(), FOLDER,
        [{"name": "claim.pdf", "kind": "file", "size": 3,
          "id": "opaque", "path": "Alice/claim.pdf"}],
        tmp_path,
        on_progress=lambda done, total, current: progress.append(
            (done, total, current)),
        on_retry=lambda what, attempt, total, error: retries.append(
            (what, attempt, total, str(error))),
    )

    assert files[0]["local"] == "Alice/claim.pdf"
    assert progress == [(0, 1, "Alice/claim.pdf"), (1, 1, None)]
    assert retries == [
        ("downloading Alice/claim.pdf", 1, batch_source.RETRIES,
         "SharePoint is rate-limiting us")
    ]


def test_fetch_batch_scopes_the_session_and_records_a_recovered_retry(
        tmp_path, monkeypatch):
    """The production copy boundary opts into batching and the Activity log."""
    from app.claims import runner

    class Source:
        active = False
        downloads = 0
        entered = 0
        exited = 0

        @contextmanager
        def batch(self, _folder_url):
            self.entered += 1
            self.active = True
            try:
                yield self
            finally:
                self.active = False
                self.exited += 1

        def list_folder(self, _folder_url, rel=""):
            assert self.active
            return [] if rel else [
                {"name": "claim.pdf", "kind": "file", "size": 3,
                 "id": "opaque", "path": "Alice/claim.pdf"}
            ]

        def download(self, _folder_url, _entry):
            assert self.active
            self.downloads += 1
            if self.downloads == 1:
                raise SourceUnavailable("SharePoint is rate-limiting us")
            return b"pdf"

    class Db:
        commits = 0

        def commit(self):
            self.commits += 1

    source, events = Source(), []
    run = SimpleNamespace(id="batch-run", folder_url=FOLDER, progress={})
    monkeypatch.setattr(runner, "get_source", lambda _url: source)
    monkeypatch.setattr(runner, "workspace_for", lambda _run_id: tmp_path / "workspace")
    monkeypatch.setattr(
        runner.telemetry, "record",
        lambda _db, _run_id, stage, level, code, message, detail="", **_kw:
            events.append((stage, level, code, message, detail)))

    files = runner._fetch_batch(Db(), run, tmp_path / "copied")

    assert files[0]["path"] == "Alice/claim.pdf"
    assert (source.entered, source.exited, source.downloads) == (1, 1, 2)
    # The last update has no file in hand, so it names none.
    assert run.progress == {"done": 1, "total": 1, "what": "downloading"}
    assert any(code == "SOURCE_RETRY"
               and "downloading Alice/claim.pdf" in message
               and "rate-limiting" in detail
               for _stage, _level, code, message, detail in events)


def test_fetching_the_listing_workbook_opens_one_session(tmp_path, monkeypatch):
    """Finding the workbook and downloading it are one visit, not two."""
    from app.claims import listing, runner

    class Source:
        opens = 0
        active = False

        @contextmanager
        def batch(self, _folder_url):
            self.opens += 1
            self.active = True
            try:
                yield self
            finally:
                self.active = False

        def list_folder(self, _folder_url, rel=""):
            assert self.active, "the listing must be read inside the session"
            return [{"name": "listing.xlsx", "kind": "file", "size": 3,
                     "id": "opaque", "path": "listing.xlsx"}]

        def download(self, _folder_url, _entry):
            assert self.active, "the download must reuse the same session"
            return b"xls"

    source = Source()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(listing, "get_source", lambda _url: source)
    monkeypatch.setattr(runner, "workspace_for", lambda _run_id: workspace)
    run = SimpleNamespace(
        id="listing-run",
        listing_url="https://example.sharepoint.com/sites/x/AP/listing.xlsx")

    path = listing.listing_path(run)

    assert path is not None and path.read_bytes() == b"xls"
    assert source.opens == 1


@pytest.fixture()
def real_source(server_url, monkeypatch):
    monkeypatch.setenv("MCP_URL", server_url)
    for var in ("MCP_AUTH_HEADER", "MCP_AUTH_VALUE", "MCP_TOOL_RESOLVE_FOLDER",
                "MCP_TOOL_LIST_ITEMS", "MCP_TOOL_GET_DOCUMENT"):
        monkeypatch.delenv(var, raising=False)
    return RealMcpSource(FOLDER)


@pytest.mark.skipif(not GEN.is_dir(), reason="run samples/generate_claims_sample.py first")
def test_nested_folder_over_real_mcp_lists_and_downloads_with_retry(real_source, tmp_path):
    """The whole nested round trip through the protocol: subfolders found,
    every file downloaded by opaque id, and the fake's every-7th-call
    ReadError absorbed by the walker's retry (11 folder listings > 7)."""
    from fake_mcp import mcp_server

    mcp_server._claims_calls = 0
    entries = batch_source.walk_folder(real_source, FOLDER)
    folders = [e for e in entries if e["kind"] == "folder"]
    files = [e for e in entries if e["kind"] == "file"]
    assert len(folders) == 10 and len(files) == 44
    assert mcp_server._claims_calls >= 7, "the transient failure was never exercised"
    one = next(f for f in files if f["path"].endswith(".xlsx"))
    data = real_source.download(FOLDER, one)
    assert data == (mcp_server.CLAIMS_DIR / one["path"]).read_bytes()
    monkeypatch_dest = tmp_path / "dest"
    copied = batch_source.download_all(real_source, FOLDER, entries[:6], monkeypatch_dest)
    assert all((monkeypatch_dest / c["path"]).is_file() for c in copied)


@pytest.mark.skipif(not GEN.is_dir(), reason="run samples/generate_claims_sample.py first")
def test_one_claims_copy_opens_and_resolves_sharepoint_once(
        real_source, tmp_path, monkeypatch):
    """The walk and every download share one resolved MCP conversation."""
    from app import mcp_client

    counts = {"sessions": 0, "resolutions": 0}
    original_enter = mcp_client.McpSession.__aenter__
    original_resolve = RealMcpSource._resolve

    async def counted_enter(self):
        counts["sessions"] += 1
        return await original_enter(self)

    async def counted_resolve(self, session):
        counts["resolutions"] += 1
        return await original_resolve(self, session)

    monkeypatch.setattr(mcp_client.McpSession, "__aenter__", counted_enter)
    monkeypatch.setattr(RealMcpSource, "_resolve", counted_resolve)

    with real_source.batch(FOLDER):
        entries = batch_source.walk_folder(real_source, FOLDER)
        copied = batch_source.download_all(
            real_source, FOLDER, entries[:6], tmp_path / "copied")
        # A caller mistake, deliberately NOT SourceUnavailable: that is
        # what claims/source.py retries, and a bug must not be retried.
        with pytest.raises(RuntimeError):
            real_source.list_folder(FOLDER + "/another-batch")

    assert copied, "the test must exercise at least one download"
    assert counts == {"sessions": 1, "resolutions": 1}


def test_source_down_is_a_structured_source_unavailable(monkeypatch):
    monkeypatch.setenv("MCP_URL", "http://127.0.0.1:1/mcp")  # nothing listens here
    src = RealMcpSource(FOLDER)
    with pytest.raises(SourceUnavailable):
        batch_source.walk_folder(src, FOLDER)


def test_a_copy_that_never_opens_leaves_the_source_as_it_found_it(monkeypatch):
    """A failed copy must not leave the source pointed somewhere else."""
    monkeypatch.setenv("MCP_URL", "http://127.0.0.1:1/mcp")  # nothing listens here
    src = RealMcpSource(FOLDER)

    with pytest.raises(SourceUnavailable):
        with src.batch(FOLDER + "/another-batch"):
            pass

    assert src.folder_url == FOLDER
    assert src._batch is None


@pytest.mark.skipif(not GEN.is_dir(), reason="run samples/generate_claims_sample.py first")
def test_unpack_zip_keeps_the_folder_tree(tmp_path):
    entries = batch_source.unpack_zip(GEN / "demo_claims_batch.zip", tmp_path)
    files = [e for e in entries if e["kind"] == "file"]
    folders = [e for e in entries if e["kind"] == "folder"]
    assert len(files) == 44 and len(folders) == 10
    assert (tmp_path / "Aegene Ong_1" / "Aegene Ong_Approval.pdf").is_file()


def test_unpack_zip_strips_one_common_root_folder(tmp_path):
    z = tmp_path / "b.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("batch/A_1/report.xlsx", b"x")
        zf.writestr("batch/A_1/receipts.pdf", b"y")
    dest = tmp_path / "out"
    entries = batch_source.unpack_zip(z, dest)
    assert (dest / "A_1" / "report.xlsx").is_file()
    assert {e["path"] for e in entries if e["kind"] == "folder"} == {"A_1"}


def test_unpack_zip_strips_a_double_wrapper(tmp_path):
    z = tmp_path / "b.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Claims/Emp B1 Test/Aegene Ong_1/report.xlsx", b"x")
        zf.writestr("Claims/Emp B1 Test/Audrey Ng/report.xlsx", b"y")
    dest = tmp_path / "out"
    entries = batch_source.unpack_zip(z, dest)
    assert (dest / "Aegene Ong_1" / "report.xlsx").is_file()
    assert {e["path"] for e in entries if e["kind"] == "folder"} == {"Aegene Ong_1", "Audrey Ng"}


def test_unpack_zip_refuses_path_escapes_and_bad_zips(tmp_path):
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../../etc/passwd", b"x")
    dest = tmp_path / "out"
    batch_source.unpack_zip(z, dest)  # '..' segments dropped, nothing escapes
    assert not (tmp_path / "etc").exists()
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(SourceUnavailable):
        batch_source.unpack_zip(bad, dest)


def test_strip_wrapper_roots_peels_non_person_wrappers():
    strip = batch_source.strip_wrapper_roots
    # The ICMR bug: a wrapper around the employee folders is peeled.
    assert strip(["Emp B1 Test/Aegene Ong_1/r.xlsx",
                  "Emp B1 Test/Aegene Ong/Aegene Ong (Revised)/r.xlsx",
                  "Emp B1 Test/Audrey Ng Shao Ying/r.xlsx"]) == "Emp B1 Test/"
    # A double wrapper unwraps level by level, even when a wrapper's own
    # name ('Claims') reads like a person to the heuristic — the level
    # holding several person-named folders is the batch.
    assert strip(["Claims/Aug 2026/Emp B1 Test/Aegene Ong_1/r.xlsx",
                  "Claims/Aug 2026/Emp B1 Test/Audrey Ng/r.xlsx"]) == "Claims/Aug 2026/Emp B1 Test/"
    # A stray file at the wrapper level does not stop the peel — it
    # simply becomes a batch-root file.
    assert strip(["Emp B1 Test/notes.pdf",
                  "Emp B1 Test/Aegene Ong_1/r.xlsx"]) == "Emp B1 Test/"
    # A wrapper titled with document words ('Claims', 'batch') is never
    # kept as a person's folder, even around a single employee.
    assert strip(["Claims/Aegene Ong_1/r.xlsx"]) == "Claims/"


def test_strip_wrapper_roots_keeps_a_single_employee_folder():
    strip = batch_source.strip_wrapper_roots
    # A sole folder of loose files IS one employee's folder.
    assert strip(["A_1/report.xlsx", "A_1/receipts.pdf"]) == ""
    # A person-named wrapper stays even when it holds subfolders.
    assert strip(["Aegene Ong/Receipts/a.pdf", "Aegene Ong/report.xlsx"]) == ""
    assert strip(["Aegene Ong_1/Receipts/a.pdf", "Aegene Ong_1/Reports/r.xlsx"]) == ""
    # No common root, nothing to do; empty input, nothing to do.
    assert strip(["A_1/r.xlsx", "B_2/r.xlsx"]) == ""
    assert strip([]) == ""


def test_strip_wrapper_roots_preserves_single_employee_layouts():
    strip = batch_source.strip_wrapper_roots
    # Month or category subfolders inside one employee's folder are not a
    # batch — the employee folder must survive, whatever its naming style.
    assert strip(["Aegene Ong/July/r.xlsx", "Aegene Ong/August/r.xlsx"]) == ""
    assert strip(["A_1/Receipts/a.pdf", "A_1/Reports/r.xlsx"]) == ""
    assert strip(["EMP001/July/a.pdf", "EMP001/August/b.pdf"]) == ""
    # A period-named wrapper around person folders is still peeled.
    assert strip(["July 2026/Aegene Ong_1/r.xlsx",
                  "July 2026/Audrey Ng/r.xlsx"]) == "July 2026/"
    # Peeling is capped: a crafted deeply nested path cannot make the
    # helper walk (or copy) thousands of levels.
    deep = "/".join(["a"] * 500) + "/f.pdf"
    assert strip([deep]).count("/") == batch_source.MAX_WRAPPER_LEVELS


def test_raw_path_guards_run_before_wrapper_detection(tmp_path):
    # A small zip can name one file in kilobytes of nested folders; it is
    # refused before wrapper detection or the listing walks the paths.
    z = tmp_path / "deep.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("/".join(["a"] * 600) + "/f.pdf", b"x")
    with pytest.raises(batch_source.QuotaExceeded) as exc:
        batch_source.unpack_zip(z, tmp_path / "out")
    assert "characters long" in str(exc.value)


def test_unpack_zip_keeps_month_subfolders_of_one_employee(tmp_path):
    z = tmp_path / "m.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("Aegene Ong/July/r1.pdf", b"x")
        zf.writestr("Aegene Ong/August/r2.pdf", b"y")
    entries = batch_source.unpack_zip(z, tmp_path / "out")
    assert {e["path"] for e in entries if e["kind"] == "folder" and e["depth"] == 1} == {"Aegene Ong"}


def test_ingest_uploaded_keeps_the_folder_tree(tmp_path):
    staged, dest = tmp_path / "upload", tmp_path / "out"
    (staged / "A_1" / "receipts").mkdir(parents=True)
    (staged / "A_1" / "report.xlsx").write_bytes(b"xlsx")
    (staged / "A_1" / "receipts" / "grab.pdf").write_bytes(b"pdf")
    (staged / "flat.pdf").write_bytes(b"pdf")
    entries = batch_source.ingest_uploaded(staged, dest)
    files = {e["path"]: e for e in entries if e["kind"] == "file"}
    assert set(files) == {"A_1/report.xlsx", "A_1/receipts/grab.pdf", "flat.pdf"}
    assert {e["path"] for e in entries if e["kind"] == "folder"} == {"A_1", "A_1/receipts"}
    assert (dest / "A_1" / "receipts" / "grab.pdf").read_bytes() == b"pdf"
    assert all("local" in f and f["size"] for f in files.values())


def test_ingest_uploaded_strips_wrapper_folders(tmp_path):
    # The ICMR layout: the reviewer dragged in the folder AROUND the
    # employee folders. The survey must see the employees, not the wrapper.
    staged, dest = tmp_path / "upload", tmp_path / "out"
    (staged / "Emp B1 Test" / "Aegene Ong" / "Aegene Ong (Revised)").mkdir(parents=True)
    (staged / "Emp B1 Test" / "Aegene Ong_1").mkdir()
    (staged / "Emp B1 Test" / "Audrey Ng Shao Ying").mkdir()
    (staged / "Emp B1 Test" / "Aegene Ong" / "Aegene Ong (Revised)" / "r.xlsx").write_bytes(b"x")
    (staged / "Emp B1 Test" / "Aegene Ong_1" / "Aegene Ong_ER.xlsx").write_bytes(b"x")
    (staged / "Emp B1 Test" / "Audrey Ng Shao Ying" / "Audrey Ng_ER.xlsx").write_bytes(b"x")
    entries = batch_source.ingest_uploaded(staged, dest)
    files = {e["path"] for e in entries if e["kind"] == "file"}
    assert files == {"Aegene Ong/Aegene Ong (Revised)/r.xlsx",
                     "Aegene Ong_1/Aegene Ong_ER.xlsx",
                     "Audrey Ng Shao Ying/Audrey Ng_ER.xlsx"}
    tops = {e["path"] for e in entries if e["kind"] == "folder" and e["depth"] == 1}
    assert tops == {"Aegene Ong", "Aegene Ong_1", "Audrey Ng Shao Ying"}
    assert (dest / "Aegene Ong_1" / "Aegene Ong_ER.xlsx").is_file()
    assert not (dest / "Emp B1 Test").exists()


def test_ingest_uploaded_enforces_the_run_quotas(tmp_path, monkeypatch):
    staged, dest = tmp_path / "upload", tmp_path / "out"
    staged.mkdir()
    (staged / "a.pdf").write_bytes(b"pdf")
    (staged / "b.pdf").write_bytes(b"pdf")
    monkeypatch.setattr(batch_source, "MAX_TOTAL_FILES", 1)
    with pytest.raises(batch_source.QuotaExceeded) as exc:
        batch_source.ingest_uploaded(staged, dest)
    assert "1 files a run may have" in str(exc.value)
    monkeypatch.setattr(batch_source, "MAX_TOTAL_FILES", 1500)
    monkeypatch.setattr(batch_source, "MAX_FILE_MB", 0)
    with pytest.raises(batch_source.QuotaExceeded) as exc:
        batch_source.ingest_uploaded(staged, dest)
    assert "MB limit" in str(exc.value)


def test_page_quota_after_download_is_run_wide(tmp_path, monkeypatch):
    files = [{"path": "A_1/x.pdf", "pages": 150}, {"path": "A_1/y.pdf", "pages": 60}]
    batch_source.check_page_quotas(files)  # 210 pages in one folder: fine at ingestion (per-case later)
    monkeypatch.setattr(batch_source, "MAX_TOTAL_PAGES", 200)
    with pytest.raises(batch_source.QuotaExceeded) as exc:
        batch_source.check_page_quotas(files)
    assert "200 pages a run may have" in str(exc.value)


def test_folder_entry_shape():
    e = docsource.folder_entry("a.pdf", "file", 10, "id1", "Sub")
    assert e == {"name": "a.pdf", "kind": "file", "size": 10, "id": "id1", "path": "Sub/a.pdf"}
