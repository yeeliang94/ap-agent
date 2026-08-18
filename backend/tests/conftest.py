"""Shared fixtures: the fake MCP servers, served over the real protocol.

Two fakes, because the app has to work against two shapes of server:

  mcp_server     — offers a resolve-folder tool (a convenient gateway)
  gateway_server — offers none, so the client must navigate site ->
                   libraries -> items itself (the ENTERPRISE gateway)

The second exists because testing only against the first meant the
navigation path first ran in production, over a VPN.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make both `app` (backend/) and `fake_mcp` (repo root) importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture(autouse=True)
def _fresh_reference_caches():
    """The AI-read listing is cached by content hash across runs; tests
    fake the AI differently from one another, so start each one clean."""
    from app.pipeline import reference
    reference._LISTING_CACHE.clear()
    yield


def _serve(module, monkeypatch, tmp_path):
    """Run one fake MCP server on a free port for the duration of a test.

    Each test opens its OWN session inside a single `async with`: an MCP
    session holds an anyio cancel scope, which must be entered and exited
    in the same task, so it cannot be handed across a yield fixture.
    """
    import threading
    import time

    import uvicorn

    # Serve the real sample reference folder if it exists; otherwise a
    # temporary one, so the tests never depend on generated samples.
    if not module.REFERENCE_DIR.is_dir():
        (tmp_path / "payment_listing.xlsx").write_bytes(b"PK\x03\x04stub")
        monkeypatch.setattr(module, "REFERENCE_DIR", tmp_path)

    # Port 0 = "any free one", so a busy port or a parallel run cannot
    # fail the tests; the OS tells us what it picked once bound.
    config = uvicorn.Config(module.mcp.streamable_http_app(),
                            host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):  # wait for bind
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, f"fake MCP server {module.__name__} did not start"
    port = server.servers[0].sockets[0].getsockname()[1]

    yield f"http://127.0.0.1:{port}/mcp"

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture()
def server_url(monkeypatch, tmp_path):
    """The fake that DOES offer a resolve-folder tool."""
    from fake_mcp import mcp_server

    yield from _serve(mcp_server, monkeypatch, tmp_path)


@pytest.fixture()
def gateway_url(monkeypatch, tmp_path):
    """The fake shaped like the enterprise gateway: no resolve tool."""
    from fake_mcp import gateway_server

    yield from _serve(gateway_server, monkeypatch, tmp_path)
