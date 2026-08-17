"""A real MCP client, spoken over streamable HTTP.

The enterprise SharePoint service is a Model Context Protocol server: a
JSON-RPC conversation over one HTTP endpoint, beginning with an
`initialize` handshake and offering a `tools/list` catalogue. The earlier
adapter instead POSTed to invented REST paths (`/tools/<name>`), which
only ever worked because the local fake implemented the same invention.

Two rules shape this module:

  - Tool NAMES are discovered, never assumed. Every deployment names its
    tools differently, so callers say what they need ("resolve a folder
    URL") and we match it against the server's own catalogue. When
    nothing matches, the error lists what the server actually offers —
    the single most useful line for configuring a new gateway.
  - Nothing here interprets documents. It moves JSON and bytes; meaning
    belongs to docsource.py.
"""
from __future__ import annotations

import logging
from typing import Any

try:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
except ImportError as exc:  # pragma: no cover - a setup fault, not a code path
    # A `git pull` that changes requirements.txt does nothing to an
    # already-built environment unless something installs it. When that is
    # missed, the new code meets the old libraries and fails with a name
    # nobody can act on. Say what to do instead.
    raise ImportError(
        "The MCP libraries are missing or too old (this app needs mcp 2.0 "
        "or newer). This usually means the Python packages were not "
        "re-installed after the last update. Close the app and run "
        "start.bat again, or run:\n"
        "    backend\\.venv\\Scripts\\python.exe -m pip install -r "
        "backend\\requirements.txt\n"
        "Run backend\\scripts\\doctor.py to check the whole setup."
    ) from exc

log = logging.getLogger("mcp_client")

# How long any single MCP call may take. Generous: the enterprise gateway
# resolves SharePoint sites on our behalf, which is not instant.
TIMEOUT_SECONDS = 60.0


class McpError(Exception):
    """An MCP call failed, or the server offers no tool for what we need."""


def run_sync(coro):
    """Run one coroutine to completion from synchronous code.

    The document source is called from ordinary sync functions, but the
    MCP SDK is async. asyncio.run() cannot be used because the pipeline
    is often already inside a running loop, so the work goes to a private
    thread with its own loop. One handshake per operation makes this a
    negligible cost next to the download it wraps.
    """
    import asyncio
    from concurrent.futures import ThreadPoolExecutor

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # no loop here: run directly
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _text_of(result: Any) -> str:
    """The text payload of a CallToolResult, joined across content blocks."""
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


def result_payload(result: Any) -> Any:
    """A tool result as plain Python.

    Prefers structured_content (MCP's typed channel); falls back to
    parsing the text blocks as JSON, and finally to the raw text — real
    servers vary, and a usable string beats an exception.
    """
    structured = getattr(result, "structured_content", None)
    if structured:
        return structured
    text = _text_of(result)
    if not text:
        return {}
    try:
        import json

        return json.loads(text)
    except ValueError:
        return text


class McpSession:
    """One MCP conversation. Use as an async context manager.

    A session is per-operation, not per-process: the handshake is cheap
    relative to a document download, and a long-lived session across an
    enterprise gateway is a reconnection problem we do not need.
    """

    def __init__(self, url: str, headers: dict[str, str] | None = None,
                 auth: Any = None) -> None:
        """headers is a static credential; auth is a live one.

        The enterprise gateway wants BOTH: its own API key in a header,
        and a delegated Entra OAuth token that has to be obtained,
        refreshed, and stored. `auth` takes any httpx2.Auth — including
        the SDK's own mcp.client.auth.OAuthClientProvider, which is one.

        This exists so signing in never requires touching the transport
        code below. The MCP SDK's streamable_http_client has no `auth`
        parameter of its own, which reads as "2.x cannot do OAuth" — it
        can; the auth belongs on the HTTP client we already build here.
        """
        self.url = url
        self.headers = headers or {}
        self.auth = auth
        self._session: ClientSession | None = None
        self._stack: Any = None
        self._tools: list[Any] = []

    async def __aenter__(self) -> "McpSession":
        from contextlib import AsyncExitStack

        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        try:
            # Headers (the gateway's API key) and auth (delegated OAuth)
            # ride on a client we own; the SDK builds a default one
            # otherwise, with no way to attach either.
            client = await self._stack.enter_async_context(
                httpx2.AsyncClient(headers=self.headers, auth=self.auth,
                                   timeout=TIMEOUT_SECONDS)
            )
            read, write, *_ = await self._stack.enter_async_context(
                streamable_http_client(self.url, http_client=client)
            )
            self._session = await self._stack.enter_async_context(
                ClientSession(read, write)
            )
            await self._session.initialize()
            self._tools = list((await self._session.list_tools()).tools)
            log.info("MCP %s offers %d tool(s): %s", self.url, len(self._tools),
                     ", ".join(t.name for t in self._tools))
        except BaseException:
            await self._stack.aclose()
            self._stack = None
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None

    @property
    def tools(self) -> list[Any]:
        """The server's tool catalogue, as the SDK's Tool objects."""
        return list(self._tools)

    @property
    def tool_names(self) -> list[str]:
        return [t.name for t in self._tools]

    def find_tool(self, *keyword_sets: tuple[str, ...]) -> str:
        """The server's own name for a tool we need.

        Each keyword set is a candidate spelling, tried in order: the
        first tool whose name contains ALL words of a set wins. This is
        how "resolve_folder_url" also matches "sharepoint_resolve_folder"
        without hardcoding either. Ambiguity and absence both raise —
        picking one of several plausible tools is how you end up calling
        a write tool by accident.
        """
        for words in keyword_sets:
            matches = [t.name for t in self._tools
                       if all(w in t.name.lower() for w in words)]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise McpError(
                    f"{len(matches)} tools match {words}: {', '.join(matches)}. "
                    "Set the tool name explicitly in .env rather than guessing.")
        raise McpError(
            f"No tool matches any of {list(keyword_sets)}. The server offers: "
            f"{', '.join(self.tool_names) or '(none)'}")

    def accepted_arguments(self, tool: str) -> set[str] | None:
        """The argument names a tool declares, or None if it declares none.

        Servers validate against their published input schema and reject
        unknown properties, so callers cannot simply pass everything they
        happen to know. Filtering against this is what lets one client
        work with servers that identify a folder differently.
        """
        for t in self._tools:
            if t.name == tool:
                schema = getattr(t, "input_schema", None) or {}
                props = schema.get("properties") if isinstance(schema, dict) else None
                return set(props) if props else None
        return None

    async def call(self, tool: str, arguments: dict) -> Any:
        if self._session is None:
            raise McpError("call() outside the session context")
        accepted = self.accepted_arguments(tool)
        if accepted is not None:
            dropped = sorted(set(arguments) - accepted)
            if dropped:
                log.debug("tool %s does not accept %s — dropping", tool, dropped)
            arguments = {k: v for k, v in arguments.items() if k in accepted}
        result = await self._session.call_tool(tool, arguments)
        if getattr(result, "is_error", False):
            # Server-reported failures carry their reason in the content.
            raise McpError(f"tool {tool!r} failed: {_text_of(result)[:500]}")
        return result_payload(result)
