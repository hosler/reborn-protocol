from reborn_protocol.gs1.interp import run
from reborn_protocol.gs1.interp import Interpreter
from reborn_protocol.gs1.lexer import tokenize
from reborn_protocol.gs1.parser import Parser
from reborn_protocol.gs1.runtime import Context, MemoryHost


def _errors(source):
    parser = Parser(tokenize(source))
    parser.parse_program()
    return parser.errors


def test_hyphenated_flag_targets_and_reads():
    ctx = run(
        "setstring this.hotel-1-speakertext,Welcome;"
        "setstring this.copy,#s(this.hotel-1-speakertext);"
    )

    assert ctx.vars.get("this", "hotel-1-speakertext") == "Welcome"
    assert ctx.vars.get("this", "copy") == "Welcome"


def test_dynamic_member_read_write_compound_and_index():
    ctx = run(
        'setstring this.business,deli;'
        'DB_Businesses.("shopclosed-" @ this.business) = 3;'
        'DB_Businesses.("shopclosed-" @ this.business) += 2;'
        'this.("stage" @ 2) = {7, 8};'
        'this.result = DB_Businesses.("shopclosed-" @ this.business);'
        'this.indexed = this.("stage" @ 2)[1];'
    )

    assert ctx.vars.get(None, "DB_Businesses.shopclosed-deli") == 5.0
    assert ctx.vars.get("this", "result") == 5.0
    assert ctx.vars.get("this", "indexed") == 8.0


def test_switch_case_default_break_and_fallthrough():
    ctx = run(
        'setstring command,male;'
        'switch (command) {'
        'case "male":'
        'case "female": { this.gender = 1; break; }'
        'default: this.gender = 2;'
        '}'
        'switch (9) { case 1: this.other = 1; default: this.other = 2; }'
    )

    assert ctx.vars.get("this", "gender") == 1.0
    assert ctx.vars.get("this", "other") == 2.0


def test_call_form_accepts_nested_function_arguments():
    source = (
        'triggeraction(x, y, "moveobject", int(mousex), int(mousey));'
    )

    assert _errors(source) == []


def test_scoped_function_params_bind_and_restore():
    ctx = run(
        "temp.x = 9;"
        "function useMP(temp.x) { this.inside = temp.x; }"
        "useMP(4);"
        "this.after = temp.x;"
    )

    assert ctx.vars.get("this", "inside") == 4.0
    assert ctx.vars.get("this", "after") == 9.0


def test_function_names_can_be_bare_variables():
    ctx = run("abs = 5; keycode = 120; this.total = abs + keycode;")

    assert ctx.vars.get("this", "total") == 125.0


def test_translation_identity_and_format_helpers():
    ctx = run(
        'this.translated = _("Ready");'
        'this.formatted = format("%s %02d %.1f %%", "Map", 3, 2.25);'
    )

    assert ctx.vars.get("this", "translated") == "Ready"
    assert ctx.vars.get("this", "formatted") == "Map 03 2.2 %"


def test_format_lexes_in_message_codes_commands_and_ternaries():
    ctx = run(
        'setstring this.message,#v(format("%s:%02d", "Map", 3));'
        'savelog2 "audit",format("%s", "entry");'
        'this.ternary = (1 == 1 ? format(_("Count: %d"), 4) : "none");'
    )

    assert ctx.vars.get("this", "message") == "Map:03"
    assert ctx.vars.get("this", "ternary") == "Count: 4"


def test_switch_uses_loose_case_insensitive_equality():
    ctx = run(
        'setstring this.command,KillCoins;'
        'switch (this.command) { case "killcoins": this.word = 1; break; }'
        'switch (1430) { case "1430": this.number = 1; break; }'
        'setstring this.key,Z;'
        'switch (this.key) { case "z": this.letter = 1; break; }'
    )

    assert ctx.vars.get("this", "word") == 1.0
    assert ctx.vars.get("this", "number") == 1.0
    assert ctx.vars.get("this", "letter") == 1.0


def test_identifier_and_dotted_switch_case_labels_parse_without_leaking():
    source = (
        "switch (this.mode) {"
        "case WARPTO.PLAYER: this.player = 1; break;"
        "case NULL: this.null = 1; break;"
        "case MODE_MOUNTED: this.mounted = 1; break;"
        "default: this.fallback = 1;"
        "}"
    )
    parser = Parser(tokenize(source))
    program = parser.parse_program()

    assert parser.errors == []
    assert len(program.body) == 1
    assert len(program.body[0].cases) == 3


def test_unparseable_switch_case_does_not_leak_its_body():
    parser = Parser(tokenize(
        "switch (this.mode) {"
        "case 1 + ; this.leaked = 1; break;"
        "default: this.also_leaked = 1;"
        "}"
        "this.after = 1;"
    ))
    program = parser.parse_program()
    ctx = Context(MemoryHost())
    Interpreter(ctx).run(program)

    assert len(parser.errors) == 1
    assert "leaked" not in ctx.vars.scopes["this"]
    assert "also_leaked" not in ctx.vars.scopes["this"]
    assert ctx.vars.get("this", "after") == 1.0


def test_with_scalar_skips_the_block():
    source = object()

    class ProbeHost(MemoryHost):
        def set_builtin(self, name, value, indices, ctx):
            if name == "x":
                self.target = ctx.this_obj
                return True
            return False

    host = ProbeHost()
    parser = Parser(tokenize("if (created) { with (0) { x = 7; } }"))
    ctx = Context(host, this_obj=source)
    Interpreter(ctx).run_event(parser.parse_program(), "created")
    assert not hasattr(host, "target")
