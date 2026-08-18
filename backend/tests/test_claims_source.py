"""The claims batch walker: nested folders, retries, quotas, zip unpacking.

Runs against the fake MCP server over the real protocol (server_url from
conftest) and against local folders. No AI anywhere here.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

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


def test_thirty_one_folders_are_refused_with_the_quota_named(tmp_path):
    _tree(tmp_path, 31, files_per=1, depth_extra=False)
    with pytest.raises(batch_source.QuotaExceeded) as exc:
        batch_source.walk_folder(LocalFolderSource(), str(tmp_path))
    assert "30 employee folders" in str(exc.value)


def test_too_many_files_in_one_folder_names_that_folder(tmp_path):
    _tree(tmp_path, 1, files_per=61, depth_extra=False)
    with pytest.raises(batch_source.QuotaExceeded) as exc:
        batch_source.walk_folder(LocalFolderSource(), str(tmp_path))
    assert "60 files" in str(exc.value) and "Person 0_0" in str(exc.value)


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


def test_source_down_is_a_structured_source_unavailable(monkeypatch):
    monkeypatch.setenv("MCP_URL", "http://127.0.0.1:1/mcp")  # nothing listens here
    src = RealMcpSource(FOLDER)
    with pytest.raises(SourceUnavailable):
        batch_source.walk_folder(src, FOLDER)


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


def test_page_quota_after_download(tmp_path):
    files = [{"path": "A_1/x.pdf", "pages": 150}, {"path": "A_1/y.pdf", "pages": 60}]
    with pytest.raises(batch_source.QuotaExceeded) as exc:
        batch_source.check_page_quotas(files)
    assert "200 pages" in str(exc.value)


def test_folder_entry_shape():
    e = docsource.folder_entry("a.pdf", "file", 10, "id1", "Sub")
    assert e == {"name": "a.pdf", "kind": "file", "size": 10, "id": "id1", "path": "Sub/a.pdf"}
