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

# Where LINKED documents come from: "local" (the samples folder / a local
# tree) or "mcp" (the real SharePoint gateway). This is the mechanism;
# whether links are offered at all is the reviewer's SharePoint-source
# switch (app/switches.py), which defaults from this value.
DOC_SOURCE = os.getenv("DOC_SOURCE", "local").strip().lower()

# The single client this MVP is configured for. The API rejects any other
# name — silently applying Client ABC's policy to a different client's
# documents would be worse than an error.
CLIENT_NAME = os.getenv("CLIENT_NAME", "Client ABC")

# Upload quotas: generous for a monthly AP batch, small enough that a wrong
# file (or a zip bomb) cannot exhaust memory or the AI budget.
MAX_ZIP_MB = 50          # compressed upload size
MAX_FILE_MB = 20         # any single document, uncompressed
MAX_BATCH_FILES = 200    # documents per batch


# --- Claims hardening switches (CLAIMS-AGENT-HARDENING.md, "Feature switches") ---
# Migrations are additive and unconditional; these gate what is READ,
# ROUTED and SHOWN, never what is stored. "1/true/yes/on" turns one on.
def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# Case fields and case routes on the HTTP contract (off = employee fields
# stay authoritative for the UI; storage unchanged).
CLAIMS_CASE_MODEL = _flag("CLAIMS_CASE_MODEL", "1")
# New runs investigate with the tool-using agent (off = the delivered
# structured-folder mapper; old runs are never reinterpreted).
CLAIMS_AGENTIC_INVESTIGATION = _flag("CLAIMS_AGENTIC_INVESTIGATION", "0")
# Shadow mode (H12): with the agentic switch OFF, also run the tool-using
# investigator on each new run and record where it agrees with / differs
# from the structured-folder mapper — nothing it proposes is used.
CLAIMS_SHADOW_INVESTIGATION = _flag("CLAIMS_SHADOW_INVESTIGATION", "0")
# Map & Group actions for a flat folder dump (off = a flat folder behaves
# as today: root files are classified, not grouped).
CLAIMS_FULL_DUMP_GROUPING = _flag("CLAIMS_FULL_DUMP_GROUPING", "0")
# run_python in the tool allowlist (off = absent; TOOL_UNAVAILABLE where
# an investigation genuinely needs it).
CLAIMS_PYTHON_SANDBOX = _flag("CLAIMS_PYTHON_SANDBOX", "0")
# The OS-level isolation runner the sandbox hands execution to (a command
# that takes <code.py> <input dir> <output dir>), and the operator's
# assertion that it isolates (network, filesystem, secrets, processes).
# Both must be set for run_python to be available — see docs/SANDBOX.md.
CLAIMS_SANDBOX_RUNNER = os.getenv("CLAIMS_SANDBOX_RUNNER", "")
CLAIMS_SANDBOX_ISOLATED = _flag("CLAIMS_SANDBOX_ISOLATED", "0")


def ensure_dirs() -> None:
    """Create the data folders on first start so nothing crashes on a fresh clone."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)


# --- Claims investigator limits (review 2026-08-19, AI loop) ---
# Token ceiling for ONE investigation (all rounds together), passed to the
# agent as UsageLimits.total_tokens_limit; a round that reaches it stops
# and the last audited proposal is normalized with a warning — never a
# run failure. 0 or less means "no token cap" (the request cap still holds).
def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip() or default)
    except ValueError:
        return default


CLAIMS_INVESTIGATOR_TOTAL_TOKENS = _int_env("CLAIMS_INVESTIGATOR_TOTAL_TOKENS", 1_500_000)


# --- Claims local-mode ingestion (review 2026-08-19, HTTP) ---
# With DOC_SOURCE=local a claims run may be started from a FOLDER PATH on
# this machine instead of a SharePoint link. That is a development
# convenience, and an arbitrary-file-read if it is left open: the server
# would copy any folder the process can read into a run workspace and put
# its pages on screen for anyone who can reach the API. So it is OFF unless
# an operator names the one tree it may read.
CLAIMS_LOCAL_ROOT = os.getenv("CLAIMS_LOCAL_ROOT", "")


def local_ingestion_root() -> Path | None:
    """The folder a local-mode run may be started from, or None (off).

    None is the safe default: without it a folder path is refused exactly
    like any other non-https link, and zip upload remains the local way in.
    """
    value = CLAIMS_LOCAL_ROOT.strip()
    if not value:
        return None
    try:
        return Path(value).expanduser().resolve()
    except (OSError, RuntimeError):
        return None
