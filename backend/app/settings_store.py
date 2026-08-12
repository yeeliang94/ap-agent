"""App-level settings the reviewer may change from the screen.

Only values that describe WHOSE documents are processed and WHERE the
reference files live belong here. Secrets (API keys, the proxy URL, the
MCP endpoint) stay in .env on purpose — they are IT's to manage, and the
browser must never see or set them.

Stored in the database so they survive restarts. The .env values act as
defaults until the first in-app save, so existing installs keep working
unchanged.
"""
from __future__ import annotations

import os

from . import config
from .db import SessionLocal
from .models import AppSetting, AuditEvent

DEFAULTS = {
    "client_name": config.CLIENT_NAME,
    "sharepoint_folder_url": os.getenv(
        "SHAREPOINT_FOLDER_URL",
        "https://example.sharepoint.com/sites/clientabc/Shared%20Documents/AP%20Reference",
    ),
}


def get_setting(key: str) -> str:
    db = SessionLocal()
    try:
        row = db.get(AppSetting, key)
        return row.value if row else DEFAULTS[key]
    finally:
        db.close()


def set_settings(values: dict[str, str]) -> None:
    """Save several settings in ONE transaction — a half-saved pair (new
    client name, old client's folder) would silently mix clients. The
    change is recorded in the audit trail with before/after values."""
    unknown = set(values) - set(DEFAULTS)
    if unknown:
        raise KeyError(", ".join(sorted(unknown)))
    db = SessionLocal()
    try:
        changes = []
        for key, value in values.items():
            row = db.get(AppSetting, key)
            old = row.value if row else DEFAULTS[key]
            if row:
                row.value = value
            else:
                db.add(AppSetting(key=key, value=value))
            if old != value:
                changes.append(f"{key}: {old!r} -> {value!r}")
        if changes:
            db.add(AuditEvent(run_id="", actor="reviewer",
                              action="settings_changed",
                              detail="; ".join(changes)))
        db.commit()
    finally:
        db.close()
