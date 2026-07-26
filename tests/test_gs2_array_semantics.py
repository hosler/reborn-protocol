"""Official array index/size conversion (quattroplay TScriptMachine).

Two runtime rules recovered from the reversed interpreter, both of which the
VM used to get wrong:

1. Every index-taking machine method converts its stack double with
   `floor(v + DOUBLE_00402440)` where DOUBLE_00402440 = 0.0001
   (TInitStatics.cpp:1003) -- add the epsilon, truncate toward zero, then
   subtract 1 when the source was negative and the truncation was inexact.
   Plain `int()` truncation loses an index whenever floating-point error
   leaves a computed index a hair below the integer it should be.
   Sites: getArrayCell (caseD_83), setArrayCell (caseD_84), getArrayCell2
   (caseD_85), setArrayCell2 (caseD_86), initArray (caseD_26), setArray
   (caseD_27), arrayDelete (caseD_89), arrayReplace (caseD_8b), arrayInsert
   (caseD_8c), charAt (caseD_72), subString (caseD_73), expandArray
   (caseD_8e). subArray (caseD_87) is the one exception.

2. Allocation is clamped into [0, 0x2710] (10000) by initArray, setArray and
   expandArray -- `new[50000]` yields 10000 cells on the real client.

Opcode-number <-> machine-method mapping above is from the case labels in
Preagonal/FourPlay/quattroplay/asm/TScriptMachine/
_ZN14TScriptMachine13executeScriptEv.s.
"""
from __future__ import annotations

from reborn_protocol.gs2 import GS2VM, GS2Object
from reborn_protocol.gs2.container import GS2Container
from reborn_protocol.gs2.values import VarRef
from reborn_protocol.gs2.vm import (
    MAX_ARRAY_SIZE, _Frame, array_index, array_size,
)


def _vm_frame():
    return GS2VM(GS2Container()), _Frame(0, [])


# --- the conversion itself -------------------------------------------------


def test_array_index_is_epsilon_floor_not_truncation():
    assert array_index(2.0) == 2
    assert array_index(2.9999999999) == 3     # int() would say 2
    assert array_index(2.9) == 2              # epsilon is 1e-4, not rounding
    assert array_index(2.99995) == 3
    assert array_index("3") == 3
    # negatives floor (the machine's cvttsd2si + `sub $0x1` fixup), they do
    # not truncate toward zero
    assert array_index(-0.5) == -1
    assert array_index(-1.0) == -1
    assert array_index(-1.5) == -2
    # NaN/inf can't reach cvttsd2si meaningfully; never raise
    assert array_index(float("nan")) == 0
    assert array_index(float("inf")) == 0


def test_array_size_clamps_to_the_official_ceiling():
    assert array_size(-5) == 0
    assert array_size(0) == 0
    assert array_size(9.99999999) == 10
    assert array_size(MAX_ARRAY_SIZE) == MAX_ARRAY_SIZE
    assert array_size(MAX_ARRAY_SIZE + 1) == MAX_ARRAY_SIZE
    assert array_size(50000) == MAX_ARRAY_SIZE


# --- reads / writes --------------------------------------------------------


def test_op_array_read_uses_epsilon_floor():
    vm, frame = _vm_frame()
    arr = ["a", "b", "c", "d"]
    frame.stack.extend([arr, 2.9999999999])
    vm._op_array(frame, None)
    assert vm.deref(frame.stack.pop(), frame) == "d"

    frame.stack.extend([arr, 2.5])
    vm._op_array(frame, None)
    assert vm.deref(frame.stack.pop(), frame) == "c"


def test_op_array_assign_uses_epsilon_floor():
    vm, frame = _vm_frame()
    arr = [0.0, 0.0, 0.0]
    frame.stack.extend([arr, 1.99999999999, "hit"])
    vm._op_array_assign(frame, None)
    assert arr == [0.0, 0.0, "hit"]


def test_op_array_multidim_uses_epsilon_floor_on_both_indices():
    vm, frame = _vm_frame()
    grid = [[0.0, 0.0], [0.0, "target"]]
    frame.stack.extend([grid, 0.99999999999, 1.0])
    vm._op_array_multidim(frame, None)
    assert frame.stack.pop() == "target"

    frame.stack.extend([grid, 1.0, 0.99999999999])
    vm._op_array_multidim(frame, None)
    assert frame.stack.pop() == "target"


def test_op_array_multidim_pops_exactly_two_indices():
    """The compiler emits ONE OP_ARRAY_MULTIDIM for `a[i, j, k]` too
    (ast.h:277 -> EXPR_MULTIARRAY for any index list longer than 1), but
    getArrayCell2 walks the stack link exactly twice and does
    `sub $0x2,%eax` (:28) on the entry count: it takes the top two entries
    as the indices and the THIRD as the base. For `a[i, j, k]` that means
    `i` is read as the base (so the read yields nothing) and `a` itself is
    left behind on the stack -- the real client mis-executes a 3-index read
    the same way. Locked in so nobody "generalises" this to N dimensions."""
    vm, frame = _vm_frame()
    cube = [[["deep"]]]
    frame.stack.extend([cube, 0.0, 0.0, 0.0])   # a[i, j, k]
    vm._op_array_multidim(frame, None)
    assert frame.stack.pop() is None            # base was the index `i`
    assert frame.stack == [cube]                # `a` never consumed

    # exactly two indices is the supported shape
    grid = [[0.0, 0.0], [0.0, "cell"]]
    frame.stack[:] = [grid, 1.0, 1.0]
    vm._op_array_multidim(frame, None)
    assert frame.stack == ["cell"]


def test_op_array_multidim_assign_index_order_and_epsilon():
    vm, frame = _vm_frame()
    grid = [[0.0, 0.0], [0.0, 0.0]]
    # stack [obj, i, j, value] (setArrayCell2 skips the value, then i, j)
    frame.stack.extend([grid, 0.0, 1.99999999999, "v"])
    vm._op_array_multidim_assign(frame, None)
    assert grid == [[0.0, 0.0, "v"], [0.0, 0.0]]


# --- allocation ------------------------------------------------------------


def test_op_array_new_clamps_and_epsilon_floors():
    vm, frame = _vm_frame()
    frame.stack.append(3.99999999)
    vm._op_array_new(frame, None)
    assert frame.stack.pop() == [0.0, 0.0, 0.0, 0.0]

    frame.stack.append(50000)
    vm._op_array_new(frame, None)
    assert len(frame.stack.pop()) == MAX_ARRAY_SIZE

    frame.stack.append(-3)
    vm._op_array_new(frame, None)
    assert frame.stack.pop() == []


def test_op_setarray_clamps_to_the_official_ceiling():
    vm, frame = _vm_frame()
    frame.stack.extend([VarRef("data"), 50000])
    vm._op_setarray(frame, None)
    assert len(vm.globals["data"]) == MAX_ARRAY_SIZE

    frame.stack.extend([VarRef("data"), 2.9999999999])
    vm._op_setarray(frame, None)
    assert vm.globals["data"] == [0.0, 0.0, 0.0]


def test_op_array_new_multidim_clamps_subarray_size():
    vm, frame = _vm_frame()
    outer = [0.0, 0.0]
    frame.stack.extend([outer, 50000])
    vm._op_array_new_multidim(frame, None)
    assert [len(row) for row in outer] == [MAX_ARRAY_SIZE, MAX_ARRAY_SIZE]


# --- string / list index ops sharing the conversion ------------------------


def test_charat_and_substr_use_epsilon_floor():
    vm, frame = _vm_frame()
    frame.stack.extend(["reborn", 2.9999999999])
    vm._op_obj_charat(frame, None)
    assert frame.stack.pop() == "o"

    # stack [s, start, length]
    frame.stack.extend(["reborn", 1.9999999999, 2.9999999999])
    vm._op_obj_substr(frame, None)
    assert frame.stack.pop() == "bor"


def test_list_index_ops_use_epsilon_floor():
    vm, frame = _vm_frame()

    arr = ["a", "b", "c"]
    frame.stack.extend([arr, 1.9999999999])
    vm._op_obj_deletestring(frame, None)
    assert arr == ["a", "b"]

    # replace/insert take [obj, value, index] (CMD_REVERSE_ARGS)
    frame.stack.extend([arr, "z", 0.9999999999])
    vm._op_obj_replacestring(frame, None)
    assert arr == ["a", "z"]

    frame.stack.extend([arr, "q", 0.9999999999])
    vm._op_obj_insertstring(frame, None)
    assert arr == ["a", "q", "z"]


def test_subarray_floors_both_bounds_with_epsilon():
    """subArray is NOT an exception to floorScriptIndex.

    An earlier comment claimed its asm carried no DOUBLE_00402440 and the
    code truncated instead; the decompiled interpreter refutes it --
    subArray() floors BOTH bounds, TScriptMachine.cpp:1844 (length) and
    :1848 (start). So 1.9999999999 means "start at 2", not 1.
    """
    vm, frame = _vm_frame()
    # stack [length, start, obj]
    frame.stack.extend([2, 1.9999999999, ["a", "b", "c", "d"]])
    vm._op_obj_subarray(frame, None)
    assert frame.stack.pop() == ["c", "d"]


# --- object vs null: regression for the Login serverlist outage -------------

def test_object_never_equals_null():
    """`findweapon(x) == null` must be FALSE when x WAS found.

    Regression: gs2_compare's object branch fell through to a numeric compare,
    and to_num() is 0.0 for both a GS2Object and None, so a found weapon
    compared equal to null. Login's -Rescripted/IRC/Login3 does

        temp.wep = findweapon("-Rescripted/Serverlist");
        if (temp.wep == null) { ... }
        if (temp.wep != null && !isObject("Serverlist_Panel"))
          temp.wep.initServerlist();

    so it skipped initServerlist() and built no GUI at all -- silently, with
    zero VM warnings. The official compare() DOES have an object/number row
    (TScriptMachine.cpp:1465, :1480) -- it just compares the object's POINTER
    as a double (objectPointerAsDouble, :46-49), not the object's value, so a
    live object is never equal to 0. See test_gs2_value_lattice for the full
    table this now falls out of.
    """
    from reborn_protocol.gs2.values import GS2Object, gs2_compare, gs2_eq

    obj = GS2Object("weapon:-Rescripted/Serverlist")
    assert gs2_eq(obj, None) is False
    assert gs2_eq(None, obj) is False
    assert gs2_compare(obj, None) != 0
    assert gs2_compare(None, obj) != 0
    # ...and the sign stays consistent when the operands swap.
    assert gs2_compare(obj, None) == -gs2_compare(None, obj)

    # A number is not an object either (to_num(obj) is also 0.0).
    assert gs2_eq(obj, 0.0) is False
    assert gs2_eq(obj, 0) is False

    # The two rules the oracle DOES specify still hold.
    assert gs2_eq(obj, obj) is True
    assert gs2_eq(obj, "WEAPON:-RESCRIPTED/SERVERLIST") is True
    assert gs2_eq(None, None) is True
