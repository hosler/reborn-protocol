"""The TScriptStackEntry type lattice and the compare() table built on it.

Oracle: Preagonal/FourPlay/quattroplay -- include/TScriptStackEntry.h:12-24
(the enum), src/TScriptStackEntry.cpp:212-231 (resolve) and
src/TScriptMachine.cpp:37-58 + 1430-1488 (the helpers and compare()).

The enum's `Null` cell is a decompilation misnomer: it is the OBJECT type, an
entry carrying a TGraalVar* that may be nullptr. `null` in GS2 source is
OP_TYPE_NULL, which pushes exactly that with a nullptr pointer
(TScriptMachine.cpp:2605-2609). Modelling it as Python's None is what let a
found weapon compare equal to null and silently killed the whole Login GUI;
GS2_NULL exists to keep it apart from "no value here" at the host boundary.
Inside compare() however, BOTH collapse to Number 0.0 -- see values.resolve
and the null-vs-string verdict in values.gs2_compare's docstring.
"""
from __future__ import annotations

from reborn_protocol.gs2 import GS2_NULL
from reborn_protocol.gs2.container import GS2Container
from reborn_protocol.gs2.values import (
    ARRAY_START, GS2Object, gs2_compare, gs2_eq, gs2_to_num, gs2_truthy,
    resolve, strtofloat, to_bool, to_num, to_str,
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


def test_object_vs_null_compares_pointer_against_zero():
    """A live object never equals null: after resolve() the null side is
    Number 0.0, so this is the object/number row -- compareNumberValues of
    the pointer against 0.0 (TScriptMachine.cpp:46-49, row :1480)."""
    obj = GS2Object("weapon:-Rescripted/Serverlist")
    assert gs2_eq(obj, GS2_NULL) is False
    assert gs2_eq(GS2_NULL, obj) is False
    assert gs2_compare(obj, GS2_NULL) == -gs2_compare(GS2_NULL, obj)
    # a live object always sorts ABOVE the null pointer
    assert gs2_compare(obj, GS2_NULL) > 0
    # ...and null is equal to itself
    assert gs2_eq(GS2_NULL, GS2_NULL) is True


def test_resolve_collapses_null_and_the_array_marker_to_number_zero():
    """values.resolve: a direct nullptr-object entry has no backing property
    and terminates at Number 0.0; everything live passes through."""
    assert resolve(GS2_NULL) == 0.0
    assert resolve(ARRAY_START) == 0.0
    obj, arr = GS2Object("o"), [1.0]
    for v in (obj, arr, "s", 2.5, None, True):
        assert resolve(v) is v


def test_unset_variable_is_number_zero_and_so_equals_null():
    """`findweapon(x)` that found nothing leaves the temp unset; the machine
    resolves that to NUMBER 0.0, and the `null` keyword resolves to the very
    same cell, so `== null` is true (row :1467). This is the branch that MUST
    still fire when the weapon is genuinely missing."""
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


def test_object_string_conversion_uses_the_object_name():
    obj = GS2Object("Skills_Games_TicTacToe")
    assert to_str(obj) == "Skills_Games_TicTacToe"


def test_literal_null_vs_string_is_a_number_compare_not_equal_to_any_string():
    """Rows :1452/:1476 DO say "a nullptr object equals any string" -- but
    compare() resolves both entries first (:1435/:1441), and resolve()
    collapses a direct Null(nullptr) entry (the `null` keyword, a null call
    result) to Number 0.0 before the switch ever runs. So `null == s` is
    just 0.0 vs strtofloat(s): an earlier wave locked `null == "5"` as TRUE
    off the unresolved row -- that was wrong both ways (see the verdict in
    values.gs2_compare)."""
    assert gs2_eq(GS2_NULL, "5") is False           # 0.0 vs 5.0
    assert gs2_eq(GS2_NULL, "anything") is False    # 0.0 vs -1.0
    assert gs2_eq("anything", GS2_NULL) is False
    assert gs2_eq(GS2_NULL, "") is True             # strtofloat("") == 0.0
    assert gs2_eq(GS2_NULL, "0") is True
    assert gs2_eq(GS2_NULL, "false") is True        # strtofloat table
    # An unset variable behaves identically -- same NUMBER cell.
    assert gs2_eq(None, "5") is False
    assert gs2_eq(None, "anything") is False
    assert gs2_eq(None, "") is True


def test_engine_property_null_divergence_is_locked():
    """KEPT DIVERGENCE (values.gs2_compare docstring): the reference's one
    live source of a nullptr object in compare() is an object-typed ENGINE
    property read (copyFromProperty, TScriptStackEntry.cpp:76-81), which
    would equal ANY string. Our hosts surface such reads as None -- the
    NUMBER cell -- because "object property, currently null" and "no such
    property" are indistinguishable at the host boundary, and treating every
    host miss as equal-to-any-string is the outage class that killed the
    Login serverlist. This test pins the SAFE behavior: not equal."""
    host_miss = None
    assert gs2_eq(host_miss, "connect") is False
    assert gs2_eq(host_miss, "skills") is False


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


def test_strtofloat_follows_the_official_table():
    """values.strtofloat (TInitStatics.cpp:4335-4386). The -1.0 no-parse row
    is the load-bearing one: it makes `<number> == "<word>"` FALSE."""
    assert strtofloat("") == 0.0
    assert strtofloat("true") == 1.0
    assert strtofloat("false") == 0.0
    assert strtofloat("True") == -1.0        # TString == is memcmp
    assert strtofloat("0x1f") == 31.0
    assert strtofloat("0x") == 0.0           # strtoul parsed nothing
    assert strtofloat("5") == 5.0
    assert strtofloat("  -2.5xyz") == -2.5   # strtod prefix parse
    assert strtofloat("1e3") == 1000.0       # exponents, unlike gs1.to_num
    assert strtofloat(".5") == 0.5
    assert strtofloat("abc") == -1.0
    assert strtofloat(" ") == -1.0
    assert strtofloat("e5") == -1.0


def test_gs2_to_num_reads_objects_and_arrays_as_zero():
    """switchTypeFloat's object row reads the var's never-written float
    slot: 0.0 -- NOT gs1's array length rule."""
    assert gs2_to_num([1.0, 2.0]) == 0.0
    assert gs2_to_num(GS2Object("o")) == 0.0
    assert gs2_to_num(GS2_NULL) == 0.0
    assert gs2_to_num(None) == 0.0
    assert gs2_to_num(True) == 1.0
    assert gs2_to_num("word") == -1.0
    assert to_num([1.0, 2.0]) == 2.0  # the GS1 rule stays its own


def test_gs2_truthiness_matches_the_branch_opcodes():
    """Conditions are conv-to-float'd then tested EXACTLY against 0.0, so a
    non-numeric string is TRUTHY (-1.0) while "0" and "false" are not."""
    assert gs2_truthy("word") is True
    assert gs2_truthy("0") is False
    assert gs2_truthy("false") is False
    assert gs2_truthy("") is False
    assert gs2_truthy(0.00005) is True   # no epsilon in OP_IF
    assert gs2_truthy(GS2_NULL) is False
    assert gs2_truthy(None) is False


def test_conv_to_float_then_not_sees_strtofloat():
    """`!("word")` through the opcode pair: OP_CONV_TO_FLOAT makes -1.0,
    OP_NOT tests it against 0.0 -> False (i.e. "word" was truthy)."""
    vm, frame = _vm_frame()
    frame.stack.append("word")
    vm._op_conv_to_float(frame, None)
    assert frame.stack[-1] == -1.0
    vm._op_not(frame, None)
    assert frame.stack.pop() is False
    # arrays still pass through the conv (gs2parser arraylen sig-conv)
    arr = [1.0]
    frame.stack.append(arr)
    vm._op_conv_to_float(frame, None)
    assert frame.stack.pop() is arr


def test_arithmetic_on_words_uses_the_minus_one():
    """`5 + "word"` is 4.0 on the real client (switchTypeFloat -> -1.0)."""
    vm, frame = _vm_frame()
    frame.stack.append(5.0)
    frame.stack.append("word")
    vm._op_conv_to_float(frame, None)
    vm._op_add(frame, None)
    assert frame.stack.pop() == 4.0


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


def test_login_show_entry_guard_falls_through_on_first_select():
    """showServerListEntry's re-entry guard, the construct behind the live
    Login fingerprint shift when this lattice landed
    (weapon-Rescripted_Serverlist.txt:559, compiled as OP_THIS;
    OP_TYPE_VAR "selectedserver"; OP_MEMBER_ACCESS; OP_TYPE_VAR "entry";
    OP_EQ; OP_IF -- gs2test-verified):

        if (this.selectedserver == entry) return;   // entry = a CSV row string

    First call ever: `this.selectedserver` is unset.  resolveObjectMember
    with create=false returns no property and no var on a dynamic-member
    miss (TScriptMachine.cpp:5290-5308), so resolve() terminates at Number
    0.0 (asm _ZN17TScriptStackEntry7resolveEP14TScriptMachine.s_decomped:
    both slots null -> type=1, value=0) and the compare is 0.0 vs
    strtofloat(row) = -1.0: NOT equal, the guard falls through and the
    entry pane (Map tab, Serverlist_Map, Connect button) builds -- which is
    what the official machine does.  The PRE-lattice `to_num`-both-sides
    rule made this spuriously EQUAL (early return, nothing built), so the
    recorded Login behaviour baseline was locking in a bug-masked shape;
    it was re-baselined when this landed, not "fixed" back."""
    vm, frame = _vm_frame()
    vm.this = GS2Object("weapon:-Rescripted/Serverlist")   # no members set
    row = '"Kingdoms of Amitopia","U Kingdoms of Amitopia",0'

    vm._op_this(frame, None)
    frame.stack.append("selectedserver")
    vm._op_member_access(frame, None)
    frame.stack.append(row)
    vm._op_eq(frame, None)
    taken = frame.stack.pop()
    assert taken is False            # guard NOT taken: pane builds

    # second call with the same entry: STRING vs STRING, casecmp equal,
    # guard taken -- the actual purpose of the line.
    vm.this.set("selectedserver", row)
    vm._op_this(frame, None)
    frame.stack.append("selectedserver")
    vm._op_member_access(frame, None)
    frame.stack.append(row)
    vm._op_eq(frame, None)
    assert frame.stack.pop() is True
