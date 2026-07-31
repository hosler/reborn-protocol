"""GS2 runtime values and coercion -- the ONE home for GS2's coercion policy.

GS2 is duck-typed: numbers (float), strings, arrays (Python lists, reference
semantics), and objects (GS2Object, case-insensitive member dict). `to_num` /
`to_bool` are shared with the sibling gs1.values module (same engine family,
same host); number->string formatting and the comparison rules are NOT --
they come from the reversed official interpreter and differ from GS1's.

Every GS2 coercion rule lives here so a new call site picks a named rule
instead of re-deriving one (`gs1.values`' module docstring tabulates the two
engines' rules side by side; they are deliberately different and must not be
unified):

  number -> string   fmt_num             float32 zero test, then "%.9f"
  string -> number   strtofloat          C strtod + true/false/0x; NO PARSE -> -1.0
  any -> number      gs2_to_num          switchTypeFloat: objects/arrays read 0.0
  truthiness         gs2_truthy          gs2_to_num(v) != 0.0, EXACT (no epsilon)
  number -> int      to_int32            truncate, then the int32 clamp
  number -> index    array_index         floor(gs2_to_num(v) + SCRIPT_EPSILON)
  compare tolerance  SCRIPT_EPSILON      1e-4, on EVERY relational op
  case folding       casefold / casecmp  ASCII-only (C strcasecmp)

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
resolve() collapses everything but Number and String (its guard
`1 < type + ~Array`, TScriptStackEntry.cpp:215, admits Null too -- asm
concurs), so only three kinds ever reach compare():

  NUMBER   Python float/int/bool, and None -- and GS2_NULL, see below
  STRING   Python str
  OBJECT   GS2Object and list (arrays ARE objects on the wire), always with
           a live pointer

GS2_NULL is the object entry whose pointer is nullptr -- what OP_TYPE_NULL
pushes (TScriptMachine.cpp:2605-2609). It is deliberately NOT `None`: None
means "the host has no value here / this variable was never set". BOTH occupy
the NUMBER cell in compare() -- an unbound name because makeProperty comes
back empty-handed, and a direct nullptr-object entry because it has no
backing property either (same terminal, TScriptStackEntry.cpp:228-229) --
but they stay distinct Python values because hosts test `arg is None` and
must never receive the sentinel (vm._denull).

Reference kinds pushed by the bytecode:
- VarRef(name): pushed by OP_TYPE_VAR; resolved against the scope chain
  (frame temps -> this -> globals -> host named objects) on deref.
- LValue(obj, key): pushed by OP_MEMBER_ACCESS; a live reference into an
  object's member slot (read via deref, written by OP_ASSIGN/OP_INC/...).
- ARRAY_START: stack marker pushed by OP_TYPE_ARRAY, consumed by
  OP_ARRAY_END / OP_FUNC_PARAMS_END / OP_CALL argument collection.
"""
from __future__ import annotations

import math
import re
import struct
from typing import Any, Dict, Iterator, Optional

from ..gs1.values import to_num, to_bool  # noqa: F401  (shared coercions)

#: The machine's universal float tolerance, DOUBLE_00402440 = 0.0001
#: (Preagonal/FourPlay/quattroplay/src/TInitStatics.cpp:1266). The same
#: constant is the array-index epsilon (array_index), the number->string
#: "print as 0" threshold (fmt_num) and the comparison tolerance
#: (compareNumberValues, TScriptMachine.cpp:36-43).
SCRIPT_EPSILON = 1e-4

#: array_index's epsilon, spelled separately because the VM re-exports it
#: under this name; it IS SCRIPT_EPSILON.
ARRAY_INDEX_EPSILON = SCRIPT_EPSILON

#: the official machine's own array-ALLOCATION cap. initArray (OP_ARRAY_NEW),
#: setArray (OP_SETARRAY) and expandArray (OP_ARRAY_NEW_MULTIDIM) all clamp
#: the requested size into [0, 10000] before allocating -- FourPlay's
#: decompiled interpreter, Preagonal/FourPlay/quattroplay/src/
#: TScriptMachine.cpp: setArray :1352 (clamp :1374-1375), initArray :1400
#: (clamp :1410), expandArray :1637 (clamp :1655-1656). `new[50000]`
#: therefore yields 10000 elements on the real client, not 50000.
MAX_ARRAY_SIZE = 0x2710


def array_index(value: Any) -> int:
    """``floor(v + 0.0001)``, NOT truncation -- floorScriptIndex,
    src/TScriptMachine.cpp:60-67.

    Used by every index-taking machine method: array cells,
    initArray/setArray/expandArray, insert/delete/replace, charAt,
    subString, subArray, foreach's index, OP_INT, OP_BWI, vecx/vecy.
    The epsilon matters: an index computed as ``(a/b)*c`` landing on
    2.99999999996 reads cell 3 on the real client, cell 2 under ``int()``.
    The entry reaching those methods was converted with switchTypeFloat
    first, hence gs2_to_num (a string index goes through strtofloat).
    """
    try:
        return math.floor(gs2_to_num(value) + ARRAY_INDEX_EPSILON)
    except (ValueError, OverflowError):  # NaN / inf
        return 0


def array_size(value: Any) -> int:
    """Official allocation-size conversion: array_index() clamped into
    [0, MAX_ARRAY_SIZE] exactly as initArray/setArray/expandArray do."""
    return max(0, min(array_index(value), MAX_ARRAY_SIZE))


def to_int32(value: Any) -> int:
    """C `static_cast<int32_t>(double)`: truncate toward zero, and yield
    INT_MIN when the value does not fit (what cvttsd2si does on x86-64, and
    what every bitwise opcode in the official machine relies on --
    src/TScriptMachine.cpp:3098-3111 casts both operands that way)."""
    try:
        n = math.trunc(gs2_to_num(value))
    except (ValueError, OverflowError):  # NaN / inf
        return -0x80000000
    if not -0x80000000 <= n < 0x80000000:
        return -0x80000000
    return n


def wrap_int32(n: int) -> int:
    """Truncate an int back into the int32 range, as the machine's bitwise
    results do (OP_BW_SHL overflowing past bit 31 wraps, it does not clamp)."""
    n &= 0xFFFFFFFF
    return n - 0x100000000 if n >= 0x80000000 else n


def _f32(x: float) -> float:
    """C `(float)x` -- demote a double to single precision. Overflow to
    infinity (what the hardware does) rather than raising."""
    try:
        return struct.unpack("<f", struct.pack("<f", x))[0]
    except (OverflowError, ValueError):
        return float("inf") if x > 0 else float("-inf")


def fmt_num(x: float) -> str:
    """Format a number the way the official machine prints it.

    TScriptStackEntry::switchTypeString (src/TScriptStackEntry.cpp:363-380)
    takes the |(float)value| < 0.0001 shortcut (:376) and emits the literal
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


#: C strtod's accepted prefix (decimal form): optional whitespace/sign, then
#: digits with optional fraction and exponent, or glibc's inf/infinity/nan
#: words. A trailing bare 'e' backtracks off, as strtod's endptr does.
_STRTOD_RE = re.compile(
    r"[ \t\n\v\f\r]*[+-]?"
    r"(?:\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?"
    r"|[iI][nN][fF](?:[iI][nN][iI][tT][yY])?|[nN][aA][nN])")

_HEX_RE = re.compile(r"[0-9a-fA-F]+")


def strtofloat(s: str) -> float:
    """The machine's ONE string->number rule, `strtofloat`
    (src/TInitStatics.cpp:4335-4386): the compare() string rows and
    switchTypeFloat (so every OP_CONV_TO_FLOAT on a string) both call it.

      empty            -> 0.0   (:4345)
      exactly "true"   -> 1.0   (:4348-4349; TString == is plain memcmp,
                                 src/TString.cpp:849-860, so case matters)
      exactly "false"  -> 0.0   (:4353-4354)
      leading "0x"     -> hex   (:4358-4375, strtoul base 16 of the rest)
      else             -> C strtod(); if strtod consumes NOTHING the result
                          is DOUBLE_004023f0 = **-1.0** (:4377-4380, constant
                          at :1269) -- NOT 0.0.

    The -1.0 row is the headline: `<number> == "<word>"` is FALSE on the
    real client for every word strtod rejects, because the word side lands
    on -1.0. Earlier waves cited this function as "non-numeric -> 0.0" and
    concluded `<unanswered name> == "anything"` is TRUE; that skipped the
    endptr check. (The reference rarely gets here in practice only because
    it pre-seeds its globals as strings, TInitStatics.cpp:4928-4937.)

    The declared `float` return type is a decompilation artifact: the asm
    returns strtod's untouched double in xmm0 (asm/TInitStatics/
    _Z10strtofloatRK7TString.s_decomped), so there is no f32 demotion here.
    """
    if not s:
        return 0.0
    if s == "true":
        return 1.0
    if s == "false":
        return 0.0
    if s[:2] in ("0x", "0X"):
        # "0X" actually falls to strtod in the reference (starts() is
        # case-sensitive), but C strtod hex-parses it to the same value.
        m = _HEX_RE.match(s, 2)
        return float(int(m.group(0), 16)) if m else 0.0
    m = _STRTOD_RE.match(s)
    if m is None:
        return -1.0
    try:
        return float(m.group(0))
    except (ValueError, OverflowError):
        return -1.0


def gs2_to_num(v: Any) -> float:
    """GS2's any->number conversion: TScriptStackEntry::switchTypeFloat
    (src/TScriptStackEntry.cpp:265-297). Strings go through strtofloat
    (:270-273); an entry with no backing property reads 0.0 (:288); and an
    OBJECT/ARRAY entry reads its var's never-written double slot, i.e. 0.0
    (TGraalVar::readFloat returns the double field for every non-link kind
    -- asm/TGraalVar/_ZN9TGraalVar9readFloatEv.s_decomped). `[1,2] + 1` is
    1.0 on the real client, not 3.0: do NOT "fix" the array row to len().

    This is deliberately not gs1.to_num (leading-prefix regex, arrays ->
    length): GS2 call sites must use this one.
    """
    if v is None or v is GS2_NULL:
        return 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        return strtofloat(v)
    return 0.0  # GS2Object, list, refs -- the object row above


def gs2_truthy(v: Any) -> bool:
    """GS2 condition truthiness. The branch opcodes test the (compiler-
    pre-converted) entry's doubleVal EXACTLY against 0.0 -- no epsilon:
    OP_IF src/TScriptMachine.cpp:2342, OP_AND :2351, OP_OR :2330,
    OP_SET_INDEX_TRUE :2321, OP_NOT :3155, OP_INLINE_CONDITIONAL :2705.
    Notable consequences of the strtofloat row: `if ("word")` is TRUE
    (-1.0), `if ("false")` and `if ("0")` are FALSE."""
    return gs2_to_num(v) != 0.0


class _GS2Null:
    """The null OBJECT entry -- type `Null` with a nullptr TGraalVar.

    This is exactly what OP_TYPE_NULL pushes (TScriptMachine.cpp:2605-2609:
    `type = Null; scriptProperty1 = nullptr`), i.e. the GS2 source keyword
    `null`. In compare() it RESOLVES to Number 0.0 exactly like an unset
    variable does (see values.resolve), so the two are interchangeable
    there; the singleton stays distinct from Python's None because the host
    boundary is not: hosts test `arg is None`, and `return null` must reach
    them as None (vm._denull), while inside the VM the sentinel still marks
    "the script said null" (e.g. OP_OBJ_LINK's no-object result).
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

    #: `script_vms` lists the GS2VMs this object serves as `this` for, enabling
    #: cross-script calls through object references. It is a list because a
    #: re-sent script reuses the previous VM's `this`, while a joined class
    #: shares its joiner's. Resolution walks newest-first so the freshest
    #: script wins and missing joined-class methods fall through to the joiner.
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

    def copy_from(self, source: Any) -> None:
        """TGraalVar::copyFrom (src/TGraalVar.cpp:2203-2371), reachable from
        script as `obj.copyfrom(o)` -- registered on the ROOT object class
        (src/TGraalVarProperties.cpp:278-285), so every object answers it.

        Semantics kept from the read body: a self-copy is a no-op (:2205),
        copying from null CLEARS the target (:2216-2221), array members are
        CLONED element-by-element (:2248-2257, via TGraalVar::clone) while
        object members stay shared references, and both the registered and
        the dynamic-var surfaces are walked (:2259-2356) -- our member dict
        is that union, so one pass over keys() covers both.

        Engine-backed subclasses are GATED OFF in the reference: the body
        early-returns for objects flagged engine-owned unless their class
        table opts in (:2208-2214; TProperties bool1, default false at
        src/TProperties.cpp:80). Exactly two classes opt in:
        TStaticVar (TStaticVarProperties.cpp:16) -- the plain script-object
        case this default implements -- and GuiControlProfile
        (gui/GuiControlProfileProperties.cpp:618). Host bridge classes for
        engine objects (GUI controls, players, levels) should override this
        with a pass to keep the reference's silent no-op."""
        if source is self:
            return
        if not isinstance(source, GS2Object):
            self.clear()
            return
        for key in list(source.keys()):
            self.set(key, copy_value(source.get(key)))

    def __len__(self) -> int:
        return len(self._members)

    def __repr__(self) -> str:
        label = self.name or "anon"
        return f"<GS2Object {label} {list(self._members.keys())[:8]}>"


def copy_value(value: Any) -> Any:
    """copyFrom's per-member value copy: arrays cloned recursively (each
    array cell is clone()d in the reference, TGraalVar.cpp:2248-2257),
    everything else -- numbers, strings, object REFERENCES -- as-is."""
    if isinstance(value, list):
        return [copy_value(v) for v in value]
    return value


_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def casefold(s: str) -> str:
    """GS2's case-folding policy: ASCII-ONLY lowering.

    Every case-insensitive string operation in the machine bottoms out in C
    `strcasecmp`/`strncasecmp` (TString::compareIgnoreCase src/TString.cpp:
    1001-1011, TString::strncasecmp :27-29), which folds bytes 'A'-'Z' and
    nothing else. Python's `str.casefold()` is NOT a substitute: it also folds
    non-ASCII and can even change length ('ss'.casefold() == 'ß'.casefold()),
    so a script comparing user/level text would take a branch the real client
    does not. Use this for every GS2 case-insensitive compare, prefix/suffix
    test and sort key."""
    return s.translate(_ASCII_LOWER)


def casecmp(a: str, b: str) -> int:
    """3-way strcasecmp over `casefold`ed operands."""
    la, lb = casefold(a), casefold(b)
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
#: docstring.
_NUMBER, _STRING, _OBJECT = 0, 1, 2


def _kind(v: Any) -> int:
    """Which resolved lattice cell `v` occupies. GS2_NULL deliberately falls
    through to NUMBER: resolve() has already collapsed it to 0.0."""
    if isinstance(v, str):
        return _STRING
    if isinstance(v, (GS2Object, list, tuple)):
        return _OBJECT
    return _NUMBER


def resolve(v: Any) -> Any:
    """TScriptStackEntry::resolve() over our value model.

    compare() resolves BOTH operands before its type switch
    (TScriptMachine.cpp:1435, :1441). resolve()'s guard admits every type
    except Number and String, so a Null entry is resolved too -- and a
    direct nullptr-object entry (the `null` keyword, or a null call result)
    has no backing property, so it terminates at "no property, no var:
    Number 0.0" (TScriptStackEntry.cpp:228-229; makeProperty's Null row
    keeps only the -- here null -- pointer, :250-253). The Array START
    marker collapses the same way (makeProperty zeroes both slots, :240-244).

    A LIVE object entry re-reads its var, which yields the var itself
    (kind >= 3 -> `Null` + readObject(), TScriptStackEntry.cpp:134-136;
    readObject returns `this` for every non-link var --
    asm/TGraalVar/_ZN9TGraalVar10readObjectEv.s_decomped), i.e. objects and
    arrays pass through unchanged -- as do numbers and strings.

    Everything Var/Unknown_5 covers is already collapsed upstream by
    vm.deref (missing bindings -> None -> the NUMBER cell, same terminal).
    """
    if v is GS2_NULL or v is ARRAY_START:
        return 0.0
    return v


def _obj_ptr(v: Any) -> int:
    """The entry's TGraalVar* as an integer.

    id() is CPython's object address, which is exactly the quantity the
    official machine compares (compareObjectPointers) and converts
    (objectPointerAsDouble, TScriptMachine.cpp:46-58). Post-resolve there is
    no nullptr-object value left in our lattice, so every pointer is live."""
    return id(v)


def gs2_compare(a: Any, b: Any) -> int:
    """3-way compare mirroring TScriptMachine::compare()
    (src/TScriptMachine.cpp:1430-1488), resolve() included. Every relational
    opcode uses it, plus OP_MIN/OP_MAX (:3286-3295) and OP_OBJ_COMPARE
    (:3352-3359).

    The whole table is a function of the two RESOLVED operands' lattice
    cells (see the module docstring); there are no per-value special cases:

      string/string -> strcasecmp                      (:1450)
      string/number -> compareNumberValues(strtofloat) (:1454, :1463)
      number/number -> compareNumberValues, 1e-4 tol   (:1467)
      object/object -> compare the two pointers        (:1478)
      object/string -> strcasecmp on the object's name (:1452, :1476)
      object/number -> compareNumberValues(pointer)    (:1465, :1480)

    The object/number row is load-bearing: findweapon(x) != null is OBJECT/OBJECT
    when found and NUMBER-0 vs nullptr when not. Routing objects through to_num()
    makes both 0.0 and silently kills Login's initServerlist().

    THE NULL-VS-STRING VERDICT. Rows :1452/:1476 carry a
    `scriptProperty1 != nullptr ? ... : 0` ternary -- read in isolation it
    says "a null object equals ANY string", and earlier waves recorded it
    that way. With resolve() applied first it is very nearly dead code:

    * the `null` keyword and a null call result resolve to Number 0.0
      (see resolve()), so `null == "5"` is FALSE (0.0 vs strtofloat "5")
      and `null == "word"` is also FALSE (0.0 vs -1.0);
    * a script variable can never hold a nullptr object: readObject
      returns the var itself for non-links, and `x = null` routes
      writeObject(nullptr) into TGraalVar::copyFrom(nullptr), which only
      CLEARS the var (asm/TGraalVar/_ZN9TGraalVar11writeObjectEPS_
      .s_decomped + .../_ZN9TGraalVar8copyFromEPS_.s_decomped) -- it reads
      back through the cleared/unset kinds as a plain value, not a null
      object.

    The one live source of a nullptr object at the switch is
    copyFromProperty's object-typed ENGINE-property row
    (TScriptStackEntry.cpp:76-81: an object property whose readObject
    returns nullptr). Our hosts surface those as None, which resolves to
    the NUMBER cell -- the same cell as a missing binding -- because the
    host boundary cannot type-distinguish "object-typed property, currently
    null" from "no such property". KEPT DIVERGENCE (test-locked): such a
    read compares 0.0 vs strtofloat(s) -- FALSE for any string strtod
does not read as zero -- where the reference's Null row would say equal
    to EVERY string. Confined to engine-property nulls, un-modelable
    without typed host misses, and it fails SAFE: the extra branches are
    NOT taken.

    One deliberate deviation: array/array is elementwise, not a pointer
    compare. Arrays really are objects here, so the official rule would make
    two arrays with equal contents unequal; our gs2_eq callers (OP_IN_OBJ /
    OP_OBJ_INDEX / OP_OBJ_REMOVESTRING) compare *elements* and want value
    semantics. Every other array row follows the object rules, so an array is
    never equal to null and never equal to a number.
    """
    a, b = resolve(a), resolve(b)
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
        if isinstance(other, str):
            return flip * casecmp(getattr(obj, "name", "") or "", other)
        return flip * _numcmp(float(_obj_ptr(obj)), gs2_to_num(other))
    if ka == _STRING and kb == _STRING:
        return casecmp(a, b)
    if ka == _STRING or kb == _STRING:
        s, num, flip = (a, b, 1) if ka == _STRING else (b, a, -1)
        return flip * _numcmp(strtofloat(s), gs2_to_num(num))
    return _numcmp(gs2_to_num(a), gs2_to_num(b))


def gs2_eq(a: Any, b: Any) -> bool:
    """GS2 OP_EQ semantics (compare() == 0)."""
    return gs2_compare(a, b) == 0
