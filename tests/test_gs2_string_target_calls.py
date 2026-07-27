"""Method calls on a string LITERAL target: `"-Serverlist_Options".showOptions()`.

The official machine converts the literal through name resolution
(TScriptStackEntry::makeProperty's String case resolves the VALUE as a name,
so an installed weapon registered in universe vars answers), while a string
held in a PROPERTY does NOT name-resolve -- switchTypeObject reads the
property's object slot and falls back to CSV-tokens-or-null
(TScriptStackEntry.cpp:299-353). Login's whole start menu drives other
weapons through the literal shape (weapon-Rescripted_Serverlist.txt:
2938-2957).

The VM's part of the contract: a literal that resolves gets the object; a
literal that does NOT resolve rides along as the LValue base so OP_CALL can
hand the NAME to the host (whose client-install stand-in may fetch the
weapon); a property-held string stays a dead reference.
"""
from __future__ import annotations

import os
import subprocess
import tempfile

import pytest

from reborn_protocol.gs2 import GS2VM, GS2Host, GS2Object
from reborn_protocol.gs2.vm import NOT_HANDLED

GS2TEST = os.environ.get("GS2TEST_BIN") or os.path.join(
    os.path.dirname(__file__), "tools", "gs2test")

CALLER_SRC = """
function callLiteral() {
  "-Serverlist_Options".showOptions("arg1");
}

function callProperty() {
  temp.w = "-Serverlist_Options";
  temp.w.showOptions("arg1");
}
"""

TARGET_SRC = """
public function showOptions(a) {
  this.opened = a;
  return 1;
}
"""


def _compile(source: str) -> bytes:
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "s.gs2")
        with open(src, "w") as fh:
            fh.write(source)
        subprocess.run([GS2TEST, src], cwd=d, check=True,
                       capture_output=True)
        with open(os.path.join(d, "s.gs2bc"), "rb") as fh:
            return fh.read()


class _Host(GS2Host):
    """Records obj-method calls; resolves one registered object name."""

    def __init__(self):
        self.globals = {}
        self.objects = {}
        self.method_calls = []

    def get_globals(self):
        return self.globals

    def get_object(self, name):
        return self.objects.get(name.lower())

    def create_object(self, classname, arg):
        return None

    def sleep(self, vm, seconds):
        pass

    def call_builtin(self, vm, name, args, obj=None):
        if obj is not None:
            self.method_calls.append((name, obj))
        return NOT_HANDLED


needs_compiler = pytest.mark.skipif(
    not os.path.exists(GS2TEST),
    reason="gs2test compiler not built (tests/tools/build_gs2test.sh)")


@needs_compiler
def test_resolved_literal_dispatches_to_the_named_objects_script():
    host = _Host()
    target = GS2VM(_compile(TARGET_SRC), name="target", host=host)
    host.objects["-serverlist_options"] = target.this
    caller = GS2VM(_compile(CALLER_SRC), name="caller", host=host)
    caller.call("callLiteral")
    assert target.this.get("opened") == "arg1"


@needs_compiler
def test_unresolved_literal_reaches_the_host_with_the_name_as_obj():
    """The host is the client-install stand-in: it must SEE the name to be
    able to fetch the weapon, so the unresolved literal survives member
    access as the call base instead of collapsing to a dead reference."""
    host = _Host()
    caller = GS2VM(_compile(CALLER_SRC), name="caller", host=host)
    caller.call("callLiteral")
    assert ("showoptions", "-Serverlist_Options") in host.method_calls


@needs_compiler
def test_property_held_string_stays_a_dead_reference():
    """switchTypeObject only name-resolves LITERALS: through a property the
    same call must neither resolve nor reach the host with the name."""
    host = _Host()
    target = GS2VM(_compile(TARGET_SRC), name="target", host=host)
    host.objects["-serverlist_options"] = target.this
    caller = GS2VM(_compile(CALLER_SRC), name="caller", host=host)
    caller.call("callProperty")
    assert target.this.get("opened") is None
    assert all(not isinstance(obj, str) for _n, obj in host.method_calls)
