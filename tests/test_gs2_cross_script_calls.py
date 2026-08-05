"""Cross-script method calls: `<global holding another script's this>.Fn()`.

A Reborn weapon publishes itself into a bare global in onCreated and every
other script on the server then calls it through that reference:

    // weapon -Player/Functions
    function onCreated() { plfunc = this; }
    public function ModifyClientR(flag, val) { ... }

    // weapon -Player/Movement
    function onCreated() { plfunc.modifyclientr("freezetime", -1); }

(Preagonal/graal-lttp/weapons/weapon-Player_Functions.txt:33-35,83 and
weapon-Player_Movement.txt:30-31.)

The VM used to resolve that member call against the host builtin table only,
so a script function whose name collides with an engine builtin was shadowed
by the builtin — `plfunc.modifyclientr(...)` reached our inert `modifyclientr`
stub instead of the weapon's own function, and the flag round-trip it exists
to perform never happened. Only `public` functions are reachable this way,
which is the engine's own rule.
"""
from __future__ import annotations

import os
import subprocess
import tempfile

import pytest

from reborn_protocol.gs2 import GS2VM, GS2Host
from reborn_protocol.gs2.vm import NOT_HANDLED

GS2TEST = os.environ.get("GS2TEST_BIN") or os.path.join(
    os.path.dirname(__file__), "tools", "gs2test")

PROVIDER_SRC = """
function onCreated() {
  plfunc = this;
}

public function ModifyClientR(flag, val) {
  this.lastflag = flag;
  this.lastval = val;
  return 1;
}

function privateHelper() {
  return 2;
}

function onWeaponFired() {
  this.fired = 7;
  return 5;
}
"""

CALLER_SRC = """
function onCreated() {
  this.viapublic = plfunc.ModifyClientR("hearts", 3);
  this.viaprivate = plfunc.privateHelper();
}

function fireIt() {
  this.rc = plfunc.trigger("onWeaponFired", null);
}

function fireMissing() {
  this.rc = plfunc.trigger("onNoSuchEvent", null);
}
"""

CASE_COLLISION_PROVIDER_SRC = """
function onCreated() {
  Games = this;
}

public function startGame(game) {
  this.started = game;
  return 1;
}
"""

CASE_COLLISION_CALLER_SRC = """
function onCreated() {
  this.games = {"local row"};
  this.result = Games.startGame("Game_TicTacToe");
}
"""


def _compile(source: str) -> bytes:
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "s.gs2")
        with open(src, "w") as fh:
            fh.write(source)
        subprocess.run([GS2TEST, src], cwd=d, check=True,
                       capture_output=True)
        out = os.path.join(d, "s.gs2bc")
        with open(out, "rb") as fh:
            return fh.read()


class _SharedHost(GS2Host):
    """Two scripts, one globals dict — like the real client, where every
    weapon VM shares one global namespace. Records builtin calls so the test
    can prove the script function beat the builtin of the same name."""

    def __init__(self):
        self.globals = {}
        self.builtin_calls = []

    def get_globals(self):
        return self.globals

    def get_object(self, name):
        return None

    def create_object(self, classname, arg):
        return None

    def sleep(self, vm, seconds):
        pass

    def call_builtin(self, vm, name, args, obj=None):
        self.builtin_calls.append(name)
        if name in ("modifyclientr", "privatehelper"):
            return 0.0        # an inert engine stub, as in the real client
        return NOT_HANDLED


needs_compiler = pytest.mark.skipif(
    not os.path.exists(GS2TEST),
    reason="gs2test compiler not built (tests/tools/build_gs2test.sh)")


@needs_compiler
def test_public_function_reached_through_a_published_this():
    host = _SharedHost()
    provider = GS2VM(_compile(PROVIDER_SRC), name="provider", host=host)
    caller = GS2VM(_compile(CALLER_SRC), name="caller", host=host)

    provider.call("onCreated")
    assert host.globals.get("plfunc") is provider.this

    caller.call("onCreated")

    # The provider's public function ran, with the caller's arguments...
    assert provider.this.get("lastflag") == "hearts"
    assert provider.this.get("lastval") == 3.0
    assert caller.this.get("viapublic") == 1.0
    # ...instead of the same-named host builtin stub.
    assert "modifyclientr" not in host.builtin_calls


@needs_compiler
def test_published_object_name_wins_case_distinct_member_during_method_call():
    """Pin the compiler shape used by a menu calling another weapon.

    `Games.startGame(...)` emits OP_TYPE_VAR/OP_CONV_TO_OBJECT followed by
    member access and OP_CALL.  The uppercase published object must not be
    shadowed by the caller's separate lowercase `this.games` array, and the
    mixed-case public function must remain callable through its folded key.
    """
    host = _SharedHost()
    provider = GS2VM(_compile(CASE_COLLISION_PROVIDER_SRC),
                     name="provider", host=host)
    caller = GS2VM(_compile(CASE_COLLISION_CALLER_SRC),
                   name="caller", host=host)

    provider.call("onCreated")
    caller.call("onCreated")

    assert provider.this.get("started") == "Game_TicTacToe"
    assert caller.this.get("result") == 1.0
    assert "startgame" not in host.builtin_calls


@needs_compiler
def test_non_public_function_is_not_reachable_across_scripts():
    """`public` is the whole cross-script surface: a plain function stays
    private and the call still falls through to the host, exactly as before."""
    host = _SharedHost()
    provider = GS2VM(_compile(PROVIDER_SRC), name="provider", host=host)
    caller = GS2VM(_compile(CALLER_SRC), name="caller", host=host)

    provider.call("onCreated")
    caller.call("onCreated")

    assert "privatehelper" in host.builtin_calls
    assert caller.this.get("viaprivate") == 0.0


@needs_compiler
def test_trigger_fires_a_non_public_event_on_the_other_script():
    """`.trigger(event, params)` is the ENGINE's event dispatch, so unlike a
    direct call it is not gated on `public` — Zelda fires every weapon with
    `findweapon(name).trigger("onweaponfired", null)`."""
    host = _SharedHost()
    provider = GS2VM(_compile(PROVIDER_SRC), name="provider", host=host)
    caller = GS2VM(_compile(CALLER_SRC), name="caller", host=host)
    provider.call("onCreated")

    caller.call("fireIt")

    assert provider.this.get("fired") == 7.0
    assert caller.this.get("rc") == 5.0


@needs_compiler
def test_trigger_on_an_event_the_script_does_not_handle_is_inert():
    host = _SharedHost()
    provider = GS2VM(_compile(PROVIDER_SRC), name="provider", host=host)
    caller = GS2VM(_compile(CALLER_SRC), name="caller", host=host)
    provider.call("onCreated")

    GS2VM.reset_coverage()
    caller.call("fireMissing")

    assert caller.this.get("rc") == 0.0
    assert "trigger" not in GS2VM.builtins_missing


@needs_compiler
def test_own_this_calls_are_unaffected():
    """A script calling its own function through `this.` keeps taking the
    existing path (the new hop is only for OTHER scripts' objects)."""
    host = _SharedHost()
    provider = GS2VM(_compile(PROVIDER_SRC), name="provider", host=host)
    provider.call("ModifyClientR", "x", 9)
    assert provider.this.get("lastval") == 9.0
