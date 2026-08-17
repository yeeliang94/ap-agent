"""Walk the four SharePoint steps against the real gateway and report shapes.

mcp_probe.py lists what the server OFFERS. This one actually walks the
path the pipeline walks — site, libraries, folder items, download — and
prints, at each step, the tool it chose, the arguments that tool accepts,
and the KEYS of what came back.

The keys are the point. Every remaining unknown is "what does this
gateway call the thing I need" — where the site id lives, what the item
list is wrapped in, whether a download arrives inline or as a link.

    backend\\.venv\\Scripts\\python.exe backend\\scripts\\sharepoint_walk.py

Read-only: it lists and reads, and never writes to SharePoint. Values are
NOT printed — only key names, counts, and types — so the output is safe
to share. The one exception is file names, which the pipeline matches on
and which are needed to diagnose it.

Output is deliberately short: it is meant to be photographed.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402,F401  (loads .env)
from app.main import _configure_tls  # noqa: E402

W = 62


def shape(value, depth: int = 0) -> str:
    """What this value IS, never what it contains."""
    if isinstance(value, dict):
        keys = list(value)
        if depth == 0:
            return "{" + ", ".join(keys) + "}"
        return f"dict({len(keys)} keys)"
    if isinstance(value, list):
        if not value:
            return "[] empty"
        return f"[{len(value)} x {shape(value[0], depth + 1)}]"
    if isinstance(value, str):
        return f"str({len(value)})"
    return type(value).__name__


def head(label: str, text: str = "") -> None:
    print(f"\n{label} {text}".rstrip())
    print("-" * W)


async def main() -> int:
    import logging

    _configure_tls()
    # Every HTTP round trip logs a line, and the transport logs its session
    # id. Useful when debugging the transport, pure noise here — and this
    # output has to survive being photographed, so length is the enemy.
    for noisy in ("httpx", "httpx2", "mcp", "mcp_client", "docsource",
                  "uvicorn", "uvicorn.error", "sharepoint_auth"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    url = os.getenv("MCP_URL", "").strip()
    folder = os.getenv("SHAREPOINT_FOLDER_URL", "").strip()
    if not url or not folder:
        print("MCP_URL and SHAREPOINT_FOLDER_URL must both be set in .env.")
        return 1
    header = os.getenv("MCP_AUTH_HEADER", "").strip()
    value = os.getenv("MCP_AUTH_VALUE", "").strip()
    headers = {header: value} if header and value else {}

    from app.docsource import (RealMcpSource, _all_pages, _first_string,
                               _item_id_for, _match_library, _names_from,
                               _reported_error, _unwrap_items,
                               parse_sharepoint_folder_url)
    from app.mcp_client import McpSession
    from app.sharepoint_auth import build_provider

    source = RealMcpSource(folder)

    head("STEP 0", "reading the folder address")
    try:
        address = parse_sharepoint_folder_url(folder)
    except Exception as exc:
        print(f"  FAILED: {exc}")
        return 1
    print(f"  site path   : {address['site_path']}")
    print(f"  library     : {address['browser_library']} -> {address['library']}")
    print(f"  folder path : {address['folder_path'] or '(library root)'}")

    auth = build_provider(url, interactive=False) if os.getenv("MCP_OAUTH") else None

    async with McpSession(url, headers, auth=auth) as session:
        head("TOOLS", f"{len(session.tool_names)} offered")
        print(f"  shared namespace: {session.namespace or '(none)'}")
        for name in session.tool_names:
            print(f"    {name}")

        async def step(number, what, keywords, args, env_var, paged=False):
            head(f"STEP {number}", what)
            override = os.getenv(env_var, "").strip()
            try:
                tool = (override if override
                        else session.find_tool(*keywords))
            except Exception as exc:
                print(f"  NO TOOL: {exc}")
                print(f"  -> set {env_var}=<name> in .env from the list above")
                return None, None
            accepted = session.accepted_arguments(tool)
            print(f"  tool     : {tool}")
            print(f"  accepts  : {', '.join(sorted(accepted)) if accepted else '(no schema published)'}")
            sending = {k: v for k, v in args.items()
                       if accepted is None or k in accepted}
            print(f"  sending  : {', '.join(sorted(sending)) or '(nothing)'}")
            try:
                # The list steps page. Follow them here too, or this
                # report says "1 file" while the app reads the whole
                # folder — a diagnostic that disagrees with the app is
                # worse than none.
                payload = (await _all_pages(session, tool, args, what) if paged
                           else await session.call(tool, args))
            except Exception as exc:
                print(f"  FAILED   : {type(exc).__name__}: {str(exc)[:200]}")
                return tool, None
            problem = _reported_error(payload)
            if problem:
                print(f"  ERROR KEY: {problem[:200]}")
            print(f"  returned : {shape(payload)}")
            return tool, payload

        # 1 -- the site
        _, site = await step(
            1, "look up the site", source.SITE_KEYWORDS,
            {"url": address.site_url, "site_url": address.site_url,
             "site_path": address["site_path"], "hostname": address["host"],
             "host": address["host"],
             "site_name": address["site_path"].rsplit("/", 1)[-1]},
            "MCP_TOOL_SITE")
        if site is None:
            return 1
        site_id = _first_string(site, ("site_id", "siteId", "id"))
        print(f"  site id  : {'FOUND' if site_id else 'NOT FOUND — which key holds it?'}")
        if not site_id:
            return 1

        # 2 -- its document libraries
        _, libs = await step(
            2, "list document libraries", source.LIBRARY_KEYWORDS,
            {"site_id": site_id, "siteId": site_id, "url": address.site_url},
            "MCP_TOOL_LIST_LIBRARIES", paged=True)
        library_id = ""
        if libs is not None:
            entries = _unwrap_items(libs)
            print(f"  entries  : {len(entries)}")
            print(f"  names    : {', '.join(_names_from(libs)[:8]) or '(none read)'}")
            matched = _match_library(libs, address["library"],
                                     address["browser_library"])
            print(f"  matched  : {'yes' if matched else 'NO — none matched the URL library'}")
            if matched:
                library_id = _first_string(matched, ("id", "library_id", "drive_id"))
                print(f"  entry keys: {', '.join(matched)}")

        # 3 -- the files
        args = {"site_id": site_id, "siteId": site_id, "url": folder,
                "library": address["library"]}
        if library_id:
            args.update(library_id=library_id, drive_id=library_id)
        if address["folder_path"]:
            args.update(folder_path=address["folder_path"],
                        path=address["folder_path"])
        _, items = await step(3, "list the folder's files", source.LIST_KEYWORDS,
                              args, "MCP_TOOL_LIST_ITEMS", paged=True)
        if items is None:
            return 1
        entries = _unwrap_items(items)
        names = _names_from(items)
        print(f"  entries  : {len(entries)}")
        print(f"  entry keys: {', '.join(entries[0]) if entries and isinstance(entries[0], dict) else '(not dicts)'}")
        print(f"  file names: {', '.join(names[:6]) or '(NONE READ — which key is the name?)'}")
        if not names:
            return 1
        item_id = _item_id_for(items, names[0])
        print(f"  item id  : {'FOUND' if item_id else 'NOT FOUND — which key is the id?'}")

        # 4 -- one document
        _, doc = await step(
            4, f"fetch {names[0]!r}", source.DOCUMENT_KEYWORDS,
            {"site_id": site_id, "library_id": library_id, "drive_id": library_id,
             "item_id": item_id or names[0], "id": item_id or names[0],
             "name": names[0]},
            "MCP_TOOL_GET_DOCUMENT")
        if doc is not None and isinstance(doc, dict):
            inline = [k for k in ("content_base64", "contentBytes", "base64", "data")
                      if doc.get(k)]
            links = [k for k in doc
                     if "download" in k.lower() or k.lower().endswith("url")]
            print(f"  inline bytes under: {inline or 'none'}")
            print(f"  link-ish keys     : {links or 'none'}")

    head("DONE")
    print("  Photograph from STEP 0 down. No values were printed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
