"""DocumentSource — where reference documents come from.

The pipeline asks this adapter for "payment_listing.xlsx" and gets bytes
back. It never knows whether they came from a local folder (development),
the fake MCP (integration testing), or the real SharePoint MCP (Windows).
Swapping source = changing DOC_SOURCE in .env, nothing else.
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import httpx

from . import config

# Full failure detail (which may contain URLs) goes to the server log ONLY.
# Anything raised to callers — and therefore possibly shown in the UI via
# run.error — is generic: temporary download URLs are bearer-like secrets.
log = logging.getLogger("docsource")


class SourceUnavailable(Exception):
    """Raised when the document source cannot be reached after retries.

    The pipeline turns this into a failed run with a readable message —
    never a crash, and never (per the enterprise rules) a browser popup
    from a background worker.
    """


class LocalFolderSource:
    """Development default: read straight from the samples folder."""

    def __init__(self) -> None:
        self.folder = config.REPO_ROOT / "samples" / "generated" / "reference"

    def list_names(self) -> list[str]:
        if not self.folder.is_dir():
            return []
        return sorted(p.name for p in self.folder.iterdir() if p.is_file())

    def get_reference(self, name: str) -> bytes:
        path = self.folder / name
        if not path.is_file():
            raise SourceUnavailable(f"Reference document {name!r} not found in {self.folder}")
        return path.read_bytes()


# Statuses where retrying cannot possibly help: the request itself is the
# problem (wrong credentials, no permission, wrong path), not the connection.
# 408 and 429 are deliberately absent — those ARE worth retrying.
NO_RETRY_STATUSES = {400, 401, 403, 404, 405, 410, 501}

# Markers of a name-resolution failure, across platforms: Windows raises
# WSAHOST_NOT_FOUND (11001), Unix EAI_NONAME (8). The run's error text is
# read by a reviewer, not an engineer, so "ConnectError" gets translated.
_DNS_MARKERS = ("getaddrinfo", "11001", "nodename nor servname",
                "name or service not known", "temporary failure in name resolution")


def _describe(exc: Exception) -> str:
    """A short, safe label for a transport failure — no URLs, no secrets."""
    text = str(exc).lower()
    if any(m in text for m in _DNS_MARKERS):
        return ("the server name could not be looked up — this usually means "
                "the VPN or corporate network is not connected")
    if "certificate" in text or "ssl" in text or "tls" in text:
        return ("the server's security certificate was rejected — the "
                "corporate certificate may not be trusted by this app")
    if "timed out" in text or isinstance(exc, httpx.TimeoutException):
        return "the server did not answer in time"
    if isinstance(exc, httpx.ConnectError):
        return "the server refused the connection or could not be reached"
    return type(exc).__name__


class McpSource:
    """Fetch through the (fake or real) SharePoint MCP contract.

    Mirrors the probe's verified navigation: resolve the folder URL, get the
    document's metadata, follow its temporary download URL. Every call
    retries, because the probed endpoint intermittently returns ReadError.
    """

    RETRIES = 3

    def __init__(self, folder_url: str | None = None) -> None:
        from . import settings_store

        self.base = os.getenv("MCP_URL", "http://127.0.0.1:8003")
        # Optional auth for the enterprise MCP gateway. The scheme differs
        # per deployment, so it is configured rather than assumed: set
        # MCP_AUTH_HEADER (e.g. "Authorization") and MCP_AUTH_VALUE (e.g.
        # "Bearer <token>") in .env. Unset = no header, which is correct
        # for the local fake MCP.
        header = os.getenv("MCP_AUTH_HEADER", "").strip()
        value = os.getenv("MCP_AUTH_VALUE", "").strip()
        self.headers = {header: value} if header and value else {}
        # Callers processing a run pass that run's snapshotted folder URL;
        # otherwise fall back to the on-screen setting (which itself falls
        # back to the SHAREPOINT_FOLDER_URL .env value until first save).
        self.folder_url = folder_url or settings_store.get_setting("sharepoint_folder_url")
        # Logged so a misconfigured .env is visible at a glance. These are
        # ordinary configured addresses, not the temporary download links
        # that the rest of this class is careful never to expose.
        # Whether auth is configured is logged; the value never is.
        log.info("MCP source: base=%s folder=%s auth_header=%s",
                 self.base, self.folder_url,
                 next(iter(self.headers), "(none set)"))

    def _call(self, tool: str, body: dict) -> dict:
        url = f"{self.base}/tools/{tool}"
        last_error = ""
        attempts = 0
        for attempt in range(1, self.RETRIES + 1):
            attempts = attempt
            try:
                r = httpx.post(url, json=body, timeout=15, headers=self.headers)
                if r.status_code == 500 and "ReadError" in r.text:
                    last_error = "HTTP 500 ReadError"  # known-transient: retry
                    log.warning("MCP %s attempt %d: transient ReadError from %s",
                                tool, attempt, url)
                    time.sleep(0.2 * attempt)
                    continue
                r.raise_for_status()
                return r.json()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                # The status code is safe to surface; the response body is not
                # (it can echo request URLs), so the body goes to the log only.
                last_error = f"HTTP {status}"
                log.warning("MCP %s attempt %d: HTTP %d from %s -- body: %.500s",
                            tool, attempt, status, url, exc.response.text)
                if status in NO_RETRY_STATUSES:
                    break
                time.sleep(0.2 * attempt)
            except httpx.HTTPError as exc:
                # Exception text can contain URLs — log it, don't raise it.
                # exc_info gives the full chain, which is the only way to tell
                # a TLS trust failure from a proxy refusal from a DNS miss.
                last_error = _describe(exc)
                log.warning("MCP %s attempt %d failed calling %s: %s",
                            tool, attempt, url, exc, exc_info=True)
                time.sleep(0.2 * attempt)
        raise SourceUnavailable(
            f"SharePoint source unavailable after {attempts} attempt(s) "
            f"calling {tool} ({last_error}). Details are in the server log."
        )

    def list_names(self) -> list[str]:
        """Every file name in the configured folder.

        Needed because real client folders use human file names ("ICMR -
        FY2026 Payment Listing.xlsx"), so the pipeline must look at what is
        actually there rather than demand fixed names.
        """
        resolved = self._call("resolve_folder_url", {"url": self.folder_url})
        items = self._call("list_library_items", {
            "site_id": resolved["site_id"], "library": resolved["library"],
        }).get("items", [])
        return sorted(
            str(i.get("name", "")) for i in items
            if i.get("kind", "file") == "file" and i.get("name")
        )

    def get_reference(self, name: str) -> bytes:
        resolved = self._call("resolve_folder_url", {"url": self.folder_url})
        # Download URLs are temporary, bearer-like, and single-use: a retry
        # must fetch FRESH metadata for a fresh link, and neither the link
        # nor raw exception text may leave the server layer.
        last_error = ""
        for attempt in range(1, self.RETRIES + 1):
            meta = self._call("get_document_metadata", {
                "site_id": resolved["site_id"], "library": resolved["library"],
                "item_id": name,
            })
            try:
                r = httpx.get(meta["download_url"], timeout=30)
                r.raise_for_status()
                return r.content
            except httpx.HTTPStatusError as exc:
                last_error = f"HTTP {exc.response.status_code}"
                log.warning("download of %s attempt %d: HTTP %d",
                            name, attempt, exc.response.status_code)
                time.sleep(0.2 * attempt)
            except httpx.HTTPError as exc:
                last_error = _describe(exc)
                log.warning("download of %s attempt %d failed: %s",
                            name, attempt, exc, exc_info=True)
                time.sleep(0.2 * attempt)
        raise SourceUnavailable(
            f"Download of {name!r} failed after {self.RETRIES} attempts "
            f"({last_error}). Details are in the server log."
        )


class RealMcpSource:
    """The enterprise SharePoint service, over the real MCP protocol.

    McpSource above speaks a bespoke REST contract that only the local
    fake implements. A genuine MCP server is a JSON-RPC conversation with
    an `initialize` handshake and a `tools/list` catalogue, so this class
    discovers the server's own tool names instead of assuming them.

    Every deployment names its tools differently. Keyword discovery
    covers the common spellings; when it cannot decide, set the name
    explicitly in .env (MCP_TOOL_RESOLVE_FOLDER, MCP_TOOL_LIST_ITEMS,
    MCP_TOOL_GET_DOCUMENT) — the error message lists exactly what the
    server offers, so filling those in is a copy-paste job.
    """

    # Candidate spellings per role, most specific first. The enterprise
    # gateway's documented tools are whoami / site discovery /
    # list_document_libraries / list_library_items / download_document —
    # note it has NO resolve-folder tool, so that step is optional.
    RESOLVE_KEYWORDS = (("resolve", "folder"), ("folder", "url"),
                        ("resolve", "path"))
    LIST_KEYWORDS = (("list", "library", "item"), ("list", "item"),
                     ("list", "file"), ("list", "children"),
                     ("list", "folder"), ("list", "drive"))
    DOCUMENT_KEYWORDS = (("download", "document"), ("download",),
                         ("get", "document"), ("document", "metadata"),
                         ("get", "file"), ("read", "file"))
    # Optional identity check: the documented gateway exposes whoami, and
    # calling it first turns "everything fails" into "sign-in is missing".
    WHOAMI_KEYWORDS = (("whoami",), ("who", "am", "i"), ("current", "user"))

    def __init__(self, folder_url: str | None = None) -> None:
        from . import settings_store

        self.url = os.getenv("MCP_URL", "")
        header = os.getenv("MCP_AUTH_HEADER", "").strip()
        value = os.getenv("MCP_AUTH_VALUE", "").strip()
        self.headers = {header: value} if header and value else {}
        self.folder_url = folder_url or settings_store.get_setting("sharepoint_folder_url")
        log.info("real MCP source: url=%s folder=%s auth_header=%s",
                 self.url, self.folder_url,
                 next(iter(self.headers), "(none set)"))

    def _tool(self, session, env_var: str, keywords) -> str:
        override = os.getenv(env_var, "").strip()
        if override:
            if override not in session.tool_names:
                raise SourceUnavailable(
                    f"{env_var}={override!r} is not a tool this server offers. "
                    f"It offers: {', '.join(session.tool_names) or '(none)'}")
            return override
        return session.find_tool(*keywords)

    async def _resolve(self, session) -> dict:
        """Turn the folder URL into whatever identifiers this server uses.

        Optional by design: the documented enterprise gateway has no
        resolve-folder tool at all (it goes site discovery ->
        list_document_libraries -> list_library_items). When no such tool
        exists we carry the raw URL forward and let the schema filter
        decide what the next tool actually wants.
        """
        from .mcp_client import McpError

        try:
            tool = self._tool(session, "MCP_TOOL_RESOLVE_FOLDER",
                              self.RESOLVE_KEYWORDS)
        except (McpError, SourceUnavailable):
            log.info("no resolve-folder tool on this server; passing the "
                     "folder URL straight through")
            return {"url": self.folder_url}
        resolved = await session.call(tool, {"url": self.folder_url})
        return resolved if isinstance(resolved, dict) else {"result": resolved}

    async def _alist_names(self) -> list[str]:
        from .mcp_client import McpSession

        async with McpSession(self.url, self.headers) as session:
            resolved = await self._resolve(session)
            tool = self._tool(session, "MCP_TOOL_LIST_ITEMS", self.LIST_KEYWORDS)
            payload = await session.call(tool, _folder_args(resolved, self.folder_url))
        return sorted(_names_from(payload))

    async def _aget_reference(self, name: str) -> bytes:
        from .mcp_client import McpSession

        async with McpSession(self.url, self.headers) as session:
            resolved = await self._resolve(session)
            tool = self._tool(session, "MCP_TOOL_GET_DOCUMENT", self.DOCUMENT_KEYWORDS)
            args = _folder_args(resolved, self.folder_url)
            payload = await session.call(tool, {**args, "item_id": name, "name": name})
            # Servers answer either with bytes inline (base64) or with a
            # temporary download link. The link is bearer-like: fetch it
            # here, never log it, never return it.
            data = _inline_bytes(payload)
            if data is not None:
                return data
            link = _download_link(payload)
            if not link:
                raise SourceUnavailable(
                    f"The document tool returned neither file content nor a "
                    f"download link for {name!r}. Keys returned: "
                    f"{sorted(payload) if isinstance(payload, dict) else type(payload).__name__}")
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.get(link, headers=self.headers)
                r.raise_for_status()
                return r.content

    def _guarded(self, coro, what: str):
        from .mcp_client import McpError, run_sync

        try:
            return run_sync(coro)
        except McpError as exc:
            log.warning("MCP %s failed: %s", what, exc, exc_info=True)
            raise SourceUnavailable(str(exc)) from exc
        except Exception as exc:
            log.warning("MCP %s failed: %s", what, exc, exc_info=True)
            raise SourceUnavailable(
                f"SharePoint source unavailable ({_describe(exc)}). "
                "Details are in the server log.") from exc

    def list_names(self) -> list[str]:
        return self._guarded(self._alist_names(), "list_names")

    def get_reference(self, name: str) -> bytes:
        return self._guarded(self._aget_reference(name), f"get_reference {name!r}")


def _folder_args(resolved: dict, folder_url: str) -> dict:
    """Whatever identifiers the resolve step produced, plus the raw URL.

    Servers disagree on what identifies a folder (site_id + library, a
    drive_id, a path). Passing through everything resolve returned, with
    the original URL as a fallback, avoids hardcoding one server's shape.
    """
    args = {k: v for k, v in resolved.items()
            if isinstance(v, (str, int)) and k not in ("name", "kind")}
    args.setdefault("url", folder_url)
    return args


def _names_from(payload) -> list[str]:
    """File names out of a list-items result, whatever it is wrapped in."""
    if isinstance(payload, dict):
        for key in ("items", "files", "value", "children", "documents", "results"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list):
        return []
    names = []
    for item in payload:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            if item.get("kind") not in (None, "file") or item.get("folder"):
                continue  # a subfolder, not a document
            name = item.get("name") or item.get("displayName") or item.get("title")
            if name:
                names.append(str(name))
    return names


def _inline_bytes(payload) -> bytes | None:
    import base64

    if not isinstance(payload, dict):
        return None
    for key in ("content_base64", "contentBytes", "base64", "data"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw:
            try:
                return base64.b64decode(raw, validate=True)
            except Exception:
                return None
    return None


def _download_link(payload) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("download_url", "downloadUrl",
                "@microsoft.graph.downloadUrl", "url", "href"):
        link = payload.get(key)
        if isinstance(link, str) and link.startswith("http"):
            return link
    return ""


def get_source(folder_url: str | None = None):
    """Pick the source from .env.

    DOC_SOURCE=local (default) reads the samples folder. DOC_SOURCE=mcp
    goes to SharePoint, and MCP_PROTOCOL decides how:
      rest (default) — the bespoke contract the local fake implements
      mcp            — the real Model Context Protocol, for the enterprise
                       gateway. Set this on Windows.

    folder_url is a run's snapshotted SharePoint folder; None means the
    current on-screen setting. The local development source ignores it.
    """
    if os.getenv("DOC_SOURCE", "local").lower() != "mcp":
        return LocalFolderSource()
    if os.getenv("MCP_PROTOCOL", "rest").lower() == "mcp":
        return RealMcpSource(folder_url)
    return McpSource(folder_url)
