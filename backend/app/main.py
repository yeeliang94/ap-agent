"""Application entry point.

Run with:  uvicorn app.main:app --reload --port 8002
(8002 matches the enterprise app's port so habits transfer.)
"""
import logging
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .db import init_db

app = FastAPI(title="AP Agent")


def _configure_tls() -> None:
    """Trust the certificates the operating system trusts.

    Corporate networks inspect HTTPS traffic by re-signing it with their
    own root certificate. Python ships a fixed list of public certificate
    authorities that cannot contain a private corporate one, so every
    HTTPS call — SharePoint MCP and the AI proxy alike — fails at the
    handshake. truststore redirects certificate checks to the OS store
    (the Windows certificate store), where the corporate root already is.

    Applies process-wide, so it must run before the first HTTPS call.
    Missing or unsupported: log loudly and continue — direct-internet
    development does not need it, and a hard failure here would block a
    working setup.
    """
    try:
        import truststore

        truststore.inject_into_ssl()
        logging.getLogger("tls").info("using the OS certificate store")
    except Exception as exc:
        logging.getLogger("tls").warning(
            "could not use the OS certificate store (%s). HTTPS to hosts "
            "behind a corporate certificate will fail with a certificate "
            "error; pip install truststore to fix.", exc)


def _configure_logging() -> None:
    """Make our own log lines visible, with a timestamp and the module name.

    Without this, Python's fallback handler prints warnings bare — no time,
    no source — so a docsource retry warning is easy to miss in uvicorn's
    output. Called from the startup hook, which runs AFTER uvicorn installs
    its own logging config; doing it at import time would be undone.
    """
    root = logging.getLogger()
    if any(getattr(h, "_ap_agent", False) for h in root.handlers):
        return  # --reload can run startup twice; don't stack duplicate handlers
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    handler._ap_agent = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(logging.INFO)

# During development the frontend dev server runs on a different port (5173),
# so the browser needs explicit permission to call this API from there.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    _configure_logging()
    _configure_tls()  # after logging, so its own result is visible
    init_db()


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


# Routes are registered at the bottom so the app object exists first.
from .routes import router  # noqa: E402

app.include_router(router, prefix="/api")

# On Windows the frontend is built once (start.bat) and served from here,
# so one process serves the whole app. /api routes above take precedence.
from fastapi.staticfiles import StaticFiles  # noqa: E402

from . import config  # noqa: E402

_dist = config.REPO_ROOT / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
