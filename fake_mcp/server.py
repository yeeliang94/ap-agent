"""Fake SharePoint MCP server — a local stand-in for the enterprise one.

Mimics the behaviour documented in the SharePoint MCP probe learnings:
  - search sites, resolve a folder URL, list folder items
  - get document metadata including a TEMPORARY download URL
  - the "Shared Documents" (browser URL) vs "Documents" (MCP) library alias
  - intermittent ReadError responses (the real endpoint does this too),
    so the app's retry logic gets exercised honestly

It serves the sample reference workbooks read-only. It cannot write —
neither can the real one.

Run:  python fake_mcp/server.py   (listens on 127.0.0.1:8003)
"""
from __future__ import annotations

import itertools
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException

REPO_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DIR = REPO_ROOT / "samples" / "generated" / "reference"
# The nested claims batch (one subfolder per employee), at "/Claims/JUL26".
CLAIMS_DIR = REPO_ROOT / "samples" / "generated" / "claims" / "batch"
CLAIMS_PATH = "/Claims/JUL26"

app = FastAPI(title="Fake SharePoint MCP")

SITE = {"site_id": "site-clientabc", "name": "Client ABC AP",
        "url": "https://example.sharepoint.com/sites/clientabc"}

# Every 7th call fails, deterministically — like the probed ReadError, but
# reproducible in tests. Callers must retry narrow requests.
_call_counter = itertools.count(1)
# Download tokens are single-use and expire, like real Graph download URLs.
_download_tokens: dict[str, Path] = {}


def _maybe_readerror() -> None:
    if next(_call_counter) % 7 == 0:
        raise HTTPException(500, "ReadError: transient upstream failure (retry)")


@app.post("/tools/search_sharepoint_sites")
def search_sites(body: dict) -> dict:
    _maybe_readerror()
    q = str(body.get("query", "")).lower()
    hits = [SITE] if q and (q in SITE["name"].lower() or "abc" in q) else []
    return {"sites": hits}


@app.post("/tools/resolve_folder_url")
def resolve_folder_url(body: dict) -> dict:
    _maybe_readerror()
    url = str(body.get("url", ""))
    # The real MCP reports the browser's "Shared Documents" library as
    # "Documents" — reproduce that alias so callers must handle it.
    if "Shared%20Documents" in url or "Shared Documents" in url:
        from urllib.parse import unquote

        tail = unquote(url).split("Shared Documents", 1)[1].strip("/")
        return {"site_id": SITE["site_id"], "library": "Documents",
                "folder_path": "/" + tail if tail else "/AP Reference"}
    raise HTTPException(404, "Folder URL not recognised")


@app.post("/tools/list_library_items")
def list_items(body: dict) -> dict:
    _maybe_readerror()
    if body.get("site_id") != SITE["site_id"] or body.get("library") != "Documents":
        raise HTTPException(404, "Unknown site or library")
    folder_path = "/" + str(body.get("folder_path", "")).strip("/")
    if folder_path.startswith(CLAIMS_PATH):
        rel = folder_path[len(CLAIMS_PATH):].strip("/")
        target = (CLAIMS_DIR / rel) if rel else CLAIMS_DIR
        if not target.is_dir():
            raise HTTPException(404, "No such folder")
        return {"items": [
            {"item_id": "claims:" + str(f.relative_to(CLAIMS_DIR)), "name": f.name,
             "size": None if f.is_dir() else f.stat().st_size,
             "kind": "folder" if f.is_dir() else "file"}
            for f in sorted(target.iterdir()) if not f.name.startswith(".")
        ]}
    items = [
        {"item_id": f.name, "name": f.name, "size": f.stat().st_size,
         "kind": "file"}
        for f in sorted(REFERENCE_DIR.iterdir()) if f.is_file()
    ]
    return {"items": items}


@app.post("/tools/get_document_metadata")
def get_metadata(body: dict) -> dict:
    _maybe_readerror()
    item_id = str(body.get("item_id", ""))
    if item_id.startswith("claims:"):
        path = (CLAIMS_DIR / item_id[len("claims:"):]).resolve()
        if not path.is_file() or CLAIMS_DIR.resolve() not in path.parents:
            raise HTTPException(404, "No such document")
    else:
        path = REFERENCE_DIR / item_id
        if not path.is_file() or path.parent != REFERENCE_DIR:
            raise HTTPException(404, "No such document")
    token = uuid.uuid4().hex
    _download_tokens[token] = path
    return {
        "item_id": path.name, "name": path.name, "size": path.stat().st_size,
        # Temporary bearer-like link — treat as sensitive, never log or
        # show to a model. Single use here to enforce the habit.
        "download_url": f"http://127.0.0.1:8003/download/{token}",
    }


@app.get("/download/{token}")
def download(token: str):
    from fastapi.responses import FileResponse

    path = _download_tokens.pop(token, None)  # single use
    if path is None:
        raise HTTPException(410, "Download link expired")
    return FileResponse(path)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8003)
