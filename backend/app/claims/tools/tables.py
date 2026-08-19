"""`compare_tables`: deterministic join / group / diff / sum over small tables.

A table is a list of row dicts (as read_cells and inspect_workbook return
them). Operations are named, bounded (MAX_TABLE_ROWS in, out) and use
Decimal for every numeric column, so a reconciliation the agent asks for
replays exactly. Spec shape:

  {"op": "sum",   "table": [...], "column": "amount"}
  {"op": "group", "table": [...], "by": ["vendor"], "sum": "amount"}
  {"op": "join",  "left": [...], "right": [...], "on": ["date", "amount"],
                  "how": "inner|left|outer"}
  {"op": "diff",  "left": [...], "right": [...], "on": ["date", "amount"]}
       → {"only_left": [...], "only_right": [...], "both": n}
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .contracts import MAX_TABLE_ROWS


class TableError(ValueError):
    pass


def _dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        d = Decimal(str(v).replace(",", "").strip())
        return d if d.is_finite() else None
    except InvalidOperation:
        return None


def _norm(v) -> str:
    """One comparable value: numbers by exact Decimal value, text folded."""
    if v is None:
        return ""
    d = _dec(v)
    if d is not None:
        return str(d.normalize())
    return " ".join(str(v).lower().split())


def _key(row: dict, cols: list[str]) -> tuple:
    return tuple(_norm(row.get(c)) for c in cols)


def _table(spec: dict, name: str) -> list[dict]:
    t = spec.get(name)
    if not isinstance(t, list) or not all(isinstance(r, dict) for r in t):
        raise TableError(f"{name} must be a list of row objects")
    if len(t) > MAX_TABLE_ROWS:
        raise TableError(f"{name} has {len(t)} rows — more than {MAX_TABLE_ROWS}")
    return t


def compare_tables(spec: dict[str, Any]) -> dict[str, Any]:
    op = spec.get("op")
    if op == "sum":
        table, col = _table(spec, "table"), spec.get("column")
        if not col:
            raise TableError("sum needs a column")
        vals = [_dec(r.get(col)) for r in table]
        total = sum((v for v in vals if v is not None), Decimal("0"))
        return {"sum": str(total), "counted": sum(1 for v in vals if v is not None),
                "skipped_non_numeric": sum(1 for v in vals if v is None)}
    if op == "group":
        table, by, sum_col = _table(spec, "table"), spec.get("by") or [], spec.get("sum")
        if not by:
            raise TableError("group needs 'by' columns")
        groups: dict[tuple, dict] = {}
        for r in table:
            k = _key(r, by)
            g = groups.setdefault(k, {"key": {c: r.get(c) for c in by}, "count": 0, "sum": Decimal("0")})
            g["count"] += 1
            if sum_col:
                d = _dec(r.get(sum_col))
                if d is not None:
                    g["sum"] += d
        out = [{"key": g["key"], "count": g["count"], **({"sum": str(g["sum"])} if sum_col else {})}
               for g in groups.values()]
        return {"groups": out[:MAX_TABLE_ROWS], "truncated": len(out) > MAX_TABLE_ROWS}
    if op in ("join", "diff"):
        left, right, on = _table(spec, "left"), _table(spec, "right"), spec.get("on") or []
        if not on:
            raise TableError(f"{op} needs 'on' columns")
        rk: dict[tuple, list[dict]] = {}
        for r in right:
            rk.setdefault(_key(r, on), []).append(r)
        if op == "diff":
            lk = {_key(r, on) for r in left}
            only_left = [r for r in left if _key(r, on) not in rk]
            only_right = [r for r in right if _key(r, on) not in lk]
            return {"only_left": only_left[:MAX_TABLE_ROWS], "only_right": only_right[:MAX_TABLE_ROWS],
                    "both": sum(1 for r in left if _key(r, on) in rk),
                    "truncated": len(only_left) > MAX_TABLE_ROWS or len(only_right) > MAX_TABLE_ROWS}
        how = spec.get("how", "inner")
        rows: list[dict] = []
        matched_right: set[int] = set()
        for r in left:
            hits = rk.get(_key(r, on), [])
            if hits:
                for h in hits:
                    matched_right.add(id(h))
                    rows.append({**{f"left.{k}": v for k, v in r.items()}, **{f"right.{k}": v for k, v in h.items()}})
            elif how in ("left", "outer"):
                rows.append({f"left.{k}": v for k, v in r.items()})
            if len(rows) > MAX_TABLE_ROWS:
                break
        if how == "outer":
            for r in right:
                if id(r) not in matched_right:
                    rows.append({f"right.{k}": v for k, v in r.items()})
        return {"rows": rows[:MAX_TABLE_ROWS], "truncated": len(rows) > MAX_TABLE_ROWS}
    raise TableError(f"unknown op {op!r} (sum, group, join, diff)")
