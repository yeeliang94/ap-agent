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
  - documents are addressed by an OPAQUE item ID, never by their file
    name. Real SharePoint issues drive-item IDs; a fake that accepted
    file names would let a name-for-ID bug pass every local test and
    fail only on the enterprise gateway — which is exactly what happened
  - a bad item ID comes back as a normal result carrying an "error" key,
    not as an MCP protocol error, because that is what the gateway does

It serves the sample reference workbooks read-only. It cannot write —
neither can the real one.

Run:  python fake_mcp/mcp_server.py    (listens on 127.0.0.1:8004)
Then: MCP_URL=http://127.0.0.1:8004/mcp  MCP_PROTOCOL=mcp  DOC_SOURCE=mcp
"""
from __future__ import annotations

import uuid
from pathlib import Path

from mcp.server.mcpserver import Context, MCPServer

REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DIR = REPO_ROOT / "samples" / "generated" / "reference"
# A NESTED folder tree (the claims batch: one subfolder per employee), so
# the app's folder walker is exercised over the real protocol. Reached at
# folder path "/Claims/<batch>"; the reference folder stays flat.
CLAIMS_DIR = REPO_ROOT / "samples" / "generated" / "claims" / "batch"
CLAIMS_PATH = "/Claims/JUL26"

SITE_ID = "site-clientabc"
_download_tokens: dict[str, Path] = {}
# Every 7th listing of the CLAIMS tree fails transiently, like the probed
# ReadError — so the walker's retry is proven, not assumed. The reference
# folder is left alone so the older tests keep their exact call counts.
_claims_calls = 0

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
        from urllib.parse import unquote

        # The folder path is whatever follows the library in the address.
        tail = unquote(url).split("Shared Documents", 1)[1].strip("/")
        return {"site_id": SITE_ID, "library": "Documents",
                "folder_path": "/" + tail if tail else "/AP Reference"}
    raise ValueError(f"Folder URL not recognised: {url}")


@mcp.tool(name="sp_list_library_items",
          description="List the files in a resolved SharePoint library folder.")
def list_library_items(site_id: str = "", library: str = "",
                       folder_path: str = "") -> dict:
    global _claims_calls
    if site_id != SITE_ID or library != "Documents":
        raise ValueError(f"Unknown site or library: {site_id!r}/{library!r}")
    folder_path = "/" + folder_path.strip("/") if folder_path else ""
    if folder_path.startswith(CLAIMS_PATH):
        _claims_calls += 1
        if _claims_calls % 7 == 0:
            raise ValueError("ReadError: transient upstream failure (retry)")
        rel = folder_path[len(CLAIMS_PATH):].strip("/")
        target = (CLAIMS_DIR / rel) if rel else CLAIMS_DIR
        if not target.is_dir() or CLAIMS_DIR.resolve() not in [target.resolve(), *target.resolve().parents]:
            return {"error": f"itemNotFound: no folder {folder_path!r}"}
        return {"items": [
            {"item_id": _opaque_id(str(f.relative_to(CLAIMS_DIR))), "name": f.name,
             "size": None if f.is_dir() else f.stat().st_size,
             "kind": "folder" if f.is_dir() else "file"}
            for f in sorted(target.iterdir()) if not f.name.startswith(".")
        ]}
    if not REFERENCE_DIR.is_dir():
        return {"items": []}
    return {"items": [
        {"item_id": _opaque_id(f.name), "name": f.name, "size": f.stat().st_size,
         "kind": "file"}
        for f in sorted(REFERENCE_DIR.iterdir()) if f.is_file()
    ]}


def _opaque_id(name: str) -> str:
    """A stable, opaque drive-item ID, as real SharePoint issues."""
    import hashlib

    return "01" + hashlib.sha1(name.encode()).hexdigest()[:24].upper()


def _name_for(item_id: str) -> str | None:
    if not REFERENCE_DIR.is_dir():
        return None
    for f in sorted(REFERENCE_DIR.iterdir()):
        if f.is_file() and _opaque_id(f.name) == item_id:
            return f.name
    return None


def _claims_path_for(item_id: str) -> Path | None:
    """The file in the nested claims tree behind an opaque id, if any."""
    if not CLAIMS_DIR.is_dir():
        return None
    for f in sorted(CLAIMS_DIR.rglob("*")):
        if f.is_file() and _opaque_id(str(f.relative_to(CLAIMS_DIR))) == item_id:
            return f
    return None


@mcp.tool(name="sp_get_document_metadata",
          description="Metadata for one document, including a temporary "
                      "single-use download URL.")
def get_document_metadata(ctx: Context, item_id: str = "", site_id: str = "",
                          library: str = "") -> dict:
    resolved = _name_for(item_id)
    nested = None if resolved else _claims_path_for(item_id)
    if resolved is None and nested is None:
        # Like the enterprise gateway: a bad item ID comes back as an
        # ordinary result carrying an "error" key, NOT an MCP error.
        return {"error": f"itemNotFound: no drive item {item_id!r}"}
    path = nested if nested is not None else REFERENCE_DIR / resolved
    # Containment check: item_id must name a file inside one of the trees.
    if not path.is_file() or not (path.resolve().parent == REFERENCE_DIR.resolve()
                                  or CLAIMS_DIR.resolve() in path.resolve().parents):
        raise ValueError(f"No such document: {item_id!r}")
    token = uuid.uuid4().hex
    _download_tokens[token] = path
    # Built from the request's own Host so the link works on whatever
    # port this server was actually given (the tests pick a free one).
    host = (ctx.headers or {}).get("host", "127.0.0.1:8004")
    return {
        "item_id": path.name, "name": path.name, "size": path.stat().st_size,
        # Temporary bearer-like link — treat as sensitive, never log it.
        "download_url": f"http://{host}/download/{token}",
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
