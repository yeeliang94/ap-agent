"""The real-MCP client: discovery, argument filtering, and failure text.

These run against the fake MCP server IN-PROCESS over the real protocol
(initialize -> tools/list -> tools/call), so they test the protocol
rather than an agreement between our own two halves — which was the whole
problem with the bespoke REST contract they replace.

The fake names its tools sp_* on purpose: nothing here may work by the
client and server sharing our internal role names.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.mcp_client import McpError, McpSession  # noqa: E402

# The server_url fixture (the fake MCP server on a free port) lives in
# conftest.py, shared with test_real_mcp_source.py.

pytestmark = pytest.mark.asyncio


FOLDER = "https://example.sharepoint.com/sites/x/Shared%20Documents/AP"


async def test_handshake_lists_the_servers_own_tools(server_url):
    async with McpSession(server_url) as session:
        # Not our internal role names — the server's.
        assert "sp_resolve_folder_url" in session.tool_names
        assert "sp_list_library_items" in session.tool_names


async def test_tools_are_found_by_keyword_not_by_assumed_name(server_url):
    async with McpSession(server_url) as session:
        assert session.find_tool(("resolve", "folder")) == "sp_resolve_folder_url"
        assert session.find_tool(("list", "item")) == "sp_list_library_items"
        # Falls through candidate spellings in order until one matches.
        assert session.find_tool(("nope", "nothing"),
                                 ("list", "item")) == "sp_list_library_items"


async def test_missing_tool_error_names_what_the_server_offers(server_url):
    """The single most useful line when configuring a new gateway."""
    async with McpSession(server_url) as session:
        with pytest.raises(McpError) as exc:
            session.find_tool(("send", "email"))
    assert "sp_resolve_folder_url" in str(exc.value)


async def test_unknown_arguments_are_dropped_not_sent(server_url):
    """Real servers validate their input schema and reject extras, so the
    client must filter against the published schema."""
    async with McpSession(server_url) as session:
        accepted = session.accepted_arguments("sp_list_library_items")
        assert accepted is not None and "site_id" in accepted
        assert "made_up_argument" not in accepted

        resolved = await session.call("sp_resolve_folder_url", {"url": FOLDER})
        # Deliberately pass junk alongside the real arguments.
        payload = await session.call("sp_list_library_items", {
            **resolved, "made_up_argument": "boom", "another": 1})
        assert isinstance(payload.get("items"), list)


async def test_the_shared_documents_alias_is_handled(server_url):
    async with McpSession(server_url) as session:
        resolved = await session.call("sp_resolve_folder_url", {"url": FOLDER})
    # Browser says "Shared Documents"; MCP reports "Documents".
    assert resolved["library"] == "Documents"


async def test_a_delegated_oauth_provider_reaches_the_http_client(server_url):
    """The enterprise gateway needs a live OAuth token as well as its API
    key. The MCP SDK's transport takes no `auth` argument, which reads as
    "this version cannot do OAuth" — it can, because the auth belongs on
    the HTTP client this module builds. Proving the wiring here is what
    stops that misreading from becoming an SDK downgrade.
    """
    import httpx2

    from app import mcp_client

    class StampingAuth(httpx2.Auth):
        def auth_flow(self, request):
            request.headers["x-delegated"] = "token"
            yield request

    provider = StampingAuth()
    seen = {}
    real_client = httpx2.AsyncClient

    def capture(**kwargs):
        seen.update(kwargs)
        return real_client(**kwargs)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(mcp_client.httpx2, "AsyncClient", capture)
    try:
        async with McpSession(server_url, {"x-api-key": "k"}, auth=provider) as s:
            assert s.tool_names  # the handshake still works with auth attached
    finally:
        monkeypatch.undo()

    assert seen["auth"] is provider
    assert seen["headers"] == {"x-api-key": "k"}


async def test_server_side_failure_becomes_McpError(server_url):
    async with McpSession(server_url) as session:
        with pytest.raises(McpError) as exc:
            await session.call("sp_resolve_folder_url", {"url": "https://nope/"})
    assert "sp_resolve_folder_url" in str(exc.value)
