"""Regression tests for GS1 interpreter bugs found vs GServer-v2 upstream.

Each case cites the GServer-v2 commit that fixed the reference C++
implementation (server/src/scripting/gs1/); see the corresponding fix in
reborn_protocol/gs1/interp.py / runtime.py for the exact upstream text.
"""
from reborn_protocol.gs1.interp import run, Interpreter
from reborn_protocol.gs1 import parse


def probe(ctx, expr):
    return Interpreter(ctx).eval(parse(expr + ";").body[0].expr)


# -- getdir(dx, dy) is angle-based, not a cardinal snap (GServer-v2 9e759e9d) -
def test_getdir_cardinals():
    ctx = run("this.left=getdir(-1,0); this.right=getdir(1,0);")
    assert probe(ctx, "this.left") == 1.0
    assert probe(ctx, "this.right") == 3.0


def test_getdir_pure_vertical():
    # The compiled GServer-v2 reference binary has a verbatim quirk here
    # (fn_getdir only computes its angle
    # `if (!DoubleIsZero(dx) || DoubleIsZero(dy))`, which is false for a
    # pure-vertical delta, so BOTH straight up and straight down fall back to
    # the 0.0 default -> "right"). That contradicts GServer's own docs table
    # ((0,-1)->0 up, (0,1)->2 down) and real gameplay expectations, so this
    # port deliberately does NOT replicate the guard: gameplay fidelity wins
    # over bug-for-bug reference fidelity here (coordinator decision; see
    # tests/test_gs1_oracle.py KNOWN_UPSTREAM_BUGS["getdir-vertical"] and
    # corpus case getdir-vertical for the oracle-verified contrast).
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


def test_getdir_zero_delta_defaults_right():
    # atan2(0,0) == 0.0, which is < pi/4 -> "right", same fallback _getangle
    # already uses for a zero vector.
    ctx = run("this.d=getdir(0,0);")
    assert probe(ctx, "this.d") == 3.0


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
