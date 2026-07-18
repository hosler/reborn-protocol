"""`sleep` suspend/resume semantics (the opt-in fix for the documented
break-the-loop tradeoff at interp.py Interpreter.__init__ / ~interp.py:66).

Default (sync) execution -- run/run_event/exec, everything pygserver and the
rest of this test suite already use -- is UNCHANGED: `sleep` inside a
while/for still breaks the enclosing loop after one iteration, because a
synchronous caller has no scheduler to resume it later. The real fix is the
opt-in `Interpreter.run_event_resumable` / module-level `run_event_resumable`
API: it returns a ResumableExecution that suspends at each `sleep` and is
driven back to completion by the host's own `.resume()` calls, preserving
loop counters, temp./local. scopes and the with()/call stack across the
suspension -- because in this tree-walking interpreter, the suspended point
IS a paused Python generator frame (see ResumableExecution's docstring in
interp.py for the full comparison against GServer-v2's explicit
m_sleepCallStack).

The bottom of this file (TestOracleSleepResume) cross-checks the design
against the REAL GServer-v2 engine (GServer-v2/bin/Oracle, skipped if not
built): unlike the shared tests/gs1_oracle_corpus.cases/test_gs1_oracle.py
harness (which fires every event through the SYNC path and so can't exercise
resumable semantics), it drives our ResumableExecution the same way a
resumable host would -- "timeout" events call .resume() on the still-pending
execution; every other event runs fresh via run_event on the same shared ctx
(the vehicle for `timeout = x` cancellation coming from an unrelated event).
See that class's docstring for what the oracle actually does with sleeps
(only TIMEOUT-typed events resume a pending sleep -- re-firing e.g. `created`
does NOT, confirmed by hand against the real binary during this work).
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from reborn_protocol.gs1 import (Context, Interpreter, MemoryHost, parse,
                                 run_event, run_event_resumable)


def _this(ctx, key):
    v = ctx.vars.get("this", key)
    return v


# -- sync fallback is UNCHANGED (regression guard) --------------------------
def test_sync_sleep_in_while_still_breaks_after_one_iteration():
    src = """
        if (created) {
            this.n = 0;
            while (this.n < 3) {
                this.n++;
                sleep 0.05;
            }
            this.done = 1;
        }
    """
    ctx = Context(MemoryHost())
    run_event(src, "created", ctx=ctx)
    # documented tradeoff: one iteration only, then the loop is abandoned --
    # but script execution CONTINUES past the (silently exited) loop, so
    # `this.done = 1` still runs.
    assert _this(ctx, "n") == 1.0
    assert _this(ctx, "done") == 1.0


def test_sync_sleep_in_for_still_breaks_after_one_iteration():
    src = """
        if (created) {
            for (i = 1; i <= 3; i++) {
                this.myvar = i;
                sleep 1;
            }
        }
    """
    ctx = Context(MemoryHost())
    run_event(src, "created", ctx=ctx)
    assert _this(ctx, "myvar") == 1.0


# -- resumable: while loop --------------------------------------------------
def test_resumable_while_loop_resumes_and_finishes():
    src = """
        if (created) {
            this.n = 0;
            while (this.n < 3) {
                this.n++;
                sleep 0.05;
            }
            this.done = 1;
        }
    """
    ctx = Context(MemoryHost())
    r = run_event_resumable(src, "created", ctx=ctx)
    assert r.pending_sleep == 0.05
    assert _this(ctx, "n") == 1.0
    assert not r.done

    r.resume()
    assert r.pending_sleep == 0.05
    assert _this(ctx, "n") == 2.0
    assert not r.done

    r.resume()
    assert r.pending_sleep == 0.05
    assert _this(ctx, "n") == 3.0
    assert not r.done

    # third sleep resumes, condition now fails, falls through to `this.done`
    r.resume()
    assert r.done
    assert r.pending_sleep is None
    assert _this(ctx, "n") == 3.0
    assert _this(ctx, "done") == 1.0

    # resume() past done is a harmless no-op
    r.resume()
    assert r.done


# -- resumable: for loop -----------------------------------------------------
def test_resumable_for_loop_preserves_counter_across_resumes():
    src = """
        if (created) {
            for (i = 1; i <= 3; i++) {
                this.myvar = i;
                sleep 1;
            }
            this.finished = 1;
        }
    """
    ctx = Context(MemoryHost())
    r = run_event_resumable(src, "created", ctx=ctx)
    seen = [_this(ctx, "myvar")]
    while not r.done:
        r.resume()
        seen.append(_this(ctx, "myvar"))
    assert seen == [1.0, 2.0, 3.0, 3.0]
    assert _this(ctx, "finished") == 1.0


# -- resumable: nested while inside for, both sleeping -----------------------
def test_resumable_nested_loops_preserve_both_counters():
    src = """
        if (created) {
            for (i = 1; i <= 2; i++) {
                temp.j = 0;
                while (temp.j < 2) {
                    temp.j++;
                    this.hits++;
                    sleep 0.01;
                }
                this.outer = i;
            }
            this.finished = 1;
        }
    """
    ctx = Context(MemoryHost())
    r = run_event_resumable(src, "created", ctx=ctx)
    hits = [_this(ctx, "hits")]
    guard = 0
    while not r.done and guard < 20:
        r.resume()
        hits.append(_this(ctx, "hits"))
        guard += 1
    assert hits == [1.0, 2.0, 3.0, 4.0, 4.0]
    assert _this(ctx, "outer") == 2.0
    assert _this(ctx, "finished") == 1.0


# -- resumable: sleep inside a user function call, called from a loop -------
def test_resumable_sleep_inside_user_function_preserves_temp_scope():
    src = """
        function helper() {
            temp.k = 0;
            while (temp.k < 2) {
                temp.k++;
                this.calls++;
                sleep 0.01;
            }
        }
        if (created) {
            this.calls = 0;
            for (i = 1; i <= 2; i++) {
                helper();
                this.iter = i;
            }
            this.finished = 1;
        }
    """
    ctx = Context(MemoryHost())
    r = run_event_resumable(src, "created", ctx=ctx)
    guard = 0
    while not r.done and guard < 20:
        r.resume()
        guard += 1
    assert _this(ctx, "calls") == 4.0
    assert _this(ctx, "iter") == 2.0
    assert _this(ctx, "finished") == 1.0


# -- resumable: with() source stack survives a suspend -----------------------
def test_resumable_with_block_restores_this_obj_after_resume():
    src = """
        if (created) {
            with (this) {
                this.a = 1;
                sleep 0.01;
                this.b = 2;
            }
            this.c = 3;
        }
    """
    ctx = Context(MemoryHost())
    r = run_event_resumable(src, "created", ctx=ctx)
    # still "inside" the with() block while suspended
    assert ctx.this_obj is not None
    assert _this(ctx, "b") != 2.0

    r.resume()
    assert r.done
    # with()'s try/finally restored the prior this_obj once the block exited
    assert ctx.this_obj is None
    assert _this(ctx, "b") == 2.0
    assert _this(ctx, "c") == 3.0


# -- `timeout = x` cancels a pending resumable sleep -------------------------
def _pending_for_loop_source():
    return """
        if (created) {
            for (i = 1; i <= 3; i++) {
                this.myvar = i;
                sleep 1;
            }
        }
    """


def test_bare_timeout_plain_assignment_cancels_pending_sleep():
    ctx = Context(MemoryHost())
    r = run_event_resumable(_pending_for_loop_source(), "created", ctx=ctx)
    assert _this(ctx, "myvar") == 1.0
    assert not r.done

    # an UNRELATED event (different execution, same ctx) reprograms the timer
    Interpreter(ctx).run_event(parse("if (playerchats) { timeout = 5; }"),
                                "playerchats")
    assert ctx.sleep_cancelled is True

    r.resume()
    assert r.done
    assert r.pending_sleep is None
    # the for loop never got to run its 2nd/3rd iteration
    assert _this(ctx, "myvar") == 1.0


def test_timeout_compound_assignment_does_not_cancel():
    # upstream gates the clear on OP_ASSIGN only -- `timeout += 1` must NOT
    # clear the pending sleep (GS1Visitor.cpp visitStatementAssignment).
    ctx = Context(MemoryHost())
    r = run_event_resumable(_pending_for_loop_source(), "created", ctx=ctx)

    Interpreter(ctx).run_event(parse("if (playerchats) { timeout += 5; }"),
                                "playerchats")
    assert ctx.sleep_cancelled is False

    r.resume()
    assert not r.done
    assert _this(ctx, "myvar") == 2.0


def test_scoped_or_indexed_timeout_does_not_cancel():
    # only the BARE, unscoped `timeout` identifier is special-cased upstream;
    # this.timeout / server.timeout / timeout[0] are ordinary variables.
    ctx = Context(MemoryHost())
    r = run_event_resumable(_pending_for_loop_source(), "created", ctx=ctx)

    Interpreter(ctx).run_event(
        parse("if (playerchats) { this.timeout = 5; server.timeout = 5; }"),
        "playerchats")
    assert ctx.sleep_cancelled is False

    r.resume()
    assert not r.done
    assert _this(ctx, "myvar") == 2.0


def test_unrelated_assignment_does_not_cancel_pending_sleep():
    ctx = Context(MemoryHost())
    r = run_event_resumable(_pending_for_loop_source(), "created", ctx=ctx)

    Interpreter(ctx).run_event(parse("if (playerchats) { this.chatted = 1; }"),
                                "playerchats")
    assert ctx.sleep_cancelled is False

    r.resume()
    assert not r.done
    assert _this(ctx, "myvar") == 2.0


def test_timeout_assignment_mid_continuation_does_not_cancel_its_own_sleep():
    # `timeout = x;` immediately before a `sleep` IN THE SAME resumed
    # continuation must not cancel that sleep -- upstream's own clear is a
    # no-op here too (the stack it would clear was already emptied by the act
    # of resuming). Only a cancel that arrives while genuinely suspended
    # counts.
    src = """
        if (created) {
            this.n = 0;
            while (this.n < 2) {
                this.n++;
                timeout = 5;
                sleep 0.05;
            }
            this.done = 1;
        }
    """
    ctx = Context(MemoryHost())
    r = run_event_resumable(src, "created", ctx=ctx)
    assert _this(ctx, "n") == 1.0
    assert not r.done

    r.resume()
    assert not r.done
    assert _this(ctx, "n") == 2.0

    r.resume()
    assert r.done
    assert _this(ctx, "done") == 1.0


# -- oracle differential: resumable sleep vs the real GServer-v2 engine -----
#
# Confirmed by hand against the built oracle binary while designing this (see
# the reborn-protocol audit): the oracle's Catch2 driver (OracleMain.cpp)
# keeps ONE compiled script/GS1Visitor alive across a case's whole `events`
# list, exactly like GServer-v2 production does for one NPC -- so it DOES
# exercise real sleep-resume, but ONLY on TIMEOUT-typed events
# (GS1Visitor::execute checks `event.type == ScriptEventType::TIMEOUT` before
# consulting m_sleepCallStack). Firing the SAME non-timeout event repeatedly
# (e.g. EVENTS created,created,created,created) does NOT resume -- each
# firing re-runs `if (created) {...}` from scratch, so a loop counter reset
# inside that block never progresses past its first sleep. That's why the
# resumable driver below only routes "timeout" events to `.resume()`; every
# other event is a fresh run_event_resumable() firing on the same ctx (which
# is also how a `timeout = x` assignment from an unrelated event gets a
# chance to cancel the pending sleep).

TESTS_DIR = Path(__file__).resolve().parent
ORACLE_BIN = Path(os.environ.get(
    "GS1_ORACLE_BIN", TESTS_DIR.parent.parent / "GServer-v2" / "bin" / "Oracle"))

_RESUME_LOOP_SRC = """
    if (created) {
        for (i = 1; i <= 3; i++) {
            this.myvar = i;
            sleep 1;
        }
    }
"""

_ORACLE_SLEEP_CASES = f"""\
=====CASE sleep-resume-timeout
=====EVENTS created,timeout,timeout,timeout
{_RESUME_LOOP_SRC}
=====CASE sleep-timeout-cancel
=====EVENTS created,playerchats,timeout,timeout,timeout
{_RESUME_LOOP_SRC}
if (playerchats) {{
    timeout = 5;
}}
=====CASE sleep-timeout-no-cancel-control
=====EVENTS created,playerchats,timeout,timeout,timeout
{_RESUME_LOOP_SRC}
if (playerchats) {{
    this.chatted = 1;
}}
"""

_ORACLE_CASE_EVENTS = {
    "sleep-resume-timeout": ["created", "timeout", "timeout", "timeout"],
    "sleep-timeout-cancel": ["created", "playerchats", "timeout", "timeout", "timeout"],
    "sleep-timeout-no-cancel-control": ["created", "playerchats", "timeout", "timeout", "timeout"],
}


def _drive_resumable(program, events, ctx):
    """Mirror GS1Visitor::execute's TIMEOUT-only resume rule: a "timeout"
    event resumes the still-pending execution (if any); any other event -
    including a REPEATED "timeout" once the pending one has finished/been
    cancelled - fires fresh via run_event_resumable on the same ctx."""
    pending = None
    for ev in events:
        if ev == "timeout" and pending is not None and not pending.done:
            pending.resume()
            continue
        fresh = Interpreter(ctx).run_event_resumable(program, ev)
        if not fresh.done:
            pending = fresh
    return ctx


@pytest.fixture(scope="module")
def oracle_sleep_results(tmp_path_factory):
    if not ORACLE_BIN.is_file():
        pytest.skip(f"oracle binary not built: {ORACLE_BIN} (cmake -DTESTS=ON)")
    in_path = tmp_path_factory.mktemp("oracle_sleep") / "cases.txt"
    out_path = tmp_path_factory.mktemp("oracle_sleep") / "results.jsonl"
    in_path.write_text(_ORACLE_SLEEP_CASES)
    subprocess.run(
        [str(ORACLE_BIN), "[oracle]"],
        cwd=ORACLE_BIN.parent,
        env={**os.environ, "GS1_ORACLE_IN": str(in_path), "GS1_ORACLE_OUT": str(out_path)},
        check=True,
        capture_output=True,
        timeout=120,
    )
    results = {}
    for line in out_path.read_text().splitlines():
        obj = json.loads(line)
        results[obj["id"]] = obj
    return results


class TestOracleSleepResume:
    @pytest.mark.parametrize("case_id", sorted(_ORACLE_CASE_EVENTS))
    def test_matches_oracle(self, case_id, oracle_sleep_results):
        oracle = oracle_sleep_results[case_id]
        assert oracle.get("compile_error") is None

        ctx = Context(MemoryHost())
        program = parse(_RESUME_LOOP_SRC if case_id == "sleep-resume-timeout"
                         else _RESUME_LOOP_SRC + (
                             "\nif (playerchats) { timeout = 5; }"
                             if case_id == "sleep-timeout-cancel"
                             else "\nif (playerchats) { this.chatted = 1; }"))
        _drive_resumable(program, _ORACLE_CASE_EVENTS[case_id], ctx)

        oracle_npc = oracle["stores"]["npc"]
        assert oracle_npc["myvar"]["num"] == _this(ctx, "myvar")
        if "chatted" in oracle_npc:
            assert oracle_npc["chatted"]["num"] == _this(ctx, "chatted")
