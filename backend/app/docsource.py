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

    def get_reference(self, name: str) -> bytes:
        path = self.folder / name
        if not path.is_file():
            raise SourceUnavailable(f"Reference document {name!r} not found in {self.folder}")
        return path.read_bytes()


class McpSource:
    """Fetch through the (fake or real) SharePoint MCP contract.

    Mirrors the probe's verified navigation: resolve the folder URL, get the
    document's metadata, follow its temporary download URL. Every call
    retries, because the probed endpoint intermittently returns ReadError.
    """

    RETRIES = 3

    def __init__(self) -> None:
        self.base = os.getenv("MCP_URL", "http://127.0.0.1:8003")
        self.folder_url = os.getenv(
            "SHAREPOINT_FOLDER_URL",
            "https://example.sharepoint.com/sites/clientabc/Shared%20Documents/AP%20Reference",
        )

    def _call(self, tool: str, body: dict) -> dict:
        last_error = ""
        for attempt in range(1, self.RETRIES + 1):
            try:
                r = httpx.post(f"{self.base}/tools/{tool}", json=body, timeout=15)
                if r.status_code == 500 and "ReadError" in r.text:
                    last_error = "ReadError"  # known-transient: retry a narrow request
                    time.sleep(0.2 * attempt)
                    continue
                r.raise_for_status()
                return r.json()
            except httpx.HTTPError as exc:
                # Exception text can contain URLs — log it, don't raise it.
                log.warning("MCP call %s attempt %d failed: %s", tool, attempt, exc)
                last_error = type(exc).__name__
                time.sleep(0.2 * attempt)
        raise SourceUnavailable(
            f"SharePoint source unavailable after {self.RETRIES} attempts "
            f"calling {tool} ({last_error}). Details are in the server log."
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
            except httpx.HTTPError as exc:
                log.warning("download of %s attempt %d failed: %s", name, attempt, exc)
                last_error = type(exc).__name__
                time.sleep(0.2 * attempt)
        raise SourceUnavailable(
            f"Download of {name!r} failed after {self.RETRIES} attempts "
            f"({last_error}). Details are in the server log."
        )


def get_source():
    """Pick the source from .env: DOC_SOURCE=local (default) or mcp."""
    kind = os.getenv("DOC_SOURCE", "local").lower()
    return McpSource() if kind == "mcp" else LocalFolderSource()
