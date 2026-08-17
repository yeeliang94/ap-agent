"""Fake SharePoint MCP shaped like the ENTERPRISE gateway.

`mcp_server.py` next door offers a single resolve-folder tool: paste a URL,
get back a site and a library. That is a convenient server, and the real
enterprise gateway is not one. It offers the three steps a person would
take by hand:

    get the site  ->  list its document libraries  ->  list the items

Testing only against the convenient fake meant the app's whole navigation
path — parsing the pasted browser URL, finding the site, matching the
library — ran for the first time on the enterprise gateway, over a VPN,
where the only feedback was a failed run.

Deliberate frictions, all taken from the real gateway's behaviour:
  - NO resolve-folder tool. The URL must be taken apart by the client.
  - Several tools mention "site", so a client matching on that word alone
    cannot tell them apart and must ask for the specific one.
  - The library is "Documents"; the browser URL says "Shared Documents".
  - Documents are addressed by an opaque item ID, never by file name.
  - A bad request comes back as a normal result carrying an "error" key,
    not as an MCP protocol error.

Run:  python fake_mcp/gateway_server.py    (listens on 127.0.0.1:8005)
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from mcp.server.mcpserver import Context, MCPServer

REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DIR = REPO_ROOT / "samples" / "generated" / "reference"

SITE_ID = "contoso.sharepoint.com,1a2b3c,4d5e6f"
LIBRARY_ID = "b!driveIdForDocuments"
LIBRARY_NAME = "Documents"          # the browser calls this "Shared Documents"
FOLDER_PATH = "AP Reference"

_download_tokens: dict[str, Path] = {}

mcp = MCPServer(name="Fake Enterprise SharePoint Gateway", version="1.0.0")


def _opaque_id(name: str) -> str:
    """A stable, opaque drive-item ID, as real SharePoint issues."""
    return "01" + hashlib.sha1(name.encode()).hexdigest()[:24].upper()


def _name_for(item_id: str) -> str | None:
    if not REFERENCE_DIR.is_dir():
        return None
    for f in sorted(REFERENCE_DIR.iterdir()):
        if f.is_file() and _opaque_id(f.name) == item_id:
            return f.name
    return None


@mcp.tool(name="sp_whoami",
          description="The signed-in user this connection is acting as.")
def whoami() -> dict:
    return {"displayName": "Test Reviewer", "mail": "reviewer@example.com"}


@mcp.tool(name="sp_get_sharepoint_site",
          description="Look up a SharePoint site by its web address.")
def get_sharepoint_site(url: str = "", site_url: str = "") -> dict:
    address = (site_url or url or "").rstrip("/")
    if "/sites/" not in address and "/teams/" not in address:
        return {"error": f"invalidRequest: {address!r} does not name a site"}
    return {"site_id": SITE_ID, "webUrl": address,
            "displayName": address.rsplit("/", 1)[-1]}


# Two more tools whose names contain "site". They exist so that a client
# matching on the bare word cannot tell the three apart — which is the
# ambiguity the real gateway produced, and the reason tool discovery has
# to ask for the specific spelling first.
@mcp.tool(name="sp_search_site_content",
          description="Full-text search across a site's content.")
def search_site_content(site_id: str = "", query: str = "") -> dict:
    return {"items": []}


@mcp.tool(name="sp_get_site_permissions",
          description="Who has access to a site.")
def get_site_permissions(site_id: str = "") -> dict:
    return {"roles": []}


@mcp.tool(name="sp_list_document_libraries",
          description="The document libraries (drives) belonging to a site.")
def list_document_libraries(site_id: str = "") -> dict:
    if site_id != SITE_ID:
        return {"error": f"itemNotFound: no site {site_id!r}"}
    return {"value": [
        {"id": LIBRARY_ID, "name": LIBRARY_NAME, "driveType": "documentLibrary"},
        {"id": "b!driveIdForSiteAssets", "name": "Site Assets",
         "driveType": "documentLibrary"},
    ]}


@mcp.tool(name="sp_list_library_items",
          description="The files and folders inside a library folder.")
def list_library_items(site_id: str = "", library_id: str = "",
                       folder_path: str = "") -> dict:
    if site_id != SITE_ID:
        return {"error": f"itemNotFound: no site {site_id!r}"}
    if library_id and library_id != LIBRARY_ID:
        return {"error": f"itemNotFound: no library {library_id!r}"}
    if folder_path and folder_path.strip("/") != FOLDER_PATH:
        return {"error": f"itemNotFound: no folder {folder_path!r}"}
    if not REFERENCE_DIR.is_dir():
        return {"value": []}
    return {"value": [
        {"id": _opaque_id(f.name), "name": f.name, "size": f.stat().st_size,
         "kind": "file"}
        for f in sorted(REFERENCE_DIR.iterdir()) if f.is_file()
    ]}


@mcp.tool(name="sp_download_document",
          description="A temporary, single-use download URL for one document.")
def download_document(ctx: Context, item_id: str = "", site_id: str = "",
                      library_id: str = "") -> dict:
    resolved = _name_for(item_id)
    if resolved is None:
        # Like the real gateway: a bad item ID is an ordinary result with
        # an "error" key, NOT an MCP protocol error.
        return {"error": f"itemNotFound: no drive item {item_id!r}"}
    path = REFERENCE_DIR / resolved
    if not path.is_file() or path.resolve().parent != REFERENCE_DIR.resolve():
        return {"error": f"itemNotFound: no drive item {item_id!r}"}
    token = uuid.uuid4().hex
    _download_tokens[token] = path
    host = (ctx.headers or {}).get("host", "127.0.0.1:8005")
    return {
        "id": item_id, "name": path.name, "size": path.stat().st_size,
        # Temporary bearer-like link — treat as sensitive, never log it.
        "@microsoft.graph.downloadUrl": f"http://{host}/download/{token}",
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

    uvicorn.run(mcp.streamable_http_app(), host="127.0.0.1", port=8005)
