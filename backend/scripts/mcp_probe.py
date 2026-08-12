"""Ask the real MCP server what it can do, and print it.

Everything about connecting to an enterprise MCP gateway that we cannot
know from here — the tool names, their arguments, whether the credentials
work — this answers in one call. Run it on the Windows machine, on the
VPN, and paste the output.

    backend\\.venv\\Scripts\\python.exe backend\\scripts\\mcp_probe.py

Reads MCP_URL, MCP_AUTH_HEADER and MCP_AUTH_VALUE from .env. Read-only:
it lists tools and, unless you pass --call, invokes nothing.

    --call    additionally try whoami and the folder listing, so you can
              see real navigation succeed or fail with a real message
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config  # noqa: E402,F401  (loads .env)
from app.main import _configure_tls  # noqa: E402


async def main() -> int:
    _configure_tls()  # corporate certificate trust, same as the server uses

    url = os.getenv("MCP_URL", "").strip()
    if not url:
        print("MCP_URL is not set in .env — nothing to probe.")
        return 1
    header = os.getenv("MCP_AUTH_HEADER", "").strip()
    value = os.getenv("MCP_AUTH_VALUE", "").strip()
    headers = {header: value} if header and value else {}

    print(f"Endpoint    : {url}")
    print(f"Auth header : {header or '(none set)'}"
          f"{' (value present)' if value else ''}")
    print("-" * 70)

    from app.mcp_client import McpSession

    try:
        async with McpSession(url, headers) as session:
            print(f"Handshake OK. {len(session.tool_names)} tool(s):\n")
            for tool in session._tools:
                schema = getattr(tool, "input_schema", None) or {}
                props = list((schema.get("properties") or {})) if isinstance(schema, dict) else []
                required = list(schema.get("required") or []) if isinstance(schema, dict) else []
                print(f"  {tool.name}")
                if tool.description:
                    print(f"      {str(tool.description).strip()[:160]}")
                print(f"      args: {', '.join(props) or '(none)'}"
                      f"{'  | required: ' + ', '.join(required) if required else ''}")
            if "--call" not in sys.argv:
                print("\nRe-run with --call to try whoami and a folder listing.")
                return 0

            print("\n" + "-" * 70)
            for words in (("whoami",), ("who", "am", "i"), ("current", "user")):
                try:
                    tool = session.find_tool(words)
                except Exception:
                    continue
                try:
                    print(f"{tool} ->", json.dumps(await session.call(tool, {}))[:400])
                except Exception as exc:
                    print(f"{tool} FAILED: {exc}")
                break
            else:
                print("no whoami-style tool found")

            folder = os.getenv("SHAREPOINT_FOLDER_URL", "").strip()
            if folder:
                from app.docsource import RealMcpSource

                src = RealMcpSource(folder)
                try:
                    print("\nFolder listing ->", src.list_names())
                except Exception as exc:
                    print(f"\nFolder listing FAILED: {exc}")
    except Exception as exc:
        print(f"\nCOULD NOT CONNECT: {type(exc).__name__}: {exc}")
        print("\nIf this mentions a certificate, TLS trust is not active. "
              "If it mentions 401/403, the credential is wrong or Entra "
              "sign-in is still required. If it mentions a name lookup, "
              "the VPN is down.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
