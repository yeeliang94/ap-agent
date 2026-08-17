"""Signing in to SharePoint on the reviewer's behalf.

The enterprise gateway wants two credentials at once: its own API key in a
header (that is MCP_AUTH_HEADER / MCP_AUTH_VALUE, set by IT), and a
delegated Entra sign-in proving WHICH person the request acts for. Only
the second needs a browser, and only the first time.

Two rules shape this module, and both matter more than they look:

  - **A background run must never open a browser.** The pipeline runs
    unattended after an upload. A sign-in window appearing behind a
    reviewer's work — or on a machine nobody is sitting at — is how an
    automated run silently waits forever. Background runs reuse the saved
    sign-in or fail with a readable instruction; only the Connect button
    opens a browser.

  - **The saved sign-in is a credential.** A refresh token is a
    long-lived key to that person's SharePoint. On Windows it is
    encrypted with DPAPI, which ties it to that Windows account: copying
    the file to another machine yields nothing. Off Windows there is no
    DPAPI, so it is kept in memory only and never written down — a
    developer machine cannot reach the gateway anyway.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

log = logging.getLogger("sharepoint_auth")

# Refresh this long before the access token actually dies, so a token
# that expires mid-request is renewed first rather than rejected.
TOKEN_REFRESH_SKEW_SECONDS = 120

# Where the browser comes back to after Entra sign-in. Fixed rather than
# random: it has to be registered as a redirect URI, and a moving port
# cannot be.
CALLBACK_PORT = int(os.getenv("MCP_OAUTH_CALLBACK_PORT", "8766"))
CALLBACK_URL = f"http://127.0.0.1:{CALLBACK_PORT}/callback"

# How long to wait for a person to finish signing in before giving up.
SIGN_IN_TIMEOUT_SECONDS = 300.0


class SignInRequired(Exception):
    """SharePoint needs a sign-in that only the reviewer can give.

    Raised instead of opening a browser when a background run finds no
    saved sign-in. The message is written for the reviewer, because it
    reaches them as the run's error.
    """


def _state_path() -> Path:
    """The saved sign-in file. Beside the app's other per-user state."""
    base = os.getenv("LOCALAPPDATA") or os.path.expanduser("~/.local/share")
    return Path(base) / "ap-agent" / "sharepoint-oauth-state.bin"


# --- storing the sign-in ----------------------------------------------------

def _dpapi_available() -> bool:
    if os.name != "nt":
        return False
    try:
        import win32crypt  # noqa: F401  (pywin32, Windows only)

        return True
    except ImportError:
        return False


def _encrypt(raw: bytes) -> bytes:
    import win32crypt

    # CryptProtectData ties the blob to this Windows account, so the file
    # is worthless if copied off the machine.
    return win32crypt.CryptProtectData(raw, "ap-agent SharePoint", None,
                                       None, None, 0)


def _decrypt(blob: bytes) -> bytes:
    import win32crypt

    return win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1]


class SavedSignIn:
    """The MCP SDK's TokenStorage, kept encrypted on disk where possible.

    Holds both halves the SDK needs: the tokens, and the client
    registration the gateway issued (dynamic registration, so there is no
    client ID to configure by hand).
    """

    def __init__(self) -> None:
        self._memory: dict = {}
        self._path = _state_path()
        self._on_disk = _dpapi_available()
        if not self._on_disk:
            log.info("SharePoint sign-in will be kept in memory only "
                     "(no DPAPI on this platform); signing in again will be "
                     "needed after a restart")

    # -- the file ----------------------------------------------------------
    def _load(self) -> dict:
        if not self._on_disk:
            return self._memory
        if not self._path.is_file():
            return {}
        try:
            import json

            return json.loads(_decrypt(self._path.read_bytes()).decode())
        except Exception:
            # A corrupt or foreign-account blob must not break the app —
            # it just means signing in again.
            log.warning("saved SharePoint sign-in could not be read; "
                        "a new sign-in will be needed", exc_info=True)
            return {}

    def _save(self, state: dict) -> None:
        if not self._on_disk:
            self._memory = state
            return
        import json

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_bytes(_encrypt(json.dumps(state).encode()))

    # -- the TokenStorage protocol ----------------------------------------
    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        raw = self._load().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens) -> None:
        state = self._load()
        state["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        # expires_in is "seconds from NOW", which stops being true the
        # moment it is written to disk. Record the absolute instant too,
        # or a token saved yesterday reads as fresh today.
        state["expires_at"] = (time.time() + int(tokens.expires_in)
                               if tokens.expires_in else None)
        self._save(state)

    def expires_at(self) -> float | None:
        """When the saved access token dies, as a wall-clock instant."""
        value = self._load().get("expires_at")
        return float(value) if value else None

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        raw = self._load().get("client")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info) -> None:
        state = self._load()
        state["client"] = client_info.model_dump(mode="json", exclude_none=True)
        self._save(state)

    # -- used by the API ---------------------------------------------------
    def forget(self) -> None:
        self._memory = {}
        if self._on_disk and self._path.is_file():
            self._path.unlink()

    def is_signed_in(self) -> bool:
        return bool(self._load().get("tokens"))


# One storage for the process: the saved sign-in is per user, not per run,
# and a second instance would race the first when writing the file.
_storage: SavedSignIn | None = None


def storage() -> SavedSignIn:
    global _storage
    if _storage is None:
        _storage = SavedSignIn()
    return _storage


# --- the sign-in itself -----------------------------------------------------

class CallbackListener:
    """Catches the browser's return trip from Entra sign-in.

    Split into "start listening" and "wait for the answer" on purpose.
    The SDK opens the browser FIRST and only then asks us to wait, so a
    listener that bound inside the waiting step would not exist yet — and
    a reviewer who is already signed in to Entra is bounced back to
    127.0.0.1 almost instantly, to a port with nothing on it. Binding
    before the browser opens removes the race entirely.

    The listener exists for one request and is then shut down: leaving a
    port open on a reviewer's machine is not something an AP tool should do.
    """

    def __init__(self) -> None:
        self._server = None
        self._answer = None

    async def start(self) -> None:
        """Bind the port. Safe to call more than once."""
        import asyncio
        from urllib.parse import parse_qs, urlsplit

        from mcp.shared.auth import AuthorizationCodeResult

        if self._server is not None:
            return
        self._answer = asyncio.get_running_loop().create_future()

        async def handle(reader, writer):
            try:
                request = (await reader.readuntil(b"\r\n")).decode("latin-1")
                target = request.split(" ")[1] if " " in request else "/"
                query = parse_qs(urlsplit(target).query)
                body = (b"Signed in. You can close this tab and go back to "
                        b"AP Assistant.")
                if "code" not in query:
                    body = (b"Sign-in did not complete: "
                            + query.get("error", ["no code returned"])[0].encode())
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                             b"Content-Length: " + str(len(body)).encode()
                             + b"\r\nConnection: close\r\n\r\n" + body)
                await writer.drain()
                if not self._answer.done() and "code" in query:
                    self._answer.set_result(AuthorizationCodeResult(
                        code=query["code"][0],
                        state=(query.get("state") or [None])[0]))
            finally:
                writer.close()

        self._server = await asyncio.start_server(handle, "127.0.0.1", CALLBACK_PORT)
        log.info("listening for the SharePoint sign-in callback on %s", CALLBACK_URL)

    async def wait(self):
        import asyncio

        await self.start()  # belt and braces: never wait on an unbound port
        try:
            return await asyncio.wait_for(self._answer,
                                          timeout=SIGN_IN_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise SignInRequired(
                "The SharePoint sign-in was not completed in time. Click "
                "Connect SharePoint and finish signing in in the browser "
                "window that opens.") from exc
        finally:
            await self.close()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None


def build_provider(server_url: str, *, interactive: bool):
    """The delegated-sign-in provider, or None when OAuth is switched off.

    interactive=False is the background pipeline: reuse a saved sign-in,
    and if there is none, say so rather than opening a browser nobody is
    watching. interactive=True is the reviewer pressing Connect.
    """
    if os.getenv("MCP_OAUTH", "").strip().lower() not in ("1", "true", "on", "yes"):
        return None

    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    saved = storage()
    if not interactive and not saved.is_signed_in():
        raise SignInRequired(
            "This app is not connected to SharePoint yet. Open AP Assistant "
            "and click 'Connect SharePoint', sign in when the browser opens, "
            "then start the run again.")

    listener = CallbackListener()

    async def open_browser(url: str) -> None:
        if not interactive:
            # The rule this module exists for.
            raise SignInRequired(
                "Your SharePoint sign-in has expired. Open AP Assistant and "
                "click 'Connect SharePoint' to sign in again, then start the "
                "run again.")
        import webbrowser

        # Listen BEFORE opening the browser: an already-signed-in reviewer
        # is redirected back almost instantly, and the port has to be
        # answering by then.
        await listener.start()
        log.info("opening the browser for SharePoint sign-in")
        webbrowser.open(url)

    async def wait() -> object:
        if not interactive:
            raise SignInRequired(
                "A SharePoint sign-in is needed and this run cannot ask for "
                "one. Click 'Connect SharePoint' first.")
        return await listener.wait()

    metadata = OAuthClientMetadata(
        client_name=os.getenv("MCP_OAUTH_CLIENT_NAME", "AP Assistant"),
        redirect_uris=[CALLBACK_URL],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=os.getenv("MCP_OAUTH_SCOPE", "").strip() or None,
    )
    provider = OAuthClientProvider(
        server_url=server_url, client_metadata=metadata, storage=saved,
        redirect_handler=open_browser, callback_handler=wait,
    )
    # The SDK reloads tokens from storage but NOT when they expire, and it
    # treats "no known expiry" as "still good". A fresh provider therefore
    # sends a long-dead access token, gets a 401, and jumps straight to a
    # full interactive sign-in — which a background run cannot do. So a
    # reviewer who signed in an hour ago would see every run fail asking
    # them to sign in again, while a perfectly good refresh token sat
    # unused. Handing the provider the expiry we saved makes it refresh
    # instead. Checked by test_an_expired_access_token_is_refreshed_...
    expires_at = saved.expires_at()
    if expires_at:
        provider.context.token_expiry_time = expires_at - TOKEN_REFRESH_SKEW_SECONDS
    return provider


async def connect(server_url: str, headers: dict) -> str:
    """Sign in now, at the reviewer's request. Returns who they signed in as.

    Performs a real handshake, because a sign-in that is never used
    against the gateway has proved nothing.
    """
    from .mcp_client import McpSession

    provider = build_provider(server_url, interactive=True)
    if provider is None:
        raise SignInRequired(
            "Delegated SharePoint sign-in is switched off. Set MCP_OAUTH=true "
            "in .env if this gateway needs it.")
    async with McpSession(server_url, headers, auth=provider) as session:
        for words in (("whoami",), ("who", "am", "i"), ("current", "user")):
            try:
                tool = session.find_tool(words)
            except Exception:
                continue
            answer = await session.call(tool, {})
            if isinstance(answer, dict):
                for key in ("displayName", "display_name", "mail",
                            "userPrincipalName", "name"):
                    if answer.get(key):
                        return str(answer[key])
            return "signed in"
        return "signed in"
