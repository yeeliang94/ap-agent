"""Application entry point.

Run with:  uvicorn app.main:app --reload --port 8002
(8002 matches the enterprise app's port so habits transfer.)
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .db import init_db

app = FastAPI(title="AP Agent")

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
    init_db()


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


# Routes are registered at the bottom so the app object exists first.
from .routes import router  # noqa: E402

app.include_router(router, prefix="/api")
