"""Central configuration.

Everything the app needs from the outside world (keys, model names, folders)
is read here, once, from the .env file at the repository root. No other file
reads environment variables directly — so when this app moves to the Windows
enterprise machine, this is the only place behaviour changes.
"""
from pathlib import Path
import os

from dotenv import load_dotenv

# The repository root is two levels up from this file (backend/app/config.py).
REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")

# --- AI access -------------------------------------------------------------
# Locally: a direct OpenAI key. On Windows: LLM_PROXY_URL points at the
# enterprise proxy and the same key variable carries the proxy key.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LLM_PROXY_URL = os.getenv("LLM_PROXY_URL", "")  # empty = direct OpenAI

# Which model plays which role. Kept separate so cheap models can do cheap
# jobs (sorting) and stronger models the careful jobs (extraction, judgment).
SORT_MODEL = os.getenv("SORT_MODEL", "gpt-4o-mini")
EXTRACT_MODEL = os.getenv("EXTRACT_MODEL", "gpt-4o")
JUDGE_MODEL = os.getenv("JUDGE_MODEL", "gpt-4o")

# --- Local storage ----------------------------------------------------------
# Every uploaded batch gets its own private folder under data/runs/<run-id>.
DATA_DIR = REPO_ROOT / "backend" / "data"
RUNS_DIR = DATA_DIR / "runs"
DB_PATH = DATA_DIR / "ap_agent.sqlite3"

# How many documents may be read by AI at the same time. Keeps us politely
# under provider rate limits; raise cautiously.
EXTRACT_CONCURRENCY = 5

# A guardrail matching the enterprise rule: one agent conversation may not
# make more than this many requests (their limit is 40; ours are tiny anyway).
MAX_AGENT_REQUESTS = 40


def ensure_dirs() -> None:
    """Create the data folders on first start so nothing crashes on a fresh clone."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
