"""Root-variable methods called on a string held in a VARIABLE.

`temp.s.savestring(file, mode)` writes the variable's own string. The
reference registers savestring (and the other 43 root methods) on its root
variable class, so every variable answers them with ITSELF as the receiver
(quattroplay/src, the variable-object property table, funcDefs lines
215-602; the saveString body, lines 530-551, reads the variable's own value;
TScriptMachine::functionCall, TScriptMachine.cpp:5514, takes the variable as
the call target whatever its value type). A name outside that table is looked up only in an object's property table / own
vars and then dropped (TScriptMachine.cpp:5283-5322), so it stays dead for a
plain string. A string LITERAL is separate: it is name-resolved to a weapon
(TScriptStackEntry::makeProperty), a slot value never is (switchTypeObject,
TScriptStackEntry.cpp:299-353).

Precedence implemented in GS2VM._op_member_access: object/array base first
(own members win), then a literal receiver (any name reaches the host), then
a slot-held string only for the root-variable method names, else dead.
"""
from __future__ import annotations

import os

import pytest

from reborn_protocol.gs2 import GS2VM, GS2Host, GS2Object, GS2VarString
from reborn_protocol.gs2.vm import NOT_HANDLED

from test_gs2_string_target_calls import GS2TEST, _compile

needs_compiler = pytest.mark.skipif(
    not os.path.exists(GS2TEST),
    reason="gs2test compiler not built (tests/tools/build_gs2test.sh)")

SRC = """
function ret() { return "abc"; }

function viaLiteral()  { "abc".savestring("f", 1); }
function viaTemp()     { temp.s = "abc"; temp.s.savestring("f", 2); }
function viaThisProp() { this.t = "abc"; this.t.savestring("f", 3); }
function viaElement()  { temp.a = {"abc", "def"}; temp.a[1].savestring("f", 4); }
function viaCall()     { ret().savestring("f", 5); }
function viaObjProp()  {
  temp.o = new TStaticVar();
  temp.o.x = "abc";
  temp.o.x.savestring("f", 6);
}
function viaObject()   { temp.o = new TStaticVar(); temp.o.savestring("f", 7); }
function notRootName() { temp.s = "abc"; temp.s.showOptions("a"); }
function strLen()      { temp.s = "abc"; return temp.s.length() + 0; }
function trimCase()    { temp.s = "  ab "; return temp.s.trim(); }
function startsCase()  { temp.s = "abc"; return temp.s.starts("ab") + 0; }
function sub()         { temp.s = "abcdef"; return temp.s.substring(1, 2); }
function numRecv()     { temp.n = 5; temp.n.savestring("f", 8); }
function nullRecv()    { temp.z = null; temp.z.savestring("f", 9); }
function emptyRecv()   { temp.e = ""; temp.e.savestring("f", 10); }
function strAddarray() { temp.s = "x"; return temp.s.addarray(1); }
function objAddarray() { temp.o = new TStaticVar(); return temp.o.addarray(1); }
function trig()        { temp.o = new TStaticVar(); temp.o.trigger("onx"); }
function ownMember()   { temp.o = new TStaticVar(); return temp.o.mine(5); }
"""


class _Host(GS2Host):
    def __init__(self):
        self.globals = {}
        self.calls = []

    def get_globals(self):
        return self.globals

    def get_object(self, name):
        return None

    def create_object(self, classname, arg):
        return GS2Object(name=classname)

    def sleep(self, vm, seconds):
        pass

    def call_builtin(self, vm, name, args, obj=None):
        if obj is not None:
            self.calls.append((name, obj, list(args)))
        return NOT_HANDLED


def _run(fn):
    host = _Host()
    vm = GS2VM(_compile(SRC), name="c", host=host)
    result = vm.call(fn)
    return host, result


@needs_compiler
@pytest.mark.parametrize("fn,mode,value", [
    ("viaLiteral", 1.0, "abc"),
    ("viaTemp", 2.0, "abc"),
    ("viaThisProp", 3.0, "abc"),
    ("viaElement", 4.0, "def"),
    ("viaCall", 5.0, "abc"),
    ("viaObjProp", 6.0, "abc"),
])
def test_string_receiver_reaches_host_in_every_form(fn, mode, value):
    host, _ = _run(fn)
    assert host.calls == [("savestring", value, ["f", mode])]


@needs_compiler
def test_slot_held_receiver_is_tagged_but_literal_is_not():
    """A host needs to tell a slot value (never a weapon name) from a
    literal (name-resolved) -- the tag is the only difference."""
    lit, _ = _run("viaLiteral")
    var, _ = _run("viaTemp")
    assert not isinstance(lit.calls[0][1], GS2VarString)
    assert isinstance(var.calls[0][1], GS2VarString)


@needs_compiler
def test_slot_held_string_with_a_non_root_method_name_stays_dead():
    host, _ = _run("notRootName")
    assert host.calls == []


@needs_compiler
def test_object_receiver_is_unchanged_and_reaches_host_as_the_object():
    host, _ = _run("viaObject")
    assert len(host.calls) == 1
    name, obj, _args = host.calls[0]
    assert name == "savestring" and isinstance(obj, GS2Object)


@needs_compiler
def test_object_own_member_still_wins_over_host():
    host = _Host()
    vm = GS2VM(_compile(SRC), name="c", host=host)
    created = []

    def create(classname, arg):
        o = GS2Object(name=classname)
        o.set("mine", lambda n: n + 1.0)
        created.append(o)
        return o

    host.create_object = create
    assert vm.call("ownMember") == 6.0
    assert host.calls == []


@needs_compiler
def test_existing_string_methods_are_unchanged():
    _, n = _run("strLen")
    assert n == 3.0
    _, s = _run("sub")
    assert s == "bc"
    host, t = _run("trimCase")
    assert t == "ab" and host.calls == []
    host, st = _run("startsCase")
    assert st == 1.0 and host.calls == []


@needs_compiler
def test_number_receiver_reaches_host_as_its_string_form():
    """The reference target is the variable itself whatever it holds
    (TScriptMachine.cpp:5514), and saveString reads it as a string."""
    host, _ = _run("numRecv")
    assert host.calls == [("savestring", "5", ["f", 8.0])]
    assert isinstance(host.calls[0][1], GS2VarString)


@needs_compiler
def test_empty_string_receiver_reaches_host():
    host, _ = _run("emptyRecv")
    assert host.calls == [("savestring", "", ["f", 10.0])]


@needs_compiler
def test_null_receiver_stays_dead():
    """Assigned-null and never-assigned look alike to the VM, and an unset
    name resolves to the caller's own object in the reference (:5533), not
    to a receiver value."""
    host, _ = _run("nullRecv")
    assert host.calls == []


@needs_compiler
def test_string_receiver_does_not_hit_the_object_only_addarray_noop():
    """addarray / sortbyvalue answer None only for real objects and lists;
    a string falls through to the host (-> dead 0.0 here)."""
    host, r = _run("strAddarray")
    assert r == 0.0
    assert host.calls == [("addarray", "x", [1.0])]
    host, r = _run("objAddarray")
    assert r is None


@needs_compiler
def test_trigger_refused_by_host_is_asked_once():
    host, _ = _run("trig")
    assert [c[0] for c in host.calls].count("trigger") == 1
