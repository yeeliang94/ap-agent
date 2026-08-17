"""Delegated SharePoint sign-in, and the one rule that must never bend.

The pipeline runs unattended after an upload. If a run can trigger an
Entra sign-in, then a browser window opens on a machine nobody is
watching and the run waits forever — or worse, it opens behind a
reviewer's work while they wonder why nothing is happening.

So: only an explicit click may open a browser. Everything else reuses the
saved sign-in or stops with an instruction. Most of this file is that one
rule, checked from every direction it could be broken.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import sharepoint_auth
from app.main import app
from app.sharepoint_auth import SignInRequired

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_storage(monkeypatch):
    """A clean, memory-only sign-in store per test.

    Off Windows there is no DPAPI, so nothing is written to disk — which
    is also exactly what we want from a test.
    """
    monkeypatch.setattr(sharepoint_auth, "_storage", None)
    monkeypatch.setattr(sharepoint_auth, "_dpapi_available", lambda: False)
    monkeypatch.setenv("MCP_OAUTH", "true")
    yield
    sharepoint_auth._storage = None


@pytest.fixture()
def no_browser(monkeypatch):
    """Fail loudly if anything tries to open a browser."""
    import webbrowser

    def forbidden(*a, **k):
        raise AssertionError("a browser was opened without an explicit click")

    monkeypatch.setattr(webbrowser, "open", forbidden)
    return forbidden


# --- the rule ---------------------------------------------------------------

def test_a_background_run_with_no_sign_in_asks_for_one_instead_of_opening_a_browser(
        no_browser):
    with pytest.raises(SignInRequired) as exc:
        sharepoint_auth.build_provider("https://gw.example.com/mcp", interactive=False)
    message = str(exc.value)
    assert "Connect SharePoint" in message   # tells the reviewer what to press
    assert "start the run again" in message  # ...and what to do after


@pytest.mark.anyio
async def test_an_expired_sign_in_mid_run_still_never_opens_a_browser(no_browser):
    """The nastier case: a sign-in exists, so the provider is built, and
    only when the gateway rejects the stale token does the SDK reach for
    the redirect handler. That handler must refuse too."""
    import asyncio

    await _pretend_signed_in()
    provider = sharepoint_auth.build_provider(
        "https://gw.example.com/mcp", interactive=False)
    assert provider is not None

    with pytest.raises(SignInRequired) as exc:
        asyncio.get_event_loop()  # noqa: B018  (documents that we are in a loop)
        await provider.context.redirect_handler("https://login.example.com/authorize")
    assert "expired" in str(exc.value)


@pytest.mark.anyio
async def test_a_background_run_never_waits_on_the_callback_server(no_browser):
    await _pretend_signed_in()
    provider = sharepoint_auth.build_provider(
        "https://gw.example.com/mcp", interactive=False)
    with pytest.raises(SignInRequired):
        await provider.context.callback_handler()


def test_the_document_source_passes_the_instruction_through_unchanged(monkeypatch):
    """A run's error text is read by a reviewer, so the sentence written
    for them must survive the trip rather than becoming 'SignInRequired'."""
    from app.docsource import RealMcpSource, SourceUnavailable

    monkeypatch.setenv("MCP_URL", "https://gw.example.com/mcp")
    source = RealMcpSource("https://x.sharepoint.com/sites/a/Documents/b")
    with pytest.raises(SourceUnavailable) as exc:
        source.list_names()
    assert "Connect SharePoint" in str(exc.value)


# --- switched off -----------------------------------------------------------

def test_no_provider_at_all_when_this_gateway_does_not_use_oauth(monkeypatch):
    """Local development and API-key-only gateways must be untouched."""
    monkeypatch.setenv("MCP_OAUTH", "false")
    assert sharepoint_auth.build_provider("https://gw/mcp", interactive=False) is None
    monkeypatch.delenv("MCP_OAUTH")
    assert sharepoint_auth.build_provider("https://gw/mcp", interactive=False) is None


# --- staying signed in ------------------------------------------------------

@pytest.mark.anyio
async def test_an_expired_access_token_is_refreshed_not_re_authorised(no_browser):
    """The failure that would have made this feature look broken.

    The SDK reloads tokens from storage but NOT their expiry, and treats
    "no known expiry" as "still valid". A provider built an hour after
    signing in would therefore send a dead access token, take the 401,
    and jump to a full interactive sign-in — which a background run
    cannot do. Every run would fail asking the reviewer to sign in again
    while a perfectly good refresh token sat unused.
    """
    import time

    from mcp.shared.auth import OAuthToken

    saved = sharepoint_auth.storage()
    await saved.set_tokens(OAuthToken(access_token="stale", token_type="Bearer",
                                      refresh_token="good", expires_in=3600))
    # Wind the clock forward: signed in, then an hour passed.
    state = saved._load()
    state["expires_at"] = time.time() - 5
    saved._save(state)

    provider = sharepoint_auth.build_provider(
        "https://gw.example.com/mcp", interactive=False)
    provider.context.current_tokens = await saved.get_tokens()

    assert not provider.context.is_token_valid(), (
        "an hour-old access token must not read as valid, or the SDK skips "
        "the refresh and starts an interactive sign-in")
    assert provider.context.current_tokens.refresh_token == "good"


@pytest.mark.anyio
async def test_a_fresh_access_token_is_used_as_is(no_browser):
    from mcp.shared.auth import OAuthToken

    saved = sharepoint_auth.storage()
    await saved.set_tokens(OAuthToken(access_token="fresh", token_type="Bearer",
                                      refresh_token="r", expires_in=3600))
    provider = sharepoint_auth.build_provider(
        "https://gw.example.com/mcp", interactive=False)
    provider.context.current_tokens = await saved.get_tokens()
    assert provider.context.is_token_valid()


@pytest.mark.anyio
async def test_the_absolute_expiry_is_what_gets_written_down():
    """expires_in is "seconds from now", which is a lie the moment it is
    saved. Only an absolute instant survives a restart."""
    import time

    from mcp.shared.auth import OAuthToken

    saved = sharepoint_auth.storage()
    await saved.set_tokens(OAuthToken(access_token="a", token_type="Bearer",
                                      expires_in=3600))
    assert saved.expires_at() == pytest.approx(time.time() + 3600, abs=5)


# --- the callback listener --------------------------------------------------

@pytest.mark.anyio
async def test_the_callback_port_is_listening_before_the_browser_opens(monkeypatch):
    """A reviewer with a live Entra session is bounced back to localhost
    almost instantly. If the browser opens first and the listener binds
    afterwards, that redirect can arrive at a dead port."""
    import socket
    import webbrowser

    reachable_when_browser_opened = {}

    def check_then_open(url):
        probe = socket.socket()
        probe.settimeout(0.5)
        reachable_when_browser_opened["ok"] = (
            probe.connect_ex(("127.0.0.1", sharepoint_auth.CALLBACK_PORT)) == 0)
        probe.close()
        return True

    monkeypatch.setattr(webbrowser, "open", check_then_open)

    provider = sharepoint_auth.build_provider(
        "https://gw.example.com/mcp", interactive=True)
    await provider.context.redirect_handler("https://login.example.com/authorize")
    try:
        assert reachable_when_browser_opened["ok"], (
            "the callback port was not accepting connections yet")
    finally:
        # The listener belongs to this provider's closure; end it cleanly.
        await _close_listener(provider)


async def _close_listener(provider):
    import asyncio

    task = asyncio.create_task(provider.context.callback_handler())
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


# --- remembering the sign-in ------------------------------------------------

@pytest.mark.anyio
async def test_the_saved_sign_in_round_trips():
    from mcp.shared.auth import OAuthToken

    saved = sharepoint_auth.storage()
    assert not saved.is_signed_in()
    await saved.set_tokens(OAuthToken(access_token="a", token_type="Bearer",
                                      refresh_token="r"))
    assert saved.is_signed_in()
    assert (await saved.get_tokens()).refresh_token == "r"
    saved.forget()
    assert not saved.is_signed_in()
    assert await saved.get_tokens() is None


@pytest.mark.anyio
async def test_an_unreadable_saved_sign_in_means_sign_in_again_not_a_crash(
        monkeypatch, tmp_path):
    """The file is tied to one Windows account. Copied to another machine
    it cannot be decrypted — that must mean 'sign in again', not a broken
    app that has to be reinstalled."""
    def cannot_decrypt(_blob):
        raise ValueError("DPAPI could not decrypt this blob")

    monkeypatch.setattr(sharepoint_auth, "_decrypt", cannot_decrypt)

    saved = sharepoint_auth.storage()
    saved._path = tmp_path / "sharepoint-oauth-state.bin"
    saved._path.write_bytes(b"not a DPAPI blob")
    saved._on_disk = True

    assert await saved.get_tokens() is None   # no exception
    assert not saved.is_signed_in()


async def _pretend_signed_in():
    from mcp.shared.auth import OAuthToken

    await sharepoint_auth.storage().set_tokens(
        OAuthToken(access_token="stale", token_type="Bearer", refresh_token="r"))


# --- what the screen asks ---------------------------------------------------

def test_the_status_endpoint_reports_whether_a_sign_in_is_needed(monkeypatch):
    monkeypatch.setenv("MCP_OAUTH", "true")
    body = client.get("/api/sharepoint/status").json()
    assert body == {"required": True, "connected": False}

    monkeypatch.setenv("MCP_OAUTH", "false")
    assert client.get("/api/sharepoint/status").json() == {
        "required": False, "connected": False}


def test_connecting_without_a_configured_gateway_says_so(monkeypatch):
    monkeypatch.delenv("MCP_URL", raising=False)
    r = client.post("/api/sharepoint/connect")
    assert r.status_code == 400
    assert "MCP_URL" in r.json()["detail"]
