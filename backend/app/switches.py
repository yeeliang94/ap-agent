"""Feature switches a reviewer may flip from the Settings screen.

Each switch is stored in the settings store (database), with the .env
value as its default until the first in-app save — existing installs keep
working unchanged. A change applies to NEW claims runs only: a run
snapshots the switches when it is created (claims routes, _store_new_run)
and per-run behavior reads that snapshot (`for_run`), so a mid-flight run
never changes rules. Live reads (`on`) are for the HTTP contract and for
runs from before the snapshot carried switches.

Secrets and machine facts (API keys, the MCP endpoint, the sandbox
runner) are NOT switches — they stay in .env, IT-managed, and the
switches route only reports whether they are set (`deployment_facts`),
never their values.
"""
from __future__ import annotations

from . import config

TRUE_WORDS = ("1", "true", "yes", "on")

# key -> (label, description). The default is the .env value, answered by
# _env_default LIVE (getattr, not captured at import) so tests that
# monkeypatch config.CLAIMS_* keep steering exactly as they did before
# the switches existed.
SWITCHES: dict[str, tuple[str, str]] = {
    "claims_sharepoint_source": (
        "SharePoint source",
        "Allow starting a claims run from a SharePoint folder link. "
        "Off, uploads (a folder, a zip, or files) are the only way in."),
    "claims_case_model": (
        "Case model",
        "Claim Cases, Source Artifacts and the investigation record on the "
        "run detail. Off, the delivered per-claimant fields stay "
        "authoritative for the screen."),
    "claims_full_dump_grouping": (
        "Full-dump grouping",
        "Map & Group actions for a batch with no per-claimant folders at "
        "all — the agent proposes the grouping for your confirmation."),
    "claims_agentic_investigation": (
        "Agentic investigation",
        "New runs investigate with the tool-using agent instead of the "
        "structured-folder mapper. Old runs are never reinterpreted."),
    "claims_shadow_investigation": (
        "Shadow investigation",
        "With agentic investigation off, also run the tool-using "
        "investigator beside the mapper and record the comparison. "
        "Nothing it proposes is used."),
}

_CONFIG_ATTR = {
    "claims_case_model": "CLAIMS_CASE_MODEL",
    "claims_full_dump_grouping": "CLAIMS_FULL_DUMP_GROUPING",
    "claims_agentic_investigation": "CLAIMS_AGENTIC_INVESTIGATION",
    "claims_shadow_investigation": "CLAIMS_SHADOW_INVESTIGATION",
}


def _env_default(key: str) -> bool:
    if key == "claims_sharepoint_source":
        return config.DOC_SOURCE == "mcp"
    return bool(getattr(config, _CONFIG_ATTR[key]))


def defaults() -> dict[str, str]:
    """The stored defaults, for the settings store's DEFAULTS table."""
    return {key: ("1" if _env_default(key) else "0") for key in SWITCHES}


def saved(key: str) -> str | None:
    """The reviewer's stored choice, or None when nobody has saved one."""
    from . import settings_store

    return settings_store.get_saved(key)


def on(key: str) -> bool:
    """The switch's live value — steers the HTTP contract and new runs.
    A reviewer's saved choice wins; without one, the .env value answers."""
    value = saved(key)
    if value is None:
        return _env_default(key)
    return value.strip().lower() in TRUE_WORDS


def snapshot() -> dict[str, bool]:
    """Every switch's value right now — stamped onto a run at creation."""
    return {key: on(key) for key in SWITCHES}


def for_run(run_snapshot: dict | None, key: str) -> bool:
    """The switch as this run knows it: its snapshot when it has one, the
    live value for runs created before snapshots carried switches."""
    stamped = (run_snapshot or {}).get("switches") or {}
    if key in stamped:
        return bool(stamped[key])
    return on(key)


def listed() -> list[dict]:
    """Every switch with its words and value, for the Settings screen.
    `saved` says whether a reviewer chose it or the .env default answers."""
    return [{"key": key, "label": label, "description": description,
             "value": on(key), "default": _env_default(key),
             "saved": saved(key) is not None}
            for key, (label, description) in SWITCHES.items()]


def deployment_facts() -> list[dict]:
    """IT-managed .env facts, shown read-only — whether a thing is set,
    never a secret's value."""
    return [
        {"label": "Document source mechanism (.env DOC_SOURCE)",
         "value": config.DOC_SOURCE},
        {"label": "Python sandbox",
         "value": "on" if config.CLAIMS_PYTHON_SANDBOX else "off"},
        {"label": "Sandbox runner",
         "value": "set" if config.CLAIMS_SANDBOX_RUNNER else "not set"},
        {"label": "AI key",
         "value": "set" if config.OPENAI_API_KEY else "not set"},
        {"label": "AI proxy",
         "value": "set" if config.LLM_PROXY_URL else "direct"},
        {"label": "Models (sort / extract / judge)",
         "value": f"{config.SORT_MODEL} / {config.EXTRACT_MODEL} / {config.JUDGE_MODEL}"},
        {"label": "Local ingestion root",
         "value": "set" if config.CLAIMS_LOCAL_ROOT else "not set"},
    ]
