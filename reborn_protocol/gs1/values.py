"""GS1 value coercion.

GS1 is loosely typed: a value is a number (float), a string, or an array. The
C++ engine's GameValue can even hold a number AND a string simultaneously; we
model the common single-type case and coerce on demand. See memory:
gs1-python-port.
"""
from __future__ import annotations

import re

_LEADING_NUM = re.compile(r"\s*[-+]?(?:\d+\.?\d*|\.\d+)")


def to_num(v) -> float:
    """Coerce a value to a number. Strings yield their leading numeric prefix."""
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = _LEADING_NUM.match(v)
        if not m:
            return 0.0
        try:
            return float(m.group(0))
        except ValueError:
            return 0.0
    if isinstance(v, (list, tuple)):
        return float(len(v))
    return 0.0


def fmt_num(x: float) -> str:
    """Format a number the way GS1 prints it (integers without a decimal).

    GServer-v2 formats non-integers with `std::format("{}", value)`, which
    (like Python's `str`/`repr` for float, since 3.1) produces the shortest
    decimal that round-trips back to the same double -- NOT a fixed 6-decimal
    truncation (e.g. #v(2/3) is "0.6666666666666666", not "0.666667").
    """
    if x != x:  # NaN
        return "0"
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))
    return repr(x)


def to_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return fmt_num(float(v))
    if isinstance(v, (list, tuple)):
        return ",".join(to_str(x) for x in v)
    return str(v)


def to_bool(v) -> bool:
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        # a string is truthy if it is non-empty; a numeric string by its value
        m = _LEADING_NUM.fullmatch(v.strip()) if v else None
        if m:
            return to_num(v) != 0
        return v != ""
    if isinstance(v, (list, tuple)):
        return len(v) > 0
    return bool(v)


# ---------------------------------------------------------------------------
# GS1-only coercions.
#
# `to_num`/`to_bool` above are SHARED with reborn_protocol.gs2 (a different
# language with its own, C/JS-like truthiness/coercion rules) -- do not
# change their semantics here. The functions below implement GS1's own
# GameValue coercion rules (ScriptContainers.h GameValue::getCopy<T>,
# GS1Visitor.cpp condition/operator evaluation) and are used ONLY by
# reborn_protocol.gs1.interp for GS1 script evaluation.
# ---------------------------------------------------------------------------

# CommonTypes.h DoubleIsZero: std::abs(value) < std::numeric_limits<double>::epsilon()
_DOUBLE_EPS = 2.220446049250313e-16


def is_double_zero(x: float) -> bool:
    """GServer-v2's DoubleIsZero: used by GS1's unary `!` (NOT a flag test --
    `!` always converts its operand to a number and tests it near zero, even
    though `if(number)` itself is never truthy; this is intentionally not
    De Morgan-consistent with `if`/`&&`/`||`)."""
    return abs(x) < _DOUBLE_EPS


def gs1_num(v) -> float:
    """GS1 arithmetic/relational coercion (GameValue::getCopy<double>): a
    bool is 1.0/0.0, a number is itself, but a STRING NEVER numeric-parses --
    getCopy<double> only inspects m_number/m_boolean, never m_text. So a
    stored string flag always coerces to 0.0 in arithmetic (`3 + this.s`
    where this.s holds "25" is 3, not 28) even though `strtofloat()`/`to_num`
    explicitly parse the same text on request. Bare numeric LITERAL tokens
    never hit this function at all -- the parser already turns them into a
    real float (ast.Num) at parse time, so they're unaffected."""
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, (list, tuple)):
        return float(len(v))
    return 0.0


def gs1_truthy(v) -> bool:
    """GS1 'flag'/condition truthiness (GameValue::getCopy<bool>, used by
    `if`/`while`/`?:`/`&&`/`||`): true only for an actual boolean True (a
    comparison result, or the `true` literal) or a non-empty string. Numbers
    are NEVER truthy here -- `if (5)` is false, `5 && 2` is false -- which is
    why this is a separate function from `gs1_num`'s arithmetic coercion and
    from the unary-`!`-specific `is_double_zero`."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v != ""
    return False
