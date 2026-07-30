from reborn_protocol.gs1 import Context, Interpreter, MemoryHost, PREEMPTED, parse


def _program(statement_count=12):
    increments = "\n".join("this.total += 1;" for _ in range(statement_count))
    return parse(f"if (created) {{ this.total = 0; {increments} }}")


def _this(ctx, name):
    return ctx.vars.get("this", name)


def test_disabled_budget_preserves_sleep_only_yields():
    program = parse("if (created) { this.a = 1; sleep 1.25; this.a = 2; }")
    ctx = Context(MemoryHost())
    interp = Interpreter(ctx, resumable=True)

    assert list(interp.iter_event(program, "created")) == [1.25]
    assert _this(ctx, "a") == 2.0


def test_enabled_budget_preempts_and_preserves_final_state():
    program = _program()
    plain_ctx = Context(MemoryHost())
    Interpreter(plain_ctx).run_event(program, "created")

    sliced_ctx = Context(MemoryHost())
    interp = Interpreter(sliced_ctx, resumable=True)
    interp.statement_budget = 3
    yields = list(interp.iter_event(program, "created"))

    assert len(yields) > 1
    assert all(value is PREEMPTED for value in yields)
    assert _this(sliced_ctx, "total") == _this(plain_ctx, "total") == 12.0


def test_sleep_delay_is_distinct_from_preemption():
    program = parse(
        "if (created) { this.a = 1; this.b = 2; sleep 0.75; this.c = 3; }"
    )
    ctx = Context(MemoryHost())
    interp = Interpreter(ctx, resumable=True)
    interp.statement_budget = 2

    yields = list(interp.iter_event(program, "created"))

    assert PREEMPTED in yields
    assert 0.75 in yields
    assert PREEMPTED is not 0.75


def test_run_event_never_exposes_preemption():
    ctx = Context(MemoryHost())
    # Even a resumable interpreter can be driven through the synchronous API;
    # the entry point itself, not a convention at its call sites, keeps the
    # cooperative sentinel out of that execution path.
    interp = Interpreter(ctx, resumable=True)
    interp.statement_budget = 1

    assert interp.run_event(_program(), "created") is None
    assert _this(ctx, "total") == 12.0
