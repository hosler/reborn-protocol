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

from reborn_protocol.gs2 import GS2VM, GS2Object, printf_format, gs2_eq

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


def _baseline_id(path: str) -> str:
    """Relative id for a baseline path, against whichever root it's under."""
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
    assert objects.call("withBlock") == 30


def test_nested_member(objects):
    assert objects.call("nestedMember") == 99


def test_obj_type(objects):
    assert objects.call("objType") == 13  # float=0, string=1, array=3


def test_global_var(objects):
    assert objects.call("globalVar") == 123


def test_missing_function_returns_none(objects):
    assert objects.call("noSuchFunction") is None


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
    assert not gs2_eq("abc", "ABC")
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
