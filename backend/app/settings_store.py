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

from . import config, switches
from .db import SessionLocal
from .models import AppSetting, AuditEvent

DEFAULTS = {
    # Feature switches (app/switches.py): stored as "0"/"1", env = default.
    **switches.defaults(),
    "client_name": config.CLIENT_NAME,
    "sharepoint_folder_url": os.getenv(
        "SHAREPOINT_FOLDER_URL",
        "https://example.sharepoint.com/sites/clientabc/Shared%20Documents/AP%20Reference",
    ),
    # What the payment-listing draft signs and estimates (listing_draft):
    # the names on the Prepared by / Reviewed by line, and the bank charge
    # per payment the client budgets (RM 0.10 is Maybank's IBG fee).
    "draft_prepared_by": "",
    "draft_reviewed_by": "",
    "draft_bank_charge": "0.10",
}


DRAFT_KEYS = ("draft_prepared_by", "draft_reviewed_by", "draft_bank_charge")


def draft_settings_raw() -> dict[str, str]:
    """The three draft settings as stored — what a run snapshots at start."""
    return {key: get_setting(key) for key in DRAFT_KEYS}


def draft_settings(snapshot: dict | None = None) -> dict:
    """The listing writer's inputs, typed: names as text, the charge as a
    Decimal per payment. Read from the run's snapshot when it has one, so
    a later edit in Settings never changes an older run's draft; runs
    created before the snapshot carried these fall back to today's values.
    """
    from decimal import Decimal, InvalidOperation
    raw = (snapshot or {}).get("draft_settings") or draft_settings_raw()
    try:
        charge = Decimal(raw.get("draft_bank_charge") or "0")
        if not charge.is_finite():
            raise InvalidOperation
    except InvalidOperation:
        charge = Decimal("0")
    return {"prepared_by": raw.get("draft_prepared_by", ""),
            "reviewed_by": raw.get("draft_reviewed_by", ""),
            "bank_charge": charge}


def get_setting(key: str) -> str:
    db = SessionLocal()
    try:
        row = db.get(AppSetting, key)
        return row.value if row else DEFAULTS[key]
    finally:
        db.close()


def get_saved(key: str) -> str | None:
    """The stored value alone — None when nobody has saved one. Callers
    with a livelier default than DEFAULTS (the switches read config at
    call time) use this instead of get_setting."""
    db = SessionLocal()
    try:
        row = db.get(AppSetting, key)
        return row.value if row else None
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
