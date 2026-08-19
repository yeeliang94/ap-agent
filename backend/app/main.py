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
DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=DEV_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Methods that change something. GET is left alone: it changes nothing,
# and blocking it would break the browser loading the app itself.
_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def _refuse_cross_site_writes(request, call_next):
    """Refuse state-changing requests that came from another website.

    CORS is not a defence here. It stops a page READING our answers; it
    does not stop it SENDING a request. Any web page a reviewer visits can
    post an ordinary HTML form at http://127.0.0.1:8002/api/... and the
    browser will deliver it with the reviewer's own machine's authority —
    approving a flag, deleting the SharePoint sign-in, or popping a
    sign-in window open.

    Browsers do attach an Origin header to those cross-site posts, so
    checking it is enough. A request with NO Origin is not a cross-site
    browser request at all (curl, the test client, the probe scripts), so
    it passes.
    """
    if request.method in _WRITE_METHODS:
        origin = request.headers.get("origin")
        if origin and origin not in DEV_ORIGINS:
            from urllib.parse import urlsplit

            # Same-origin is the normal case: the app is served by this
            # very process, so its own page's Origin is our own host.
            if urlsplit(origin).netloc != request.url.netloc:
                from fastapi.responses import JSONResponse

                logging.getLogger("main").warning(
                    "refused a %s to %s from another site (Origin: %s)",
                    request.method, request.url.path, origin)
                return JSONResponse(
                    status_code=403,
                    content={"detail": "This request came from another "
                                       "website and was refused."})
    return await call_next(request)


@app.on_event("startup")
async def _startup() -> None:
    _configure_logging()
    _configure_tls()  # after logging, so its own result is visible
    init_db()
    # The one event loop background stages run on. A SYNC route (a plain
    # `def` handler) runs in a worker thread where no loop is running, so
    # the loop has to be remembered here, while we are on it, or
    # start_background has nothing to hand its coroutine to.
    from .claims import runner as claims_runner
    claims_runner.set_loop()
    # Runs are in-process tasks: any run still mid-stage now was orphaned
    # by the previous process and must be shown as failed, not "extracting".
    from .pipeline.runner import fail_interrupted_runs
    fail_interrupted_runs()
    # Same for claims runs: they are their own tables and their own tasks.
    # SINGLE-PROCESS ASSUMPTION: a run in an in-progress status can only be
    # this machine's orphan because exactly one process ever owns the
    # database. Under two workers (uvicorn --workers 2, a second container)
    # this would fail the OTHER process's live runs at every start. Moving
    # to more than one process means first giving a run an owner (a process
    # id / lease) and only reconciling runs whose owner is gone.
    from .claims.runner import fail_interrupted_runs as fail_interrupted_claims_runs
    fail_interrupted_claims_runs()


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


# Routes are registered at the bottom so the app object exists first.
from .routes import router  # noqa: E402

app.include_router(router, prefix="/api")

# The claims module: its own routes under /api/claims-runs, its own tables.
from .claims.routes import router as claims_router  # noqa: E402
from .claims.routes import settings_router as claims_settings_router  # noqa: E402

app.include_router(claims_router, prefix="/api")
app.include_router(claims_settings_router, prefix="/api")

# On Windows the frontend is built once (start.bat) and served from here,
# so one process serves the whole app. /api routes above take precedence.
from fastapi.staticfiles import StaticFiles  # noqa: E402

from . import config  # noqa: E402

_dist = config.REPO_ROOT / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")
