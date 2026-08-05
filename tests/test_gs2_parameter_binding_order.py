"""Hand-assembled regressions for script-function parameter binding.

THE convention (one, shared by the official compiler and gs2test): every
argument list and every parameter list is pushed in REVERSE source /
declaration order. Pop order is therefore source order at call sites and
declaration order in the prologue, and OP_FUNC_PARAMS_END binds a plain
positional zip.

Oracle: the decompiled reference client. massTempAssign
(Preagonal/FourPlay/quattroplay/src/TScriptMachine.cpp:4001-4116) pairs the
topmost name entry with the topmost argument entry, walking both down --
with both lists reverse-pushed, that top-aligned pairing is exactly
declaration-order-to-source-order. The external entry agrees:
prepareScriptFunctionExecution pushes the bottom Array marker
(TScriptMachine.cpp:2117-2137) and stackAddTokens pushes params[n-1..0] so
params[0] pops first (TScriptMachine.cpp:3814-3826).

Proof of the reverse push, from real bytecode:

- gs2test compiling `function CheckTiles(cdir, ani, act)` emits the
  prologue VAR act; VAR ani; VAR cdir, and the call site
  `CheckTiles(1, 5, "slash")` pushes "slash", 5, 1.
- The Zelda: A Link to the Past server's -Player/Movement weapon (official
  compiler) declares `function onKeyPressed(code, key)` -- the canonical
  engine event signature -- and its prologue carries VAR 'Key' then
  VAR 'code'.

Two prologue styles encode the SAME order rule: bare VarRefs (gs2test and
some live weapons) and temp.<name> LValue references (OP_TEMP; OP_TYPE_VAR;
OP_MEMBER_ACCESS -- most live Login-server weapons, including Login
Mobile's UI weapons). A reversal applied to only one style inverted every
internal call in temp-style weapons and collapsed Login Mobile's mobile
serverlist UI on 2026-08-05 while all other suites stayed green.
"""

import struct

from reborn_protocol.gs2 import GS2Container, FunctionEntry, GS2VM, Op


def _op(opnum, value=None):
    if value is None:
        return bytes([opnum])
    return bytes([opnum, 0xF3]) + struct.pack(">b", value)


def _indexed(opnum, index):
    return bytes([opnum, 0xF0, index])


def _params(*indexes):
    """Prologue for params declared in `indexes` order (temp.<name> style).

    Both compilers push the parameter references in REVERSE declaration
    order, so emit them reversed here.
    """
    return (_op(Op.OP_TYPE_ARRAY)
            + b"".join(_op(Op.OP_TEMP) + _indexed(Op.OP_TYPE_VAR, i)
                       + _op(Op.OP_MEMBER_ACCESS) for i in reversed(indexes))
            + _op(Op.OP_FUNC_PARAMS_END) + _op(Op.OP_JMP))


def _bare_params(*indexes):
    """Prologue for params declared in `indexes` order (bare VarRef style,
    the shape of LTTP's `-Player/Movement` CheckTiles prologue)."""
    return (_op(Op.OP_TYPE_ARRAY)
            + b"".join(_indexed(Op.OP_TYPE_VAR, i) for i in reversed(indexes))
            + _op(Op.OP_FUNC_PARAMS_END) + _op(Op.OP_JMP))


def _call(target_index, *arguments):
    # Call expressions push arguments in reverse source order (both
    # compilers; verified against gs2test output and the LTTP weapon's
    # triggerserver/CheckTiles call sites).
    return (_op(Op.OP_TYPE_ARRAY) + b"".join(reversed(arguments))
            + _indexed(Op.OP_TYPE_VAR, target_index) + _op(Op.OP_CALL))


def _number(value):
    return _op(Op.OP_TYPE_NUMBER, value)


def _container(functions):
    strings = ["first", "second", "third", "return_first", "return_second",
               "return_third", "g", "nested", "array_value"]
    code = bytearray()
    entries = []
    instruction_index = 0
    from reborn_protocol.gs2 import decode
    for name, body in functions:
        entries.append(FunctionEntry(name, instruction_index))
        code.extend(body)
        instruction_index += len(decode(body))
    return GS2Container(functions=entries, strings=strings, code=bytes(code))


def test_three_parameters_bind_in_declaration_order():
    declarations = _params(0, 1, 2)
    vm = GS2VM(_container([
        ("return_first", declarations + _indexed(Op.OP_TYPE_VAR, 0) + _op(Op.OP_RET)),
        ("return_second", declarations + _indexed(Op.OP_TYPE_VAR, 1) + _op(Op.OP_RET)),
        ("return_third", declarations + _indexed(Op.OP_TYPE_VAR, 2) + _op(Op.OP_RET)),
    ]))

    assert vm.call("return_first", 1, 2, 3) == 1
    assert vm.call("return_second", 1, 2, 3) == 2
    assert vm.call("return_third", 1, 2, 3) == 3


def test_bare_varref_parameters_bind_in_declaration_order():
    # The LTTP CheckTiles shape: OP_TYPE_ARRAY; VAR <last>..VAR <first>;
    # OP_FUNC_PARAMS_END, no temp. references.
    declarations = _bare_params(0, 1, 2)
    vm = GS2VM(_container([
        ("return_first", declarations + _indexed(Op.OP_TYPE_VAR, 0) + _op(Op.OP_RET)),
        ("return_third", declarations + _indexed(Op.OP_TYPE_VAR, 2) + _op(Op.OP_RET)),
    ]))

    assert vm.call("return_first", 1, 2, 3) == 1
    assert vm.call("return_third", 1, 2, 3) == 3


def test_internal_call_binds_like_external_call():
    # Login Mobile's regression path: bytecode-to-bytecode calls inside
    # temp.<name>-style weapons. The call site's reverse-pushed args must
    # meet the prologue's reverse-pushed names as a plain zip; reversing
    # only the LValue name list inverted these while vm.call() stayed
    # correct.
    body = (_params()
            + _call(3, _number(7), _number(8), _number(9))
            + _op(Op.OP_RET))
    for prologue in (_params(0, 1, 2), _bare_params(0, 1, 2)):
        vm = GS2VM(_container([
            ("return_first", prologue + _indexed(Op.OP_TYPE_VAR, 0) + _op(Op.OP_RET)),
            ("nested", body),
        ]))
        assert vm.call("nested") == 7


def test_argument_count_mismatches_do_not_shift_parameters():
    declarations = _params(0, 1, 2)
    numeric_second = (_indexed(Op.OP_TYPE_VAR, 1) + _number(0)
                      + _op(Op.OP_ADD) + _op(Op.OP_RET))
    vm = GS2VM(_container([
        ("return_first", declarations + _indexed(Op.OP_TYPE_VAR, 0) + _op(Op.OP_RET)),
        ("return_second", declarations + numeric_second),
        ("return_third", declarations + _indexed(Op.OP_TYPE_VAR, 2) + _op(Op.OP_RET)),
    ]))

    assert vm.call("return_first", 7) == 7
    assert vm.call("return_second", 7) == 0
    assert vm.call("return_third", 1, 2, 3, 4, 5) == 3


def test_type_array_argument_is_preserved():
    vm = GS2VM(_container([
        ("array_value", _params(0) + _indexed(Op.OP_TYPE_VAR, 0) + _op(Op.OP_RET)),
    ]))
    value = [1.0, 2.0]
    assert vm.call("array_value", value) == value


def test_login_mobile_clamp_shape():
    """The exact shape captured live from Login Mobile on 2026-08-05.

    Weapon blob 53db7ee997... carries, at byte 0x05AD of its code segment:

        17                  OP_TYPE_ARRAY
        bd 16 f0 7e 23      OP_TEMP; OP_TYPE_VAR 'max_val'; OP_MEMBER_ACCESS
        bd 16 f0 7f 23      OP_TEMP; OP_TYPE_VAR 'min_val'; OP_MEMBER_ACCESS
        bd 16 f0 80 23      OP_TEMP; OP_TYPE_VAR 'val';     OP_MEMBER_ACCESS
        33 0a               OP_FUNC_PARAMS_END; OP_JMP

    i.e. `function clamp(val, min_val, max_val)` with the refs pushed in
    reverse declaration order. Its internal call site pushes 3, 2,
    temp.scale for the source call `clamp(temp.scale, 2, 3)` -- reverse
    source order. Plain pop-order zip binds val=scale, min_val=2,
    max_val=3. The LValue-only reversal shipped on 2026-08-04 bound
    max_val=scale / val=3 instead, and Login Mobile's UI construction
    (which scales every control through clamp) collapsed 33/33 -> 29/33.
    """
    strings = ["clamp", "val", "min_val", "max_val"]
    prologue = (_op(Op.OP_TYPE_ARRAY)
                + _op(Op.OP_TEMP) + _indexed(Op.OP_TYPE_VAR, 3) + _op(Op.OP_MEMBER_ACCESS)
                + _op(Op.OP_TEMP) + _indexed(Op.OP_TYPE_VAR, 2) + _op(Op.OP_MEMBER_ACCESS)
                + _op(Op.OP_TEMP) + _indexed(Op.OP_TYPE_VAR, 1) + _op(Op.OP_MEMBER_ACCESS)
                + _op(Op.OP_FUNC_PARAMS_END) + _op(Op.OP_JMP))
    # return val * 100 + min_val * 10 + max_val, to observe all three slots
    body = (_indexed(Op.OP_TYPE_VAR, 1) + _op(Op.OP_TYPE_NUMBER, 100) + _op(Op.OP_MUL)
            + _indexed(Op.OP_TYPE_VAR, 2) + _op(Op.OP_TYPE_NUMBER, 10) + _op(Op.OP_MUL)
            + _op(Op.OP_ADD)
            + _indexed(Op.OP_TYPE_VAR, 3) + _op(Op.OP_ADD) + _op(Op.OP_RET))
    caller = (_op(Op.OP_TYPE_ARRAY) + _op(Op.OP_FUNC_PARAMS_END) + _op(Op.OP_JMP)
              # clamp(1, 2, 3): args pushed 3, 2, 1 (reverse source order,
              # the captured call-site shape)
              + _op(Op.OP_TYPE_ARRAY)
              + _op(Op.OP_TYPE_NUMBER, 3) + _op(Op.OP_TYPE_NUMBER, 2)
              + _op(Op.OP_TYPE_NUMBER, 1)
              + _indexed(Op.OP_TYPE_VAR, 0) + _op(Op.OP_CALL)
              + _op(Op.OP_RET))

    from reborn_protocol.gs2 import decode
    code = bytearray()
    entries = []
    idx = 0
    for name, chunk in (("clamp", prologue + body), ("caller", caller)):
        entries.append(FunctionEntry(name, idx))
        code.extend(chunk)
        idx += len(decode(chunk))
    vm = GS2VM(GS2Container(functions=entries, strings=strings,
                            code=bytes(code)))

    # val=1, min_val=2, max_val=3, both externally and through the
    # captured internal call-site shape.
    assert vm.call("clamp", 1, 2, 3) == 123
    assert vm.call("caller") == 123


def test_nested_call_binds_inner_result_to_first_parameter():
    nested_body = (_params()
                   + _call(3, _call(6, _number(1)), _number(2))
                   + _op(Op.OP_RET))
    vm = GS2VM(_container([
        ("return_first", _params(0, 1) + _indexed(Op.OP_TYPE_VAR, 0) + _op(Op.OP_RET)),
        ("g", _params(0) + _indexed(Op.OP_TYPE_VAR, 0) + _op(Op.OP_RET)),
        ("nested", nested_body),
    ]))

    assert vm.call("nested") == 1
