"""`onAction = function(){...}` / `x = function(){...}; x();` resolution.

An anonymous function expression is not a value in the bytecode: the
compiler hoists the body into a public script function with a generated
name and emits a READ of `this.<that name>` in its place
(GS2CompilerVisitor.cpp:970-994 -> OP_THIS; OP_TYPE_VAR; OP_MEMBER_ACCESS;
OP_CONV_TO_OBJECT; name from ParserContext::generateLambdaFuncName,
Parser.cpp:133). The VM resolves that read against its own function table
(_ScriptFnRef -> GS2ScriptFunction) so the assignment stores something a
host can actually call.
"""
from __future__ import annotations

import os

from reborn_protocol.gs2 import GS2VM, GS2Host, GS2Object, GS2ScriptFunction
from reborn_protocol.gs2.container import GS2Container
from reborn_protocol.gs2.values import LValue, VarRef
from reborn_protocol.gs2.vm import _Frame

LAMBDA_BYTECODE = os.path.join(
    os.path.dirname(__file__), "fixtures", "gs2_baselines", "functions",
    "03_lambdas.bytecode")


def _member_read(vm: GS2VM, frame: _Frame, base, name: str):
    """Replay `<base>.<name>` (OP_MEMBER_ACCESS) and deref the result."""
    frame.stack.extend([base, VarRef(name)])
    vm._op_member_access(frame, None)
    return vm.deref(frame.stack.pop(), frame)


def test_lambda_bytecode_calls_reach_the_hoisted_functions():
    """End-to-end on real gs2parser output: 03_lambdas.gs2 assigns three
    lambdas into temps and calls them, and passes two more as callbacks.
    Every one of those calls used to land in builtins_missing ("unknown
    method lambda1()") because the member read returned None."""
    vm = GS2VM(open(LAMBDA_BYTECODE, "rb").read(), name="lambdas")
    assert "public.function_100_1" in {f.name for f in vm.container.functions}

    GS2VM.reset_coverage()
    vm.call("testLambdas")
    vm.call("testHigherOrder")

    assert GS2VM.builtins_missing == {}
    for name in ("lambda1", "lambda2", "lambda3", "callback", "func"):
        assert GS2VM.builtins_called.get(name), name


def test_this_read_of_generated_name_yields_a_callable():
    vm = GS2VM(open(LAMBDA_BYTECODE, "rb").read(), name="lambdas")
    frame = _Frame(0, [])

    fn = _member_read(vm, frame, vm.this, "function_100_1")
    assert isinstance(fn, GS2ScriptFunction)
    assert fn() == "hello from lambda"

    # function(x, y) { return x + y; }
    add = _member_read(vm, frame, vm.this, "function_101_1")
    assert add(5, 10) == 15


def test_script_function_public_api():
    vm = GS2VM(open(LAMBDA_BYTECODE, "rb").read(), name="lambdas")
    assert vm.script_function("nosuchthing") is None
    fn = vm.script_function("higherOrderFunction")
    assert isinstance(fn, GS2ScriptFunction)
    assert fn(lambda v: v * 3, 4) == 12
    # name lookup is case-insensitive, like every other function lookup
    assert vm.script_function("HIGHERORDERFUNCTION") is not None


def test_joined_class_functions_resolve_through_this():
    """has_function()/call() already recurse into joined classes, so a
    handler declared in a joined class resolves off `this` too."""
    joiner = GS2VM(GS2Container(), name="weapon")
    klass = GS2VM(open(LAMBDA_BYTECODE, "rb").read(), name="class")
    klass.this = joiner.this
    joiner.joined.append(klass)

    frame = _Frame(0, [])
    fn = _member_read(joiner, frame, joiner.this, "function_100_1")
    assert isinstance(fn, GS2ScriptFunction)
    assert fn() == "hello from lambda"


def test_stored_member_value_wins_over_the_function_fallback():
    vm = GS2VM(open(LAMBDA_BYTECODE, "rb").read(), name="lambdas")
    frame = _Frame(0, [])
    vm.this.set("function_100_1", "stored")
    assert _member_read(vm, frame, vm.this, "function_100_1") == "stored"

    # and a write through the reference lands on the object, not the table
    frame.stack.extend([vm.this, VarRef("function_101_1")])
    vm._op_member_access(frame, None)
    ref = frame.stack.pop()
    ref.set(7)
    assert vm.this.get("function_101_1") == 7


def test_fallback_only_applies_to_the_current_this_object():
    """A miss on an unrelated object stays None -- `player.onCreated` must
    not start resolving to the script's own onCreated."""
    other = GS2Object(name="player")

    class _Host(GS2Host):
        def get_object(self, name):
            return other if name == "player" else None

        def get_globals(self):
            return {}

    vm = GS2VM(open(LAMBDA_BYTECODE, "rb").read(), name="lambdas", host=_Host())
    frame = _Frame(0, [])
    frame.stack.extend([other, VarRef("function_100_1")])
    vm._op_member_access(frame, None)
    ref = frame.stack.pop()
    assert type(ref) is LValue
    assert ref.get() is None


def test_fallback_follows_with_block_this_rebinding():
    """Inside `new GuiButton("b") { onAction = function(){...}; }` OP_THIS
    pushes the control under construction, so the generated-name read
    happens against the control -- it has to resolve there too."""
    vm = GS2VM(open(LAMBDA_BYTECODE, "rb").read(), name="lambdas")
    ctrl = GS2Object(name="ctrl")
    frame = _Frame(0, [])
    frame.with_stack.append(ctrl)

    vm._op_this(frame, None)
    assert frame.stack[-1] is ctrl
    frame.stack.append(VarRef("function_100_1"))
    vm._op_member_access(frame, None)
    vm._op_conv_to_object(frame, None)
    # the enclosing `onAction = <that>` is an OP_ASSIGN, which derefs
    frame.stack.insert(0, LValue(ctrl, "onaction"))
    vm._op_assign(frame, None)

    handler = ctrl.get("onaction")
    assert isinstance(handler, GS2ScriptFunction)
    assert handler() == "hello from lambda"
