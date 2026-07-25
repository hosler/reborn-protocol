"""The TScriptStackEntry type lattice and the compare() table built on it.

Oracle: Preagonal/FourPlay/quattroplay -- include/TScriptStackEntry.h:12-24
(the enum), src/TScriptStackEntry.cpp:212-231 (resolve) and
src/TScriptMachine.cpp:37-58 + 1430-1488 (the helpers and compare()).

The enum's `Null` cell is a decompilation misnomer: it is the OBJECT type, an
entry carrying a TGraalVar* that may be nullptr. `null` in GS2 source is
OP_TYPE_NULL, which pushes exactly that with a nullptr pointer
(TScriptMachine.cpp:2605-2609) -- while an unset variable resolves to
NUMBER 0.0 (TScriptStackEntry.cpp:228-229). Modelling both as Python's None
is what let a found weapon compare equal to null and silently killed the whole
Login GUI; GS2_NULL exists to keep them apart.
"""
from __future__ import annotations

from reborn_protocol.gs2 import GS2_NULL
from reborn_protocol.gs2.container import GS2Container
from reborn_protocol.gs2.values import (
    GS2Object, gs2_compare, gs2_eq, to_bool, to_num, to_str,
)
from reborn_protocol.gs2.vm import GS2VM, _Frame, _denull


def _vm_frame():
    return GS2VM(GS2Container()), _Frame(0, [])


# --- GS2_NULL is a value like any other outside compare() ------------------


def test_gs2_null_coerces_exactly_like_none():
    assert to_num(GS2_NULL) == 0.0
    assert to_str(GS2_NULL) == ""
    assert to_bool(GS2_NULL) is False
    assert bool(GS2_NULL) is False


# --- the compare() table ---------------------------------------------------


def test_object_vs_null_is_a_pointer_compare():
    """compareObjectPointers (TScriptMachine.cpp:51-58, row :1478)."""
    obj = GS2Object("weapon:-Rescripted/Serverlist")
    assert gs2_eq(obj, GS2_NULL) is False
    assert gs2_eq(GS2_NULL, obj) is False
    assert gs2_compare(obj, GS2_NULL) == -gs2_compare(GS2_NULL, obj)
    # a live object always sorts ABOVE the null pointer
    assert gs2_compare(obj, GS2_NULL) > 0
    # ...and null is equal to itself
    assert gs2_eq(GS2_NULL, GS2_NULL) is True


def test_unset_variable_is_number_zero_and_so_equals_null():
    """`findweapon(x)` that found nothing leaves the temp unset; the machine
    resolves that to NUMBER 0.0 and objectPointerAsDouble(nullptr) is also
    0.0, so `== null` is true (row :1465). This is the branch that MUST still
    fire when the weapon is genuinely missing."""
    assert gs2_eq(None, GS2_NULL) is True
    assert gs2_eq(0.0, GS2_NULL) is True
    assert gs2_eq(0, GS2_NULL) is True
    assert gs2_eq(1.0, GS2_NULL) is False


def test_object_vs_number_compares_the_pointer_not_the_value():
    """Row :1465/:1480 -- objectPointerAsDouble, NOT to_num(obj). This is the
    exact bug that broke the Login GUI: to_num() is 0.0 for an object AND for
    null, so a numeric fallthrough reported them equal."""
    obj = GS2Object("x")
    assert gs2_eq(obj, 0.0) is False
    assert gs2_compare(obj, 0.0) > 0      # a real address is a big positive
    assert gs2_compare(0.0, obj) < 0
    assert gs2_eq(obj, None) is False


def test_object_vs_string_compares_the_object_name():
    """Row :1452/:1476, strcasecmp on TGraalVar::name."""
    obj = GS2Object("Serverlist_Panel")
    assert gs2_eq(obj, "serverlist_panel") is True
    assert gs2_eq(obj, "something else") is False
    assert gs2_compare(obj, "zzz") == -gs2_compare("zzz", obj)


def test_null_object_compares_equal_to_every_string():
    """The official ternary refuses to dereference a nullptr and returns 0
    instead (`right->scriptProperty1 != nullptr ? ... : 0`, :1452/:1476), so
    `null == <any string>` is TRUE on the real client."""
    assert gs2_eq(GS2_NULL, "anything") is True
    assert gs2_eq("anything", GS2_NULL) is True
    assert gs2_eq(GS2_NULL, "") is True
    # An UNSET variable is a number, so it only matches numerically-zero
    # strings -- the two cells really are distinguishable.
    assert gs2_eq(None, "5") is False
    assert gs2_eq(GS2_NULL, "5") is True


def test_arrays_are_objects_so_they_are_never_null_and_never_a_number():
    """An array is a TGraalVar with cells, i.e. an OBJECT entry with a live
    pointer. `temp.arr == null` used to be TRUE for an empty array because
    to_num([]) is its length; that is the same bug class as the weapon."""
    empty: list = []
    full = [1.0, 2.0]
    assert gs2_eq(empty, GS2_NULL) is False
    assert gs2_eq(full, GS2_NULL) is False
    assert gs2_eq(empty, 0.0) is False
    assert gs2_eq(full, 2.0) is False
    assert gs2_eq(empty, None) is False
    # ...but array/array stays elementwise (documented deviation: OP_IN_OBJ /
    # OP_OBJ_INDEX / OP_OBJ_REMOVESTRING compare elements by value).
    assert gs2_eq([1.0, 2.0], [1.0, 2.0]) is True
    assert gs2_eq([1.0], [1.0, 2.0]) is False


def test_compare_is_antisymmetric_across_every_cell_pair():
    obj, arr = GS2Object("o"), [1.0]
    values = [0.0, 1.0, -3.5, None, True, "", "abc", "5", obj, arr, GS2_NULL]
    for a in values:
        for b in values:
            assert gs2_compare(a, b) == -gs2_compare(b, a), (a, b)


# --- GS2_NULL never escapes the expression stack ---------------------------


def test_null_is_normalised_to_none_on_assignment():
    """A host GS2Object subclass must never be handed the sentinel."""
    vm, frame = _vm_frame()
    target = GS2Object("host")
    from reborn_protocol.gs2.values import LValue

    vm._write_ref(LValue(target, "slot"), GS2_NULL, frame)
    assert target.get("slot") is None


def test_null_is_normalised_to_none_in_call_arguments():
    from reborn_protocol.gs2.values import ARRAY_START

    vm, frame = _vm_frame()
    frame.stack.extend([ARRAY_START, GS2_NULL, "keep"])
    assert vm._pop_args(frame) == ["keep", None]


def test_denull_leaves_everything_else_alone():
    obj = GS2Object("o")
    for v in (None, 0.0, "", [], obj, False):
        assert _denull(v) is v


# --- end-to-end: the Login serverlist branch -------------------------------


def test_op_type_null_drives_the_serverlist_branch():
    """`temp.wep = findweapon(...); if (temp.wep != null) temp.wep.init...()`
    -- OP_TYPE_VAR/OP_MEMBER_ACCESS, OP_TYPE_NULL, OP_NEQ, as emitted by the
    official compiler in weapon-Rescripted_IRC_Login3.gs2bc."""
    vm, frame = _vm_frame()
    weapon = GS2Object("weapon:-Rescripted/Serverlist")

    # found: `!= null` must be TRUE
    frame.stack.append(weapon)
    vm._op_type_null(frame, None)
    vm._op_neq(frame, None)
    assert frame.stack.pop() is True

    # missing (host returned nothing): `!= null` must be FALSE
    frame.stack.append(None)
    vm._op_type_null(frame, None)
    vm._op_neq(frame, None)
    assert frame.stack.pop() is False
