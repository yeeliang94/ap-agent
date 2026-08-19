"""What a claims settings body may say, and what it means.

The per-client profile is the few values CODE needs — mileage rates,
tolerances, the receipt-optional list, the category list and rule, the
listing column pins, which checks are on. Every one of them ends up in an
arithmetic comparison or an output column, so each is validated on the way
in and refused with a sentence a reviewer can act on ("Rate for 'Car' must
be a number per km, e.g. 0.64"), not a type name.

That is why this stays hand-written rather than becoming field types on a
request model: a wrong rate is a 400 with an explanation, not a 422 with a
JSON pointer. The route keeps a request model for the SHAPE of the body
(profile is an object, playbook is text) and hands the values here.

Validation is all-or-nothing: everything is checked before anything is
saved, so a body with one bad rate changes nothing at all.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import HTTPException

MAX_PLAYBOOK_CHARS = 4000


def merged_profile(current: dict, profile_in: dict) -> dict:
    """The client's profile with the submitted fields validated and applied.

    Only the keys present in `profile_in` are touched; the rest of the
    stored profile is carried through untouched.
    """
    from . import profile as profile_mod

    merged = {**current}
    if "mileage_rates" in profile_in:
        rates = profile_in["mileage_rates"]
        if not isinstance(rates, dict):
            raise HTTPException(400, "mileage_rates must map vehicle type to a rate.")
        clean = {}
        for vehicle, rate in rates.items():
            vehicle = str(vehicle).strip()
            try:
                value = Decimal(str(rate).strip())
                if not value.is_finite() or value <= 0 or value > 100:
                    raise InvalidOperation
            except InvalidOperation:
                raise HTTPException(400, f"Rate for {vehicle!r} must be a number per km, e.g. 0.64.")
            if vehicle:
                clean[vehicle] = f"{value.normalize():f}"
        merged["mileage_rates"] = clean
    if "km_tolerance" in profile_in:
        try:
            tol = Decimal(str(profile_in["km_tolerance"]).strip() or "0")
            if not tol.is_finite() or tol < 0 or tol > 100:
                raise InvalidOperation
        except InvalidOperation:
            raise HTTPException(400, "km tolerance must be a number of km, e.g. 0 or 0.5.")
        merged["km_tolerance"] = f"{tol.normalize():f}"
    if "receipt_date_window_days" in profile_in:
        try:
            days = int(profile_in["receipt_date_window_days"])
        except (TypeError, ValueError):
            raise HTTPException(400, "receipt date window must be a whole number of days.")
        if days < 0 or days > 31:
            raise HTTPException(400, "receipt date window must be between 0 and 31 days.")
        merged["receipt_date_window_days"] = days
    for key in ("receipt_optional_items",):
        if key in profile_in:
            items = profile_in[key]
            if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
                raise HTTPException(400, f"{key} must be a list of expense item names.")
            merged[key] = [i.strip() for i in items if i.strip()][:200]
    if "unclaimed_receipt_threshold" in profile_in:
        try:
            thr = Decimal(str(profile_in["unclaimed_receipt_threshold"]).strip() or "0")
            if not thr.is_finite() or thr < 0 or thr > 1_000_000:
                raise InvalidOperation
        except InvalidOperation:
            raise HTTPException(400, "unclaimed receipt threshold must be an amount in MYR, e.g. 100.")
        merged["unclaimed_receipt_threshold"] = f"{thr.normalize():f}"
    if "mileage_item_pattern" in profile_in:
        pat = str(profile_in["mileage_item_pattern"]).strip()
        if not pat or len(pat) > 60:
            raise HTTPException(400, "mileage item pattern must be 1–60 characters.")
        merged["mileage_item_pattern"] = pat
    if "category_rule" in profile_in:
        merged["category_rule"] = str(profile_in["category_rule"]).strip()[:1000]
    if "categories" in profile_in:
        cats = profile_in["categories"]
        if not isinstance(cats, list):
            raise HTTPException(400, "categories must be a list of {item, gl}.")
        merged["categories"] = [{"item": str(c.get("item", "")).strip()[:80],
                                 "gl": str(c.get("gl", "")).strip()[:20]}
                                for c in cats if isinstance(c, dict) and c.get("item")][:300]
    if "file_role_patterns" in profile_in:
        pats = profile_in["file_role_patterns"]
        if not isinstance(pats, list):
            raise HTTPException(400, "file_role_patterns must be a list of {pattern, role}.")
        from . import mapping

        merged["file_role_patterns"] = [
            {"pattern": str(p.get("pattern", "")).strip()[:120], "role": str(p.get("role", ""))}
            for p in pats if isinstance(p, dict) and p.get("pattern")
            and p.get("role") in mapping.ROLES][:100]
    if "listing_columns" in profile_in:
        from . import listing as listing_mod

        pins = profile_in["listing_columns"]
        if not isinstance(pins, dict):
            raise HTTPException(400, "listing_columns must map a header text to a role, 'blank' or '=text'.")
        clean_pins = {}
        for k, v in pins.items():
            k, v = str(k).strip()[:80], str(v).strip()[:120]
            if not k:
                continue
            if not (v == "blank" or v.startswith("=") or v in listing_mod.ROLES):
                raise HTTPException(400, f"listing column {k!r}: {v!r} must be a role "
                                         f"({', '.join(listing_mod.ROLES)}), 'blank', or '=text'.")
            clean_pins[k] = v
        merged["listing_columns"] = clean_pins
    if "checks" in profile_in:
        checks = profile_in["checks"]
        if not isinstance(checks, dict):
            raise HTTPException(400, "checks must map a check code to on/off.")
        merged["checks"] = {str(k): bool(v) for k, v in checks.items()
                            if str(k) in profile_mod.CHECK_CODES}
    return merged


def clean_playbook(text) -> str:
    """The client's steering paragraph, as it will be stored."""
    if not isinstance(text, str) or len(text) > MAX_PLAYBOOK_CHARS:
        raise HTTPException(400, f"The playbook must be text, at most {MAX_PLAYBOOK_CHARS} characters.")
    return text.strip()
