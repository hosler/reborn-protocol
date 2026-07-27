"""era-2006 "new GS1" constructs (2026-07-27 era local bring-up hunt).

Era's clientside weapons mix classic GS1 with GS2-style syntax that the 2006
official client accepted — shipped content is the oracle here (the constructs
below are verbatim from era's -System/-MoveSystem/-BulletSystem/-EventSystem/
-HPControls clientside, which our engine previously rejected wholesale):

- function declarations WITH parameters + calls with arguments
- GS2-convention event functions (onTimeout/onPlayerEnters/onActionX)
- command call-form with quoted args / empty parens (enableweapons())
- command names as variable reads in expression position (if (replaceani))
- `@` string concat
- chained assignment (a = b = false)
- method calls on refs and call results (.tokenize()/.pos()/.add())
"""
import pytest

from reborn_protocol.gs1.parser import Parser, parse
from reborn_protocol.gs1.lexer import tokenize
from reborn_protocol.gs1.interp import Interpreter
from reborn_protocol.gs1.runtime import Context, MemoryHost


def run(src, event=None):
    ctx = Context(host=MemoryHost())
    it = Interpreter(ctx)
    prog = parse(src)
    if event is None:
        it.run(prog)
    else:
        it.run_event(prog, event)
    return ctx


def errors_of(src):
    p = Parser(tokenize(src))
    p.parse_program()
    return p.errors


def test_funcdef_params_parse_and_bind():
    ctx = run("function f(a, b) { this.sum = a + b; }\nf(3, 4);")
    assert ctx.vars.get("this", "sum") == 7.0


def test_funcdef_params_restore_globals():
    ctx = run("a = 9;\nfunction f(a) { this.inner = a; }\nf(5);\nthis.after = a;")
    assert ctx.vars.get("this", "inner") == 5.0
    assert ctx.vars.get("this", "after") == 9.0


def test_call_value_with_args():
    ctx = run("function f(a) { return; }\nx = f(2);")
    # value form must parse (returns 0.0 — GS1 user functions return nothing)
    assert ctx.vars.get(None, "x") == 0.0


def test_event_function_fires_on_event():
    ctx = run("function onTimeout() { this.fired = 1; }", event="timeout")
    assert ctx.vars.get("this", "fired") == 1.0


def test_event_function_case_insensitive():
    ctx = run("function ONPLAYERENTERS() { this.hit = 1; }", event="playerenters")
    assert ctx.vars.get("this", "hit") == 1.0


def test_classic_flag_blocks_still_fire():
    ctx = run("if (playerenters) { this.classic = 1; }", event="playerenters")
    assert ctx.vars.get("this", "classic") == 1.0


def test_command_callform_quoted_args():
    # era -System: triggeraction(0, 0, "serverside", "-System", "unequipwep");
    assert errors_of(
        'triggeraction(0, 0, "serverside", "-System", "unequipwep");') == []


def test_command_callform_variable_args():
    assert errors_of(
        'function onActionBoomerang(dmg, acct, guild)\n'
        '{\n'
        'triggeraction(0,0,"serverside","-BulletSystem","boomerang",dmg,acct,guild);\n'
        '}\n') == []


def test_command_callform_empty_parens():
    # era -System onCreated: enableweapons();
    assert errors_of('enableweapons();') == []


def test_command_space_form_unchanged():
    assert errors_of('triggeraction 0,0,serverside,-MoveSystem,carry,#p(0),#a;') == []
    assert errors_of('say2 hello world;') == []


def test_command_word_as_flag_in_condition():
    # era -MoveSystem: if (replaceani) { ... }
    assert errors_of('if (replaceani) { x = 1; }') == []
    assert errors_of('if (!replaceani && x) { x = 1; }') == []


def test_at_concat():
    ctx = run('setstring this.name,Era;\nclient.rupees = "$" @ 12;')
    assert ctx.vars.get("client", "rupees") == "$12"


def test_chained_assignment():
    ctx = run("atm = grabdisabled = onphone = 3;")
    for name in ("atm", "grabdisabled", "onphone"):
        assert ctx.vars.get(None, name) == 3.0


def test_method_tokenize_and_index():
    ctx = run('setstring clientr.jail,a b c;\n'
              'temp.jail = clientr.jail.tokenize();\n'
              'setstring this.second,#I(temp.jail,1);')
    assert ctx.vars.get("this", "second") == "b"


def test_method_pos():
    ctx = run('setstring this.n,era_gym.nw;\n'
              'if (this.n.pos("gym") > 0) { this.hit = 1; }')
    assert ctx.vars.get("this", "hit") == 1.0


def test_method_add_mutates_list_flag():
    ctx = run('client.message.add("hello");\nclient.message.add("bye");')
    assert ctx.vars.get("client", "message") == ["hello", "bye"]


def test_method_chain_on_call_result_is_inert():
    # findWeaponNPC(...) is unmodelled: the statement must parse and no-op,
    # not take the surrounding block down with it.
    assert errors_of('if (x) {\n'
                     'findWeaponNPC("-System").showFloat("*Cured!");\n'
                     'this.y = 1;\n'
                     '}\n') == []


def test_settimer_maps_to_timeout_var():
    ctx = run('setTimer(0.5);')
    assert ctx.vars.get(None, "timeout") == 0.5


def test_settimer_zero_cancels():
    ctx = run('timeout = 3;\nsetTimer(0);')
    # cancel rule: any value <= 0.0001 deactivates (TScriptSpace.cpp:121-129)
    assert ctx.vars.get(None, "timeout") == 0.0
