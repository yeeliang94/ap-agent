"""The client profile, playbook and last confirmed map — per client name.

How the agent is told what a client does (PRD, "Steering"):

  profile   structured VALUES that code applies exactly — mileage rates by
            vehicle, km tolerance, receipt date window, receipt-optional
            items, the mileage item pattern, file-role patterns, the
            category list, which checks are on. Every value may carry
            who/when/evidence set it.
  playbook  half a page of plain-language notes the AI is shown at the
            map step and in every worker. Steers WHERE to look; never
            decides pass/fail.
  last map  the client's last confirmed claim map, shown to the map AI as
            a worked example on the next run.

Company facts are NEVER hard-coded here (PRD, "Universal rules vs company
facts"): the defaults below are the universal defaults (exact km, same-day
receipts, every check on) and EMPTY client values. LinkedIn's rates and
receipt-optional items live in the sample's ground truth and are set
through Settings, discovered (v2), or learned from decisions (Flow 5).

Stored in app_settings under keys that carry the client name, so several
clients can coexist even though the app runs one at a time today. A run
takes a SNAPSHOT of all three at start and is judged by that snapshot.
"""
from __future__ import annotations

import copy
import json
from datetime import date

from ..db import SessionLocal
from ..models import AppSetting, AuditEvent

# Universal defaults. Client values start empty.
PROFILE_DEFAULTS: dict = {
    # {"Car": "0.64", "Motorcycle": "0.35"} — as text, Decimal-safe
    "mileage_rates": {},
    # km difference tolerated between the claimed km and the map (0 = exact
    # or exactly double for a return trip)
    "km_tolerance": "0",
    # how many days a receipt's date may differ from the row's (0 = same day)
    "receipt_date_window_days": 0,
    # expense items allowed to say "receipt included = N"
    "receipt_optional_items": [],
    # a mileage line on the report is recognised by this text in its item
    "mileage_item_pattern": "mileage",
    # [{"item": "Taxi", "gl": "713070"}, ...] — the client's category list
    "categories": [],
    # prose: how a mixed report gets its listing category
    "category_rule": "",
    # [{"pattern": "*_Approval.pdf", "role": "ignore"}, ...]
    "file_role_patterns": [],
    # check code -> on/off; absent means on
    "checks": {},
    # field -> {"by": "reviewer", "at": "2026-08-18", "evidence": "..."}
    "set_by": {},
}

CHECK_CODES = ("NO_RECEIPT", "RECEIPT_AMBIGUOUS", "DUPLICATE_RECEIPT",
               "UNCLAIMED_RECEIPT", "CURRENCY_MISMATCH", "MILEAGE_RATE",
               "MILEAGE_LINE_MISMATCH", "MILEAGE_DISCREPANCY", "MILEAGE_NO_MAP",
               "CATEGORY_UNCLEAR", "REPORT_UNREADABLE", "NO_REPORT")


def _key(kind: str, client: str) -> str:
    return f"claims_{kind}:{client.strip()}"


def _get(key: str, default: str = "") -> str:
    db = SessionLocal()
    try:
        row = db.get(AppSetting, key)
        return row.value if row else default
    finally:
        db.close()


def _set(values: dict[str, str], action: str, detail: str) -> None:
    db = SessionLocal()
    try:
        for key, value in values.items():
            row = db.get(AppSetting, key)
            if row:
                row.value = value
            else:
                db.add(AppSetting(key=key, value=value))
        db.add(AuditEvent(run_id="", actor="reviewer", action=action, detail=detail[:2000]))
        db.commit()
    finally:
        db.close()


def get_profile(client: str) -> dict:
    """The client's profile, with every default filled in."""
    raw = _get(_key("profile", client))
    stored = {}
    if raw:
        try:
            stored = json.loads(raw)
        except json.JSONDecodeError:
            stored = {}
    profile = copy.deepcopy(PROFILE_DEFAULTS)
    for key, value in stored.items():
        if key in profile:
            profile[key] = value
    return profile


def save_profile(client: str, profile: dict, by: str = "reviewer",
                 evidence: str = "") -> dict:
    """Store the profile. Only known fields are kept; changed fields get a
    'set by <who> on <date>' mark so the Settings screen (and every flag's
    basis) can say where a value came from. Audited."""
    current = get_profile(client)
    cleaned = copy.deepcopy(current)
    changes = []
    for key in PROFILE_DEFAULTS:
        if key in ("set_by",) or key not in profile:
            continue
        if profile[key] != current.get(key):
            changes.append(f"{key}: {current.get(key)!r} -> {profile[key]!r}")
            cleaned[key] = profile[key]
            cleaned["set_by"][key] = {"by": by, "at": date.today().isoformat(),
                                      "evidence": evidence}
    if changes:
        _set({_key("profile", client): json.dumps(cleaned)},
             "claims_profile_changed", f"{client}: " + "; ".join(changes))
    return cleaned


def get_playbook(client: str) -> str:
    return _get(_key("playbook", client))


def save_playbook(client: str, text: str) -> None:
    old = get_playbook(client)
    if old != text:
        _set({_key("playbook", client): text}, "claims_playbook_changed",
             f"{client}: playbook edited ({len(old)} -> {len(text)} characters)")


def get_last_map(client: str) -> dict:
    raw = _get(_key("last_map", client))
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def save_last_map(client: str, claim_map: dict, run_id: str) -> None:
    _set({_key("last_map", client): json.dumps({"run_id": run_id, "map": claim_map,
                                                   "at": date.today().isoformat()})},
         "claims_last_map_saved", f"{client}: map of run {run_id} remembered")


def forget_last_map(client: str) -> None:
    _set({_key("last_map", client): ""}, "claims_last_map_forgotten", client)


def snapshot(client: str) -> dict:
    """Everything a run needs to be judged consistently, frozen at start."""
    return {"client_name": client, "profile": get_profile(client),
            "playbook": get_playbook(client), "last_map": get_last_map(client)}


def profile_of(snapshot_: dict | None) -> dict:
    """The profile inside a run's snapshot, defaults filled in."""
    profile = copy.deepcopy(PROFILE_DEFAULTS)
    for key, value in ((snapshot_ or {}).get("profile") or {}).items():
        if key in profile:
            profile[key] = value
    return profile


def check_enabled(profile: dict, code: str) -> bool:
    return bool((profile.get("checks") or {}).get(code, True))


def basis_for(profile: dict, field: str, text: str) -> str:
    """'client profile: <text> (set by reviewer on 2026-08-18)' — the
    sentence a flag shows as the rule it applied and where it came from."""
    mark = (profile.get("set_by") or {}).get(field) or {}
    if mark:
        who = f" (set by {mark.get('by', 'reviewer')} on {mark.get('at', '?')}"
        who += f", evidence: {mark['evidence']})" if mark.get("evidence") else ")"
    else:
        who = " (default)"
    return f"client profile: {text}{who}"
