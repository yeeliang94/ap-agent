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


@pytest.fixture(scope="session")
def claims_sample_assets(tmp_path_factory):
    """One read-only survey and manifest for sample-based contract tests."""
    from app.claims import manifest as manifest_mod
    from app.claims import source as batch_source
    from app.claims import survey as survey_mod

    archive = Path(__file__).resolve().parents[2] / "samples" / "generated" / "claims" / "demo_claims_batch.zip"
    if not archive.is_file():
        pytest.skip("run samples/generate_claims_sample.py first")
    workspace = tmp_path_factory.mktemp("client-a-assets")
    files_dir = workspace / "files"
    entries = batch_source.unpack_zip(archive, files_dir)
    files = [entry for entry in entries if entry["kind"] == "file"]
    survey = survey_mod.survey_batch(files_dir, files)
    manifest = manifest_mod.build_manifest(files_dir, files)
    return files_dir, survey, manifest


def _serve(module, tmp_path):
    """Run one fake MCP server on a free port for the test session.

    Each test opens its OWN session inside a single `async with`: an MCP
    session holds an anyio cancel scope, which must be entered and exited
    in the same task, so client sessions are never shared.  The stateless
    fake HTTP server itself can be reused safely.
    """
    import threading
    import time

    import uvicorn

    # Serve the real sample reference folder if it exists; otherwise a
    # temporary one, so the tests never depend on generated samples.
    original_reference_dir = module.REFERENCE_DIR
    replaced_reference_dir = not original_reference_dir.is_dir()
    if replaced_reference_dir:
        (tmp_path / "payment_listing.xlsx").write_bytes(b"PK\x03\x04stub")
        module.REFERENCE_DIR = tmp_path

    # Port 0 = "any free one", so a busy port or a parallel run cannot
    # fail the tests; the OS tells us what it picked once bound.
    config = uvicorn.Config(module.mcp.streamable_http_app(),
                            host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    startup_errors: list[BaseException] = []

    def run_server():
        try:
            server.run()
        except (Exception, SystemExit) as exc:  # uvicorn reports bind failure as SystemExit
            startup_errors.append(exc)

    thread = threading.Thread(target=run_server, daemon=True)
    try:
        thread.start()
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            if startup_errors or not thread.is_alive():
                break
            time.sleep(0.01)
        detail = f": {startup_errors[0]}" if startup_errors else ""
        assert server.started, f"fake MCP server {module.__name__} did not start{detail}"
        port = server.servers[0].sockets[0].getsockname()[1]

        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        if replaced_reference_dir:
            module.REFERENCE_DIR = original_reference_dir


@pytest.fixture(scope="session")
def server_url(tmp_path_factory):
    """The fake that DOES offer a resolve-folder tool."""
    from fake_mcp import mcp_server

    yield from _serve(mcp_server, tmp_path_factory.mktemp("fake-mcp-reference"))


@pytest.fixture(scope="session")
def gateway_url(tmp_path_factory):
    """The fake shaped like the enterprise gateway: no resolve tool."""
    from fake_mcp import gateway_server

    yield from _serve(gateway_server, tmp_path_factory.mktemp("fake-gateway-reference"))
