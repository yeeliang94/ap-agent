"""Local-folder source containment (review 2026-08-19, AI-loop slice).

`LocalFolderSource` is the development batch source: a "folder link" is a
folder on this machine. `list_folder` has always refused a relative path
that leaves the batch folder; `download` did not — it joined
`entry["path"]` onto the folder and read whatever came out. The entry
normally comes from our own walk, but it travels through the walker
(and, in local mode, through a request body) before it gets here, so an
entry naming `../../.ssh/id_rsa` or an absolute path would have been read
and ingested into a run as if it were a claim file. Both calls make the
same check now.
"""
from __future__ import annotations

import pytest

from app.docsource import LocalFolderSource, SourceUnavailable


@pytest.fixture()
def batch(tmp_path):
    root = tmp_path / "batch"
    (root / "Aegene Ong_1").mkdir(parents=True)
    (root / "Aegene Ong_1" / "report.xlsx").write_bytes(b"a claim file")
    outside = tmp_path / "secret.txt"
    outside.write_text("not a claim file")
    return root, outside


def test_download_reads_only_inside_the_batch_folder(batch):
    root, outside = batch
    src = LocalFolderSource()
    assert src.download(str(root), {"path": "Aegene Ong_1/report.xlsx"}) == b"a claim file"

    for escaping in ("../secret.txt", "Aegene Ong_1/../../secret.txt", str(outside)):
        with pytest.raises(SourceUnavailable) as exc:
            src.download(str(root), {"path": escaping})
        assert "outside the batch folder" in str(exc.value), escaping

    # a symlink out of the tree is an escape once resolved, not a shortcut
    (root / "link.xlsx").symlink_to(outside)
    with pytest.raises(SourceUnavailable):
        src.download(str(root), {"path": "link.xlsx"})

    # a path inside the folder that simply is not there reads as missing,
    # not as an escape
    with pytest.raises(SourceUnavailable) as exc:
        src.download(str(root), {"path": "Aegene Ong_1/nope.xlsx"})
    assert "not found under" in str(exc.value)


def test_list_folder_still_refuses_the_same_paths(batch):
    root, outside = batch
    src = LocalFolderSource()
    assert [e["name"] for e in src.list_folder(str(root))] == ["Aegene Ong_1"]
    assert [e["name"] for e in src.list_folder(str(root), "Aegene Ong_1")] == ["report.xlsx"]
    for escaping in ("..", "Aegene Ong_1/../..", str(outside.parent)):
        with pytest.raises(SourceUnavailable):
            src.list_folder(str(root), escaping)
