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
        # Callers processing a run pass that run's snapshotted folder URL;
        # otherwise fall back to the on-screen setting (which itself falls
        # back to the SHAREPOINT_FOLDER_URL .env value until first save).
        self.folder_url = folder_url or settings_store.get_setting("sharepoint_folder_url")
        # Logged so a misconfigured .env is visible at a glance. These are
        # ordinary configured addresses, not the temporary download links
        # that the rest of this class is careful never to expose.
        log.info("MCP source: base=%s folder=%s", self.base, self.folder_url)

    def _call(self, tool: str, body: dict) -> dict:
        url = f"{self.base}/tools/{tool}"
        last_error = ""
        attempts = 0
        for attempt in range(1, self.RETRIES + 1):
            attempts = attempt
            try:
                r = httpx.post(url, json=body, timeout=15)
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
                last_error = type(exc).__name__
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
                last_error = type(exc).__name__
                log.warning("download of %s attempt %d failed: %s",
                            name, attempt, exc, exc_info=True)
                time.sleep(0.2 * attempt)
        raise SourceUnavailable(
            f"Download of {name!r} failed after {self.RETRIES} attempts "
            f"({last_error}). Details are in the server log."
        )


def get_source(folder_url: str | None = None):
    """Pick the source from .env: DOC_SOURCE=local (default) or mcp.

    folder_url is a run's snapshotted SharePoint folder; None means the
    current on-screen setting. The local development source ignores it.
    """
    kind = os.getenv("DOC_SOURCE", "local").lower()
    return McpSource(folder_url) if kind == "mcp" else LocalFolderSource()
