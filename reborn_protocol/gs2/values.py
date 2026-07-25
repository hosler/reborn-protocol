"""GS2 runtime values and coercion.

GS2 is duck-typed: numbers (float), strings, arrays (Python lists, reference
semantics), and objects (GS2Object, case-insensitive member dict). `to_num` /
`to_bool` are shared with the sibling gs1.values module (same engine family,
same host); number->string formatting and the comparison rules are NOT --
they come from the reversed official interpreter and differ from GS1's.

Comparison: every relational opcode (OP_EQ/NEQ/LT/GT/LTE/GTE, and also
OP_MIN/OP_MAX/OP_OBJ_COMPARE) funnels through one 3-way TScriptMachine::
compare(), reproduced here as gs2_compare(). Operands arrive *unconverted*
for those ops (GS2CompilerVisitor.cpp emits no conversion ops for them,
unlike the arithmetic ops which get OP_CONV_TO_FLOAT), so the type-pair
table matters.

The official value lattice (TScriptStackEntry.h:12-24) is
Array/Number/String/Null/Var/Unknown_5. Array is a stack *marker*, Var and
Unknown_5 are unresolved references that TScriptStackEntry::resolve() collapses
before any comparison, and `Null` is a misnomer from the decompilation: it is
the OBJECT type, an entry holding a `TGraalVar*` that may or may not be
nullptr (TScriptStackEntry::switchTypeObject, TScriptStackEntry.cpp:299-353).
So after resolve() only three kinds ever reach compare():

  NUMBER   Python float/int/bool, and None
  STRING   Python str
  OBJECT   GS2Object, list (arrays ARE objects on the wire), and GS2_NULL

GS2_NULL is the object entry whose pointer is nullptr -- what OP_TYPE_NULL
pushes (TScriptMachine.cpp:2605-2609). It is deliberately NOT `None`: None
means "the host has no value here / this variable was never set", which the
official machine resolves to Number 0.0 (resolve() -> TScriptStackEntry.cpp:
228-229). Conflating the two is what broke the Login server: see gs2_compare.

Reference kinds pushed by the bytecode:
- VarRef(name): pushed by OP_TYPE_VAR; resolved against the scope chain
  (frame temps -> this -> globals -> host named objects) on deref.
- LValue(obj, key): pushed by OP_MEMBER_ACCESS; a live reference into an
  object's member slot (read via deref, written by OP_ASSIGN/OP_INC/...).
- ARRAY_START: stack marker pushed by OP_TYPE_ARRAY, consumed by
  OP_ARRAY_END / OP_FUNC_PARAMS_END / OP_CALL argument collection.
"""
from __future__ import annotations

import struct
from typing import Any, Dict, Iterator, Optional

from ..gs1.values import to_num, to_bool  # noqa: F401  (shared coercions)

#: The machine's universal float tolerance, DOUBLE_00402440 = 0.0001
#: (Preagonal/FourPlay/quattroplay/src/TInitStatics.cpp:1266). The same
#: constant is the array-index epsilon (vm.array_index), the number->string
#: "print as 0" threshold (fmt_num) and the comparison tolerance
#: (compareNumberValues, TScriptMachine.cpp:36-43).
SCRIPT_EPSILON = 1e-4


def _f32(x: float) -> float:
    """C `(float)x` -- demote a double to single precision. Overflow to
    infinity (what the hardware does) rather than raising."""
    try:
        return struct.unpack("<f", struct.pack("<f", x))[0]
    except (OverflowError, ValueError):
        return float("inf") if x > 0 else float("-inf")


def fmt_num(x: float) -> str:
    """Format a number the way the official machine prints it.

    TScriptStackEntry::switchTypeString (quattroplay asm/TScriptStackEntry/
    _ZN17TScriptStackEntry16switchTypeStringEP14TScriptMachineb.s_decomped:
    37-58) takes the |(float)value| < 0.0001 shortcut and emits the literal
    string "0"; otherwise it hands the *double* to TString::operator<<(double)
    -> TString::adddouble(v, width=0, precision=-1, ...) (src/TString.cpp:
    1497-1501 and 1139-1174), which is `snprintf("%.9f")` with trailing '0's
    and then a trailing '.' stripped.

    So GS2 prints at most 9 decimals, NOT the shortest round-tripping repr
    GS1/GServer uses: 2/3 is "0.666666667" here and "0.6666666666666666" in
    GS1. Do not "unify" the two.
    """
    f = _f32(x)
    # NaN fails both comparisons and falls through to snprintf, as in C.
    if -SCRIPT_EPSILON < f < SCRIPT_EPSILON:
        return "0"
    s = "%.9f" % x
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


class _GS2Null:
    """The null OBJECT entry -- type `Null` with a nullptr TGraalVar.

    This is exactly what OP_TYPE_NULL pushes (TScriptMachine.cpp:2605-2609:
    `type = Null; scriptProperty1 = nullptr`), i.e. the GS2 source keyword
    `null`. It is a distinct singleton and NOT Python's None because the two
    resolve to different lattice cells:

      null      -> OBJECT with pointer 0
      unset var -> NUMBER 0.0 (TScriptStackEntry::resolve, .cpp:228-229)

    They agree against numbers (a nullptr formats as 0.0 through
    objectPointerAsDouble) and disagree against strings and objects, which is
    where the interesting branches live.

    Falsy, numerically zero, stringifies empty -- so every non-comparison use
    behaves like the None it replaces.
    """

    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "<gs2 null>"


#: singleton pushed by OP_TYPE_NULL; see _GS2Null.
GS2_NULL = _GS2Null()


def to_str(v) -> str:
    """GS2 stringification (OP_CONV_TO_STRING / OP_JOIN / format %s)."""
    if v is None or v is GS2_NULL:
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
    reads yield None and writes are dropped. obj may also be a plain list
    (member access on an array): reads/writes stay dead, but the base is
    retained so method calls (arr.addarray(...), arr.sortbyvalue(...)) can
    dispatch against the array instead of silently missing.
    """

    __slots__ = ("obj", "key")

    def __init__(self, obj: Optional[Any], key: str):
        self.obj = obj
        self.key = key

    def get(self) -> Any:
        if isinstance(self.obj, GS2Object):
            return self.obj.get(self.key)
        return None

    def set(self, value: Any) -> None:
        if isinstance(self.obj, GS2Object):
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

    #: `script_vms` lists the GS2VMs this object serves as `this` for, oldest
    #: first (maintained by GS2VM.this's setter). It is what makes
    #: cross-script method calls work: a script that publishes itself with
    #: `plfunc = this;` is reached by others as `plfunc.SomePublicFunction()`,
    #: and the VM resolves that through this back-reference. Empty on every
    #: other object.
    #:
    #: It is a LIST because one this-object legitimately serves several VMs:
    #: a re-sent script reuses the previous VM's this (to keep state), and a
    #: joined class instance shares its joiner's. Resolution walks it newest
    #: first, so the freshest script wins and a joined class that does not
    #: declare the name falls through to the script that joined it.
    __slots__ = ("_members", "name", "script_vms")

    def __init__(self, name: str = ""):
        self._members: Dict[str, Any] = {}
        self.name = name
        self.script_vms: list = []

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


_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def _casecmp(a: str, b: str) -> int:
    """TString::compareIgnoreCase == strcasecmp (src/TString.cpp:1001-1011):
    byte-wise compare after ASCII-only case folding (NOT str.casefold(),
    which also folds non-ASCII)."""
    la, lb = a.translate(_ASCII_LOWER), b.translate(_ASCII_LOWER)
    return -1 if la < lb else (1 if la > lb else 0)


def _numcmp(left: float, right: float) -> int:
    """compareNumberValues (TScriptMachine.cpp:36-43): equality (and
    ordering) carries a 0.0001 tolerance, so 0.99999 < 1.0 is FALSE."""
    if right > left + SCRIPT_EPSILON:
        return -1
    if left > right + SCRIPT_EPSILON:
        return 1
    return 0


#: lattice cells that survive TScriptStackEntry::resolve() -- see the module
#: docstring. Ordered so the compare table below reads like the C++ switch.
_NUMBER, _STRING, _OBJECT = 0, 1, 2


def _kind(v: Any) -> int:
    """Which resolved lattice cell `v` occupies."""
    if isinstance(v, str):
        return _STRING
    if isinstance(v, (GS2Object, list, tuple)) or v is GS2_NULL:
        return _OBJECT
    return _NUMBER


def _obj_ptr(v: Any) -> int:
    """The entry's TGraalVar* as an integer.

    id() is CPython's object address, which is exactly the quantity the
    official machine compares (compareObjectPointers) and converts
    (objectPointerAsDouble, TScriptMachine.cpp:46-58). GS2_NULL is the
    nullptr, so it is the one object with pointer 0 -- which is why it and
    only it compares equal to the number 0."""
    return 0 if v is GS2_NULL else id(v)


def gs2_compare(a: Any, b: Any) -> int:
    """3-way compare mirroring TScriptMachine::compare()
    (src/TScriptMachine.cpp:1430-1488). Every relational opcode uses it, plus
    OP_MIN/OP_MAX (:3286-3295) and OP_OBJ_COMPARE (:3352-3359).

    The whole table is a function of the two operands' lattice cells (see the
    module docstring); there are no per-value special cases:

      string/string -> strcasecmp                      (:1450)
      string/number -> compareNumberValues(strtofloat) (:1454, :1463)
      number/number -> compareNumberValues, 1e-4 tol   (:1467)
      object/object -> compare the two pointers        (:1478)
      object/string -> strcasecmp on the object's name, or 0 when the object
                       pointer is nullptr              (:1452, :1476)
      object/number -> compareNumberValues(pointer)    (:1465, :1480)

    The object/number row is the one that matters most and the one we used to
    get wrong. `findweapon(x) != null` is OBJECT vs OBJECT when the weapon
    exists (two different pointers -> unequal, the branch is taken) and
    NUMBER-0 vs OBJECT-nullptr when it does not (both 0.0 -> equal). A version
    of this function that ran objects through to_num() saw 0.0 on both sides
    and reported a found weapon EQUAL to null, so Login's -Rescripted/IRC/
    Login3 skipped initServerlist() and built no GUI at all, with no error.

    One deliberate deviation: array/array is elementwise, not a pointer
    compare. Arrays really are objects here, so the official rule would make
    two arrays with equal contents unequal; our gs2_eq callers (OP_IN_OBJ /
    OP_OBJ_INDEX / OP_OBJ_REMOVESTRING) compare *elements* and want value
    semantics. Every other array row follows the object rules, so an array is
    never equal to null and never equal to a number.
    """
    ka, kb = _kind(a), _kind(b)
    if ka == _OBJECT and kb == _OBJECT:
        if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
            if len(a) != len(b):
                return -1 if len(a) < len(b) else 1
            for x, y in zip(a, b):
                c = gs2_compare(x, y)
                if c:
                    return c
            return 0
        pa, pb = _obj_ptr(a), _obj_ptr(b)
        return 0 if pa == pb else (-1 if pa < pb else 1)
    if ka == _OBJECT or kb == _OBJECT:
        obj, other, flip = (a, b, 1) if ka == _OBJECT else (b, a, -1)
        ptr = _obj_ptr(obj)
        if isinstance(other, str):
            # a nullptr object has no name to compare, and the official code
            # returns 0 rather than dereferencing it -- `null == "anything"`
            if ptr == 0:
                return 0
            return flip * _casecmp(getattr(obj, "name", "") or "", other)
        return flip * _numcmp(float(ptr), to_num(other))
    if ka == _STRING and kb == _STRING:
        return _casecmp(a, b)
    return _numcmp(to_num(a), to_num(b))


def gs2_eq(a: Any, b: Any) -> bool:
    """GS2 OP_EQ semantics (compare() == 0)."""
    return gs2_compare(a, b) == 0
