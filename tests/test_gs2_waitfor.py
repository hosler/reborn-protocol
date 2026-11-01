"""OP_WAITFOR delegates dereferenced arguments and retains its expression frame."""
from pathlib import Path

import pytest

from reborn_protocol.gs2 import GS2Object, GS2VM, VMCoroutineWait

VECTOR = Path(__file__).parent / "fixtures/gs2/vm/08_waitfor.gs2bc"


class Wait(VMCoroutineWait):
    def __init__(self, value):
        self.value = value

    def ready(self):
        return True

    def result(self):
        return self.value


class Host:
    def __init__(self, value, suspend=True):
        self.value = value
        self.suspend = suspend
        self.calls = []

    def get_globals(self):
        return {}

    def waitfor(self, vm, obj, event, timeout):
        self.calls.append((vm, obj, event, timeout, vm.call_is_suspendable))
        return Wait(self.value) if self.suspend else self.value


@pytest.mark.parametrize("value", [1.0, 0.0])
def test_waitfor_yields_host_wait_and_pushes_result_on_resume(value):
    host = Host(value)
    vm = GS2VM(VECTOR.read_bytes(), host=host)
    target = GS2Object()
    gen = vm.iter_call("awaitEvent", target, "ReAdY", 0.5)

    assert isinstance(next(gen), Wait)
    assert host.calls == [(vm, target, "ReAdY", 0.5, True)]
    assert vm.this.get("continued") is None
    with pytest.raises(StopIteration) as done:
        next(gen)
    assert done.value.value == value
    assert vm.this.get("result") == value
    assert vm.this.get("saved") == 7.0
    assert vm.this.get("continued") == 1.0


@pytest.mark.parametrize("value", [1.0, 0.0])
def test_waitfor_can_return_host_result_synchronously(value):
    host = Host(value, suspend=False)
    vm = GS2VM(VECTOR.read_bytes(), host=host)
    assert vm.call("awaitEvent", vm.this, "ready", 0.5) == value
    assert host.calls == [(vm, vm.this, "ready", 0.5, False)]


@pytest.mark.parametrize("host", [None, object()])
def test_waitfor_without_host_hook_returns_zero(host):
    vm = GS2VM(VECTOR.read_bytes(), host=host)
    assert vm.call("awaitEvent", vm.this, "ready", 0.5) == 0.0
