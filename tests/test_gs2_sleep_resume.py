from reborn_protocol.gs2 import GS2VM
from reborn_protocol.gs2.disasm import Instruction, Operand
from reborn_protocol.gs2.opcodes import Op


def _vm(ops, strings=("main", "helper")):
    vm = GS2VM(open("tests/fixtures/gs2/vm/01_arith.gs2bc", "rb").read())
    vm.strings = list(strings)
    vm.instructions = []
    for idx, item in enumerate(ops):
        op, value = item if isinstance(item, tuple) else (item, None)
        operand = (Operand("number", -1, "i32", value)
                   if value is not None else None)
        vm.instructions.append(Instruction(idx, idx, int(op), operand))
    vm.functions = {"main": 0}
    return vm


def test_sleep_suspends_with_frame_and_this_intact():
    vm = _vm([
        Op.OP_THIS, (Op.OP_TYPE_NUMBER, 0.25), Op.OP_SLEEP, Op.OP_RET,
    ])
    gen = vm.iter_call("main")

    assert next(gen) == 0.25
    assert vm.this is vm.thiso
    try:
        next(gen)
    except StopIteration as done:
        assert done.value is vm.this
    else:
        raise AssertionError("coroutine did not finish")


def test_sleep_preserves_parameters_and_temps():
    vm = _vm([
        Op.OP_TYPE_ARRAY, (Op.OP_TYPE_VAR, 2), Op.OP_FUNC_PARAMS_END,
        Op.OP_TEMP, (Op.OP_TYPE_VAR, 3), Op.OP_MEMBER_ACCESS,
        (Op.OP_TYPE_NUMBER, 2), Op.OP_ASSIGN,
        (Op.OP_TYPE_NUMBER, 0.25), Op.OP_SLEEP,
        (Op.OP_TYPE_VAR, 2), Op.OP_TEMP, (Op.OP_TYPE_VAR, 3),
        Op.OP_MEMBER_ACCESS, Op.OP_ADD, Op.OP_RET,
    ], strings=("main", "helper", "arg", "saved"))
    gen = vm.iter_call("main", 5)

    assert next(gen) == 0.25
    try:
        next(gen)
    except StopIteration as done:
        assert done.value == 7
    else:
        raise AssertionError("coroutine did not finish")


def test_nested_script_call_suspends():
    vm = _vm([
        Op.OP_TYPE_ARRAY, (Op.OP_TYPE_VAR, 1), Op.OP_CALL, Op.OP_RET,
        (Op.OP_TYPE_NUMBER, 7), (Op.OP_TYPE_NUMBER, 0.5),
        Op.OP_SLEEP, Op.OP_RET,
    ])
    vm.functions["helper"] = 4
    gen = vm.iter_call("main")

    assert next(gen) == 0.5
    try:
        next(gen)
    except StopIteration as done:
        assert done.value == 7
    else:
        raise AssertionError("coroutine did not finish")


def test_sync_call_uses_host_sleep_fallback():
    class Host:
        def __init__(self):
            self.sleeps = []

        def sleep(self, vm, seconds):
            self.sleeps.append(seconds)

        def get_globals(self):
            return {}

    vm = _vm([
        (Op.OP_TYPE_NUMBER, 8), (Op.OP_TYPE_NUMBER, 0.75),
        Op.OP_SLEEP, Op.OP_RET,
    ])
    vm.host = Host()

    assert vm.call("main") == 8
    assert vm.host.sleeps == [0.75]


def test_instruction_budget_resets_after_sleep():
    vm = _vm([
        (Op.OP_TYPE_NUMBER, 9), (Op.OP_TYPE_NUMBER, 0.1),
        Op.OP_SLEEP, Op.OP_RET,
    ])
    vm.max_ops = 3
    gen = vm.iter_call("main")

    assert next(gen) == 0.1
    try:
        next(gen)
    except StopIteration as done:
        assert done.value == 9
    else:
        raise AssertionError("coroutine did not finish")
