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
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.mcp_client import McpError, McpSession  # noqa: E402

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def server_url(monkeypatch, tmp_path):
    """The fake MCP server, running for the duration of one test.

    Each test opens its OWN session inside a single `async with`: an MCP
    session holds an anyio cancel scope, which must be entered and exited
    in the same task, so it cannot be handed across a yield fixture.
    """
    import threading
    import time

    import uvicorn

    from fake_mcp import mcp_server

    # Serve the real sample reference folder if it exists; otherwise a
    # temporary one, so the tests never depend on generated samples.
    if not mcp_server.REFERENCE_DIR.is_dir():
        (tmp_path / "payment_listing.xlsx").write_bytes(b"PK\x03\x04stub")
        monkeypatch.setattr(mcp_server, "REFERENCE_DIR", tmp_path)

    config = uvicorn.Config(mcp_server.mcp.streamable_http_app(),
                            host="127.0.0.1", port=8005, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):  # wait for bind
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "fake MCP server did not start"

    yield "http://127.0.0.1:8005/mcp"

    server.should_exit = True
    thread.join(timeout=5)


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


async def test_server_side_failure_becomes_McpError(server_url):
    async with McpSession(server_url) as session:
        with pytest.raises(McpError) as exc:
            await session.call("sp_resolve_folder_url", {"url": "https://nope/"})
    assert "sp_resolve_folder_url" in str(exc.value)
