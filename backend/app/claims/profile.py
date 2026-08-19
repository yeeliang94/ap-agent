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
    # MYR; a receipt no row uses is a NOTE below this and an OPEN flag at or
    # above it (a large unclaimed receipt is how a missed line looks)
    "unclaimed_receipt_threshold": "100",
    # a mileage line on the report is recognised by this text in its item
    "mileage_item_pattern": "mileage",
    # [{"item": "Taxi", "gl": "713070"}, ...] — the client's category list
    "categories": [],
    # prose: how a mixed report gets its listing category
    "category_rule": "",
    # [{"pattern": "*_Approval.pdf", "role": "ignore"}, ...]
    "file_role_patterns": [],
    # Listing columns the reviewer pins (H9), over the AI's header map:
    # {"Header text": "<role>" | "blank" | "=literal text"} — e.g.
    # {"Processed by": "=AP team", "Cost Center": "blank"}. Roles are
    # listing.ROLES; the universal roles (vendor_name, amount) stay required.
    "listing_columns": {},
    # check code -> on/off; absent means on
    "checks": {},
    # field -> {"by": "reviewer", "at": "2026-08-18", "evidence": "..."}
    "set_by": {},
}

# The flag catalogue — ONE table the checks, the Review screen, the Settings
# toggles and the tests all read, so a flag can never reach a person as a
# bare code. Per code:
#   title       what the reviewer sees as the heading
#   meaning     one plain sentence: what the system found
#   what_to_do  one plain sentence: the reviewer's move
#   kind        money (a payment could be wrong) | evidence (a read to
#               confirm) | mileage | structure (a file or a rule the run
#               needed) | note (never blocks)
#   blocks      "open" (a person must decide) or "info" (a note) by default
#   toggle      whether a client may switch the check off (run-level
#               controls cannot be)
CATALOGUE: dict[str, dict] = {
    "NO_RECEIPT": {
        "title": "No receipt for this row", "kind": "money", "blocks": "open",
        "meaning": "No receipt in the bundle has this row's date, amount and currency.",
        "what_to_do": "Look at the page named (if any): if the row is wrong, fix the value; if the "
                      "receipt is missing, accept to leave the row out; if the client allows it "
                      "without a receipt, dismiss with a note."},
    "RECEIPT_AMBIGUOUS": {
        "title": "Several receipts could be this row's", "kind": "evidence", "blocks": "open",
        "meaning": "More than one receipt matches the row's day, amount and currency, and they "
                   "could not be told apart.",
        "what_to_do": "Open the candidates and choose; dismiss with a note naming which one supports "
                      "the row."},
    "DUPLICATE_RECEIPT": {
        "title": "One receipt claimed by several rows", "kind": "money", "blocks": "open",
        "meaning": "Two or more rows lean on the same single receipt; a receipt supports one row.",
        "what_to_do": "Keep the row the receipt belongs to; accept the others to leave them out, or "
                      "fix a misread value so they match their own receipts."},
    "DUPLICATE_SCAN": {
        "title": "The same receipt appears twice", "kind": "money", "blocks": "open",
        "meaning": "Two pages carry a receipt with the same vendor, date, amount and currency, and "
                   "each is matched to a different row — one receipt scanned twice, or two real ones.",
        "what_to_do": "Compare the two pages. If they are the same receipt, accept one row to leave "
                      "it out; if they are two real receipts, dismiss with a note."},
    "SHARED_RECEIPT": {
        "title": "Receipt also claimed by another employee", "kind": "money", "blocks": "open",
        "meaning": "A receipt with the same vendor, date, amount and currency supports a row of "
                   "another employee in this batch.",
        "what_to_do": "Decide who the expense belongs to; accept the other person's row to leave it "
                      "out, or dismiss with a note if both are genuine."},
    "UNCLAIMED_RECEIPT": {
        "title": "A receipt no row uses", "kind": "note", "blocks": "info",
        "meaning": "A receipt in the bundle supports none of the report's rows — nothing to pay, "
                   "noted so it is not lost. Above the client's threshold it needs a decision, "
                   "because a large unclaimed receipt is how a missed line looks.",
        "what_to_do": "Usually nothing. If a line was left off the report, the employee resubmits; "
                      "if a row was misread, fix it so it matches this receipt."},
    "CURRENCY_MISMATCH": {
        "title": "Currency or exchange arithmetic is off", "kind": "money", "blocks": "open",
        "meaning": "A foreign-currency row has no rate, its amount × rate does not equal its MYR "
                   "total, or its receipt is in another currency.",
        "what_to_do": "Check the receipt's currency and the typed rate; fix the value, or accept to "
                      "leave the row out."},
    "EVIDENCE_UNCERTAIN": {
        "title": "Matched, but the read is uncertain", "kind": "evidence", "blocks": "open",
        "meaning": "The row is matched, but the receipt has no date, a date that fits only after a "
                   "swap or a second read, a low-confidence amount, or an uncertain km figure.",
        "what_to_do": "Open the page and confirm what it says; dismiss with a note if it is right, "
                      "or fix the value if it is not."},
    "MILEAGE_RATE": {
        "title": "Mileage rate is not the client's", "kind": "mileage", "blocks": "open",
        "meaning": "The rate on the KM tab (typed, or implied by amount ÷ km) is not one of the "
                   "client's rates.",
        "what_to_do": "Fix the rate or the amount; if the client's rates in Settings are out of date, "
                      "update them and re-verify."},
    "MILEAGE_ARITHMETIC": {
        "title": "km × rate does not equal the amount", "kind": "mileage", "blocks": "open",
        "meaning": "On a KM-tab row, the kilometres times the rate is not the amount claimed.",
        "what_to_do": "Fix the value that is wrong, or accept to leave the row out."},
    "MILEAGE_LINE_MISMATCH": {
        "title": "Report and KM tab disagree", "kind": "mileage", "blocks": "open",
        "meaning": "A mileage line on the report has no KM-tab trip of the same date and amount, or "
                   "a KM-tab trip has no report line.",
        "what_to_do": "Find the twin on the other tab and fix the date or amount that differs; "
                      "accept the odd one out to leave it out."},
    "MILEAGE_DISCREPANCY": {
        "title": "km claimed differs from the map", "kind": "mileage", "blocks": "open",
        "meaning": "The kilometres claimed are not what the map page prints (allowing exactly double "
                   "for a trip the narrative calls a return).",
        "what_to_do": "Open the map page; fix the km if the claim is wrong, or dismiss with a note if "
                      "the map is."},
    "MILEAGE_NO_MAP": {
        "title": "No map for this trip", "kind": "mileage", "blocks": "open",
        "meaning": "No map page in the bundle carries this trip's date (or, as a note, a map trip no "
                   "KM row claims).",
        "what_to_do": "Ask for the map or accept to leave the trip out; a note needs nothing."},
    "CATEGORY_UNCLEAR": {
        "title": "Listing category not settled", "kind": "structure", "blocks": "open",
        "meaning": "The category for the listing row could not be decided from the report's purpose "
                   "and lines, or no category list is known.",
        "what_to_do": "Choose the category on the employee's line; if there is no list, add it in "
                      "Settings → Claims."},
    "REPORT_UNREADABLE": {
        "title": "The report could not be read", "kind": "structure", "blocks": "open",
        "meaning": "The report file, tab or workbook could not be opened or its layout could not be "
                   "confirmed; the employee was checked from receipts only.",
        "what_to_do": "Open the file; fix it (or its map role) and re-verify, or acknowledge to pay "
                      "from the receipt-derived rows."},
    "REPORT_TOTAL_MISMATCH": {
        "title": "The report's lines do not add up to its total", "kind": "structure", "blocks": "open",
        "meaning": "The lines were read and checked, but their sum is not what the report's total "
                   "cell says — usually a typo in the total, sometimes a line the reader missed.",
        "what_to_do": "Compare the two figures on the report; acknowledge if the lines are right (the "
                      "listing pays the lines), or re-verify after fixing the file."},
    "NO_REPORT": {
        "title": "No expense report — rows built from receipts", "kind": "structure", "blocks": "open",
        "meaning": "The folder has receipts but no report, so the rows to pay were built from the "
                   "receipts found.",
        "what_to_do": "Confirm the derived list is what should be paid; acknowledge, or fix values."},
    "MISSING_REFERENCE": {
        "title": "A control this batch needs is not set", "kind": "structure", "blocks": "open",
        "toggle": False, "identity": ("what",),
        "meaning": "The batch needs something the run does not have — mileage rates for the KM check, "
                   "or the month's listing for the column order — so that check could not run.",
        "what_to_do": "Set it (Settings → Claims, or link the listing) and re-verify, or acknowledge "
                      "to proceed without it."},
    # ---- hardening H3–H8: files, grouping, evidence-derived lines, tools ----
    "ARTIFACT_UNRESOLVED": {
        "title": "A file nobody has placed", "kind": "structure", "blocks": "open",
        "toggle": False, "identity": ("artifact_id",),
        "cites": "the file itself ({file, page 1})",
        "meaning": "A file in the batch has no disposition yet: it was not read as a report or receipts, "
                   "and no one has said it is irrelevant, unreadable or a duplicate. Nothing uploaded "
                   "vanishes silently, so it blocks the output until settled.",
        "what_to_do": "Open the file. Mark it irrelevant (with why), unreadable, or a duplicate — or move it "
                      "into the right case at the map and re-verify."},
    "CLAIMANT_UNKNOWN": {
        "title": "Nobody knows whose claim this is", "kind": "structure", "blocks": "open",
        "toggle": False, "identity": ("case_id",),
        "cites": "the case ({what: case id}); the case's files are listed on the Map & Group screen",
        "meaning": "The case has lines or evidence but no confirmed claimant: the files carry no name or "
                   "code, or only weak hints. A payment cannot go to a guessed person.",
        "what_to_do": "Set the claimant on the case (from a roster, the approval e-mail, or by asking), "
                      "or exclude the case."},
    "OWNERSHIP_CONFLICT": {
        "title": "Two people could own this", "kind": "structure", "blocks": "open",
        "toggle": False, "identity": ("case_id",),
        "cites": "the case ({what: case id}) and the first conflicting file ({file})",
        "meaning": "Strong identity signals in the case's files point at different people (two names, or "
                   "a name and a code that belong to someone else).",
        "what_to_do": "Look at the cited files; split the case, move the evidence, or set the claimant "
                      "with a note."},
    "UNASSIGNED_EVIDENCE": {
        "title": "Evidence that belongs to no case", "kind": "note", "blocks": "info",
        "identity": ("evidence_id", "artifact_id"),
        "cites": "the evidence page ({file, page, position})",
        "meaning": "A receipt or file was read but could not be placed with any case on a sound basis. "
                   "It is listed so it is not lost; nothing is paid on it.",
        "what_to_do": "If it belongs to someone, move it into their case at the map; otherwise leave it."},
    "CLAIM_AMOUNT_UNCONFIRMED": {
        "title": "Amounts taken from receipts, not from a claim", "kind": "money", "blocks": "open",
        "toggle": False, "identity": ("case_id",),
        "cites": "the first derived line's receipt page ({file, page, position}); every line is listed in the reason",
        "meaning": "The case's lines were built from receipts because no claim summary states what is claimed. "
                   "A receipt total is a proposal, not an approved amount — one confirmation per case, with "
                   "every derived line listed.",
        "what_to_do": "Read the listed lines; dismiss with a note to confirm they are what should be paid, or "
                      "fix a value / accept a line's flag to leave it out first."},
    "PURPOSE_UNKNOWN": {
        "title": "No stated purpose", "kind": "note", "blocks": "info",
        "identity": ("case_id",),
        "cites": "the case ({what: case label})",
        "meaning": "No file of this case states the purpose of the expenses; the lines were built from evidence. "
                   "Recorded so the gap is visible — the category decision (CATEGORY_UNCLEAR when it could not "
                   "be settled) is what blocks.",
        "what_to_do": "Usually nothing; if the category shown looks wrong for these lines, choose another on the case."},
    "NO_SUMMARY": {
        "title": "No claim summary — lines derived from evidence", "kind": "structure", "blocks": "open",
        "identity": ("case_id",),
        "cites": "the first receipt page ({file, page, position}), or the case ({what}) when none",
        "meaning": "No expense report or claim list was found for this case, so the lines to pay were built "
                   "from the receipts and maps found (the delivered NO_REPORT, for any input shape).",
        "what_to_do": "Confirm the derived list is what should be paid; acknowledge, or fix values."},
    "TOOL_UNAVAILABLE": {
        "title": "A tool this investigation needed is switched off", "kind": "structure", "blocks": "open",
        "toggle": False, "identity": ("what",),
        "cites": "the tool and call ({what: tool:call id})",
        "meaning": "The investigation genuinely needed a capability that is not enabled here (for example "
                   "the isolated Python sandbox), so part of it could not be done.",
        "what_to_do": "Do that part by hand and acknowledge, or enable the tool where policy allows and "
                      "re-run."},
    "TOOL_FAILED": {
        "title": "A read failed during the investigation", "kind": "structure", "blocks": "open",
        "toggle": False, "identity": ("what",),
        "cites": "the tool and call ({what: tool:call id}), or 'budget'",
        "meaning": "A tool call (a workbook, document, search or calculation) failed or was cut short, so "
                   "the investigation may be incomplete for the named file.",
        "what_to_do": "Open the file named; if it is fine, re-verify; if not, mark it unreadable."},
    "SANDBOX_LIMIT": {
        "title": "A calculation was stopped at its limit", "kind": "structure", "blocks": "open",
        "toggle": False, "identity": ("what",),
        "cites": "the sandbox call ({what: run_python:call id})",
        "meaning": "Model-written Python hit a time, memory or output limit and was killed; nothing it "
                   "produced was used.",
        "what_to_do": "Nothing to trust here; the rest of the run stands. Acknowledge, or re-run."},
}

# The checks a client may switch off (run-level controls stay on).
CHECK_CODES = tuple(code for code, entry in CATALOGUE.items() if entry.get("toggle", True))

# The kinds, in the order the Review screen shows them.
FLAG_KINDS = ("money", "evidence", "mileage", "structure", "note")


# The default identity of a flag instance: its code + the row and the
# evidence it is about. Catalogue entries name another identity where the
# flag is about something else (a file, a case, a run-level control).
DEFAULT_IDENTITY = ("row_id", "evidence_id")


def describe(code: str) -> dict:
    """The catalogue entry, or a plain fallback so an unknown code still
    renders (title from the code) rather than failing."""
    entry = CATALOGUE.get(code)
    if entry:
        return {"code": code, **entry, "toggle": entry.get("toggle", True),
                "identity": list(entry.get("identity", DEFAULT_IDENTITY))}
    return {"code": code, "title": code.replace("_", " ").capitalize(), "meaning": "", "what_to_do": "",
            "kind": "structure", "blocks": "open", "toggle": True, "identity": list(DEFAULT_IDENTITY)}


def flag_key(flag) -> tuple:
    """The idempotency key of a flag (a dict from checks, or a ClaimFlag):
    its code plus the identity fields the catalogue names, so a re-run or a
    regroup never raises the same finding twice and a decided flag stays
    decided. "what" reads cite["what"] (run-level controls)."""
    get = (lambda k: flag.get(k)) if isinstance(flag, dict) else (lambda k: getattr(flag, k, None))
    code = get("code")
    fields = CATALOGUE.get(code, {}).get("identity", DEFAULT_IDENTITY)
    parts = [code]
    for f in fields:
        if f == "what":
            parts.append(str((get("cite") or {}).get("what", "")))
        else:
            parts.append(str(get(f) or ""))
    return tuple(parts)


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
