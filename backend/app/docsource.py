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

    # -- folder walking (claims batches) ---------------------------------
    # In local development a "folder link" is a folder on this machine,
    # so a whole batch tree can be read without SharePoint or a zip.

    def list_folder(self, folder_url: str, rel: str = "") -> list[dict]:
        root = Path(folder_url).expanduser()
        if not root.is_dir():
            raise SourceUnavailable(
                f"{folder_url!r} is not a folder on this machine. In local mode "
                "the folder link must be a folder path (or upload a zip).")
        target = _contained(root, rel, "folder")
        entries = []
        for p in sorted(target.iterdir()):
            if p.name.startswith("."):
                continue
            entries.append(folder_entry(
                name=p.name, kind="folder" if p.is_dir() else "file",
                size=None if p.is_dir() else p.stat().st_size,
                item_id=str(p.relative_to(root)), rel=rel))
        return entries

    def download(self, folder_url: str, entry: dict) -> bytes:
        # The SAME containment check `list_folder` makes: `entry["path"]`
        # is normally one our own walk produced, but it arrives through
        # the walker and must not be trusted to stay inside the batch
        # folder — an entry naming `../../.ssh/id_rsa` (or an absolute
        # path) is refused here, not read and ingested into a run.
        root = Path(folder_url).expanduser()
        path = _contained(root, str(entry.get("path") or ""), "file")
        if not path.is_file():
            raise SourceUnavailable(f"{entry['path']!r} not found under {folder_url}")
        return path.read_bytes()


def _contained(root: Path, rel: str, what: str) -> Path:
    """`root / rel`, required to stay inside `root` once resolved (so
    `..`, an absolute path and a symlink out of the tree are all refused)."""
    target = (root / rel) if rel else root
    resolved, root_resolved = target.resolve(), root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise SourceUnavailable(f"{what} {rel!r} is outside the batch folder")
    return target


def folder_entry(name: str, kind: str, size, item_id: str, rel: str) -> dict:
    """One line of a folder listing, in the shape the claims walker uses,
    whichever source produced it. `path` is relative to the batch folder;
    `id` is whatever the source needs to fetch the entry again."""
    return {"name": name, "kind": kind, "size": size, "id": item_id,
            "path": f"{rel}/{name}" if rel else name}


# Statuses where retrying cannot possibly help: the request itself is the
# problem (wrong credentials, no permission, wrong path), not the connection.
# 408 and 429 are deliberately absent — those ARE worth retrying.
NO_RETRY_STATUSES = {400, 401, 403, 404, 405, 410, 501}

def _describe(exc: Exception) -> str:
    """A short, safe label for a transport failure — no URLs, no secrets.

    One shared translator with telemetry, so a failure does not read one
    way in the run diary and another in the run's error text. It also
    unwraps the ExceptionGroups the MCP SDK's task groups raise, which a
    local copy of this logic kept failing to do.
    """
    from .telemetry import describe_failure

    return describe_failure(exc)


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

    # -- folder walking (claims batches) ---------------------------------

    def list_folder(self, folder_url: str, rel: str = "") -> list[dict]:
        resolved = self._call("resolve_folder_url", {"url": folder_url})
        base = str(resolved.get("folder_path", "")).rstrip("/")
        items = self._call("list_library_items", {
            "site_id": resolved["site_id"], "library": resolved["library"],
            "folder_path": f"{base}/{rel}" if rel else base,
        }).get("items", [])
        return [folder_entry(name=str(i.get("name", "")),
                             kind="folder" if i.get("kind") == "folder" else "file",
                             size=i.get("size"), item_id=str(i.get("item_id", i.get("name"))),
                             rel=rel)
                for i in items if i.get("name")]

    def download(self, folder_url: str, entry: dict) -> bytes:
        resolved = self._call("resolve_folder_url", {"url": folder_url})
        last_error = ""
        for attempt in range(1, self.RETRIES + 1):
            meta = self._call("get_document_metadata", {
                "site_id": resolved["site_id"], "library": resolved["library"],
                "item_id": entry["id"],
            })
            try:
                r = httpx.get(meta["download_url"], timeout=30)
                r.raise_for_status()
                return r.content
            except httpx.HTTPError as exc:
                last_error = _describe(exc)
                log.warning("download of %s attempt %d failed: %s",
                            entry["path"], attempt, exc, exc_info=True)
                time.sleep(0.2 * attempt)
        raise SourceUnavailable(
            f"Download of {entry['path']!r} failed after {self.RETRIES} attempts "
            f"({last_error}). Details are in the server log.")


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
    # Used only when there is no resolve-folder tool. Ordered most
    # specific first because several tools mention "site" — matching on
    # the bare word picks up whoami-style and search-style tools too, and
    # find_tool refuses to guess between them.
    SITE_KEYWORDS = (("get", "sharepoint", "site"), ("get", "site"),
                     ("sharepoint", "site"), ("resolve", "site"), ("site",))
    LIBRARY_KEYWORDS = (("list", "document", "librar"), ("list", "librar"),
                        ("get", "librar"), ("librar",), ("drive",))
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

        if os.getenv("MCP_TOOL_RESOLVE_FOLDER", "").strip():
            # Explicitly configured: a wrong name must fail loudly, not
            # be mistaken for "this server has no resolve tool".
            tool = self._tool(session, "MCP_TOOL_RESOLVE_FOLDER",
                              self.RESOLVE_KEYWORDS)
        else:
            try:
                tool = session.find_tool(*self.RESOLVE_KEYWORDS)
            except McpError:
                log.info("no resolve-folder tool on this server; navigating "
                         "from the folder URL instead")
                return await self._navigate(session)
        resolved = await session.call(tool, {"url": self.folder_url})
        return resolved if isinstance(resolved, dict) else {"result": resolved}

    async def _navigate(self, session) -> dict:
        """Find the folder the long way: site, then library, then done.

        The documented enterprise gateway has no single "here is a URL,
        give me the folder" tool. It has the three steps a person would
        take by hand, so this takes them: read the pasted browser address,
        ask for the site, ask that site for its libraries, and match the
        one the address named.
        """
        from .mcp_client import McpError

        address = parse_sharepoint_folder_url(self.folder_url)

        try:
            site_tool = self._tool(session, "MCP_TOOL_SITE", self.SITE_KEYWORDS)
        except McpError as exc:
            raise SourceUnavailable(
                f"This MCP server offers no way to look up a SharePoint site, "
                f"and no resolve-folder tool either, so the folder cannot be "
                f"found. {exc} You can name it directly by putting "
                f"MCP_TOOL_SITE=<tool name> in .env.") from exc
        site = await session.call(site_tool, {
            "url": address.site_url, "site_url": address.site_url,
            "site_path": address["site_path"], "hostname": address["host"],
            "host": address["host"], "site_name": address["site_path"].rsplit("/", 1)[-1],
        })
        _raise_if_error(site, f"find the SharePoint site {address.site_url}")
        site_id = _first_string(site, ("site_id", "siteId", "id"))
        if not site_id:
            raise SourceUnavailable(
                f"The site lookup did not return a site id for "
                f"{address.site_url}. It returned: "
                f"{sorted(site) if isinstance(site, dict) else type(site).__name__}")

        # The library step is optional: some gateways accept a library by
        # name directly, and asking for a list we do not need is a wasted
        # call plus another way to fail.
        library_id = ""
        try:
            library_tool = self._tool(session, "MCP_TOOL_LIST_LIBRARIES",
                                      self.LIBRARY_KEYWORDS)
        except McpError:
            log.info("no list-libraries tool; using the library name from the URL")
        else:
            libraries = await _all_pages(
                session, library_tool,
                {"site_id": site_id, "siteId": site_id, "url": address.site_url},
                "list the site's document libraries")
            matched = _match_library(libraries, address["library"],
                                     address["browser_library"])
            if matched is None:
                offered = ", ".join(_names_from(libraries)) or "(none listed)"
                raise SourceUnavailable(
                    f"The site has no document library called "
                    f"{address['browser_library']!r}. It offers: {offered}. "
                    f"Check the SharePoint folder setting.")
            library_id = _first_string(matched, _ID_KEYS)

        resolved = {"site_id": site_id, "library": address["library"]}
        if library_id:
            # Both, because gateways differ on which they want and the
            # schema filter drops whichever this one does not declare.
            resolved["library_id"] = library_id
            resolved["drive_id"] = library_id
        if address["folder_path"]:
            resolved["folder_path"] = address["folder_path"]
            resolved["path"] = address["folder_path"]
        log.info("navigated %s -> site %s, library %r, folder %r",
                 self.folder_url, site_id, address["library"],
                 address["folder_path"] or "(root)")
        return resolved

    async def _alisting(self, session, resolved: dict):
        """Every item in the folder, across every page.

        Kept as raw entries (not reduced to names) because the download
        step needs the identifiers that live alongside each name.
        """
        tool = self._tool(session, "MCP_TOOL_LIST_ITEMS", self.LIST_KEYWORDS)
        missing = session.required_arguments(tool) - set(
            _folder_args(resolved, self.folder_url))
        if missing:
            # Saying which earlier step should have produced it beats
            # letting the server answer "'drive_id' is a required
            # property" — that names the symptom, not the gap.
            named = ", ".join(sorted(missing))
            if missing & _LIBRARY_ID_KEYS:
                raise SourceUnavailable(
                    f"The folder could not be listed because {named} is "
                    f"needed and was not found earlier — the document library "
                    f"lookup did not return an id for "
                    f"{resolved.get('library', 'the library')!r}. Check the "
                    f"SharePoint folder setting names a real library.")
            raise SourceUnavailable(
                f"The folder could not be listed because {tool!r} requires "
                f"{named}, which this app does not know how to supply. Name "
                f"the right tool with MCP_TOOL_LIST_ITEMS in .env, or pass "
                f"these argument names on to whoever maintains this app.")
        return await _all_pages(session, tool,
                                _folder_args(resolved, self.folder_url),
                                "list the SharePoint folder")

    def _auth(self):
        """The delegated sign-in, if this gateway needs one.

        interactive=False without exception: this class only ever runs
        inside a background pipeline run, and a background run must never
        open a browser. The Connect button builds its own provider.
        """
        from .sharepoint_auth import build_provider

        return build_provider(self.url, interactive=False)

    async def _alist_names(self) -> list[str]:
        from .mcp_client import McpSession

        async with McpSession(self.url, self.headers, auth=self._auth()) as session:
            resolved = await self._resolve(session)
            payload = await self._alisting(session, resolved)
        return sorted(_names_from(payload))

    async def _aget_reference(self, name: str) -> bytes:
        from .mcp_client import McpSession

        async with McpSession(self.url, self.headers, auth=self._auth()) as session:
            resolved = await self._resolve(session)
            # Real SharePoint addresses a document by an opaque drive-item
            # ID, never by its display name, so the listing is re-read to
            # translate the one into the other. Servers that genuinely are
            # name-addressed publish no ID; for those the name is the ID.
            listing = await self._alisting(session, resolved)
            item_id = _item_id_for(listing, name) or name

            tool = self._tool(session, "MCP_TOOL_GET_DOCUMENT", self.DOCUMENT_KEYWORDS)
            # Servers spell the identifier argument differently. Send it
            # under every spelling THIS tool declares — and plain item_id
            # when it declares no schema at all, which is what the previous
            # behaviour assumed.
            accepted = session.accepted_arguments(tool)
            id_args = [k for k in _ID_KEYS if accepted and k in accepted] or ["item_id"]
            args = {**_folder_args(resolved, self.folder_url), "name": name}
            args.update(dict.fromkeys(id_args, item_id))

            payload = await session.call(tool, args)
            # Servers answer either with bytes inline (base64) or with a
            # temporary download link. The link is bearer-like: fetch it
            # here, never log it, never return it.
            data = _inline_bytes(payload)
            if data is not None:
                return data
            link = _download_link(payload)
            if not link:
                log.warning("document tool %s returned no content for %r "
                            "(item id %r); payload keys: %s", tool, name, item_id,
                            sorted(payload) if isinstance(payload, dict) else type(payload).__name__)
                raise SourceUnavailable(_no_document_reason(name, payload))
            # The auth header is the GATEWAY's credential. Download links
            # usually point at a different host (pre-signed storage) and
            # carry their own authorisation, so the key travels only when
            # the link stays on the gateway itself.
            link_headers = self.headers if _same_host(link, self.url) else {}
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.get(link, headers=link_headers)
                r.raise_for_status()
                return r.content

    def _guarded(self, coro, what: str):
        from .mcp_client import McpError, run_sync
        from .sharepoint_auth import SignInRequired

        try:
            return run_sync(coro)
        except SourceUnavailable:
            raise  # already carries its specific, user-facing reason
        except SignInRequired as exc:
            # Already written for the reviewer, and it tells them exactly
            # which button to press. Passed through untouched.
            log.warning("MCP %s needs a sign-in: %s", what, exc)
            raise SourceUnavailable(str(exc)) from exc
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

    # -- folder walking (claims batches) ---------------------------------
    # A batch is a folder of employee subfolders. Listing a SUBfolder is
    # the same list-items call with the subfolder's path (and, when the
    # listing published one, its id) in place of the batch folder's.

    def _subfolder(self, resolved: dict, rel: str) -> dict:
        if not rel:
            return resolved
        sub = dict(resolved)
        base = str(resolved.get("folder_path") or resolved.get("path") or "").strip("/")
        path = f"{base}/{rel}" if base else rel
        sub["folder_path"] = path
        sub["path"] = path
        return sub

    async def _alist_folder(self, folder_url: str, rel: str) -> list[dict]:
        from .mcp_client import McpSession

        async with McpSession(self.url, self.headers, auth=self._auth()) as session:
            resolved = await self._resolve(session)
            payload = await self._alisting(session, self._subfolder(resolved, rel))
        entries = []
        for item in _unwrap_items(payload):
            if not isinstance(item, dict):
                continue
            name = _item_name(item)
            if not name:
                continue
            is_folder = (item.get("kind") == "folder" or bool(item.get("folder"))
                         or item.get("type") == "folder")
            entries.append(folder_entry(
                name=name, kind="folder" if is_folder else "file",
                size=item.get("size"), item_id=_item_id_for({"items": [item]}, name) or name,
                rel=rel))
        return entries

    async def _adownload(self, folder_url: str, entry: dict) -> bytes:
        from .mcp_client import McpSession

        parent, _, name = entry["path"].rpartition("/")
        async with McpSession(self.url, self.headers, auth=self._auth()) as session:
            resolved = self._subfolder(await self._resolve(session), parent)
            tool = self._tool(session, "MCP_TOOL_GET_DOCUMENT", self.DOCUMENT_KEYWORDS)
            accepted = session.accepted_arguments(tool)
            id_args = [k for k in _ID_KEYS if accepted and k in accepted] or ["item_id"]
            args = {**_folder_args(resolved, folder_url), "name": name}
            args.update(dict.fromkeys(id_args, entry["id"]))
            payload = await session.call(tool, args)
            data = _inline_bytes(payload)
            if data is not None:
                return data
            link = _download_link(payload)
            if not link:
                raise SourceUnavailable(_no_document_reason(entry["path"], payload))
            link_headers = self.headers if _same_host(link, self.url) else {}
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.get(link, headers=link_headers)
                r.raise_for_status()
                return r.content

    def list_folder(self, folder_url: str, rel: str = "") -> list[dict]:
        self.folder_url = folder_url
        return self._guarded(self._alist_folder(folder_url, rel), f"list_folder {rel!r}")

    def download(self, folder_url: str, entry: dict) -> bytes:
        self.folder_url = folder_url
        return self._guarded(self._adownload(folder_url, entry), f"download {entry['path']!r}")


# --- reading a SharePoint address the way a person copies it ----------------
# The enterprise gateway has no resolve-folder tool, so the browser URL a
# reviewer pastes has to be taken apart here. Two forms appear in practice:
#
#   .../sites/ClientABC/Shared Documents/AP Reference
#   .../sites/ClientABC/Shared Documents/Forms/AllItems.aspx?id=%2Fsites%2F…
#
# The second is what the address bar shows once you click into a folder:
# the real path hides in the "id" query parameter, and the visible path
# stops at the library.
_VIEW_PAGES = ("forms/allitems.aspx", "forms/dispform.aspx", "forms/editform.aspx")

# SharePoint shows "Shared Documents" in the browser and calls the same
# library "Documents" over the API. Matching literally finds nothing.
_LIBRARY_ALIASES = {"shared documents": "Documents"}


class FolderAddress(dict):
    """Where a SharePoint folder lives, in the parts the gateway asks for."""

    @property
    def site_url(self) -> str:
        return f"{self['scheme']}://{self['host']}{self['site_path']}"


def parse_sharepoint_folder_url(url: str) -> FolderAddress:
    """Split a browser folder URL into site, library, and folder path.

    Raises SourceUnavailable with a readable message rather than guessing:
    a misread address means listing the wrong folder, and checking this
    month's invoices against the wrong listing is worse than stopping.
    """
    from urllib.parse import parse_qs, unquote, urlsplit

    parts = urlsplit((url or "").strip())
    if not parts.scheme or not parts.netloc:
        raise SourceUnavailable(
            "The SharePoint folder setting is not a full web address. Open the "
            "folder in your browser and copy the whole address bar, starting "
            "with https://.")

    # When the address is a view page, the real path is in "id" (or
    # "RootFolder"), and the visible path is just the page's own location.
    path = unquote(parts.path)
    if any(page in path.lower() for page in _VIEW_PAGES):
        query = parse_qs(parts.query)
        inner = (query.get("id") or query.get("RootFolder")
                 or query.get("rootfolder") or [""])[0]
        if not inner:
            raise SourceUnavailable(
                "That SharePoint address points at a list view rather than a "
                "folder. Click into the folder itself, then copy the address.")
        path = unquote(inner)

    segments = [s for s in path.split("/") if s]
    # A site lives at /sites/<name> or /teams/<name>; without either, the
    # address is the tenant root and names no document library.
    for marker in ("sites", "teams"):
        if marker in segments:
            index = segments.index(marker)
            if len(segments) > index + 1:
                site_path = "/" + "/".join(segments[:index + 2])
                rest = segments[index + 2:]
                break
    else:
        raise SourceUnavailable(
            "That SharePoint address does not include a site (the /sites/… or "
            "/teams/… part). Copy the address from inside the document "
            "library you want to use.")

    if not rest:
        raise SourceUnavailable(
            "That SharePoint address stops at the site and does not name a "
            "document library. Open the library (for example Documents), then "
            "copy the address.")

    library = rest[0]
    folder_path = "/".join(rest[1:])
    # "Shared Documents" arrives as two segments in some tenants.
    if len(rest) > 1 and f"{rest[0]} {rest[1]}".lower() in _LIBRARY_ALIASES:
        library = f"{rest[0]} {rest[1]}"
        folder_path = "/".join(rest[2:])

    return FolderAddress(
        scheme=parts.scheme, host=parts.netloc, site_path=site_path,
        library=_LIBRARY_ALIASES.get(library.lower(), library),
        browser_library=library, folder_path=folder_path,
    )


def _match_library(payload, library: str, browser_library: str) -> dict | None:
    """The library entry the pasted address named, or None.

    Matched against both spellings because the browser says "Shared
    Documents" and the API says "Documents", and different gateways
    report one, the other, or both.
    """
    wanted = {library.strip().lower(), browser_library.strip().lower()}
    wanted |= {_LIBRARY_ALIASES.get(w, "").lower() for w in list(wanted)} - {""}
    for item in _unwrap_items(payload):
        if not isinstance(item, dict):
            continue
        names = {_item_name(item).strip().lower()}
        names |= {str(item.get(k, "")).strip().lower()
                  for k in ("library", "libraryName", "driveName")}
        if names & wanted:
            return item
    return None


def _first_string(payload, keys: tuple[str, ...]) -> str:
    """The first of `keys` holding a non-empty string, at the top level or
    one envelope down. Servers wrap single results inconsistently."""
    candidates = [payload]
    if isinstance(payload, dict):
        candidates += [v for v in payload.values() if isinstance(v, dict)]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in keys:
            value = candidate.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value)
    return ""


# Where a server puts "there is more, ask again with this".
_PAGE_TOKEN_KEYS = ("skip_token", "skipToken", "next_skip_token",
                    "nextSkipToken", "next_token", "nextToken",
                    "continuation_token", "continuationToken")

# A reference folder needs a handful of pages at most. Hitting this means
# something is looping, and a partial listing is the one outcome that must
# never pass quietly: a missing payment listing would be blamed on the
# folder, and a half-read folder could match last year's file.
MAX_PAGES = 25


def _next_page_token(payload) -> tuple[str, str]:
    """(key, token) for "there is more", or ("", "") when the page is last."""
    if not isinstance(payload, dict):
        return "", ""
    for key in _PAGE_TOKEN_KEYS:
        value = payload.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return key, str(value)
    return "", ""


def _cursor_arguments(session, tool: str, response_key: str) -> list[str]:
    """Which ARGUMENT this tool wants the continuation token in.

    The name a server answers with need not be the name it asks for, and
    arguments a tool does not declare are dropped before sending. Picking
    the request name out of the tool's own schema is what stops us
    politely re-requesting page one forever.
    """
    accepted = session.accepted_arguments(tool)
    if accepted is None:
        # No published schema, so nothing will be filtered. Echo the key
        # the server itself used, which most servers accept back.
        return [k for k in dict.fromkeys([response_key, "skip_token"]) if k]
    return [k for k in _PAGE_TOKEN_KEYS if k in accepted]


async def _all_pages(session, tool: str, args: dict, what: str) -> list:
    """Every page of a paginated list tool, as one list of entries.

    Both list tools on the enterprise gateway take a skip_token, which
    means a long folder arrives in pieces. Reading only the first piece
    looks exactly like a short folder, so it would never be noticed —
    the run would simply not see some of the files.

    Every way this can go wrong ends in an exception rather than a short
    list. A partial folder listing is the one outcome that must never
    pass quietly: it reads as "the payment listing is not there", or
    worse, quietly matches last year's file because this year's was on a
    page nobody fetched.
    """
    args = dict(args)
    entries: list = []
    seen: set[str] = set()
    for page in range(1, MAX_PAGES + 1):
        payload = await session.call(tool, args)
        _raise_if_error(payload, what)
        entries.extend(_unwrap_items(payload))
        response_key, token = _next_page_token(payload)
        if not token:
            if page > 1:
                log.info("%s: read %d entries across %d page(s)",
                         tool, len(entries), page)
            return entries

        if token in seen:
            # The same token twice means asking again returned the same
            # page. Treating that as "finished" would hand back a
            # duplicated, truncated folder and call it complete.
            raise SourceUnavailable(
                f"SharePoint offered the same continuation token twice when "
                f"asked to {what}, so the rest of the folder cannot be "
                f"reached. Refusing to work from a partial listing.")

        cursor_args = _cursor_arguments(session, tool, response_key)
        if not cursor_args:
            raise SourceUnavailable(
                f"SharePoint said there is more to read when asked to "
                f"{what}, but {tool!r} publishes no argument to ask for the "
                f"next page (it answered with {response_key!r}). Refusing to "
                f"work from a partial listing.")

        seen.add(token)
        for key in cursor_args:
            args[key] = token
    raise SourceUnavailable(
        f"SharePoint kept offering more pages when asked to {what} "
        f"({MAX_PAGES} requested). Refusing to continue rather than work "
        f"from part of the folder.")


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


# Envelope names seen in the wild. "libraries" is here because the
# enterprise gateway uses it and its absence read as "this site has no
# document libraries" — a wrong answer that looked like a real one.
_ENVELOPE_KEYS = ("items", "files", "value", "children", "documents",
                  "results", "libraries", "drives", "lists", "entries",
                  "document_libraries", "documentLibraries")

# Lists that are ABOUT the call rather than the answer. Never mistaken
# for content, however lonely they are in the payload.
_DIAGNOSTIC_KEYS = frozenset({"errors", "warnings", "messages", "diagnostics",
                              "notes", "logs", "links", "hints", "details"})


def _unwrap_items(payload) -> list:
    """The list of items inside whatever envelope the server wrapped it in."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in _ENVELOPE_KEYS:
        if isinstance(payload.get(key), list):
            return payload[key]
    # Last resort: exactly one key holds a list, so there is nothing to
    # confuse it with. This is what stops the NEXT unrecognised envelope
    # name from silently becoming "the folder is empty".
    candidates = [value for key, value in payload.items()
                  if isinstance(value, list)
                  and key.lower() not in _DIAGNOSTIC_KEYS]
    if len(candidates) != 1:
        return []
    only = candidates[0]
    # Entries in a real listing are objects with fields. A bare list of
    # strings under a name we do not recognise is far likelier to be
    # messages than files — and turning error text into "file names" is
    # how a failed call becomes a folder full of documents that
    # do not exist.
    if all(isinstance(entry, dict) for entry in only):
        return only
    return []


def _item_name(item: dict) -> str:
    return str(item.get("name") or item.get("displayName") or item.get("title") or "")


# How servers spell "which document". SharePoint's own is an opaque
# drive-item ID; the display name is NOT interchangeable with it.
_ID_KEYS = ("item_id", "itemId", "id", "drive_item_id", "driveItemId",
            "file_id", "fileId", "unique_id", "uniqueId")

# The subset that identifies a LIBRARY rather than a document. A missing
# one of these means the library lookup came up short, which is a
# different story from a tool wanting an argument we have never heard of.
_LIBRARY_ID_KEYS = frozenset({"drive_id", "driveId", "library_id", "libraryId"})


def _item_id_for(payload, name: str) -> str:
    """The server's own identifier for the file displayed as `name`.

    Returns "" when the listing publishes no identifier — some servers
    really are name-addressed, and callers fall back to the name for those.
    """
    items = [i for i in _unwrap_items(payload) if isinstance(i, dict)]
    # Exact first; SharePoint itself treats names case-insensitively, so a
    # case-only difference is the same file rather than a miss.
    for match in (lambda n: n == name, lambda n: n.lower() == name.lower()):
        for item in items:
            if not match(_item_name(item)):
                continue
            for key in _ID_KEYS:
                value = item.get(key)
                if isinstance(value, (str, int)) and str(value):
                    return str(value)
    return ""


def _redact_links(text: str) -> str:
    """Server text may quote a temporary download URL; those are secrets.

    One shared redactor with telemetry: two copies of "hide the secrets"
    is one copy too many, because only one of them gets fixed.
    """
    from .telemetry import redact_links

    return redact_links(text)


def _text_at(payload: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, dict):
            value = value.get("message") or value.get("code") or ""
        if isinstance(value, str) and value.strip():
            return _redact_links(value.strip())[:300]
    return ""


# Keys that MEAN failure wherever they appear. Deliberately short: a
# "message" or "detail" beside a perfectly good list of files is a
# gateway being chatty ("showing 100 of 250"), not an error, and treating
# it as one would fail a run that had already succeeded.
_ERROR_KEYS = ("error", "errorMessage")
_EXPLANATION_KEYS = ("message", "detail", "description")


def _reported_error(payload) -> str:
    """An explicit, unambiguous server-reported failure — or "".

    Used to decide whether an otherwise-successful response is really a
    failure, so it only trusts keys that can mean nothing else.
    """
    if not isinstance(payload, dict):
        return ""
    return _text_at(payload, _ERROR_KEYS)


def _failure_reason(payload) -> str:
    """Why a call failed, when we ALREADY know it failed.

    Looser than _reported_error on purpose: at this point there is no
    risk of mistaking chatter for a failure, and any sentence the server
    offers beats saying nothing.
    """
    if not isinstance(payload, dict):
        return ""
    return (_text_at(payload, _ERROR_KEYS)
            or _text_at(payload, _EXPLANATION_KEYS))


def _raise_if_error(payload, what: str) -> None:
    """Stop when a successful-looking result is actually a failure.

    This gateway reports bad requests as ordinary results carrying an
    "error" key. Every call therefore has to look, or the failure turns
    into an empty list and gets blamed on whatever comes next.
    """
    reason = _reported_error(payload)
    if reason:
        raise SourceUnavailable(f"SharePoint could not {what}: {reason}")


def _no_document_reason(name: str, payload) -> str:
    """Why the document tool produced no bytes, in words a reviewer can use."""
    reported = _failure_reason(payload)
    if reported:
        return f"SharePoint could not return {name!r}: {reported}"
    return (f"The document tool returned neither file content nor a download "
            f"link for {name!r}. Keys returned: "
            f"{sorted(payload) if isinstance(payload, dict) else type(payload).__name__}")


def _names_from(payload) -> list[str]:
    """File names out of a list-items result, whatever it is wrapped in."""
    names = []
    for item in _unwrap_items(payload):
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            if item.get("kind") not in (None, "file") or item.get("folder"):
                continue  # a subfolder, not a document
            name = _item_name(item)
            if name:
                names.append(name)
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
                continue  # not base64 under this key; another may be
    return None


def _download_link(payload) -> str:
    """A download-specific link only.

    Generic keys ("url", "href") are excluded on purpose: servers echo
    arguments back, and we SEND a "url" — accepting it here would fetch
    the folder's web page and call it a document.
    """
    if not isinstance(payload, dict):
        return ""
    for key in ("download_url", "downloadUrl",
                "@microsoft.graph.downloadUrl"):
        link = payload.get(key)
        if isinstance(link, str) and link.startswith("http"):
            return link
    return ""


def _same_host(link: str, base: str) -> bool:
    """True when a link points at the same scheme://host:port as base."""
    from urllib.parse import urlsplit

    a, b = urlsplit(link), urlsplit(base)
    return (a.scheme, a.netloc) == (b.scheme, b.netloc) and bool(a.netloc)


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
