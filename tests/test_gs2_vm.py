"""GS2 VM semantics tests.

Fixtures under tests/fixtures/gs2/vm/ are GS2 sources compiled with the
gs2parser's own gs2test compiler (the exact compiler GServer uses), so the
VM is tested against real production bytecode, not hand-assembled streams.
Each .gs2 file sits next to its compiled .gs2bc; assertions encode the
semantics the source implies.

Also includes the corpus smoke-run: every baseline .bytecode must load and
execute (toplevel + every function, argless) without any exception escaping
the VM -- the "zero VM crashes" guarantee.
"""
from __future__ import annotations

import glob
import math
import os

import pytest

from reborn_protocol.gs2 import GS2VM, GS2Object, printf_format, gs2_eq, to_str
from reborn_protocol.gs2.container import GS2Container
from reborn_protocol.gs2.values import GS2_NULL, LValue, VarRef, fmt_num, gs2_compare
from reborn_protocol.gs2.vm import _Frame

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "gs2", "vm")

BASELINES_ROOT = os.path.join(
    os.path.dirname(__file__), "..", "..", "GServer-v2", "build", "dependencies",
    "fc", "gs2parser-src", "tests", "baselines",
)
# Vendored subset of the same corpus (tests/fixtures/gs2_baselines/) so this
# suite has real baseline coverage even in checkouts without GServer-v2 built
# alongside it (e.g. CI).
VENDORED_BASELINES_ROOT = os.path.join(os.path.dirname(__file__), "fixtures", "gs2_baselines")
BASELINE_FILES = sorted(
    glob.glob(os.path.join(BASELINES_ROOT, "**", "*.bytecode"), recursive=True)
    + glob.glob(os.path.join(VENDORED_BASELINES_ROOT, "**", "*.bytecode"), recursive=True)
)


def test_with_assignment_only_writes_object_that_has_name():
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    scope = GS2Object(name="scope")
    frame.with_stack.append(scope)

    vm._assign_name("localOnly", 3, frame)
    assert not scope.has("localOnly")
    assert vm.globals["localonly"] == 3

    scope.set("existing", 1)
    vm._assign_name("existing", 4, frame)
    assert scope.get("existing") == 4


def test_conv_to_object_preserves_unset_array_targets():
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    obj = GS2Object(name="target")

    member = LValue(obj, "rows")
    frame.stack.append(member)
    vm._op_conv_to_object(frame, None)
    assert frame.stack.pop() is member
    frame.stack.extend([member, 3])
    vm._op_setarray(frame, None)
    assert obj.get("rows") == [0.0, 0.0, 0.0]

    global_ref = VarRef("rows")
    frame.stack.append(global_ref)
    vm._op_conv_to_object(frame, None)
    assert frame.stack.pop() is global_ref
    frame.stack.extend([global_ref, 2])
    vm._op_setarray(frame, None)
    assert vm.globals["rows"] == [0.0, 0.0]

    obj.set("rows", [1])
    frame.stack.append(member)
    vm._op_conv_to_object(frame, None)
    assert frame.stack.pop() == [1]


def _baseline_id(path: str) -> str:
    """Return the relative ID for a baseline path under either root."""
    for root in (BASELINES_ROOT, VENDORED_BASELINES_ROOT):
        if os.path.commonpath([os.path.abspath(path), os.path.abspath(root)]) == os.path.abspath(root):
            return os.path.relpath(path, root)
    return path


def load(name: str) -> GS2VM:
    path = os.path.join(FIXTURES, name + ".gs2bc")
    with open(path, "rb") as fh:
        return GS2VM(fh.read(), name=name)


@pytest.fixture(scope="module")
def arith():
    return load("01_arith")


@pytest.fixture(scope="module")
def strings():
    return load("02_strings")


@pytest.fixture(scope="module")
def control():
    return load("03_control")


@pytest.fixture(scope="module")
def arrays():
    return load("04_arrays")


@pytest.fixture(scope="module")
def functions():
    return load("05_functions")


@pytest.fixture(scope="module")
def objects():
    return load("06_objects")


# ---------------------------------------------------------------- arithmetic

def test_add(arith):
    assert arith.call("addNums", 2, 3) == 5


def test_string_args_coerce(arith):
    assert arith.call("addNums", "5", "7") == 12


def test_precedence(arith):
    assert arith.call("precedence") == 12  # 2 + 12 - 2


def test_modulo(arith):
    assert arith.call("modulo", 7, 3) == 1


def test_power_caret(arith):
    # '^' is pow in GS2 (compiler maps ExpressionOp::Pow -> OP_POW)
    assert arith.call("power") == 1024


def test_pow_builtin(arith):
    assert arith.call("powBuiltin", 3, 4) == 81


def test_unary(arith):
    # -5 + abs(-3) + int(2.9) = -5 + 3 + 2
    assert arith.call("unary", 5) == 0


def test_min_max(arith):
    assert arith.call("minMax", 3, 9) == 309


def test_div_by_zero_is_zero(arith):
    assert arith.call("divByZero", 5) == 0


def test_bitwise(arith):
    assert arith.call("bitwise") == (12 & 10) + (12 | 3) + (1 << 4)


# ------------------------------------------------------------------- strings

def test_concat(strings):
    assert strings.call("concat", "a", "b") == "a-b"


def test_length(strings):
    assert strings.call("upperLen", "hello") == 5


def test_substring(strings):
    assert strings.call("subStr", "abcdef") == "cde"


def test_substring_negative_len(strings):
    assert strings.call("subToEnd", "abcdef") == "cdef"


def test_pos(strings):
    assert strings.call("findPos", "hello", "ll") == 2
    assert strings.call("findPos", "hello", "zz") == -1


def test_starts_ends(strings):
    assert strings.call("startsEnds", "hello") == 2
    assert strings.call("startsEnds", "yellow") == 0


def test_trim(strings):
    assert strings.call("trimIt", "  hi  ") == "hi"


def test_charat(strings):
    assert strings.call("charAt", "abc", 1) == "b"


def test_tokenize(strings):
    assert strings.call("tokenCount", "a,b,c") == 3
    assert strings.call("secondToken", "a,b,c") == "b"


def test_format(strings):
    assert strings.call("fmt", 5, "yo") == "x=5 y=yo"


def test_char(strings):
    assert strings.call("charOf", 65) == "A"


def test_number_to_string(strings):
    assert strings.call("numToStr", 5) == "5"
    assert strings.call("numToStr", 2.5) == "2.5"


# ------------------------------------------------------------------- control

def test_if_else(control):
    assert control.call("ifElse", 20) == "big"
    assert control.call("ifElse", 7) == "mid"
    assert control.call("ifElse", 1) == "small"


def test_while(control):
    assert control.call("whileSum", 5) == 15


def test_for(control):
    assert control.call("forSum", 5) == 10  # 0+1+2+3+4


def test_break_continue(control):
    assert control.call("breakContinue", 10) == 8  # 0+1+3+4


def test_switch(control):
    assert control.call("switchTest", 1) == "one"
    assert control.call("switchTest", 2) == "few"
    assert control.call("switchTest", 3) == "few"
    assert control.call("switchTest", 9) == "many"


def test_ternary(control):
    assert control.call("ternary", 3) == "pos"
    assert control.call("ternary", -1) == "nonpos"


def test_logical_statement(control):
    assert control.call("logicAnd", 1, 1) == 1
    assert control.call("logicAnd", 1, 0) == 0
    assert control.call("logicOr", 0, 1) == 1
    assert control.call("logicOr", 0, 0) == 0


def test_logical_inline(control):
    assert bool(control.call("logicInline", 1, 1)) is True
    assert bool(control.call("logicInline", 1, 0)) is False
    assert bool(control.call("logicInline", 0, 1)) is False


def test_not(control):
    assert bool(control.call("notTest", 0)) is True
    assert bool(control.call("notTest", 5)) is False


def test_in_range(control):
    assert control.call("inRange", 5) == 1
    assert control.call("inRange", 3) == 1  # inclusive bounds
    assert control.call("inRange", 7) == 1
    assert control.call("inRange", 9) == 0


def test_nested_loops(control):
    assert control.call("nestedLoops", 3) == 9


# -------------------------------------------------------------------- arrays

def test_array_literal(arrays):
    assert arrays.call("literalSum") == 60


def test_array_size(arrays):
    assert arrays.call("arrSize") == 4


def test_array_assign(arrays):
    assert arrays.call("arrAssign") == 42


def test_array_add(arrays):
    assert arrays.call("arrAdd") == 303


def test_array_delete(arrays):
    assert arrays.call("arrDelete") == 203


def test_array_insert(arrays):
    assert arrays.call("arrInsert") == 2


def test_array_replace(arrays):
    assert arrays.call("arrReplace") == 99


def test_array_remove(arrays):
    assert arrays.call("arrRemove") == 207


def test_array_index(arrays):
    assert arrays.call("arrIndex") == 1


def test_subarray(arrays):
    assert arrays.call("subArr") == 302


def test_foreach(arrays):
    assert arrays.call("forEachSum") == 14


def test_elem_inc_temp(arrays):
    # arr[i]++/-- write back into the list (element reference, not a copy)
    assert arrays.call("elemInc") == 402


def test_elem_inc_this_member(arrays):
    assert arrays.call("elemIncThis") == 201


def test_in_array(arrays):
    assert arrays.call("inArray", "dog") == 1
    assert arrays.call("inArray", "bird") == 0


def test_new_array(arrays):
    assert arrays.call("newArray") == 5


def test_arraylen(arrays):
    assert arrays.call("arrayLen") == 2


def test_array_to_string(arrays):
    assert arrays.call("strJoinArr") == "1,2,3"


# ----------------------------------------------------------------- functions

def test_recursion(functions):
    assert functions.call("fib", 10) == 55


def test_cross_function_call(functions):
    assert functions.call("outer", 5) == 12


def test_implicit_return_zero(functions):
    assert functions.call("noReturn") == 0


def test_chained_assign(functions):
    assert functions.call("chainAssign") == 21


def test_pre_increment(functions):
    assert functions.call("preIncr") == 606


def test_post_increment(functions):
    assert functions.call("postIncr") == 605


def test_decrement(functions):
    assert functions.call("decrTest") == 4


def test_missing_arg_is_null(functions):
    assert functions.call("defaultParam", "a") == "a|"


# ------------------------------------------------------------------- objects

def test_this_persists_across_calls(objects):
    assert objects.call("setThis") == 42
    assert objects.call("readThis") == 42


def test_this_isolated_between_instances():
    a = load("06_objects")
    b = load("06_objects")
    a.call("setThis")
    assert b.call("readThis") is None  # no shared mutable state


def test_this_string(objects):
    assert objects.call("thisString") == "hello"


def test_new_object_members(objects):
    assert objects.call("makeObj") == 12


def test_with_block(objects):
    assert objects.call("withBlock") == 0


def test_nested_member(objects):
    assert objects.call("nestedMember") == 99


def test_obj_type(objects):
    # `n.type()*100 + s.type()*10 + arr.type()` where n=5, s="x", arr={1}.
    # opcodes.h guesses OP_OBJ_TYPE is a 0/1/2/3 type tag (which would make
    # this 13); the official handler only ever pushes 3.0 (an object holding
    # array cells) or 0.0 -- Preagonal/FourPlay/quattroplay/
    # src/TScriptMachine.cpp:3207-3213. So a number and a plain string both
    # read 0 and only the array reads 3.
    assert objects.call("objType") == 3


def test_global_var(objects):
    assert objects.call("globalVar") == 123


def test_missing_function_returns_none(objects):
    assert objects.call("noSuchFunction") is None


# --------------------------------------------------------------------- link

@pytest.fixture(scope="module")
def link():
    return load("07_link")


def test_link_aliases_the_array(link):
    # `temp.b = temp.a.link()` links rather than copies
    # (TScriptEnvironment::makeLinkVar -> linkValueTo,
    # Preagonal/FourPlay/quattroplay/src/TScriptEnvironment.cpp:175-193), so
    # adding through b is visible through a.
    assert link.call("aliasArray") == 4


def test_link_of_this_is_this(link):
    assert link.call("linkOfThis") == 9


def test_link_argument_does_not_truncate_the_argument_list(link):
    # The decompiled OP_OBJ_LINK sets the entry type to the arg-list start
    # marker (src/TScriptMachine.cpp:3339); the asm stores the object type
    # instead (asm/TScriptMachine/
    # _ZN14TScriptMachine13executeScriptEv.s_decomped:3235). With the marker
    # reading, `sumSizes(1, temp.arr.link(), 3)` would lose both neighbours.
    assert link.call("linkAsMiddleArgument") == 6


def test_link_of_a_non_object_is_null():
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])

    obj = GS2Object(name="holder")
    arr = [1.0]
    obj.set("rows", arr)
    frame.stack.append(LValue(obj, "rows"))
    vm._op_obj_link(frame, None)
    assert frame.stack.pop() is arr

    frame.stack.append(VarRef("neverSet"))
    vm._op_obj_link(frame, None)
    assert frame.stack.pop() is GS2_NULL


# --------------------------------------------------------------- unit pieces

def test_printf_format():
    assert printf_format("x=%d", [5.0]) == "x=5"
    assert printf_format("%s-%s", ["a", "b"]) == "a-b"
    assert printf_format("%.2f", [1.2345]) == "1.23"
    assert printf_format("%x", [255.0]) == "ff"
    assert printf_format("100%%", []) == "100%"
    assert printf_format("%d %d", [1.0]) == "1 0"  # missing args read as ""/0


def test_gs2_eq():
    assert gs2_eq(5.0, "5")
    assert gs2_eq("abc", "abc")
    # String/string comparison is strcasecmp in the official machine:
    # TScriptMachine::compare() -> TString::compareIgnoreCase
    # (Preagonal/FourPlay/quattroplay/src/TScriptMachine.cpp:1449 and
    # src/TString.cpp:1001-1011). "abc" == "ABC" is TRUE there.
    assert gs2_eq("abc", "ABC")
    assert not gs2_eq("abc", "abd")
    assert gs2_eq([1.0, 2.0], [1.0, 2.0])
    assert not gs2_eq([1.0], [1.0, 2.0])
    o = GS2Object()
    assert gs2_eq(o, o)
    assert not gs2_eq(o, GS2Object())


# ---------------------------------------------------------------- corpus run

@pytest.mark.skipif(not BASELINE_FILES, reason="gs2parser baselines not present")
@pytest.mark.parametrize("path", BASELINE_FILES,
                         ids=[_baseline_id(p) for p in BASELINE_FILES])
def test_corpus_executes_without_crash(path):
    """Every baseline script must load, run its toplevel, and survive an
    argless invocation of every function without any exception escaping the
    VM (missing builtins are logged + return 0 by design)."""
    with open(path, "rb") as fh:
        vm = GS2VM(fh.read(), name=os.path.basename(path))
    vm.max_ops = 60_000  # keep pathological loops cheap in CI
    vm.run_toplevel()
    for fname in list(vm.functions):
        vm.call(fname)


# --- expression-cache registers (official-compiler ops 45-47) ------------


class _RegInstr:
    """Minimal Instruction stand-in carrying just an operand value."""

    class _Operand:
        def __init__(self, value):
            self.value = value

    def __init__(self, value):
        self.operand = self._Operand(value)


def test_register_store_peeks_and_load_aliases():
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    obj = GS2Object(name="o")
    ref = LValue(obj, "member")

    frame.stack.append(ref)
    vm._op_conv_to_property(frame, None)
    vm._op_reg_store(frame, _RegInstr(3))
    # store must NOT pop -- the compiler emits OP_INDEX_DEC right after
    assert frame.stack == [ref]
    vm._op_index_dec(frame, None)
    assert frame.stack == []

    # the register holds the REFERENCE: later writes are visible on load
    obj.set("member", 7)
    vm._op_reg_load(frame, _RegInstr(3))
    assert frame.stack[-1] is ref
    assert vm.deref(frame.stack.pop(), frame) == 7


def test_conv_to_property_wraps_bare_name_strings_only():
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    frame.stack.append("somevar")
    vm._op_conv_to_property(frame, None)
    assert isinstance(frame.stack[-1], VarRef)
    assert frame.stack[-1].name == "somevar"
    frame.stack[:] = [4.0]
    vm._op_conv_to_property(frame, None)
    assert frame.stack == [4.0]


def test_register_bounds_and_unset_slots():
    from reborn_protocol.gs2.vm import MAX_REGISTER_INDEX
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    frame.stack.append("x")
    # out-of-range store is dropped (official machine bounds at 0x3FF)
    vm._op_reg_store(frame, _RegInstr(MAX_REGISTER_INDEX + 1))
    assert frame.registers == {}
    # unset slot loads null
    vm._op_reg_load(frame, _RegInstr(9))
    assert frame.stack[-1] is None


def test_register_cache_bytecode_end_to_end():
    """Assembled replica of the live -Serverlist_Chat cache pattern:
    `this.v; OP_CONV_TO_PROPERTY; OP_REG_STORE 0; OP_INDEX_DEC` then a write
    to this.v, then `OP_REG_LOAD 0` returned -- the cached reference must
    observe the later write."""
    from reborn_protocol.gs2.container import FunctionEntry
    code = bytes([
        0x17,                    # OP_TYPE_ARRAY (param list start)
        0x33,                    # OP_FUNC_PARAMS_END
        0x0A,                    # OP_JMP (function prologue no-op)
        0xB4,                    # OP_THIS
        0x16, 0xF0, 0x00,        # OP_TYPE_VAR [0] 'v'
        0x23,                    # OP_MEMBER_ACCESS -> LValue(this, 'v')
        0x2F,                    # OP_CONV_TO_PROPERTY
        0x2D, 0xF3, 0x00,        # OP_REG_STORE 0 (peek)
        0x20,                    # OP_INDEX_DEC
        0xB4,                    # OP_THIS
        0x16, 0xF0, 0x00,        # OP_TYPE_VAR [0] 'v'
        0x23,                    # OP_MEMBER_ACCESS
        0x14, 0xF3, 0x05,        # OP_TYPE_NUMBER 5
        0x32,                    # OP_ASSIGN (this.v = 5)
        0x2E, 0xF3, 0x00,        # OP_REG_LOAD 0
        0x07,                    # OP_RET
    ])
    container = GS2Container(functions=[FunctionEntry(name="f", op_index=0)],
                             strings=["v"], code=code)
    vm = GS2VM(container, name="regtest")
    assert vm.call("f") == 5
    assert vm.this.get("v") == 5


# --- with-block `this` rebinding + construction-block semantics ----------


def test_this_rebinds_to_innermost_with_target():
    # official: with-entry sets this=target (non-player); WITHEND's
    # findActionObject restores it scanning the with-list; thiso untouched
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    outer, inner = GS2Object(name="outer"), GS2Object(name="inner")

    vm._op_this(frame, None)
    assert frame.stack.pop() is vm.this

    frame.with_stack.append(outer)
    frame.with_stack.append(inner)
    vm._op_this(frame, None)
    assert frame.stack.pop() is inner
    vm._op_thiso(frame, None)
    assert frame.stack.pop() is vm.thiso

    frame.with_stack.pop()
    vm._op_this(frame, None)
    assert frame.stack.pop() is outer
    frame.with_stack.pop()
    vm._op_this(frame, None)
    assert frame.stack.pop() is vm.this


def test_this_rebinding_skips_player_target():
    from reborn_protocol.gs2.vm import GS2Host

    player = GS2Object(name="player")

    class _Host(GS2Host):
        def get_object(self, name):
            return player if name == "player" else None

    vm = GS2VM(GS2Container(), host=_Host())
    frame = _Frame(0, [])
    frame.with_stack.append(player)
    vm._op_this(frame, None)
    assert frame.stack.pop() is vm.this

    ctrl = GS2Object(name="ctrl")
    frame.with_stack[:] = [ctrl, player]
    vm._op_this(frame, None)
    assert frame.stack.pop() is ctrl


class _ConstructionHost:
    """Host that names created objects after the ctor arg, resolves them by
    name, and records with-scope method calls (addcontrol)."""

    def __init__(self):
        from reborn_protocol.gs2.vm import GS2Host
        self.objects = {}
        self.calls = []

    def create_object(self, classname, arg):
        obj = GS2Object(name=str(arg))
        self.objects[str(arg).lower()] = obj
        return obj

    def get_object(self, name):
        return self.objects.get(str(name).lower())

    def call_builtin(self, vm, name, args, obj=None):
        from reborn_protocol.gs2.vm import NOT_HANDLED
        if name == "addcontrol":
            self.calls.append((obj, list(args)))
            return 0.0
        return NOT_HANDLED

    def sleep(self, vm, seconds):
        pass

    def get_globals(self):
        raise NotImplementedError


def test_construction_block_bytecode_end_to_end():
    """Assembled replica of the live construction pattern (weapon
    -Serverlist_Chat): name; INLINE_NEW; 3x COPY; classname; NEW_OBJECT;
    ASSIGN; CONV_TO_OBJECT; WITH { this.tag = 9; addcontrol("child"); }
    WITHEND. `this.tag` must land on the object under construction, and the
    bare addcontrol() call must dispatch to the host WITH the with-target
    (this is how nested controls are parented -- the compiler emits
    addcontrol into the enclosing with-scope; createObject takes no
    parent)."""
    from reborn_protocol.gs2.container import FunctionEntry
    code = bytes([
        0x17,                    # 0  OP_TYPE_ARRAY
        0x33,                    # 1  OP_FUNC_PARAMS_END
        0x0A,                    # 2  OP_JMP
        0x15, 0xF0, 0x00,        # 3  OP_TYPE_STRING 'Btn'
        0x28,                    # 4  OP_INLINE_NEW
        0x1E, 0x1E, 0x1E,        # 5-7 OP_COPY_LAST_OP x3
        0x15, 0xF0, 0x01,        # 8  OP_TYPE_STRING 'TestCtrl'
        0x22,                    # 9  OP_CONV_TO_STRING
        0x2A,                    # 10 OP_NEW_OBJECT
        0x32,                    # 11 OP_ASSIGN
        0x24,                    # 12 OP_CONV_TO_OBJECT
        0x96, 0xF3, 0x19,        # 13 OP_WITH -> #25
        0xB4,                    # 14 OP_THIS
        0x16, 0xF0, 0x02,        # 15 OP_TYPE_VAR 'tag'
        0x23,                    # 16 OP_MEMBER_ACCESS
        0x14, 0xF3, 0x09,        # 17 OP_TYPE_NUMBER 9
        0x32,                    # 18 OP_ASSIGN (this.tag = 9)
        0x17,                    # 19 OP_TYPE_ARRAY
        0x15, 0xF0, 0x03,        # 20 OP_TYPE_STRING 'child'
        0x16, 0xF0, 0x04,        # 21 OP_TYPE_VAR 'addcontrol'
        0x06,                    # 22 OP_CALL
        0x20,                    # 23 OP_INDEX_DEC
        0x97,                    # 24 OP_WITHEND
        0x20,                    # 25 OP_INDEX_DEC (drop leftover name)
        0x14, 0xF3, 0x07,        # 26 OP_TYPE_NUMBER 7
        0x07,                    # 27 OP_RET
    ])
    container = GS2Container(
        functions=[FunctionEntry(name="f", op_index=0)],
        strings=["Btn", "TestCtrl", "tag", "child", "addcontrol"],
        code=code)
    host = _ConstructionHost()
    vm = GS2VM(container, name="ctortest", host=host)
    assert vm.call("f") == 7
    btn = host.objects["btn"]
    assert btn.get("tag") == 9          # this.x landed on the new object
    assert vm.this.get("tag") is None   # not on the script object
    assert host.calls == [(btn, ["child"])]  # dispatched with with-target


# --- official-compiler param binding (temp.<name> LValues) ----------------
# The live Login-server weapons (e.g. -ReShared's public.indexOf) declare
# params that the official compiler pushes as OP_TEMP; OP_TYPE_VAR;
# OP_MEMBER_ACCESS (an LValue on the frame temps object), not as the bare
# VarRefs gs2test emits. OP_FUNC_PARAMS_END must bind through the reference.


def test_func_params_bind_official_temp_member_style():
    from reborn_protocol.gs2.container import FunctionEntry

    code = bytes([
        0x17,                    # 0 OP_TYPE_ARRAY (param list start)
        0xBD,                    # 1 OP_TEMP
        0x16, 0xF0, 0x01,        # 2 OP_TYPE_VAR 'b'
        0x23,                    # 3 OP_MEMBER_ACCESS -> LValue(temps,'b')
        0xBD,                    # 4 OP_TEMP
        0x16, 0xF0, 0x00,        # 5 OP_TYPE_VAR 'a'
        0x23,                    # 6 OP_MEMBER_ACCESS -> LValue(temps,'a')
        0x33,                    # 7 OP_FUNC_PARAMS_END (binds a, then b)
        0x0A,                    # 8 OP_JMP
        0xBD,                    # 9 OP_TEMP
        0x16, 0xF0, 0x00,        # 10 OP_TYPE_VAR 'a'
        0x23,                    # 11 OP_MEMBER_ACCESS
        0xBD,                    # 12 OP_TEMP
        0x16, 0xF0, 0x01,        # 13 OP_TYPE_VAR 'b'
        0x23,                    # 14 OP_MEMBER_ACCESS
        0x3D,                    # 15 OP_SUB
        0x07,                    # 16 OP_RET
    ])
    container = GS2Container(functions=[FunctionEntry(name="f", op_index=0)],
                             strings=["a", "b"], code=code)
    vm = GS2VM(container, name="paramtest")
    assert vm.call("f", 7.0, 3.0) == 4.0
    # params must not leak into globals under garbage names
    assert vm.globals == {}


# --- OP_CONV_TO_OBJECT CSV-string tokenization ----------------------------
# Official switchTypeObject (quattroplay asm): a string-valued PROPERTY
# converts to a temporary token array iff it contains a comma or is fully
# quoted; other strings stay non-objects. Live -Mobile/Serverlist rows
# ('"name","P 42",...') depend on this to be indexable as arrays.


def test_conv_to_object_tokenizes_csv_string_property():
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    vm.globals["rows"] = '"Login","P 42",desc'

    frame.stack.append(VarRef("rows"))
    vm._op_conv_to_object(frame, None)
    assert frame.stack.pop() == ["Login", "P 42", "desc"]

    obj = GS2Object(name="o")
    obj.set("csv", "a,b,c")
    frame.stack.append(LValue(obj, "csv"))
    vm._op_conv_to_object(frame, None)
    assert frame.stack.pop() == ["a", "b", "c"]

    # fully-quoted single token converts too (first+last char are quotes)
    vm.globals["one"] = '"solo"'
    frame.stack.append(VarRef("one"))
    vm._op_conv_to_object(frame, None)
    assert frame.stack.pop() == ["solo"]

    # plain and empty strings stay unconverted references
    vm.globals["plain"] = "hello world"
    frame.stack.append(VarRef("plain"))
    vm._op_conv_to_object(frame, None)
    ref = frame.stack.pop()
    assert isinstance(ref, VarRef) and ref.name == "plain"

    vm.globals["empty"] = ""
    frame.stack.append(VarRef("empty"))
    vm._op_conv_to_object(frame, None)
    assert isinstance(frame.stack.pop(), VarRef)

    # computed string ENTRIES are name-resolved, never CSV-converted
    # (official checks the property slot, not the raw stack string)
    frame.stack.append("x,y,z")
    vm._op_conv_to_object(frame, None)
    assert frame.stack.pop() == "x,y,z"

    # conversion yields a TEMP: mutating it must not touch the property
    frame.stack.append(VarRef("rows"))
    vm._op_conv_to_object(frame, None)
    frame.stack.pop().append("junk")
    assert vm.globals["rows"] == '"Login","P 42",desc'


def test_array_index_on_plain_string_pushes_zero():
    # official getArrayCell: indexing a null-object (plain string) entry
    # pushes 0.0, not a character
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    frame.stack.extend(["hello", 1.0])
    vm._op_array(frame, None)
    assert frame.stack.pop() == 0.0


# --- array method dispatch (addarray / sortbyvalue / friends) -------------
# -Mobile/Serverlist sortServers() calls gold_servers.sortbyvalue(...) and
# sorted_servers.addarray(...): OP_MEMBER_ACCESS on a list base must retain
# the list so the call dispatches, and the VM implements the universal
# array methods natively (host still gets first refusal).


def test_member_access_retains_list_base():
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    lst = [1.0, 2.0]
    frame.stack.extend([lst, VarRef("addarray")])
    vm._op_member_access(frame, None)
    ref = frame.stack.pop()
    assert isinstance(ref, LValue) and ref.obj is lst and ref.key == "addarray"
    # reads/writes through a list-based LValue stay dead
    assert ref.get() is None
    ref.set(5)
    assert lst == [1.0, 2.0]


def test_list_methods_native_dispatch():
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])

    lst = [1.0]
    assert vm._call_target(LValue(lst, "addarray"), [[2.0, 3.0]], frame) is None
    assert lst == [1.0, 2.0, 3.0]

    vm._call_target(LValue(lst, "add"), ["x"], frame)
    assert lst == [1.0, 2.0, 3.0, "x"]
    assert vm._call_target(LValue(lst, "size"), [], frame) == 4.0
    assert vm._call_target(LValue(lst, "index"), [3.0], frame) == 2.0
    vm._call_target(LValue(lst, "clear"), [], frame)
    assert lst == []

    rows = ['"b",20', '"a",5', '"c",100']
    vm._call_target(LValue(rows, "sortbyvalue"), [1.0, "float", 1.0], frame)
    assert rows == ['"a",5', '"b",20', '"c",100']
    vm._call_target(LValue(rows, "sortbyvalue"), [1.0, "float", 0.0], frame)
    assert rows == ['"c",100', '"b",20', '"a",5']
    vm._call_target(LValue(rows, "sortbyvalue"), [0.0, "string", 1.0], frame)
    assert rows == ['"a",5', '"b",20', '"c",100']

    # unknown methods still fall through to builtins_missing, returning 0.0
    assert vm._call_target(LValue(lst, "definitelynotamethod"), [], frame) == 0.0


def test_root_object_methods_answer_on_any_object():
    """`addarray` / `sortbyvalue` are registered on the root object class, so
    they exist on every object -- Preagonal/FourPlay/quattroplay/
    src/TGraalVarProperties.cpp:233 and :575, installed by that file's
    constructor (:12-15). On a receiver with no array cells they do nothing,
    but they must not be reported as unknown methods, and (unlike a bare
    name) they must not fall through to the global surface."""
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    obj = GS2Object(name="npc")

    GS2VM.reset_coverage()
    assert vm._call_target(LValue(obj, "sortbyvalue"), [0.0, "string", 1.0], frame) is None
    assert vm._call_target(LValue(obj, "addarray"), [[1.0]], frame) is None
    assert not GS2VM.builtins_missing

    # names the reference compiles to opcodes instead of registering stay
    # array-only: on a plain object they are genuinely unknown
    assert vm._call_target(LValue(obj, "add"), [1.0], frame) == 0.0
    assert "add" in GS2VM.builtins_missing
    GS2VM.reset_coverage()


def test_copyfrom_is_a_root_object_method():
    """`obj.copyfrom(o)` is registered on the root object class
    (Preagonal/FourPlay/quattroplay/src/TGraalVarProperties.cpp:278-285,
    'v' params "o") and its body is TGraalVar::copyFrom
    (src/TGraalVar.cpp:2203-2371): members copied with arrays CLONED,
    object references shared, null source CLEARS, self-copy no-ops. The
    live Scripted_RC weapon bytecode calls it six times."""
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    src = GS2Object(name="src")
    src.set("n", 5.0)
    src.set("s", "hi")
    src.set("arr", [1.0, [2.0]])
    shared = GS2Object(name="shared")
    src.set("obj", shared)

    dst = GS2Object(name="dst")
    dst.set("stale", 1.0)
    GS2VM.reset_coverage()
    assert vm._call_target(LValue(dst, "copyfrom"), [src], frame) is None
    assert not GS2VM.builtins_missing
    assert dst.get("n") == 5.0 and dst.get("s") == "hi"
    assert dst.get("obj") is shared              # objects stay references
    assert dst.get("arr") == [1.0, [2.0]]
    assert dst.get("arr") is not src.get("arr")  # arrays are cloned...
    assert dst.get("arr")[1] is not src.get("arr")[1]  # ...recursively
    assert dst.get("stale") == 1.0               # copy, not replace

    # a null/None source clears (TGraalVar.cpp:2216-2221)
    vm._call_target(LValue(dst, "copyfrom"), [GS2_NULL], frame)
    assert len(dst) == 0

    # self-copy is a no-op (:2205)
    src_len = len(src)
    vm._call_target(LValue(src, "copyfrom"), [src], frame)
    assert len(src) == src_len

    # array-valued receiver: the source array replaces the target, cloned
    lst = [9.0]
    vm._call_target(LValue(lst, "copyfrom"), [[1.0, [2.0]]], frame)
    assert lst == [1.0, [2.0]]
    GS2VM.reset_coverage()


def test_lookup_resolves_dotted_makevar_names():
    """OP_MAKEVAR pushes the WHOLE dotted string as one VarRef name --
    `makevar("temp.creds." @ temp.field)` compiles to OP_CONV_TO_STRING;
    OP_MAKEVAR with no Call op (gs2test-verified) -- so deref must walk the
    dots: head through the normal scope chain, the rest through members
    (the live Mobile weapon-LoginScreen getAccountField idiom)."""
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    creds = GS2Object(name="creds")
    creds.set("pass", "blob")
    frame.temps.set("creds", creds)
    assert vm._lookup("temp.creds.pass", frame) == "blob"
    # a miss anywhere along the path stays None (resolves to Number 0.0)
    assert vm._lookup("temp.creds.nope", frame) is None
    assert vm._lookup("temp.nope.pass", frame) is None
    # non-temp heads walk the same chain: a global object holder
    vm.globals["cfg"] = cfg = GS2Object(name="cfg")
    cfg.set("port", 14900.0)
    assert vm._lookup("cfg.port", frame) == 14900.0


def test_member_write_on_undefined_bare_name_vivifies_holder():
    """`tmp.node = x` with no prior `tmp` (a plain identifier, NOT the
    temp. prefix -- the live Login serverlist builder's idiom): GS2Engine's
    variable collection auto-creates the holder object on member WRITE.
    Reads through an undefined name stay None and create nothing."""
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])

    # write path: tmp.node = 5 vivifies global object "tmp"
    frame.stack.extend([VarRef("tmp"), VarRef("node")])
    vm._op_member_access(frame, None)
    ref = frame.stack.pop()
    assert ref.get() is None                      # read before write: dead
    assert "tmp" not in vm.globals                # ...and nothing created
    ref.set(5)
    holder = vm.globals["tmp"]
    assert isinstance(holder, GS2Object)
    assert holder.get("node") == 5

    # subsequent access resolves the now-real object (plain LValue path)
    frame.stack.extend([VarRef("tmp"), VarRef("node")])
    vm._op_member_access(frame, None)
    assert frame.stack.pop().get() == 5

    # pure read path never vivifies
    frame.stack.extend([VarRef("ghost"), VarRef("x")])
    vm._op_member_access(frame, None)
    assert frame.stack.pop().get() is None
    assert "ghost" not in vm.globals

    # a name that HOLDS a value (even a plain number) is defined -- member
    # writes through it remain a dead ref (conservative: only genuinely
    # unresolved names vivify; a scalar is never silently replaced)
    scope = GS2Object(name="scope")
    scope.set("tmp2", 0)
    frame.with_stack.append(scope)
    frame.stack.extend([VarRef("tmp2"), VarRef("y")])
    vm._op_member_access(frame, None)
    frame.stack.pop().set(7)
    assert scope.get("tmp2") == 0
    frame.with_stack.pop()


def test_starts_ends_are_case_insensitive():
    # Reference semantics: the C# client's engine dispatches OP_OBJ_STARTS /
    # OP_OBJ_ENDS to TString::startsIgnoreCase / endsIgnoreCase (FourPlay
    # TScriptMachine::executeScript, caseD_74) -- the live Login server's
    # isLoginServer() relies on "Login".starts("login") being true.
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    frame.stack.extend(["Login", "login"])
    vm._op_obj_starts(frame, None)
    assert frame.stack.pop() is True
    frame.stack.extend(["MOBILE", "bile"])
    vm._op_obj_ends(frame, None)
    assert frame.stack.pop() is True
    frame.stack.extend(["Login", "xyz"])
    vm._op_obj_starts(frame, None)
    assert frame.stack.pop() is False


# ------------------------------------------------- official-interpreter parity
#
# The cases below were recovered from the reversed official interpreter that
# now ships as readable C++ in Preagonal/FourPlay/quattroplay (executeScript
# lives in src/TScriptMachine.cpp:2148-3686, with the matching x86 in
# asm/TScriptMachine/_ZN14TScriptMachine13executeScriptEv.s_decomped).
# Citations are file:line at FourPlay commit 4862ff00.


def test_mod_is_floored_not_fmod():
    # src/TScriptMachine.cpp:3091 -- `left - right * floor(left / right)`.
    # C fmod would give -1.0 / 1.0 for the negative cases.
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    for a, b, expected in [(7, 3, 1.0), (-7, 3, 2.0), (7, -3, -2.0),
                           (-7, -3, -1.0), (7.5, 2, 1.5), (5, 0, 0.0)]:
        frame.stack.extend([float(a), float(b)])
        vm._op_mod(frame, None)
        assert frame.stack.pop() == pytest.approx(expected), (a, b)


def test_int_floors_with_the_index_epsilon():
    # src/TScriptMachine.cpp:3221-3225 calls floorScriptIndex (:60-67), i.e.
    # floor(v + 0.0001) -- not truncation toward zero.
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    for v, expected in [(2.9, 2.0), (2.99999999, 3.0), (-2.5, -3.0),
                        (-0.00005, 0.0), (3.0, 3.0)]:
        frame.stack.append(v)
        vm._op_int(frame, None)
        assert frame.stack.pop() == expected, v


def test_number_to_string_is_9_decimals_trimmed():
    # switchTypeString: |(float)v| < 0.0001 prints the literal "0", anything
    # else goes through TString::adddouble -> "%.9f" with trailing zeros and
    # a trailing '.' stripped (asm/TScriptStackEntry/_ZN17TScriptStackEntry16
    # switchTypeStringEP14TScriptMachineb.s_decomped:37-58, src/TString.cpp:
    # 1139-1174). GS1 (gs1.values.fmt_num) prints the shortest round-tripping
    # repr instead -- the two engines genuinely differ.
    assert to_str(5.0) == "5"
    assert to_str(2.5) == "2.5"
    assert to_str(-2.5) == "-2.5"
    assert to_str(2.0 / 3.0) == "0.666666667"
    assert to_str(0.1) == "0.1"
    assert to_str(0.00005) == "0"
    assert to_str(-0.00005) == "0"
    assert to_str(0.0) == "0"
    assert to_str(1e20) == "100000000000000000000"
    assert fmt_num(1.0000000004) == "1"


def test_string_compare_is_case_insensitive_and_numeric_against_numbers():
    # src/TScriptMachine.cpp:1430-1487 (compare()): string/string is
    # strcasecmp, string/number is a numeric compare of strtofloat(string).
    assert gs2_compare("abc", "ABC") == 0
    assert gs2_compare("abc", "abd") < 0
    assert gs2_compare("B", "a") > 0
    assert gs2_compare("5", 5.0) == 0
    # strtofloat of a word strtod can't read is -1.0 (values.strtofloat),
    # so `<number> == "<word>"` is FALSE -- earlier waves locked ==0 here.
    assert gs2_compare("abc", 0.0) < 0
    assert gs2_compare("abc", -1.0) == 0
    assert gs2_compare(3.0, "10") < 0        # numeric, not lexicographic


def test_numeric_comparison_carries_the_0001_tolerance():
    # compareNumberValues (src/TScriptMachine.cpp:36-43): a < b only when
    # b > a + 0.0001, so near-equal floats compare EQUAL.
    assert gs2_compare(0.99999, 1.0) == 0
    assert gs2_compare(1.0, 1.00005) == 0
    assert gs2_compare(1.0, 1.0002) < 0
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    frame.stack.extend([0.99999, 1.0])
    vm._op_lt(frame, None)
    assert frame.stack.pop() is False
    frame.stack.extend([0.99999, 1.0])
    vm._op_gte(frame, None)
    assert frame.stack.pop() is True


def test_min_max_keep_the_winning_operands_type():
    # src/TScriptMachine.cpp:3286-3295 copies the winning stack entry, so the
    # result keeps its type; comparison is compare(), so strings work.
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    frame.stack.extend(["b", "a"])
    vm._op_min(frame, None)
    assert frame.stack.pop() == "a"
    frame.stack.extend(["b", "a"])
    vm._op_max(frame, None)
    assert frame.stack.pop() == "b"
    frame.stack.extend([3.0, 9.0])
    vm._op_min(frame, None)
    assert frame.stack.pop() == 3.0


def test_bitwise_ops_use_int32_semantics():
    # src/TScriptMachine.cpp:3096-3111: both operands go through a C int32
    # cast and shift counts are masked to 5 bits.
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    frame.stack.extend([1.0, 33.0])
    vm._op_bw_leftshift(frame, None)
    assert frame.stack.pop() == 2.0          # 33 & 31 == 1, not 1 << 33
    frame.stack.extend([1.0, 31.0])
    vm._op_bw_leftshift(frame, None)
    assert frame.stack.pop() == -2147483648.0  # wraps like int32
    frame.stack.append(-2.5)
    vm._op_bwi(frame, None)                  # ~floorScriptIndex(-2.5) == ~-3
    assert frame.stack.pop() == 2.0


def test_vecx_vecy_do_not_wrap_past_direction_3():
    # src/TScriptMachine.cpp:3313-3325: dir > 3 reads 0, there is no % 4.
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    for d, x, y in [(0, 0.0, -1.0), (1, -1.0, 0.0), (2, 0.0, 1.0),
                    (3, 1.0, 0.0), (5, 0.0, 0.0)]:
        frame.stack.append(float(d))
        vm._op_vecx(frame, None)
        assert frame.stack.pop() == x, d
        frame.stack.append(float(d))
        vm._op_vecy(frame, None)
        assert frame.stack.pop() == y, d


def test_sin_cos_are_not_snapped_to_zero():
    # The official handlers are bare libm calls (src/TScriptMachine.cpp:
    # 3248-3253); only GS2Engine's C# port snaps small results to 0.
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    frame.stack.append(math.pi)
    vm._op_sin(frame, None)
    assert frame.stack.pop() == math.sin(math.pi) != 0.0


def test_official_only_opcodes_66_67_104_121():
    # Opcodes the open-source compiler never emits but the official
    # interpreter implements: logical and/or (:3127-3151), 3-way compare
    # (:3352-3358) and the runtime-typed `+` (:3452-3475).
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    frame.stack.extend([2.0, 3.0])
    vm._op_unknown_66(frame, None)
    assert frame.stack.pop() == 1.0
    frame.stack.extend([0.0, 3.0])
    vm._op_unknown_66(frame, None)
    assert frame.stack.pop() == 0.0
    frame.stack.extend([0.0, 3.0])
    vm._op_unknown_67(frame, None)
    assert frame.stack.pop() == 1.0
    frame.stack.extend(["abc", "abd"])
    vm._op_obj_compare(frame, None)
    assert frame.stack.pop() == -1.0
    frame.stack.extend(["a", 5.0])
    vm._op_dynamic_add(frame, None)
    assert frame.stack.pop() == "a5"
    frame.stack.extend([2.0, 5.0])
    vm._op_dynamic_add(frame, None)
    assert frame.stack.pop() == 7.0


def test_getdir_ties_go_to_the_vertical_axis():
    # TScriptMachine::getDir (src/TScriptMachine.cpp:1278-1304): the x branch
    # needs a STRICTLY larger |x|, so a 45-degree vector reads as up/down.
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    for dx, dy, expected in [(5.0, 1.0, 3.0), (-5.0, 1.0, 1.0),
                             (1.0, 5.0, 2.0), (1.0, -5.0, 0.0),
                             (1.0, 1.0, 2.0), (0.0, 0.0, 2.0)]:
        frame.stack.extend([dx, dy])
        vm._op_getdir(frame, None)
        assert frame.stack.pop() == expected, (dx, dy)


def test_in_range_is_inclusive_with_tolerance_and_walks_arrays():
    # ValueInRange mode 0 (src/unsorted.cpp:1359-1376) -- the only mode the
    # open-source compiler emits -- plus the all-cells array form
    # (src/TScriptMachine.cpp:92-135).
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    for v, lo, hi, expected in [(5.0, 0.0, 10.0, True), (0.0, 0.0, 10.0, True),
                                (10.0, 0.0, 10.0, True), (10.00005, 0.0, 10.0, True),
                                (10.001, 0.0, 10.0, False), (-1.0, 0.0, 10.0, False)]:
        frame.stack.extend([v, lo, hi])
        vm._op_in_range(frame, None)
        assert frame.stack.pop() is expected, (v, lo, hi)

    frame.stack.extend([[1.0, 2.0, 3.0], 0.0, 10.0])
    vm._op_in_range(frame, None)
    assert frame.stack.pop() is True
    frame.stack.extend([[1.0, 99.0], 0.0, 10.0])
    vm._op_in_range(frame, None)
    assert frame.stack.pop() is False


def test_in_range_modes_and_the_out_of_table_default():
    """The four ValueInRange modes select which ends are exclusive, and a mode
    outside 0..3 is `default: return false` (src/unsorted.cpp:1364-1375) --
    NOT "behave like mode 0". No real bytecode we have carries the operand, so
    this only matters if a stray one is ever attached, but silently widening a
    range there would be exactly the kind of invisible branch flip we are
    guarding against."""
    from reborn_protocol.gs2.vm import _value_in_range

    # mode 0: both ends inclusive; 1: hi exclusive; 2: lo exclusive; 3: both
    assert _value_in_range(0.0, 0.0, 10.0, 0) is True
    assert _value_in_range(10.0, 0.0, 10.0, 0) is True
    assert _value_in_range(10.0, 0.0, 10.0, 1) is False
    assert _value_in_range(0.0, 0.0, 10.0, 1) is True
    assert _value_in_range(0.0, 0.0, 10.0, 2) is False
    assert _value_in_range(10.0, 0.0, 10.0, 2) is True
    assert _value_in_range(0.0, 0.0, 10.0, 3) is False
    assert _value_in_range(10.0, 0.0, 10.0, 3) is False
    assert _value_in_range(5.0, 0.0, 10.0, 3) is True
    # out of table
    assert _value_in_range(5.0, 0.0, 10.0, 4) is False
    assert _value_in_range(5.0, 0.0, 10.0, -1) is False
