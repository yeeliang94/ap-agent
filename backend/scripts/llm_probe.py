"""Ask the AI proxy whether our key works, and print what it said.

The sorting stage failing with "401 Unauthorized" tells you the proxy
refused us, but not why — and running the whole pipeline to find out
costs a batch upload and several minutes. This asks the one question
directly.

    backend\\.venv\\Scripts\\python.exe backend\\scripts\\llm_probe.py

Reads LLM_PROXY_URL, OPENAI_API_KEY and SORT_MODEL from .env. It sends
the smallest possible chat request ("say ok") to each candidate path.

Why several paths and headers: an OpenAI-compatible gateway may serve
/chat/completions or /v1/chat/completions, and a LiteLLM-family gateway
may want its key in Authorization OR in x-litellm-api-key — the
SharePoint MCP on this project needs the latter. Guessing wrong looks
identical to a bad key (401 either way), so the guessing is done here,
once, in the open.

Read-only, and it never prints the key.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app import config  # noqa: E402
from app.main import _configure_tls  # noqa: E402

# Path suffixes an OpenAI-compatible gateway might serve chat on.
PATHS = ("/chat/completions", "/v1/chat/completions", "/openai/chat/completions")


def _header_styles(key: str) -> list[tuple[str, dict]]:
    """The ways a gateway might expect the key to be presented."""
    return [
        ("Authorization: Bearer …", {"Authorization": f"Bearer {key}"}),
        ("x-litellm-api-key: Bearer …", {"x-litellm-api-key": f"Bearer {key}"}),
        ("x-litellm-api-key: <key>", {"x-litellm-api-key": key}),
        ("api-key: <key>", {"api-key": key}),  # Azure-style
    ]


def main() -> int:
    _configure_tls()  # corporate certificate trust, same as the server uses

    base = config.LLM_PROXY_URL.rstrip("/")
    if not base:
        print("LLM_PROXY_URL is not set in .env — the app would talk to "
              "OpenAI directly, and this probe has nothing to test.")
        return 1
    if not config.OPENAI_API_KEY:
        print("OPENAI_API_KEY is not set in .env — nothing to authenticate with.")
        return 1

    model = config.SORT_MODEL
    print(f"Proxy : {base}")
    print(f"Model : {model}")
    print(f"Key   : present, {len(config.OPENAI_API_KEY)} characters (not shown)")
    print("-" * 70)

    body = {"model": model, "max_tokens": 5,
            "messages": [{"role": "user", "content": "say ok"}]}

    winners = []
    for path in PATHS:
        for label, headers in _header_styles(config.OPENAI_API_KEY):
            url = base + path
            try:
                r = httpx.post(url, json=body, headers=headers, timeout=30)
            except Exception as exc:
                print(f"{path:28} {label:28} FAILED  {type(exc).__name__}: "
                      f"{str(exc)[:90]}")
                continue
            note = ""
            if r.status_code >= 400:
                # The body says WHY far more often than the status does —
                # "model not found" and "invalid key" are both 401 on some
                # gateways. It cannot contain our key; we only sent it.
                note = "  " + r.text.strip().replace("\n", " ")[:120]
            print(f"{path:28} {label:28} HTTP {r.status_code}{note}")
            if r.status_code < 400:
                winners.append((path, label))

    print("-" * 70)
    if winners:
        path, label = winners[0]
        print(f"WORKS: {base}{path} with {label}")
        if path != "/chat/completions":
            print(f"\nThe app currently sends to {base}/chat/completions, which is "
                  f"not this. Set LLM_PROXY_URL so that the client's own "
                  f"'/chat/completions' suffix lands on the working path.")
        if not label.startswith("Authorization"):
            print("\nThe working header is NOT Authorization, which is the only "
                  "one pydantic-ai's OpenAI client sends. model_layer.py needs "
                  "a custom http client to send this header instead.")
        return 0

    print("NOTHING WORKED. Every combination was rejected.")
    print("\n401 everywhere usually means the key is not entitled to the CHAT "
          "service, even when the same key works for another service such as "
          "the SharePoint MCP — those are different hosts and different "
          "entitlements. 404 everywhere means the address is wrong. A "
          "certificate complaint means TLS trust is not active.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
