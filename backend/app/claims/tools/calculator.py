"""`calculate`: exact Decimal arithmetic over a tiny expression grammar.

Numbers, + - * / and parentheses, unary minus, and the functions sum, abs,
min, max, round(x, places). Parsed with Python's ast and walked against an
ALLOWLIST — no names, no attributes, no calls but the four, no eval — and
capped at MAX_CALC_OPS operations. Every number is a Decimal from its
literal text, so 0.1 + 0.2 is 0.3 and money reconciles to the cent.
"""
from __future__ import annotations

import ast
import re
from decimal import Decimal, DecimalException, InvalidOperation, localcontext

from .contracts import MAX_CALC_OPS

MAX_EXPRESSION_CHARS = 2000


class CalculationError(ValueError):
    pass


def calculate(expression: str, places: int | None = None) -> Decimal:
    """Evaluate the expression exactly. places quantizes the final value."""
    if not isinstance(expression, str) or not expression.strip():
        raise CalculationError("empty expression")
    if len(expression) > MAX_EXPRESSION_CHARS:
        raise CalculationError(f"expression longer than {MAX_EXPRESSION_CHARS} characters")
    try:
        tree = ast.parse(_quote_numbers(expression.strip()), mode="eval")
    except SyntaxError as exc:
        raise CalculationError(f"cannot parse: {exc.msg}") from exc
    counter = {"ops": 0}
    with localcontext() as ctx:
        ctx.prec = 34
        # Decimal signals its own failures (Overflow on 1e999999 * 1e999999,
        # Underflow, InvalidOperation on quantizing a huge value) as
        # DecimalException. They are BAD INPUT — the model wrote an
        # expression the arithmetic cannot answer — not a broken tool, so
        # they become CalculationError (BAD_INPUT) instead of reaching the
        # harness's catch-all as TOOL_FAILED.
        try:
            value = _eval(tree.body, counter)
            if not isinstance(value, Decimal):
                raise CalculationError("the expression is not a number")
            if not value.is_finite():
                raise CalculationError("the result is not finite")
            if places is not None:
                value = value.quantize(Decimal(1).scaleb(-int(places)))
        except CalculationError:
            raise
        except DecimalException as exc:
            raise CalculationError(f"the arithmetic does not have an answer: {type(exc).__name__}") from exc
    return value


# Every number literal becomes a string literal before parsing, so 24.00
# reaches Decimal as the text "24.00" (a float would make it 24.0 and lose
# the cents' exponent — 24.0 + 0.1 + 0.2 is 24.3, not 24.30).
_NUMBER = re.compile(r"(?<![\w.'\"])(\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?(?![\w.'\"])")


def _quote_numbers(text: str) -> str:
    return _NUMBER.sub(lambda m: f"'{m.group(0)}'", text)


def _tick(counter: dict) -> None:
    counter["ops"] += 1
    if counter["ops"] > MAX_CALC_OPS:
        raise CalculationError(f"more than {MAX_CALC_OPS} operations")


def _eval(node, counter):
    _tick(counter)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float, str)):
            raise CalculationError("only numbers are allowed")
        try:
            return Decimal(str(node.value)) if not isinstance(node.value, str) else Decimal(node.value.strip())
        except InvalidOperation as exc:
            raise CalculationError(f"not a number: {node.value!r}") from exc
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _eval(node.operand, counter)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp):
        a, b = _eval(node.left, counter), _eval(node.right, counter)
        if isinstance(node.op, ast.Add):
            return a + b
        if isinstance(node.op, ast.Sub):
            return a - b
        if isinstance(node.op, ast.Mult):
            return a * b
        if isinstance(node.op, ast.Div):
            if b == 0:
                raise CalculationError("division by zero")
            return a / b
        raise CalculationError(f"operator {type(node.op).__name__} is not allowed")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise CalculationError("only sum, abs, min, max and round(x, places) may be called")
        name = node.func.id
        args = []
        for a in node.args:
            if isinstance(a, (ast.List, ast.Tuple)):
                args.extend(_eval(e, counter) for e in a.elts)
            else:
                args.append(_eval(a, counter))
        if name == "sum":
            return sum(args, Decimal("0"))
        if name == "abs" and len(args) == 1:
            return abs(args[0])
        if name == "min" and args:
            return min(args)
        if name == "max" and args:
            return max(args)
        if name == "round" and 1 <= len(args) <= 2:
            places = int(args[1]) if len(args) == 2 else 2
            if not 0 <= places <= 10:
                raise CalculationError("round places must be 0..10")
            return args[0].quantize(Decimal(1).scaleb(-places))
        raise CalculationError(f"function {name!r} is not allowed")
    if isinstance(node, (ast.List, ast.Tuple)):
        raise CalculationError("a bare list is not a number — use sum([...])")
    raise CalculationError(f"{type(node).__name__} is not allowed in a calculation")
