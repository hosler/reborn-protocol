"""Regression tests for GS1 interpreter bugs found vs GServer-v2 upstream.

Each case cites the GServer-v2 commit that fixed the reference C++
implementation (server/src/scripting/gs1/); see the corresponding fix in
reborn_protocol/gs1/interp.py / runtime.py for the exact upstream text.
"""
import pytest

from reborn_protocol.gs1.interp import run, Interpreter, _keycode
from reborn_protocol.gs1 import parse
from reborn_protocol.gs1.lexer import IDENTIFIER, MESSAGECODE, tokenize as lex
from reborn_protocol.gs1.runtime import MemoryHost


def probe(ctx, expr):
    return Interpreter(ctx).eval(parse(expr + ";").body[0].expr)


def this_value(ctx, name):
    return ctx.vars.get("this", name)


# -- quoted tokenizer and custom delimiter behavior (a167a592) ------------
def test_tokenize_preserves_quoted_text_and_splits_standard_delimiters():
    ctx = run('setstring test,This, is "A, test" string; tokenize #s(test); '
              'this.count=tokenscount; setstring this.t0,#t(0); '
              'setstring this.t1,#t(1); setstring this.t2,#t(2); '
              'setstring this.t3,#t(3);')
    assert this_value(ctx, "count") == 4.0
    assert [this_value(ctx, f"t{i}") for i in range(4)] == [
        "This", "is", "A, test", "string",
    ]


def test_tokenize2_uses_delimiters_then_text_and_preserves_quotes():
    ctx = run('setstring test,This, is "A, test" string; '
              'tokenize2 i,#s(test); this.count=tokenscount; '
              'setstring this.t0,#t(0); setstring this.t1,#t(1); '
              'setstring this.t2,#t(2); setstring this.t3,#t(3); '
              'setstring this.t4,#t(4); setstring this.t5,#t(5);')
    assert this_value(ctx, "count") == 6.0
    assert [this_value(ctx, f"t{i}") for i in range(6)] == [
        "Th", "s", "s", "A, test", "str", "ng",
    ]


def test_tokenize_unmatched_quote_consumes_remainder_and_suppresses_gaps():
    ctx = run('tokenize one,,, "two, three; this.count=tokenscount; '
              'setstring this.t0,#t(0); setstring this.t1,#t(1);')
    assert this_value(ctx, "count") == 2.0
    assert this_value(ctx, "t0") == "one"
    assert this_value(ctx, "t1") == "two, three"


# -- keycode returns Windows virtual-key codes (6785583d) -----------------
@pytest.mark.parametrize(("key", "expected"), [
    ("a", 0x41), ("Ztail", 0x5A), ("5", 0x35), (" ", 0x20),
    (";", 0xBA), (":", 0xBA), ("+", 0xBB), ("<", 0xBC),
    ("_", 0xBD), (">", 0xBE), ("?", 0xBF), ("~", 0xC0),
    ("{", 0xDB), ("\\", 0xDC), ("}", 0xDD), ("'", 0xDE),
    ("@", 0), ("", 0),
])
def test_keycode_virtual_key_mapping(key, expected):
    assert _keycode([key]) == expected


def test_keycode_builtin_uses_only_first_character():
    assert probe(run(""), "keycode(atail)") == 0x41


# -- doubled-hash escape (dd213040) ---------------------------------------
def test_hash_escape_in_expression_condition_context():
    tokens = lex("if (## == ##) { this.ok=1; }")
    hashes = [token for token in tokens if token.text == "#"]
    assert [token.type for token in hashes] == [IDENTIFIER, IDENTIFIER]
    assert this_value(run("if (## == ##) { this.ok=1; }"), "ok") == 1.0


def test_hash_escape_in_raw_say2_string():
    host = MemoryHost()
    run("say2 alpha##beta;", host=host)
    assert host.log == [("say2", ["alpha#beta"])]


def test_hash_escape_in_setstring_value():
    ctx = run("setstring this.value,alpha##beta;")
    assert this_value(ctx, "value") == "alpha#beta"


def test_hash_escape_adjacent_to_real_message_code():
    class MessageHost(MemoryHost):
        def message_code(self, code, args, ctx):
            return "expanded" if code == "#c" else ""

    tokens = lex("setstring this.value,###c;")
    hash_tokens = [token for token in tokens if token.text.startswith("#")]
    assert [(token.type, token.text) for token in hash_tokens] == [
        ("STRING", "#"),
        (MESSAGECODE, "#c"),
    ]
    ctx = run("setstring this.value,###c;", host=MessageHost())
    assert this_value(ctx, "value") == "#expanded"


def test_weapon_message_codes_route_index_and_bare_selection_to_host():
    class WeaponHost(MemoryHost):
        def __init__(self):
            super().__init__()
            self.calls = []

        def weapon_message_code(self, code, index, ctx):
            self.calls.append((code, index))
            return f"{code}:{index}"

    host = WeaponHost()
    ctx = run("setstring this.a,#w; setstring this.b,#W(2.9);", host=host)
    assert this_value(ctx, "a") == "#w:None"
    assert this_value(ctx, "b") == "#W:2"
    assert host.calls == [("#w", None), ("#W", 2)]


def test_dynamic_player_prop_in_captured_weapon_construct():
    class PlayerPropHost(MemoryHost):
        def __init__(self):
            super().__init__()
            self.reads = []

        def message_code(self, code, args, ctx):
            self.reads.append(code)
            return "live-value"

    host = PlayerPropHost()
    source = ("if (!strequals(#P(10),0)) {\xa7"
              "setplayerprop #P9,;\xa7setplayerprop #P(10),;\xa7}")
    run(source, host=host)
    assert host.reads == ["#P10"]
    assert host.log == [
        ("setplayerprop", ["#P9", ""]),
        ("setplayerprop", ["#P10", ""]),
    ]


def test_memory_host_weapon_codes_keep_empty_default():
    ctx = run("setstring this.a,#w; setstring this.b,#W(99);")
    assert this_value(ctx, "a") == ""
    assert this_value(ctx, "b") == ""


# -- getdir(dx, dy) is angle-based, not a cardinal snap (GServer-v2 9e759e9d) -
def test_getdir_cardinals():
    ctx = run("this.left=getdir(-1,0); this.right=getdir(1,0);")
    assert probe(ctx, "this.left") == 1.0
    assert probe(ctx, "this.right") == 3.0


def test_getdir_pure_vertical():
    # (0,-1)->0 up, (0,1)->2 down: GServer's docs table and, since 68019d91,
    # its code (the older 9e759e9d code mis-guarded this case and returned 3).
    ctx = run("this.up=getdir(0,-1); this.down=getdir(0,1);")
    assert probe(ctx, "this.up") == 0.0
    assert probe(ctx, "this.down") == 2.0


def test_getdir_diagonals_biased_up_and_down():
    # Previously getdir(1,-1)/getdir(-1,-1) fell through the cardinal-only
    # snap straight to the "down" default (2.0) instead of "up" (0.0).
    ctx = run("this.ne=getdir(1,-1); this.nw=getdir(-1,-1); "
              "this.se=getdir(1,1); this.sw=getdir(-1,1);")
    assert probe(ctx, "this.ne") == 0.0   # up-right -> biased up
    assert probe(ctx, "this.nw") == 0.0   # up-left  -> biased up
    assert probe(ctx, "this.se") == 2.0   # down-right -> biased down
    assert probe(ctx, "this.sw") == 2.0   # down-left  -> biased down


def test_getdir_zero_delta_is_up():
    # 68019d91 (origin/beta4 GS1Functions.cpp fn_getdir): |dy| >= |dx| and
    # dy is zero -> 0 (up). Was 3 under the old atan2 form.
    ctx = run("this.d=getdir(0,0);")
    assert probe(ctx, "this.d") == 0.0


def test_getdir_vectors_from_68019d91():
    cases = [((0, -1), 0), ((-1, 0), 1), ((0, 1), 2), ((1, 0), 3),
             ((0.5, -0.5), 0), ((-0.5, -0.5), 0), ((0.5, 0.5), 2),
             ((-0.5, 0.5), 2), ((0.5, 0.4), 3), ((0, 0), 0),
             ((1e-17, 0), 0), ((1, 1), 2), ((1, -1), 0)]
    for (dx, dy), want in cases:
        ctx = run(f"this.d=getdir({dx},{dy});")
        assert probe(ctx, "this.d") == float(want), (dx, dy)


# -- `%` is floored on the int64-truncated operands (c3f6d8bf) ----------------
def test_modulo_floored_vectors_from_c3f6d8bf():
    cases = [("-1%5", 4), ("7%-5", -3), ("-7%3", 2), ("-7%-5", -2),
             ("7%-3", -2), ("7.9%3", 1), ("-7.9%3", 2), ("-0.5%5", 0),
             ("-4.5%5", 1), ("1.5%5", 1), ("-5%5", 0), ("23%8", 7),
             ("5%0", 0)]
    for expr, want in cases:
        ctx = run(f"this.m={expr};")
        assert probe(ctx, "this.m") == float(want), expr


def test_compound_modulo_is_floored_too():
    # Deliberate deviation: origin/beta4 left `%=` on the old truncated rule
    # (GS1Visitor.cpp OP_ASSIGN_MOD); we use the floored rule for both
    # operators (GS2 OP_MOD, the reference client's only modulus, is floored:
    # Preagonal/FourPlay/quattroplay/src/TScriptMachine.cpp:3091).
    ctx = run("this.x=10; this.x %= 3; this.y=-1; this.y %= 5;")
    assert probe(ctx, "this.x") == 1.0
    assert probe(ctx, "this.y") == 4.0


# -- negative values in an array literal (84319f0d) --------------------------
def test_array_literal_with_negative_values():
    # Catch_tests GS1_tests.cpp "arithmetic/array": size()==11, at(3)==-1.
    ctx = run("this.nv = {1,1,0,-1,-1,0,1,1,0,-1,-1};"
              "this.n = arraylen(this.nv); this.e = this.nv[3];")
    assert probe(ctx, "this.n") == 11.0
    assert probe(ctx, "this.e") == -1.0


# -- setarray on an existing array resizes, preserving contents (f6803352) --
def test_setarray_grow_preserves_existing_values():
    ctx = run("this.arr = {1,2,3}; setarray this.arr, 5;")
    assert probe(ctx, "this.arr") == [1.0, 2.0, 3.0, 0.0, 0.0]


def test_setarray_shrink_truncates():
    ctx = run("this.arr = {1,2,3}; setarray this.arr, 2;")
    assert probe(ctx, "this.arr") == [1.0, 2.0]


def test_setarray_negative_size_clamps_to_empty():
    ctx = run("this.arr = {1,2,3}; setarray this.arr, -2;")
    assert probe(ctx, "this.arr") == []


def test_setarray_on_unset_var_creates_fresh_zero_array():
    ctx = run("setarray this.arr, 3;")
    assert probe(ctx, "this.arr") == [0.0, 0.0, 0.0]


def test_setarray_on_scalar_var_replaces_with_fresh_zero_array():
    # The existing value isn't an array (fn_setarray's var->has<vector<double>>
    # check is false), so it's just replaced rather than "preserved".
    ctx = run("this.arr = 5; setarray this.arr, 3;")
    assert probe(ctx, "this.arr") == [0.0, 0.0, 0.0]


# -- reserved constants pi / allstats / allfeatures (66813d8b) --------------
def test_pi_constant():
    ctx = run("this.a = pi;")
    assert abs(probe(ctx, "this.a") - 3.14159265358979) < 1e-9


def test_sin_of_pi_over_2_is_one():
    # NOTE a real GServer-v2 build resolves 'pi' as a bare flag (-> 0.0)
    # rather than the constant when it appears *inside a function call's
    # argument list*, so sin(pi/2) is actually sin(0) == 0 upstream (see
    # tests/test_gs1_oracle.py's KNOWN_UPSTREAM_BUGS["pi-in-call"], and its
    # oracle case). We deliberately resolve 'pi' everywhere a bare reference
    # can appear -- this is the documented, requested behavior for this port
    # and is friendlier to corpus scripts than replicating that lexer-mode
    # quirk.
    ctx = run("this.a = sin(pi/2);")
    assert abs(probe(ctx, "this.a") - 1.0) < 1e-9


def test_allstats_and_allfeatures_constants():
    ctx = run("this.a = allstats; this.b = allfeatures;")
    assert probe(ctx, "this.a") == 65535.0
    assert probe(ctx, "this.b") == 65535.0


def test_bare_assignment_to_reserved_constant_is_ignored():
    # Upstream rejects this at parse time ("reserved keyword ... cannot be
    # used as an identifier"); this port ignores the write instead of
    # raising, but 'pi' must keep reading back as the constant either way.
    ctx = run("pi = 3; this.a = pi;")
    assert abs(probe(ctx, "this.a") - 3.14159265358979) < 1e-9


def test_scoped_variable_named_pi_is_a_normal_variable():
    # Only the BARE, unscoped name is reserved -- this.pi is an ordinary
    # this-scoped variable, unrelated to the pi constant.
    ctx = run("this.pi = 99;")
    assert probe(ctx, "this.pi") == 99.0


# -- single-word `elseif` keyword (live GTA scripts) ----------------------
def test_elseif_keyword_selects_correct_branch():
    ctx = run('this.x=2; if (this.x==1) { this.r=1; } '
              'elseif (this.x==2) { this.r=2; } '
              'elseif (this.x==3) { this.r=3; } '
              'else { this.r=4; }')
    assert this_value(ctx, "r") == 2.0


def test_elseif_does_not_shift_brace_accounting():
    # An unknown-identifier `elseif` used to leave its `{` dangling, so
    # every later statement attached to the wrong block. The trailing
    # assignment must remain a separate top-level statement that runs.
    src = ('if (this.a==1) { this.r=1; } '
           'elseif (this.b==1) { this.r=2; } '
           'this.done=1;')
    prog = parse(src)
    assert len(prog.body) == 2
    ctx = run(src)
    assert this_value(ctx, "done") == 1.0


# -- exclusive range closers with arithmetic bounds (live GTA *Clock) -----
def test_range_exclusive_upper_bound_with_arithmetic():
    ctx = run('this.a=0; this.y=5; this.r=0; this.r2=0; '
              'if (this.y in |this.a,this.a+10>) this.r=1; '
              'if (this.y in |this.a,this.a+5>) this.r2=1; '
              'this.done=1;')
    assert this_value(ctx, "r") == 1.0      # 5 in [0,10) -> true
    assert this_value(ctx, "r2") == 0.0     # 5 in [0,5) -> false
    assert this_value(ctx, "done") == 1.0   # statement not dropped


# -- #s/#I first argument defaults to client flags (d5b61a2d) -------------
# Oracle: GServer-v2 origin/beta4 server/src/scripting/gs1/GS1MessageCodes.cpp
# :180-215 (flagProcessingMessageCodes = #I, #s; expectingFlag cleared after
# the first packaged argument) with GS1Visitor.cpp:1647 (`!storage &&
# expectingFlag` -> CLIENT storage). Before d5b61a2d the flag stayed set for
# the #I index too, so an unprefixed index variable was read from client flags.
def _eval_msg(ctx, expr):
    return Interpreter(ctx).eval(parse(expr + ";").body[0].expr)


def _ctx_with(client=None, this=None, bare=None):
    from reborn_protocol.gs1.runtime import Context, VarStore
    ctx = Context(MemoryHost(), VarStore(player_flags=dict(bare or {})))
    ctx.vars.scopes["client"].update(client or {})
    ctx.vars.scopes["this"].update(this or {})
    return ctx


@pytest.mark.parametrize("expr,expected", [
    ("#s(mylist)", "zero,one,two"),           # unprefixed -> client
    ("#s(client.mylist)", "zero,one,two"),
    ("#s(this.mylist)", "wrong,list"),        # explicit prefix kept
    ("#I(mylist,1)", "one"),
    ("#I(this.mylist,1)", "list"),
])
def test_message_code_first_argument_scope(expr, expected):
    ctx = _ctx_with(client={"mylist": "zero,one,two"},
                    this={"mylist": "wrong,list"})
    assert to_str_(_eval_msg(ctx, expr)) == expected


def test_message_code_index_is_ordinary_not_client_flag():
    ctx = _ctx_with(client={"mylist": "zero,one,two", "i": "2"},
                    bare={"i": 1.0})
    assert _eval_msg(ctx, "#I(mylist,i)") == "one"


def test_message_code_unset_client_flag_falls_back_to_bare_store():
    ctx = _ctx_with(bare={"mylist": "a,b"})
    assert _eval_msg(ctx, "#s(mylist)") == "a,b"
    assert _eval_msg(ctx, "#s(missing)") == ""


def to_str_(v):
    from reborn_protocol.gs1.values import to_str
    return to_str(v)
