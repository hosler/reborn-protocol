"""The two engines' coercion policies, and that each engine's call sites use
its OWN policy.

The GS1/GS2 differences here are deliberate (two engines, two reversed
sources) -- these tests pin them so a future "cleanup" that unifies them
fails loudly. The case-folding tests additionally pin a behaviour FIX: the
GS2 VM's `.starts`/`.ends` opcodes and the client host's `sort` keys used
Python `str.casefold()`, which folds non-ASCII and contradicted the same
module's own oracle-cited `casecmp`.
"""
import math

import pytest

from reborn_protocol.gs1 import values as gs1v
from reborn_protocol.gs2 import values as gs2v
from reborn_protocol.gs2.vm import GS2VM
from reborn_protocol.gs2.opcodes import Op
from reborn_protocol.gs2.disasm import Instruction


# --- case folding: ASCII-only, per C strcasecmp -----------------------------

# ß folds to "ss" under str.casefold (changing LENGTH); İ folds to "i̇" (two
# code points). C strncasecmp does neither, so neither may our engine.
NON_ASCII_PAIRS = [
    ("ß", "SS"),
    ("İ", "i"),
    ("Ä", "ä"),
    ("ﬀ", "ff"),
]


@pytest.mark.parametrize("a,b", NON_ASCII_PAIRS)
def test_casefold_is_ascii_only(a, b):
    """A non-ASCII character is never folded onto another string."""
    assert gs2v.casefold(a) == a
    assert gs2v.casecmp(a, b) != 0
    # ...whereas Python's casefold WOULD have equated at least some of these,
    # which is exactly the divergence being pinned.
    assert gs2v.casefold(a) == a.translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                      "abcdefghijklmnopqrstuvwxyz"))


def test_casefold_folds_ascii():
    assert gs2v.casefold("LoGiN") == "login"
    assert gs2v.casecmp("Login", "LOGIN") == 0


def _run_ops(ops):
    """Execute a hand-built instruction list on a bare VM and return the top
    of the frame stack (no container needed for pure string opcodes)."""
    vm = GS2VM.__new__(GS2VM)
    vm.host = None
    frame = type("F", (), {"stack": [], "temps": {}})()
    for op, *rest in ops:
        if op == "push":
            frame.stack.append(rest[0])
            continue
        handler = getattr(vm, f"_op_{op.name[3:].lower()}")
        handler(frame, Instruction(0, op, None, 0))
    return frame.stack[-1]


def test_op_starts_is_ascii_only_case_insensitive():
    # ASCII folding still applies (Login's isLoginServer relies on it)...
    assert _run_ops([("push", "Login"), ("push", "LOG"), (Op.OP_OBJ_STARTS,)])
    # ...but "ß" must not match the prefix "SS": str.casefold() said it did.
    assert not _run_ops([("push", "ßx"), ("push", "SS"), (Op.OP_OBJ_STARTS,)])


def test_op_ends_is_ascii_only_case_insensitive():
    assert _run_ops([("push", "weaponFOO"), ("push", "foo"), (Op.OP_OBJ_ENDS,)])
    assert not _run_ops([("push", "xß"), ("push", "SS"), (Op.OP_OBJ_ENDS,)])


# --- number -> string: two different rules ----------------------------------

def test_float_to_string_rules_differ_per_engine():
    # GS2: snprintf("%.9f") with trailing zeros stripped.
    assert gs2v.fmt_num(2 / 3) == "0.666666667"
    # GS1: shortest decimal that round-trips.
    assert gs1v.fmt_num(2 / 3) == "0.6666666666666666"


def test_gs2_prints_sub_epsilon_as_zero_gs1_does_not():
    """GS2's float32 zero shortcut has no GS1 counterpart."""
    assert gs2v.fmt_num(1e-9) == "0"
    assert gs1v.fmt_num(1e-9) == "1e-09"


# --- compare tolerance ------------------------------------------------------

def test_compare_epsilons_are_named_and_distinct_in_use():
    assert gs2v.SCRIPT_EPSILON == 1e-4
    assert gs1v.COMPARE_EPSILON == 1e-4
    assert gs1v._DOUBLE_EPS < 1e-15

    # GS2 applies the tolerance to ORDERING too: 0.99999 < 1.0 is false.
    assert gs2v.gs2_compare(0.99999, 1.0) == 0
    # GS1 applies it to equality only; `<` is exact (interp._binop).
    assert gs1v.doubles_are_same(0.99999, 1.0)
    assert 0.99999 < 1.0

    # The unary-`!` rule is a THIRD epsilon and must stay separate.
    assert not gs1v.is_double_zero(1e-5)
    assert gs1v.doubles_are_same(1e-5, 0.0)


# --- number -> int ----------------------------------------------------------

def test_gs1_int_truncates_gs1_index_floors():
    assert gs1v.gs1_int(-2.7) == -2
    assert gs1v.gs1_index(-2.7) == -3
    # No index epsilon in GS1, unlike GS2's array_index.
    assert gs1v.gs1_index(2.99999) == 2
    assert gs2v.array_index(2.99999) == 3


def test_gs2_to_int32_clamps_out_of_range_to_int_min():
    assert gs2v.to_int32(-2.7) == -2
    assert gs2v.to_int32(2**31) == -0x80000000
    assert gs2v.to_int32(float("nan")) == -0x80000000
    assert gs2v.wrap_int32(0x80000000) == -0x80000000


def test_gs2_array_size_clamps_to_official_ceiling():
    assert gs2v.array_size(50000) == gs2v.MAX_ARRAY_SIZE == 10000
    assert gs2v.array_size(-5) == 0


def test_vm_reexports_the_policy_home():
    """The VM must not grow a second copy of any of these."""
    from reborn_protocol.gs2 import vm
    for name in ("array_index", "array_size", "to_int32", "wrap_int32",
                 "MAX_ARRAY_SIZE", "ARRAY_INDEX_EPSILON"):
        assert getattr(vm, name) is getattr(gs2v, name), name


# --- GS1 call sites go through the GS1 policy -------------------------------

def test_gs1_modulo_truncates_both_operands():
    from reborn_protocol.gs1.interp import Interpreter
    assert Interpreter._mod(7.9, 3.0) == 1.0
    assert math.copysign(1.0, Interpreter._mod(-7.9, 3.0)) == -1.0
    assert Interpreter._mod(5.0, 0.0) == 0.0
