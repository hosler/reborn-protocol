"""The `a,b in |x,y|` lookahead must rewind, not blow up the enclosing block.

parse_in speculatively reads a comma-separated value list because GS1 lets ALL
of `2,3` be range-tested at once. Those commas usually belong to somebody else
(a command's argument list, an array literal), so the speculation is supposed
to rewind. It didn't when the speculative parse RAISED: an array literal with
an empty trailing slot (`{,-1,}` — live Bomber room0.nw's furniture catalog)
ends in `,}`, so the lookahead consumed the comma and ran parse_exponent onto
the '}'. The escaping ParseError put the statement into panic recovery, which
backs onto that '}' and offers it to parse_block as the ENCLOSING block's
closing brace: the rest of the if-body was re-parsed at top level and ran
UNCONDITIONALLY.
"""
import pytest

from reborn_protocol.gs1 import ast, parse
from reborn_protocol.gs1.interp import run_event
from reborn_protocol.gs1.lexer import tokenize
from reborn_protocol.gs1.parser import Parser
from reborn_protocol.gs1.runtime import UNSET


def _parse(src):
    p = Parser(tokenize(src))
    return p.parse_program(), p.errors


GOOD = 'if (created) { this.off={0,-1,0}; this.x=5; this.y=6; }'
EMPTY_SLOT = 'if (created) { this.off={,-1,};   this.x=5; this.y=6; }'


@pytest.mark.parametrize("src", [GOOD, EMPTY_SLOT])
def test_array_literal_keeps_the_if_body_intact(src):
    prog, errors = _parse(src)
    assert errors == []
    assert len(prog.body) == 1                      # not 3: nothing leaked out
    assert isinstance(prog.body[0], ast.If)
    assert len(prog.body[0].then) == 3


def test_the_truncated_body_no_longer_runs_unconditionally():
    # `created` never fires here, so nothing in the body may take effect.
    ctx = run_event(EMPTY_SLOT, "playerenters")
    assert ctx.vars.get("this", "x") is UNSET
    assert ctx.vars.get("this", "y") is UNSET


@pytest.mark.parametrize(("expr", "expected"), [
    ("2,3 in |1,4|", True),          # every value must be in range
    ("2,5 in |1,4|", False),
    ("47 in {47,49}", True),         # membership in an array literal
    ("48 in {47,49}", False),
    ("3 in |1,3>", False),           # exclusive upper bound
    ("3 in |1,4>", True),
])
def test_in_forms_still_parse_and_evaluate(expr, expected):
    ctx = run_event("if (created) { this.r = (%s); }" % expr, "created")
    assert bool(ctx.vars.get("this", "r")) is expected


def test_in_list_over_variables_still_binds_every_value():
    prog, errors = _parse("this.r = a,b in |x,y|;")
    assert errors == []
    node = prog.body[0].value
    assert isinstance(node, ast.InExpr)
    assert len(node.values) == 2


# --- panic recovery must take the failed statement's block with it -----------
#
# Live classic Bomber (bomber.eevul.net:14916, bomblobby.nw, captured
# 2026-07-26) mangles every block comment on the way out: the server eats the
# '/' of the opening '/*' and deletes the closing '*/' outright, so an NPC
# whose author disabled a chunk of code arrives at the client as
#
#     *if(playerenters || timeout) { ...the disabled body... }
#
# The stray '*' fails at statement start. Recovery used to stop at the first
# ';' INSIDE the block, which left the '{' dangling and re-parsed the disabled
# body at the enclosing level - so NPC 50's blackhole effect (seteffect, 55
# showimg layers and a self-rearming `timeout = 0.05`) ran unconditionally in
# the lobby, and the block's own '}' surfaced as a second, bogus error.

MANGLED_BLOCK_COMMENT = (
    "*if(playerenters||timeout){\n"
    "timereverywhere;\n"
    "showimg 300,light2.png,x-2.8,y-1.6;\n"
    "timeout = 0.05;\n"
    "}"
)


def test_mangled_block_comment_is_skipped_whole():
    prog, errors = _parse(MANGLED_BLOCK_COMMENT)
    assert len(errors) == 1                          # the '*', and only that
    assert "OP_MUL" in str(errors[0])
    assert prog.body == []                           # nothing leaked to top level


def test_statement_after_a_mangled_block_still_parses():
    prog, errors = _parse(MANGLED_BLOCK_COMMENT + "\nthis.after = 1;")
    assert len(errors) == 1
    assert len(prog.body) == 1
    assert isinstance(prog.body[0], ast.Assign)


def test_disabled_body_does_not_execute():
    # The event the mangled head names DOES fire here; the body must still be
    # inert, because the author commented the whole construct out.
    ctx = run_event("*if(playerenters){ this.hit = 1; }", "playerenters")
    assert ctx.vars.get("this", "hit") is UNSET


def test_recovery_does_not_close_the_enclosing_block_early():
    prog, errors = _parse(
        "if (created) { *bad{x;} this.a = 1; }\nthis.b = 2;")
    assert errors                                    # the '*' is still reported
    assert len(prog.body) == 2                       # if + the trailing assign
    assert isinstance(prog.body[0], ast.If)
    assert isinstance(prog.body[1], ast.Assign)


# The lexer's comment rules are NOT what broke here: GS1's unquoted
# slash-strings (`startswith(/play,#c)`) live in string-argument modes, which
# never skip comments, so they cannot collide with the '/*' opener.
@pytest.mark.parametrize("src", [
    "if (startswith(/play,#c)) { this.a=1; }",
    "if (startswith(/stream,#c)) { this.a=1; } // trailing\nthis.b=2;",
    "if (created) { this.a=1; }\n/* a block\n comment */\nthis.b=2;",
    "if (playerchats) {\n/*\n  if (startswith(/play,#c)) {\n"
    "    setcharprop #P1,x;\n  }\n*/\n  if(startswith(/stop,#c))"
    " { setcharprop #P1,; }\n}",
])
def test_slash_strings_and_block_comments_coexist(src):
    _prog, errors = _parse(src)
    assert errors == []


if __name__ == '__main__':
    pytest.main([__file__, '-v'])


# -- 2gta live-content tolerance wave (2026-07-27) ---------------------------
# Every case below is lifted from a script the live GTA server actually
# serves; each used to drop statements (or the whole script) and now parses.

def test_double_semicolon_before_else_binds_the_else():
    # weapon *Quiver: `if (c) stmt;; else stmt;`
    prog, errors = _parse("if (a>1) this.x=1;; else this.x=2;\nthis.y=3;")
    assert errors == []
    assert isinstance(prog.body[0], ast.If) and prog.body[0].els is not None
    assert len(prog.body) == 2


def test_stray_semicolon_without_else_is_inert():
    prog, errors = _parse("if (a>1) this.x=1;;\nthis.y=3;")
    assert errors == []
    assert prog.body[0].els is None
    assert len(prog.body) == 2


def test_stray_toplevel_closing_brace_is_skipped_silently():
    # weapon -System3 ends in one extra '}'
    prog, errors = _parse("this.x=1;\n}\nthis.y=2;")
    assert errors == []
    assert len(prog.body) == 2


def test_member_named_like_a_builtin_function_lexes_as_property():
    # weapons *Staff Tool / Grog: `sin(this.sin)` — '.sin' is a property
    ctx = run_event("this.sin=1; this.out = sin(this.sin);", None)
    import math
    assert abs(ctx.vars.scopes["this"]["out"] - math.sin(1)) < 1e-9


def test_colon_equals_assignment():
    # weapon Space Storm is written entirely with ':='
    ctx = run_event("this.a := 3; this.b := this.a + 1;", None)
    assert ctx.vars.scopes["this"]["b"] == 4.0


def test_break_call_form_is_tolerated():
    # weapon -magic/Effect: `break();`
    ctx = run_event(
        "for (this.i=0; this.i<5; this.i++) { if (this.i==2) break(); }",
        None)
    assert ctx.vars.scopes["this"]["i"] == 2.0


def test_array_literal_assignment_without_semicolon():
    # weapon -BarrelRide: `this.x={...}` newline `this.y={...}`, no ';'
    ctx = run_event("this.x={1,0,3}\nthis.y={4,5,6}\nthis.z=this.x[2]+this.y[1];",
                    None)
    assert ctx.vars.scopes["this"]["z"] == 8.0


def test_function_call_inside_array_literal_arg():
    # weapon Poly Trail: `showpoly this.a,{random(...),...};` — the nested
    # FUNCTION state must pop at its own ')' inside the literal
    prog, errors = _parse(
        "showpoly this.a,{random(playerx+0.5,playerx+2.5),"
        "random(playery+1,playery+3)};\nthis.q=1;")
    assert errors == []
    assert len(prog.body) == 2


def test_parenthesized_text_in_function_string_arg():
    # weapon Vampire Bite: `!strcontains(#n(this.i),(paused))` — the '('
    # inside the string arg must not let the inner ')' close the call
    prog, errors = _parse(
        "if (!strcontains(#n(0),(paused)) && this.a>0) this.x=1;\nthis.y=2;")
    assert errors == []
    assert isinstance(prog.body[0], ast.If)
    assert len(prog.body) == 2


def test_messagecode_arg_keeps_first_paren_pop_quirk():
    # gs2emu oracle lock: #E(...) ends at the FIRST ')' regardless of nesting
    # (tests/gs1_oracle_corpus.cases `base64encode` keeps a literal ')')
    prog, errors = _parse("setstring this.e, #E(base64encode(#s(this.h)));")
    assert errors == []


def test_junk_char_drops_one_statement_not_the_script():
    # weapon -GC Droppables2: a literal '\' typo inside one line
    prog, errors = _parse("this.a=1;\nboxy = {1\\0,10};\nthis.b=2;")
    assert len(errors) <= 1
    names = [s.target.parts[0].name for s in prog.body
             if isinstance(s, ast.Assign)]
    assert "a" in [n.split('.')[-1] for n in names] or len(prog.body) >= 2
