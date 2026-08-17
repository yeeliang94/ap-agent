"""One screen that says whether this machine is set up correctly.

Written to be PHOTOGRAPHED. The Windows machine cannot copy text out, so
every line here is short and the verdict is at the bottom in plain words.

    backend\\.venv\\Scripts\\python.exe backend\\scripts\\doctor.py

Checks the two things that actually go wrong: Python packages left behind
by a git pull, and .env settings that do not match the gateway. Reads
nothing secret and prints no values — only whether each is set.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402,F401  (loads .env)

LINE = "-" * 58
problems: list[str] = []


def _version(package: str) -> str:
    import importlib.metadata as md

    try:
        return md.version(package)
    except Exception:
        return ""


def _row(label: str, value: str, ok: bool | None = None) -> None:
    mark = "" if ok is None else ("  OK" if ok else "  <-- WRONG")
    print(f"  {label:<24} {value:<14}{mark}")


def main() -> int:
    print()
    print("AP Agent environment check")
    print(LINE)
    _row("python", ".".join(str(n) for n in sys.version_info[:3]))

    mcp_version = _version("mcp")
    mcp_major = int(mcp_version.split(".")[0]) if mcp_version else 0
    _row("mcp", mcp_version or "MISSING", mcp_major >= 2)
    if mcp_major < 2:
        problems.append(
            "Python packages are OUT OF DATE (mcp must be 2.0 or newer).\n"
            "  This is the usual cause after a git pull. Fix it with:\n\n"
            "    backend\\.venv\\Scripts\\python.exe -m pip install "
            "-r backend\\requirements.txt\n")

    httpx2_version = _version("httpx2")
    _row("httpx2", httpx2_version or "MISSING", bool(httpx2_version))

    truststore = _version("truststore")
    _row("truststore", truststore or "MISSING", bool(truststore))
    if not truststore:
        problems.append("truststore is missing, so the corporate certificate "
                        "will not be trusted.")

    if os.name == "nt":
        pywin32 = _version("pywin32")
        _row("pywin32", pywin32 or "MISSING", bool(pywin32))
        if not pywin32:
            problems.append("pywin32 is missing, so the SharePoint sign-in "
                            "cannot be saved between restarts.")

    # --- the two imports the MCP client depends on ------------------------
    print(LINE)
    try:
        from mcp.client.streamable_http import streamable_http_client  # noqa: F401

        _row("MCP transport", "importable", True)
    except Exception as exc:
        _row("MCP transport", type(exc).__name__, False)
        problems.append(f"The MCP transport could not be imported ({exc}). "
                        "Re-install the Python packages as shown above.")

    try:
        import httpx2
        from mcp.client.auth import OAuthClientProvider

        is_auth = issubclass(OAuthClientProvider, httpx2.Auth)
        _row("OAuth provider", "httpx2.Auth" if is_auth else "NOT httpx2.Auth",
             is_auth)
        if not is_auth:
            problems.append(
                "This mcp version's OAuth provider does not fit the HTTP "
                "client, which means the packages do not match each other. "
                "Re-install them as shown above.")
    except Exception as exc:
        _row("OAuth provider", type(exc).__name__, False)
        problems.append(f"The OAuth provider could not be imported ({exc}).")

    # --- settings (names and yes/no only; never values) -------------------
    print(LINE)
    for name in ("DOC_SOURCE", "MCP_PROTOCOL", "MCP_OAUTH"):
        _row(name, os.getenv(name, "(not set)"))
    for name in ("MCP_URL", "SHAREPOINT_FOLDER_URL", "MCP_AUTH_HEADER"):
        _row(name, "set" if os.getenv(name, "").strip() else "(not set)")
    for name in ("MCP_AUTH_VALUE", "OPENAI_API_KEY"):
        _row(name, "set" if os.getenv(name, "").strip() else "(not set)")

    if os.getenv("DOC_SOURCE", "").lower() == "mcp":
        if os.getenv("MCP_PROTOCOL", "rest").lower() != "mcp":
            problems.append("MCP_PROTOCOL must be 'mcp' for the enterprise "
                            "gateway (it is currently the local fake's 'rest').")
        if not os.getenv("MCP_URL", "").strip():
            problems.append("MCP_URL is not set, so there is no gateway to call.")

    try:
        from app import sharepoint_auth

        saved = sharepoint_auth.storage().is_signed_in()
        _row("SharePoint sign-in", "saved" if saved else "not saved yet")
        if os.getenv("MCP_OAUTH", "").lower() in ("1", "true", "on", "yes") and not saved:
            problems.append("No SharePoint sign-in is saved. Open the app and "
                            "click 'Connect SharePoint'.")
    except Exception as exc:
        _row("SharePoint sign-in", type(exc).__name__, False)

    # --- verdict ----------------------------------------------------------
    print(LINE)
    if not problems:
        print("  VERDICT: this machine looks correctly set up.")
        print("  If SharePoint still fails, run mcp_probe.py for the")
        print("  gateway's own answer.")
        return 0
    print(f"  VERDICT: {len(problems)} thing(s) to fix\n")
    for i, problem in enumerate(problems, 1):
        print(f"  {i}. {problem}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
