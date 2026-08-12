"""Fake SharePoint MCP server — speaking the REAL MCP protocol.

`server.py` next door implements a bespoke REST contract. That made local
testing worthless for the enterprise path: the app and the fake shared an
invention that the real gateway does not implement, so passing locally
proved only that our two halves agreed with each other.

This server is a genuine Model Context Protocol server over streamable
HTTP: an `initialize` handshake, a `tools/list` catalogue, `tools/call`
invocations. Exercising the app against it actually tests the protocol.

Deliberate frictions, kept from the probe learnings:
  - tool names are NOT the app's internal role names, proving discovery
    works rather than lucky naming
  - the "Shared Documents" (browser URL) vs "Documents" (MCP) alias
  - download URLs are temporary and single-use

It serves the sample reference workbooks read-only. It cannot write —
neither can the real one.

Run:  python fake_mcp/mcp_server.py    (listens on 127.0.0.1:8004)
Then: MCP_URL=http://127.0.0.1:8004/mcp  MCP_PROTOCOL=mcp  DOC_SOURCE=mcp
"""
from __future__ import annotations

import uuid
from pathlib import Path

from mcp.server.mcpserver import MCPServer

REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DIR = REPO_ROOT / "samples" / "generated" / "reference"

SITE_ID = "site-clientabc"
_download_tokens: dict[str, Path] = {}

mcp = MCPServer(name="Fake SharePoint MCP", version="1.0.0")


# Names chosen to differ from the app's role names on purpose: the client
# must find these by keyword, exactly as it will have to on the real
# gateway. "sp_" prefix and "documents" plural are typical of real servers.
@mcp.tool(name="sp_resolve_folder_url",
          description="Resolve a SharePoint folder URL to its site and library.")
def resolve_folder_url(url: str) -> dict:
    # The real MCP reports the browser's "Shared Documents" library as
    # "Documents" — reproduce that alias so callers must handle it.
    if "Shared%20Documents" in url or "Shared Documents" in url:
        return {"site_id": SITE_ID, "library": "Documents",
                "folder_path": "/AP Reference"}
    raise ValueError(f"Folder URL not recognised: {url}")


@mcp.tool(name="sp_list_library_items",
          description="List the files in a resolved SharePoint library folder.")
def list_library_items(site_id: str = "", library: str = "",
                       folder_path: str = "") -> dict:
    if site_id != SITE_ID or library != "Documents":
        raise ValueError(f"Unknown site or library: {site_id!r}/{library!r}")
    if not REFERENCE_DIR.is_dir():
        return {"items": []}
    return {"items": [
        {"item_id": f.name, "name": f.name, "size": f.stat().st_size,
         "kind": "file"}
        for f in sorted(REFERENCE_DIR.iterdir()) if f.is_file()
    ]}


@mcp.tool(name="sp_get_document_metadata",
          description="Metadata for one document, including a temporary "
                      "single-use download URL.")
def get_document_metadata(item_id: str = "", site_id: str = "",
                          library: str = "") -> dict:
    path = REFERENCE_DIR / item_id
    # Containment check: item_id must name a file directly in the folder.
    if not path.is_file() or path.resolve().parent != REFERENCE_DIR.resolve():
        raise ValueError(f"No such document: {item_id!r}")
    token = uuid.uuid4().hex
    _download_tokens[token] = path
    return {
        "item_id": path.name, "name": path.name, "size": path.stat().st_size,
        # Temporary bearer-like link — treat as sensitive, never log it.
        "download_url": f"http://127.0.0.1:8004/download/{token}",
    }


@mcp.custom_route("/download/{token}", methods=["GET"])
async def download(request):
    from starlette.responses import FileResponse, PlainTextResponse

    path = _download_tokens.pop(request.path_params["token"], None)  # single use
    if path is None:
        return PlainTextResponse("Download link expired", status_code=410)
    return FileResponse(path)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(mcp.streamable_http_app(), host="127.0.0.1", port=8004)
