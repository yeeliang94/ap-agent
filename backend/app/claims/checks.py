"""The checks (PRD Flow 3c–3e). Code decides; a person clears.

Inputs are plain dicts (rows, evidence, the profile), so the same code
runs inside a worker and again, instantly, when a reviewer corrects a
value. Every flag carries:

  code    — a code from the catalogue in profile.py (title, meaning, what
            to do — the words a person sees)
  reason  — the plain sentence for THIS row
  basis   — the rule it applied and where it came from
  cite    — where to look: {"file", "page", "position"} or {"sheet", "row"}
  status  — "open" (a person must decide) or "info" (a note; never blocks)

Matching rows ↔ receipts: same currency, same amount (to the cent), same
day (the profile's date window, default 0). One candidate → matched.
Several → an AI tie-break among the candidates only (skipped when they
are value-identical: the first is taken); unsure → RECEIPT_AMBIGUOUS.
None → NO_RECEIPT (naming how many pages in how many files were searched,
and any same-day receipt whose amount differs), unless the row says
receipt=N AND the item is receipt-optional in the profile → an info note.
A receipt may support one row: two rows on the same receipt →
DUPLICATE_RECEIPT on both. The same receipt by value on two pages, each
supporting a different row → DUPLICATE_SCAN on both. Receipts no row
claims → UNCLAIMED_RECEIPT: a note below the client's threshold, open at
or above it. Foreign rows: rate present and ≠ 1, amount × rate = total,
receipt currency = row currency, else CURRENCY_MISMATCH. A match on a
receipt with no date, a date that fits only after a swap or the second
read, or a low-confidence date/amount/currency → EVIDENCE_UNCERTAIN (a
person confirms; never accepted silently). The same receipt matched under
two employees is the worker's run-close pass (SHARED_RECEIPT).

Mileage rows ↔ map trips: same date; km equal (± tolerance, default 0),
or exactly double when the narrative says a return trip; else
MILEAGE_DISCREPANCY with both numbers. No trip that day → MILEAGE_NO_MAP.
A match on a km figure the reader was unsure of → EVIDENCE_UNCERTAIN.
Rate not one of the profile's rates → MILEAGE_RATE (a row with no rate
typed is judged by amount ÷ km). km × rate ≠ amount → MILEAGE_ARITHMETIC.
A report mileage line with no KM row (or the reverse) →
MILEAGE_LINE_MISMATCH.

Controls the batch needs and cannot find are RUN-LEVEL flags, never a
silent skip: profile has no mileage rate but the batch has mileage rows
→ MISSING_REFERENCE (raised once per run by the worker).
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from . import profile as profile_mod

INFO = "info"
OPEN = "open"


def _dec(text) -> Decimal | None:
    if text is None or text == "":
        return None
    try:
        d = Decimal(str(text))
        return d if d.is_finite() else None
    except InvalidOperation:
        return None


def _date(text) -> date | None:
    try:
        return date.fromisoformat(str(text)) if text else None
    except ValueError:
        return None


def _swap_day_month(d: date) -> date | None:
    try:
        return date(d.year, d.day, d.month)
    except ValueError:
        return None


def _flag(code: str, reason: str, basis: str = "", cite: dict | None = None,
          row_id: str = "", evidence_id: str = "", status: str = OPEN) -> dict:
    return {"code": code, "reason": reason, "basis": basis, "cite": cite or {},
            "row_id": row_id, "evidence_id": evidence_id, "status": status}


def _row_cite(row: dict) -> dict:
    return {"sheet": row.get("sheet", ""), "row": row.get("row", 0)}


def _ev_cite(ev: dict) -> dict:
    return {"file": ev.get("file", ""), "page": ev.get("page", 0), "position": ev.get("position", "")}


def _where(ev: dict) -> str:
    pos = f", {ev['position']}" if ev.get("position") else ""
    return f"{ev.get('file', '?')} page {ev.get('page', '?')}{pos}"


def _conf_note(ev: dict) -> str:
    conf = ev.get("confidence") or {}
    if not conf:
        return ""
    return " (low-confidence read: " + "; ".join(f"{k}: {v}" for k, v in list(conf.items())[:3]) + ")"


# The receipt fields a match stands on; doubt on any of these goes to a person.
_CRITICAL = ("date", "amount", "currency", "receipt", "page")


def _receipt_key(ev: dict) -> tuple:
    """What makes two receipts 'the same' by value: vendor (case and spaces
    folded), date, amount to the cent, currency."""
    v = ev.get("values") or {}
    vendor = " ".join(str(v.get("vendor") or "").lower().split())
    if not vendor:
        # no vendor read: two such receipts cannot be called "the same"
        return ("<no vendor>", ev.get("id") or id(ev))
    amount = _dec(v.get("amount"))
    return (vendor, str(v.get("date") or ""), str(amount.quantize(Decimal("0.01"))) if amount is not None else "",
            (v.get("currency") or "MYR").upper())


def _receipt_doubts(ev: dict, row: dict, window: int) -> list[str]:
    """Why a matched receipt is not to be trusted without a look: no date,
    a date that only fits after swapping day and month or taking the
    second read's value, or a low-confidence note on a critical field."""
    v, conf = ev.get("values") or {}, ev.get("confidence") or {}
    doubts = []
    ed, d = _date(v.get("date")), _date(row["values"].get("date"))
    if not ed:
        doubts.append("the receipt has no readable date, so it cannot prove the day")
    elif d and abs((ed - d).days) > window:
        doubts.append(f"the receipt reads {ed.isoformat()}, the row says {d.isoformat()} "
                      "(matched only by swapping day and month, or by the second read's date)")
    for k in _CRITICAL:
        if conf.get(k):
            doubts.append(f"{k}: {conf[k]}")
    return doubts


def _is_mileage(row: dict, profile: dict) -> bool:
    pattern = (profile.get("mileage_item_pattern") or "mileage").lower()
    item = (row["values"].get("item_name") or row["values"].get("item") or "").lower()
    return pattern in item


async def run_checks(rows: list[dict], evidence: list[dict], profile: dict, employee: dict,
                     searched: tuple[int, int], tie_break=None) -> dict:
    """Run every check for one employee.

    rows: [{"id", "kind": expense|mileage|derived, "sheet", "row", "values": {...}}]
    evidence: [{"id", "kind": receipt|map_trip, "file", "page", "position",
                "values": {...}, "confidence": {...}}]
    searched: (pages, files) — how much was looked through, for NO_RECEIPT
    tie_break: async (row, candidates) -> evidence id or "" — the AI, only
               among candidates; None means "always ambiguous"

    Returns {"flags": [...], "verdicts": {row_id: (verdict, evidence_id)},
             "matches": {evidence_id: row_id}, "notes": [...]}.
    """
    flags: list[dict] = []
    verdicts: dict[str, tuple[str, str]] = {}
    matches: dict[str, str] = {}
    notes: list[str] = []
    on = lambda code: profile_mod.check_enabled(profile, code)  # noqa: E731

    receipts = [e for e in evidence if e["kind"] == "receipt"]
    trips = [e for e in evidence if e["kind"] == "map_trip"]
    expense_rows = [r for r in rows if r["kind"] in ("expense", "derived") and not _is_mileage(r, profile)]
    report_mileage_rows = [r for r in rows if r["kind"] == "expense" and _is_mileage(r, profile)]
    km_rows = [r for r in rows if r["kind"] == "mileage"]
    window = int(profile.get("receipt_date_window_days") or 0)
    optional = {i.strip().lower() for i in (profile.get("receipt_optional_items") or [])}
    pages, files = searched

    # ---- foreign-currency arithmetic ---------------------------------------
    for row in expense_rows + report_mileage_rows:
        v = row["values"]
        cur = (v.get("currency") or "MYR").upper()
        if cur == "MYR" or not on("CURRENCY_MISMATCH"):
            continue
        amount, rate, total = _dec(v.get("amount")), _dec(v.get("rate")), _dec(v.get("total"))
        if rate is None or rate == 1:
            flags.append(_flag("CURRENCY_MISMATCH",
                               f"Row {row['row']} is in {cur} but has no exchange rate typed"
                               + (" (rate is 1)" if rate == 1 else "") + ".",
                               "universal rule: a foreign-currency row needs its rate; the rate is "
                               "accepted as typed and the arithmetic checked",
                               _row_cite(row), row_id=row["id"]))
        elif amount is not None and total is not None and (amount * rate).quantize(Decimal("0.01")) != total:
            flags.append(_flag("CURRENCY_MISMATCH",
                               f"Row {row['row']}: {cur} {amount} × {rate} = "
                               f"{(amount * rate).quantize(Decimal('0.01'))}, but the row's MYR total "
                               f"says {total}.",
                               "universal rule: amount × rate must equal the MYR total to the cent",
                               _row_cite(row), row_id=row["id"]))

    # ---- rows ↔ receipts -------------------------------------------------------
    def candidates(row: dict) -> list[dict]:
        v = row["values"]
        amount, cur, d = _dec(v.get("amount")), (v.get("currency") or "MYR").upper(), _date(v.get("date"))
        out = []
        for e in receipts:
            ev = e["values"]
            amounts = {_dec(ev.get("amount")), _dec(ev.get("amount_alt"))} - {None}
            if amount not in amounts or (ev.get("currency") or "MYR").upper() != cur:
                continue
            ed = _date(ev.get("date"))
            ed_alt = _date(ev.get("date_alt"))
            if d and ed and abs((ed - d).days) > window and ed_alt and abs((ed_alt - d).days) <= window:
                ed = ed_alt  # the second read's date fits; the low-confidence note stays on the receipt
            if d and ed and abs((ed - d).days) > window:
                # The reader sometimes takes 10/07 as October 7. Same
                # amount and currency on the day/month-swapped date is
                # accepted as a candidate — with a note the reviewer sees.
                swapped = _swap_day_month(ed)
                if swapped is None or abs((swapped - d).days) > window:
                    continue
                e = {**e, "confidence": {**(e.get("confidence") or {}),
                                         "date": f"read as {ed.isoformat()}; matched as {swapped.isoformat()} "
                                                 "(day and month swapped)"}}
            if d and not ed and window == 0:
                # a receipt with no readable date cannot prove the day; keep
                # it as a candidate so a person sees it rather than a NO_RECEIPT
                pass
            out.append(e)
        return out

    cand: dict[str, list[dict]] = {r["id"]: candidates(r) for r in expense_rows}
    # Pass 1: unique pairs (one candidate, and that receipt is nobody else's
    # only candidate).
    claims: dict[str, list[str]] = {}
    for rid, cs in cand.items():
        for c in cs:
            claims.setdefault(c["id"], []).append(rid)
    for row in expense_rows:
        cs = cand[row["id"]]
        if len(cs) == 1 and len(claims.get(cs[0]["id"], [])) == 1:
            matches[cs[0]["id"]] = row["id"]
            verdicts[row["id"]] = ("matched", cs[0]["id"])
    # Pass 2: the rest.
    for row in expense_rows:
        if row["id"] in verdicts:
            continue
        v = row["values"]
        cs = [c for c in cand[row["id"]] if c["id"] not in matches]
        item = (v.get("item_name") or v.get("item") or "").strip()
        if not cs:
            included = (v.get("receipt_included") or "").upper()
            if included == "N" and item.lower() in optional:
                verdicts[row["id"]] = ("optional", "")
                flags.append(_flag("NO_RECEIPT",
                                   f"Row {row['row']} ({item}, {v.get('currency', 'MYR')} {v.get('amount')}) "
                                   f"says receipt included = N; {item} is receipt-optional for this client, "
                                   "so this is a note, not a problem.",
                                   profile_mod.basis_for(profile, "receipt_optional_items",
                                                         f"receipt-optional items: {', '.join(profile.get('receipt_optional_items') or [])}"),
                                   _row_cite(row), row_id=row["id"], status=INFO))
                continue
            if row["kind"] == "derived":
                continue  # built from a receipt: it IS matched by construction
            if not on("NO_RECEIPT"):
                continue
            # A same-day receipt with a different amount is worth naming:
            # it is usually the receipt, and the row is what is wrong.
            d = _date(v.get("date"))
            near = [e for e in receipts if e["id"] not in matches and _date(e["values"].get("date")) == d
                    and (e["values"].get("currency") or "MYR").upper() == (v.get("currency") or "MYR").upper()]
            hint = ""
            if len(near) == 1:
                ea = _dec(near[0]["values"].get("amount"))
                ra = _dec(v.get("amount"))
                if ea is not None and ra is not None:
                    diff = (ra - ea).quantize(Decimal("0.01"))
                    hint = (f" A receipt from {near[0]['values'].get('vendor', '?')} on that day shows "
                            f"{near[0]['values'].get('currency', 'MYR')} {ea} — the row is "
                            f"{'RM ' + str(abs(diff)) + (' more' if diff > 0 else ' less')} "
                            f"({_where(near[0])}){_conf_note(near[0])}.")
            other_cur = [e for e in receipts if e["id"] not in matches
                         and _dec(e["values"].get("amount")) == _dec(v.get("amount"))
                         and _date(e["values"].get("date")) == d
                         and (e["values"].get("currency") or "MYR").upper() != (v.get("currency") or "MYR").upper()]
            if other_cur and on("CURRENCY_MISMATCH"):
                e = other_cur[0]
                flags.append(_flag("CURRENCY_MISMATCH",
                                   f"Row {row['row']} is in {v.get('currency', 'MYR')} but the matching "
                                   f"receipt ({_where(e)}) is in {e['values'].get('currency')}.",
                                   "universal rule: the receipt's currency must equal the row's",
                                   _ev_cite(e), row_id=row["id"], evidence_id=e["id"]))
                verdicts[row["id"]] = ("no_evidence", "")
                continue
            flags.append(_flag("NO_RECEIPT",
                               f"Row {row['row']} ({v.get('date', '?')}, {item}, "
                               f"{v.get('currency', 'MYR')} {v.get('amount')}) has no receipt with the same "
                               f"date, amount and currency — searched {pages} page(s) in {files} file(s)."
                               + hint
                               + (" The row says receipt included = N, but " + item + " is not "
                                  "receipt-optional for this client." if included == "N" else ""),
                               "universal rule: every claim row needs a receipt of the same day, amount and "
                               "currency" + (f"; receipt-optional items: {', '.join(profile.get('receipt_optional_items') or []) or 'none'}"),
                               _row_cite(row) if not near else _ev_cite(near[0]),
                               row_id=row["id"], evidence_id=near[0]["id"] if len(near) == 1 else ""))
            verdicts[row["id"]] = ("no_evidence", "")
            continue
        # The same receipt claimed by several rows, and it is the only
        # candidate for each: one receipt cannot support them all.
        rivals = [r for r in expense_rows if r["id"] != row["id"] and r["id"] not in verdicts
                  and len(cand[r["id"]]) == 1 and cs and len(cs) == 1 and cand[r["id"]][0]["id"] == cs[0]["id"]]
        if len(cs) == 1 and rivals:
            e = cs[0]
            for r in [row] + rivals:
                if on("DUPLICATE_RECEIPT"):
                    flags.append(_flag("DUPLICATE_RECEIPT",
                                       f"Rows {', '.join(str(x['row']) for x in [row] + rivals)} all claim "
                                       f"{v.get('currency', 'MYR')} {v.get('amount')} on {v.get('date')}, but only "
                                       f"one receipt supports them ({e['values'].get('vendor', '?')}, "
                                       f"{_where(e)}){_conf_note(e)}. A receipt may support one row.",
                                       "universal rule: one receipt supports one row",
                                       _ev_cite(e), row_id=r["id"], evidence_id=e["id"]))
                verdicts[r["id"]] = ("duplicate", e["id"])
            continue
        if len(cs) == 1:
            matches[cs[0]["id"]] = row["id"]
            verdicts[row["id"]] = ("matched", cs[0]["id"])
            continue
        # Several candidates: ask the AI to break the tie among THESE only —
        # unless they are the same receipt by every value the reader gives
        # (vendor, date, amount, currency): nothing to tell apart, so the
        # first is taken and DUPLICATE_SCAN below says the rest.
        picked = ""
        if len({_receipt_key(c) for c in cs}) == 1:
            picked = cs[0]["id"]
            notes.append(f"row {row['row']}: {len(cs)} value-identical receipts; the first was taken "
                         "without the AI (see DUPLICATE_SCAN if another row takes the other)")
        elif tie_break is not None:
            try:
                picked = await tie_break(row, cs) or ""
            except Exception:
                picked = ""
        if picked and any(c["id"] == picked for c in cs):
            matches[picked] = row["id"]
            verdicts[row["id"]] = ("matched", picked)
            notes.append(f"row {row['row']}: {len(cs)} candidate receipts, tie broken by the AI")
            continue
        verdicts[row["id"]] = ("ambiguous", "")
        if on("RECEIPT_AMBIGUOUS"):
            flags.append(_flag("RECEIPT_AMBIGUOUS",
                               f"Row {row['row']} ({v.get('date')}, {item}, {v.get('currency', 'MYR')} "
                               f"{v.get('amount')}) could be any of {len(cs)} receipts: "
                               + "; ".join(f"{c['values'].get('vendor', '?')} at {_where(c)}{_conf_note(c)}" for c in cs)
                               + ". Choose which one supports it.",
                               "universal rule: same day, amount and currency; the AI could not tell them apart",
                               _ev_cite(cs[0]), row_id=row["id"], evidence_id=cs[0]["id"]))
    # A match on evidence the reader was not sure of (no date, or the two
    # reads disagreed on date/amount/currency, or the reader said so) is
    # never accepted silently: a person confirms it.
    if on("EVIDENCE_UNCERTAIN"):
        by_id = {e["id"]: e for e in receipts}
        for row in expense_rows:
            verdict, eid = verdicts.get(row["id"], ("", ""))
            if verdict != "matched" or not eid or eid not in by_id or row["kind"] == "derived":
                continue
            e = by_id[eid]
            doubts = _receipt_doubts(e, row, window)
            if doubts:
                v = row["values"]
                flags.append(_flag("EVIDENCE_UNCERTAIN",
                                   f"Row {row['row']} ({v.get('date')}, {v.get('item_name') or v.get('item') or ''}, "
                                   f"{v.get('currency', 'MYR')} {v.get('amount')}) is matched to the receipt from "
                                   f"{e['values'].get('vendor', '?')} at {_where(e)}, but that read is uncertain: "
                                   + "; ".join(doubts) + ". Look at the page and confirm, or correct the value.",
                                   "universal rule: evidence with a missing or doubtful date, amount or currency "
                                   "is confirmed by a person, never accepted silently",
                                   _ev_cite(e), row_id=row["id"], evidence_id=e["id"]))
    # The same receipt on two pages, each supporting a different row: one
    # receipt scanned twice (a double claim) or two real receipts that
    # happen to agree on every value — a person tells which.
    if on("DUPLICATE_SCAN"):
        twins: dict[tuple, list[dict]] = {}
        for e in receipts:
            twins.setdefault(_receipt_key(e), []).append(e)
        for group in twins.values():
            claimed = [(e, matches[e["id"]]) for e in group if e["id"] in matches]
            if len({rid for _, rid in claimed}) < 2:
                continue
            for e, rid in claimed:
                others = [x for x, r in claimed if r != rid]
                flags.append(_flag("DUPLICATE_SCAN",
                                   f"The receipt from {e['values'].get('vendor', '?')} ({e['values'].get('date') or 'no date'}, "
                                   f"{e['values'].get('currency', 'MYR')} {e['values'].get('amount')}) at {_where(e)} "
                                   f"also appears at {'; '.join(_where(x) for x in others)}, and each copy supports a "
                                   "different row. If it is one receipt scanned twice, one of those rows is a "
                                   "double claim; if they are two real receipts, dismiss with a note.",
                                   "universal rule: one receipt supports one row; two pages with the same vendor, "
                                   "date, amount and currency are shown to a person",
                                   _ev_cite(e), row_id=rid, evidence_id=e["id"]))
    # The client's threshold; 0 is a real setting (every unclaimed receipt
    # needs a decision), so only a missing/unparsable value takes the default.
    threshold = _dec(profile.get("unclaimed_receipt_threshold"))
    if threshold is None:
        threshold = Decimal(profile_mod.PROFILE_DEFAULTS["unclaimed_receipt_threshold"])
    for e in receipts:
        if e["id"] not in matches and on("UNCLAIMED_RECEIPT"):
            ev = e["values"]
            amount = _dec(ev.get("amount"))
            big = amount is not None and amount >= threshold
            flags.append(_flag("UNCLAIMED_RECEIPT",
                               f"A receipt from {ev.get('vendor', '?')} ({ev.get('date') or 'no date'}, "
                               f"{ev.get('currency', 'MYR')} {ev.get('amount')}) at {_where(e)} supports no "
                               f"row of the report{_conf_note(e)}. "
                               + (f"It is at or above the client's RM {threshold} threshold — a receipt this "
                                  "size that no row claims is how a missed line looks. Was a line left off the "
                                  "report, or is a row misread? Acknowledge, or fix the row so it matches."
                                  if big else "Nothing to pay — noted so it is not lost."),
                               "universal rule: nothing uploaded vanishes silently"
                               + ("; " + profile_mod.basis_for(profile, "unclaimed_receipt_threshold",
                                                               f"unclaimed receipts at or above RM {threshold} need a decision")
                                  if big else ""),
                               _ev_cite(e), evidence_id=e["id"], status=OPEN if big else INFO))

    # ---- mileage: report lines ↔ KM rows ------------------------------------------
    unpaired_km = list(km_rows)
    for line in report_mileage_rows:
        lv = line["values"]
        twin = next((k for k in unpaired_km if k["values"].get("date") == lv.get("date")
                     and _dec(k["values"].get("amount")) == _dec(lv.get("total") or lv.get("amount"))), None)
        if twin is not None:
            unpaired_km.remove(twin)
            verdicts[line["id"]] = ("matched", "")
            line["values"]["km_row_id"] = twin["id"]
        else:
            verdicts[line["id"]] = ("no_evidence", "")
            if on("MILEAGE_LINE_MISMATCH"):
                flags.append(_flag("MILEAGE_LINE_MISMATCH",
                                   f"Report row {line['row']} is a mileage line ({lv.get('date')}, MYR "
                                   f"{lv.get('total') or lv.get('amount')}) with no KM-tab trip of the same "
                                   "date and amount.",
                                   profile_mod.basis_for(profile, "mileage_item_pattern",
                                                         "one report line per trip, paired with the KM tab by date + amount"),
                                   _row_cite(line), row_id=line["id"]))
    for k in unpaired_km:
        if report_mileage_rows or any(r["kind"] == "expense" for r in rows):
            if on("MILEAGE_LINE_MISMATCH"):
                kv = k["values"]
                flags.append(_flag("MILEAGE_LINE_MISMATCH",
                                   f"KM-tab row {k['row']} ({kv.get('date')}, {kv.get('km')} km, MYR "
                                   f"{kv.get('amount')}) has no matching mileage line on the report.",
                                   profile_mod.basis_for(profile, "mileage_item_pattern",
                                                         "one report line per trip, paired with the KM tab by date + amount"),
                                   _row_cite(k), row_id=k["id"]))

    # ---- mileage: KM rows ↔ map trips; rate ------------------------------------------
    rates = {str(v): k for k, v in (profile.get("mileage_rates") or {}).items()}
    rate_values = [_dec(r) for r in rates]
    tol = _dec(profile.get("km_tolerance")) or Decimal("0")
    used_trips: set[str] = set()
    rates_text = ", ".join(f"{veh} RM {r}/km" for r, veh in rates.items())
    for k in km_rows:
        kv = k["values"]
        km, rate, amount = _dec(kv.get("km")), _dec(kv.get("rate")), _dec(kv.get("amount"))
        if rate_values and on("MILEAGE_RATE"):
            if rate is not None and rate not in rate_values:
                flags.append(_flag("MILEAGE_RATE",
                                   f"KM-tab row {k['row']} uses RM {rate}/km; the client's rates are {rates_text}.",
                                   profile_mod.basis_for(profile, "mileage_rates", "mileage rates " + rates_text),
                                   _row_cite(k), row_id=k["id"]))
            elif rate is None and km and amount is not None:
                # No rate typed: the rate the row implies (amount ÷ km) must
                # still be one of the client's, else the check is not skipped
                # silently — it is raised.
                implied = (amount / km).quantize(Decimal("0.0001"))
                if not any(abs(implied - rv) <= Decimal("0.005") for rv in rate_values if rv is not None):
                    flags.append(_flag("MILEAGE_RATE",
                                       f"KM-tab row {k['row']} has no rate typed; MYR {amount} ÷ {km} km = "
                                       f"RM {implied}/km, which is none of the client's rates ({rates_text}).",
                                       profile_mod.basis_for(profile, "mileage_rates", "mileage rates " + rates_text),
                                       _row_cite(k), row_id=k["id"]))
        if rate is not None and km is not None and amount is not None and on("MILEAGE_ARITHMETIC"):
            expect = (km * rate).quantize(Decimal("0.01"))
            if expect != amount.quantize(Decimal("0.01")):
                flags.append(_flag("MILEAGE_ARITHMETIC",
                                   f"KM-tab row {k['row']}: {km} km × RM {rate}/km = {expect}, but the row's "
                                   f"amount says {amount}.",
                                   "universal rule: km × rate must equal the amount to the cent",
                                   _row_cite(k), row_id=k["id"]))
        same_day = [t for t in trips if t["values"].get("date") == kv.get("date") and t["id"] not in used_trips]
        if not same_day:
            verdicts.setdefault(k["id"], ("no_evidence", ""))
            if on("MILEAGE_NO_MAP"):
                flags.append(_flag("MILEAGE_NO_MAP",
                                   f"KM-tab row {k['row']} ({kv.get('date')}, {kv.get('from', '')} → "
                                   f"{kv.get('to', '')}, {kv.get('km')} km) has no map page for that date "
                                   f"— searched {pages} page(s) in {files} file(s).",
                                   "universal rule: every mileage claim is backed by a map",
                                   _row_cite(k), row_id=k["id"]))
            continue
        chosen, verdict, why = None, "", ""
        for t in same_day:
            printed = _dec(t["values"].get("km_printed"))
            if printed is None:
                continue
            if km is not None and abs(km - printed) <= tol:
                chosen, verdict = t, "one-way"
                break
            if km is not None and abs(km - 2 * printed) <= tol and t["values"].get("return_trip"):
                chosen, verdict = t, "return"
                break
        if chosen is not None:
            used_trips.add(chosen["id"])
            matches[chosen["id"]] = k["id"]
            verdicts[k["id"]] = ("matched", chosen["id"])
            doubt = (chosen.get("confidence") or {}).get("km_printed")
            if doubt and on("EVIDENCE_UNCERTAIN"):
                flags.append(_flag("EVIDENCE_UNCERTAIN",
                                   f"KM-tab row {k['row']} ({kv.get('date')}, {kv.get('from', '')} → "
                                   f"{kv.get('to', '')}, {km} km) matches the map at {_where(chosen)}, but the "
                                   f"km figure on that page is uncertain: {doubt}. Open the page and confirm.",
                                   "universal rule: a km figure the reader was not sure of is confirmed by a "
                                   "person, never accepted silently",
                                   _ev_cite(chosen), row_id=k["id"], evidence_id=chosen["id"]))
            continue
        t = same_day[0]
        used_trips.add(t["id"])
        printed = _dec(t["values"].get("km_printed"))
        verdicts[k["id"]] = ("no_evidence", t["id"])
        if not on("MILEAGE_DISCREPANCY"):
            continue
        if printed is None:
            why = (f"the km on the map page could not be read ({_where(t)}); the claim says {km} km. "
                   "Read it from the page and fix the value, or accept.")
        else:
            tried = "one-way"
            if t["values"].get("return_trip"):
                tried = f"one-way and return (2 × {printed} = {2 * printed})"
            elif km is not None and abs(km - 2 * printed) <= tol:
                tried = ("one-way; the claim is exactly double the map but the narrative does not "
                         "say 'and back'")
            why = (f"the claim says {km} km, the map at {_where(t)} prints {printed} km "
                   f"(tried: {tried}){_conf_note(t)}.")
        flags.append(_flag("MILEAGE_DISCREPANCY",
                           f"KM-tab row {k['row']} ({kv.get('date')}, {kv.get('from', '')} → "
                           f"{kv.get('to', '')}): {why}",
                           profile_mod.basis_for(profile, "km_tolerance",
                                                 f"km must equal the map (tolerance {tol} km), or exactly "
                                                 "double when the narrative says a return trip"),
                           _ev_cite(t), row_id=k["id"], evidence_id=t["id"]))
    for t in trips:
        if t["id"] not in used_trips and t["id"] not in matches:
            tv = t["values"]
            flags.append(_flag("MILEAGE_NO_MAP",
                               f"A map trip on {tv.get('date') or '?'} ({tv.get('purpose', '')}, "
                               f"{tv.get('km_printed') or '?'} km at {_where(t)}) matches no claimed trip. "
                               "Nothing to pay — noted.",
                               "universal rule: nothing uploaded vanishes silently",
                               _ev_cite(t), evidence_id=t["id"], status=INFO))

    for r in rows:
        verdicts.setdefault(r["id"], ("matched" if r["kind"] == "derived" else "unchecked", ""))
    return {"flags": flags, "verdicts": verdicts, "matches": matches, "notes": notes}


def needs_missing_reference(rows: list[dict], profile: dict) -> str:
    """The run-level control this employee's rows need and the profile
    lacks, as a sentence — or "" when nothing is missing."""
    if any(r["kind"] == "mileage" for r in rows) and not (profile.get("mileage_rates") or {}):
        return ("The batch has mileage rows but the client profile has no mileage rates, so the "
                "mileage-rate check could not run. Set the rates in Settings → Claims and "
                "re-verify, or acknowledge to proceed without that check.")
    return ""
