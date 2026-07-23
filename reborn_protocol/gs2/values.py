"""GS2 runtime values and coercion.

GS2 is duck-typed: numbers (float), strings, arrays (Python lists, reference
semantics), and objects (GS2Object, case-insensitive member dict). Coercion
rules follow the sibling gs1.values module (same engine family, same host)
except where the GS2 compiler/GS2Engine dictate otherwise -- notably OP_EQ /
OP_NEQ receive *unconverted* operands (GS2CompilerVisitor.cpp emits no
conversion ops for Equal/NotEqual, unlike </>/etc which get OP_CONV_TO_FLOAT),
so equality must compare strings as strings.

Reference kinds pushed by the bytecode:
- VarRef(name): pushed by OP_TYPE_VAR; resolved against the scope chain
  (frame temps -> this -> globals -> host named objects) on deref.
- LValue(obj, key): pushed by OP_MEMBER_ACCESS; a live reference into an
  object's member slot (read via deref, written by OP_ASSIGN/OP_INC/...).
- ARRAY_START: stack marker pushed by OP_TYPE_ARRAY, consumed by
  OP_ARRAY_END / OP_FUNC_PARAMS_END / OP_CALL argument collection.
"""
from __future__ import annotations

from typing import Any, Dict, Iterator, Optional

from ..gs1.values import to_num, to_str, to_bool, fmt_num  # noqa: F401  (shared coercions)


class _ArrayStart:
    __slots__ = ()

    def __repr__(self) -> str:
        return "<ARRAY_START>"


ARRAY_START = _ArrayStart()


class VarRef:
    """An unresolved variable name (OP_TYPE_VAR)."""

    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name

    def __repr__(self) -> str:
        return f"VarRef({self.name!r})"


class LValue:
    """A member slot reference: obj.key (OP_MEMBER_ACCESS).

    obj may be None for a dead reference (member access on a non-object);
    reads yield None and writes are dropped.
    """

    __slots__ = ("obj", "key")

    def __init__(self, obj: Optional["GS2Object"], key: str):
        self.obj = obj
        self.key = key

    def get(self) -> Any:
        if self.obj is None:
            return None
        return self.obj.get(self.key)

    def set(self, value: Any) -> None:
        if self.obj is not None:
            self.obj.set(self.key, value)

    def __repr__(self) -> str:
        return f"LValue({self.obj!r}.{self.key})"


class ElemRef(LValue):
    """A list-element slot reference: arr[i] (OP_ARRAY on a list).

    Subclasses LValue so every VM site that already understands LValue
    (deref, _write_ref, OP_INC/OP_DEC, OP_CONV_TO_OBJECT, ...) transparently
    handles element references too -- `this.data[1]++` and `arr[i] += x`
    write back into the list instead of mutating a popped copy (GS2Engine
    array access yields a variable reference, not a value copy)."""

    __slots__ = ("arr", "idx")

    def __init__(self, arr: list, idx: int):
        super().__init__(None, f"[{idx}]")
        self.arr = arr
        self.idx = idx

    def get(self) -> Any:
        if 0 <= self.idx < len(self.arr):
            return self.arr[self.idx]
        return None

    def set(self, value: Any) -> None:
        if 0 <= self.idx < len(self.arr):
            self.arr[self.idx] = value

    def __repr__(self) -> str:
        return f"ElemRef([...][{self.idx}])"


class GS2Object:
    """A GS2 object: a case-insensitive member dict (mirrors GS2Engine's
    VariableCollection, which lowercases everything).

    Subclass and override get/set to bridge to host-side objects (players,
    NPCs, GUI controls) without the VM knowing the difference.
    """

    __slots__ = ("_members", "name")

    def __init__(self, name: str = ""):
        self._members: Dict[str, Any] = {}
        self.name = name

    def get(self, key: str) -> Any:
        return self._members.get(key.lower())

    def set(self, key: str, value: Any) -> None:
        self._members[key.lower()] = value

    def has(self, key: str) -> bool:
        return key.lower() in self._members

    def keys(self) -> Iterator[str]:
        return iter(self._members.keys())

    def clear(self) -> None:
        self._members.clear()

    def __len__(self) -> int:
        return len(self._members)

    def __repr__(self) -> str:
        label = self.name or "anon"
        return f"<GS2Object {label} {list(self._members.keys())[:8]}>"


def gs2_eq(a: Any, b: Any) -> bool:
    """GS2 OP_EQ semantics: operands arrive unconverted, so compare strings
    as strings (case-sensitive), arrays elementwise, everything else
    numerically (with epsilon, matching gs1._eq)."""
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return False
        return all(gs2_eq(x, y) for x, y in zip(a, b))
    if isinstance(a, str) or isinstance(b, str):
        # "5" == 5 is true (numeric strings compare by value); "abc" == "abc"
        # compares as text.
        sa, sb = to_str(a), to_str(b)
        if sa == sb:
            return True
        return _is_numeric(a) and _is_numeric(b) and abs(to_num(a) - to_num(b)) < 1e-9
    if isinstance(a, GS2Object) or isinstance(b, GS2Object):
        return a is b
    return abs(to_num(a) - to_num(b)) < 1e-9


def _is_numeric(v: Any) -> bool:
    if isinstance(v, (int, float, bool)) or v is None:
        return True
    if isinstance(v, str):
        try:
            float(v.strip())
            return True
        except ValueError:
            return False
    return False
