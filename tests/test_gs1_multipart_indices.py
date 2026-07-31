"""A reference's indices are collected across every part, not just the first.

`npcs[3].save[1]` has an index on TWO path parts. `_resolve` used to stop at
the first indexed part, so a Host saw ("npcs.save", [3]) and could not tell
slot 0 from slot 1 -- which is precisely how classic Bomber distinguishes its
room controller (`npcs[n].save[1]==13`) from an arcade cabinet
(`npcs[n].save[0]==10`), so every cabinet was unreachable.

Single-indexed-part references (`tiles[x,y]`, `players[i].x`) are unaffected;
these tests pin them so the collection change cannot alter them without notice.
"""

from reborn_protocol.gs1.parser import parse
from reborn_protocol.gs1.interp import Interpreter
from reborn_protocol.gs1.runtime import Context, Host


class _RecordingHost(Host):
    """Records the (key, indices) every builtin read is resolved to."""

    def __init__(self):
        self.seen = []

    def get_builtin(self, name, indices, ctx):
        self.seen.append((name, list(indices)))
        return 0.0


def _resolve_reads(src):
    host = _RecordingHost()
    ctx = Context(host=host)
    Interpreter(ctx).run(parse(src))
    return host.seen


def test_two_deep_reference_forwards_both_indices():
    seen = _resolve_reads("this.r = npcs[3].save[1];")
    assert ("npcs.save", [3, 1]) in seen, seen


def test_slot_zero_and_one_are_distinguishable():
    """The actual Bomber predicate: these must NOT collapse to one reference."""
    zero = _resolve_reads("this.r = npcs[2].save[0];")
    one = _resolve_reads("this.r = npcs[2].save[1];")
    assert ("npcs.save", [2, 0]) in zero
    assert ("npcs.save", [2, 1]) in one
    assert zero != one


def test_two_indices_on_one_part_are_unchanged():
    """tiles[x,y] is 2D on a SINGLE part -- the pre-existing shape."""
    assert ("tiles", [4, 7]) in _resolve_reads("this.r = tiles[4,7];")


def test_single_indexed_part_is_unchanged():
    assert ("players.x", [2]) in _resolve_reads("this.r = players[2].x;")


def test_unindexed_reference_still_has_no_indices():
    assert ("playerx", []) in _resolve_reads("this.r = playerx;")
