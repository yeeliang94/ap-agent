"""RealMcpSource, end to end against the fake MCP server.

test_mcp_client.py proves the low-level client speaks the protocol; this
file proves the layer above it: listing a folder, downloading a document
via the temporary link, honouring explicit tool-name overrides, and the
payload-shape helpers that absorb how differently real servers answer.
"""
from __future__ import annotations

import pytest

from app.docsource import (
    RealMcpSource,
    SourceUnavailable,
    _download_link,
    _failure_reason,
    _folder_args,
    _inline_bytes,
    _item_id_for,
    _names_from,
    _raise_if_error,
    _reported_error,
    _same_host,
)

FOLDER = "https://example.sharepoint.com/sites/x/Shared%20Documents/AP"


@pytest.fixture()
def real_source(server_url, monkeypatch):
    monkeypatch.setenv("MCP_URL", server_url)
    for var in ("MCP_AUTH_HEADER", "MCP_AUTH_VALUE", "MCP_TOOL_RESOLVE_FOLDER",
                "MCP_TOOL_LIST_ITEMS", "MCP_TOOL_GET_DOCUMENT"):
        monkeypatch.delenv(var, raising=False)
    return RealMcpSource(FOLDER)


def test_lists_the_reference_folder(real_source):
    names = real_source.list_names()
    assert names, "expected at least one reference file"
    assert all(isinstance(n, str) for n in names)


def test_downloads_a_document_via_the_temporary_link(real_source):
    """The whole round trip: display name in, file bytes out.

    The fake addresses documents by an opaque item ID, as real SharePoint
    does, so this only passes if the download translated the name into
    that ID. Also proves the fake builds its link from the request's own
    host — the server here runs on a random free port, not its default.
    """
    from fake_mcp import mcp_server

    names = real_source.list_names()
    data = real_source.get_reference(names[0])
    assert data == (mcp_server.REFERENCE_DIR / names[0]).read_bytes()


def test_a_server_reported_error_reaches_the_caller(real_source):
    """Gateways report a bad request inside a normal result, as an "error"
    key rather than a protocol error. Reporting only "keys returned:
    ['error']" threw away the one sentence that explains the failure."""
    with pytest.raises(SourceUnavailable) as exc:
        real_source.get_reference("no-such-file.xlsx")
    assert "itemNotFound" in str(exc.value)
    assert "no-such-file.xlsx" in str(exc.value)


def test_item_id_comes_from_the_listing_never_from_the_file_name():
    listing = {"items": [
        {"name": "Payment Listing.xlsx", "item_id": "01ABCOPAQUE", "kind": "file"},
        {"name": "Policy.xlsx", "id": "01XYZOPAQUE", "kind": "file"},
    ]}
    assert _item_id_for(listing, "Payment Listing.xlsx") == "01ABCOPAQUE"
    assert _item_id_for(listing, "Policy.xlsx") == "01XYZOPAQUE"
    # SharePoint treats names case-insensitively; a case-only difference
    # is the same file, not a miss.
    assert _item_id_for(listing, "policy.xlsx") == "01XYZOPAQUE"
    assert _item_id_for(listing, "Absent.xlsx") == ""
    # A genuinely name-addressed server publishes no ID: "" tells the
    # caller to fall back to the name rather than inventing one.
    assert _item_id_for({"items": [{"name": "a.xlsx"}]}, "a.xlsx") == ""


def test_an_explicit_error_key_is_what_marks_a_response_as_failed():
    assert _reported_error({"error": "itemNotFound"}) == "itemNotFound"
    assert _reported_error({"error": {"message": "accessDenied"}}) == "accessDenied"
    assert _reported_error({"download_url": "https://x/d"}) == ""
    # A quoted temporary URL is bearer-like and must not reach the UI.
    assert _reported_error(
        {"error": "failed fetching https://x/d?token=secret"}
    ) == "failed fetching <link>"


def test_a_chatty_but_successful_response_is_not_treated_as_a_failure():
    """Gateways add notes beside good results — "showing 100 of 250".
    Reading that as an error would fail a run that had just succeeded, so
    only unmistakable error keys count."""
    chatty = {"value": [{"name": "a.xlsx", "kind": "file"}],
              "message": "Showing the first 100 of 250 items",
              "detail": "results truncated"}
    assert _reported_error(chatty) == ""
    _raise_if_error(chatty, "list the folder")   # must not raise
    assert _names_from(chatty) == ["a.xlsx"]


def test_once_a_call_has_definitely_failed_any_wording_is_better_than_none():
    """In the "no file content came back" branch the failure is already
    established, so a bare message is the only clue there is."""
    assert _failure_reason({"message": "the item is checked out"}) == \
        "the item is checked out"
    assert _failure_reason({"error": "itemNotFound",
                            "message": "ignored"}) == "itemNotFound"


def test_wrong_explicit_tool_name_fails_loudly(real_source, monkeypatch):
    """An explicitly configured tool name that the server does not offer
    must surface as a configuration error, never be silently skipped."""
    monkeypatch.setenv("MCP_TOOL_RESOLVE_FOLDER", "sp_no_such_tool")
    with pytest.raises(SourceUnavailable) as exc:
        real_source.list_names()
    assert "MCP_TOOL_RESOLVE_FOLDER" in str(exc.value)
    assert "sp_resolve_folder_url" in str(exc.value)  # ...and lists what IS offered


def test_download_link_ignores_generic_url_keys():
    """Servers echo arguments back, and we send a "url" — a generic key
    must never be mistaken for a download link."""
    assert _download_link({"url": "https://x/page", "href": "https://x/h"}) == ""
    assert _download_link({"download_url": "https://x/d"}) == "https://x/d"
    assert _download_link({"@microsoft.graph.downloadUrl": "https://x/g"}) == "https://x/g"


def test_inline_bytes_tries_every_candidate_key():
    payload = {"content_base64": "*** not base64 ***", "base64": "aGVsbG8="}
    assert _inline_bytes(payload) == b"hello"
    assert _inline_bytes({"content_base64": "!!!"}) is None


def test_same_host_gates_where_the_auth_header_may_travel():
    assert _same_host("http://127.0.0.1:8004/download/x", "http://127.0.0.1:8004/mcp")
    assert not _same_host("https://storage.example.net/f", "https://gw.example.com/mcp")
    assert not _same_host("http://gw.example.com/a", "https://gw.example.com/mcp")
    assert not _same_host("", "https://gw.example.com/mcp")


def test_names_from_unwraps_and_skips_folders():
    payload = {"items": [{"name": "a.xlsx", "kind": "file"},
                         {"name": "sub", "kind": "folder"},
                         {"name": "old", "folder": {"childCount": 2}},
                         {"name": "b.xlsx"}]}
    assert _names_from(payload) == ["a.xlsx", "b.xlsx"]
    assert _names_from(["x.xlsx"]) == ["x.xlsx"]
    assert _names_from("garbage") == []


def test_folder_args_keeps_identifiers_and_falls_back_to_url():
    args = _folder_args({"site_id": "s", "library": "L", "meta": {"x": 1}},
                        "http://folder")
    assert args == {"site_id": "s", "library": "L", "url": "http://folder"}
