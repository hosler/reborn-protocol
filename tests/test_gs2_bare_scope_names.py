"""Bare `this` / `thiso` / `temp` arriving as plain variable NAMES.

A live server's compiler emits `Gs2Utils = this;` as
`OP_TYPE_VAR 'Gs2Utils'; OP_TYPE_VAR 'this'; OP_ASSIGN` -- `this` is a variable
name, not OP_THIS. The reference resolves those names ahead of every
local/global/member lookup: "this" -> the current this-object
(Preagonal/FourPlay/quattroplay/src/TScriptMachine.cpp:5084), "thiso" -> the
script's original object (:5093), "temp" -> the call scope (:5070). Writing
to the bare name never rebinds it: assign() writes into the resolved scope
object (:5367-5419).

The streams below are hand-assembled (opcode numbers from opcodes.py, same
layout the compiler emits, see test_gs2_vm.py::test_register_cache_*).
"""
from __future__ import annotations

from reborn_protocol.gs2 import GS2VM, GS2Host
from reborn_protocol.gs2.container import FunctionEntry, GS2Container
from reborn_protocol.gs2.values import VarRef
from reborn_protocol.gs2.vm import _Frame

STR_THIS, STR_G, STR_A, STR_GOT, STR_R, STR_PUB, STR_THISO, STR_TEMP, STR_X = range(9)
STRINGS = ["this", "G", "a", "got", "r", "pub", "thiso", "temp", "x"]

PROLOGUE = [0x17, 0x33, 0x0A]          # OP_TYPE_ARRAY, OP_FUNC_PARAMS_END, OP_JMP
OP_CALL, OP_RET, OP_ARRAY, OP_ASSIGN = 0x06, 0x07, 0x17, 0x32
OP_MEMBER, OP_CONV_OBJ, OP_THIS = 0x23, 0x24, 0xB4


def var(idx):
    return [0x16, 0xF0, idx]               # OP_TYPE_VAR [idx]


def num(n):
    return [0x14, 0xF3, n]                 # OP_TYPE_NUMBER n


def _build():
    """provider: publish (bare 'this'), publisho (bare 'thiso'),
    publishthis (OP_THIS), public pub(a), assignthis; caller: go()."""
    return [
        ("publish", [], var(STR_G) + var(STR_THIS) + [OP_ASSIGN] + num(0) + [OP_RET]),
        ("publisho", [], var(STR_G) + var(STR_THISO) + [OP_ASSIGN] + num(0) + [OP_RET]),
        ("publishthis", [], var(STR_G) + [OP_THIS, OP_ASSIGN] + num(0) + [OP_RET]),
        ("public.pub", [STR_A],
         [OP_THIS] + var(STR_GOT) + [OP_MEMBER] + var(STR_A) + [OP_ASSIGN]
         + num(7) + [OP_RET]),
        ("go", [],
         [OP_THIS] + var(STR_R) + [OP_MEMBER, OP_ARRAY] + num(5) + var(STR_G)
         + [OP_CONV_OBJ] + var(STR_PUB) + [OP_MEMBER, OP_CALL, OP_ASSIGN]
         + num(0) + [OP_RET]),
        ("assignthis", [],
         var(STR_THIS) + num(5) + [OP_ASSIGN]
         + var(STR_THISO) + num(6) + [OP_ASSIGN]
         + [OP_THIS] + var(STR_X) + [OP_MEMBER] + num(1) + [OP_ASSIGN]
         + num(0) + [OP_RET]),
    ]


def _assemble(bodies):
    from reborn_protocol.gs2.disasm import decode
    code = bytearray()
    funcs = []
    for name, params, body in bodies:
        # op_index is an instruction index: decode what we have so far
        funcs.append((name, len(decode(bytes(code))) if code else 0))
        code += bytes(PROLOGUE[:1])
        for p in params:
            code += bytes(var(p))
        code += bytes(PROLOGUE[1:]) + bytes(body)
    return GS2Container(functions=[FunctionEntry(name=n, op_index=i) for n, i in funcs],
                        strings=list(STRINGS), code=bytes(code))


class _SharedHost(GS2Host):
    def __init__(self):
        self.globals = {}

    def get_globals(self):
        return self.globals

    def get_object(self, name):
        return None

    def create_object(self, classname, arg):
        return None

    def sleep(self, vm, seconds):
        pass


def _vms():
    host = _SharedHost()
    c = _assemble(_build())
    return host, GS2VM(c, name="provider", host=host), GS2VM(c, name="caller", host=host)


def test_type_var_this_publishes_current_this_and_public_call_reaches_it():
    host, provider, caller = _vms()
    provider.call("publish")
    assert host.globals.get("g") is provider.this
    caller.call("go")
    assert provider.this.get("got") == 5.0
    assert caller.this.get("r") == 7.0


def test_type_var_thiso_publishes_original_object():
    host, provider, caller = _vms()
    provider.call("publisho")
    assert host.globals.get("g") is provider.thiso
    caller.call("go")
    assert provider.this.get("got") == 5.0


def test_op_this_form_still_works():
    host, provider, caller = _vms()
    provider.call("publishthis")
    assert host.globals.get("g") is provider.this
    caller.call("go")
    assert provider.this.get("got") == 5.0
    assert caller.this.get("r") == 7.0


def test_bare_this_follows_with_target_like_op_this():
    """TScriptMachine.cpp:3605 rebinds `this` under with(); the name form
    must agree with OP_THIS."""
    from reborn_protocol.gs2 import GS2Object
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    inner = GS2Object(name="inner")
    frame.with_stack.append(inner)
    assert vm._lookup("this", frame) is inner
    assert vm._lookup("thiso", frame) is vm.thiso
    # the reference compares byte-exact: a capitalised name is a variable
    assert vm._lookup("This", frame) is None


def test_scope_names_beat_variables_of_the_same_name():
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    vm.globals["this"] = 9
    vm.this.set("thiso", 9)
    frame.temps.set("temp", 9)
    assert vm._lookup("this", frame) is vm.this
    assert vm._lookup("thiso", frame) is vm.thiso
    assert vm._lookup("temp", frame) is frame.temps


def test_assigning_to_bare_scope_names_does_not_rebind_or_shadow():
    host, provider, caller = _vms()
    provider.call("assignthis")
    assert "this" not in provider.globals and "thiso" not in provider.globals
    assert not provider.this.has("this") and not provider.this.has("thiso")
    assert provider.this.get("x") == 1.0          # `this.x = 1` still lands
    frame = _Frame(0, [])
    provider._assign_name("temp", 3, frame)
    assert not frame.temps.has("temp")
    assert provider._lookup("temp", frame) is frame.temps


def test_varref_deref_goes_through_the_same_rule():
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    assert vm.deref(VarRef("this"), frame) is vm.this


def test_case_variants_are_ordinary_variables():
    """The reference compares byte-exact (TString.cpp:849-860); `Temp`,
    `This`, `THIS`, `thisO` fall through to the normal folded variable path."""
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    for i, nm in enumerate(("Temp", "This", "THIS", "thisO")):
        vm._assign_name(nm, 5 + i, frame)
        assert vm._lookup(nm, frame) == 5 + i


def test_bare_temp_write_is_dropped_and_read_gives_scope():
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    vm._assign_name("temp", 5, frame)
    assert vm._lookup("temp", frame) is frame.temps
    assert not frame.temps.has("temp")


def test_function_parameter_named_temp_reads_as_scope_object():
    c = _assemble([("f", [STR_TEMP], var(STR_TEMP) + [OP_RET])])
    vm = GS2VM(c, name="p", host=_SharedHost())
    res = vm.call("f", 5)
    from reborn_protocol.gs2 import GS2Object
    assert isinstance(res, GS2Object)       # the scope, not the argument
    assert res.get("temp") == 5             # ...which holds the parameter


def test_thiso_stays_original_inside_with_and_nested_calls():
    from reborn_protocol.gs2 import GS2Object
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    frame.with_stack.append(GS2Object(name="w1"))
    frame.with_stack.append(GS2Object(name="w2"))
    assert vm._lookup("thiso", frame) is vm.thiso
    nested = _Frame(0, [])                   # a called function's frame
    nested.with_stack.append(GS2Object(name="w3"))
    assert vm._lookup("thiso", nested) is vm.thiso


def test_dotted_this_head_follows_with_target():
    from reborn_protocol.gs2 import GS2Object
    vm = GS2VM(GS2Container())
    frame = _Frame(0, [])
    inner = GS2Object(name="inner")
    inner.set("k", 4)
    vm.this.set("k", 1)
    frame.with_stack.append(inner)
    assert vm._lookup("this.k", frame) == 4
    assert vm._lookup("thiso.k", frame) == 1
