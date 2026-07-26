"""Coordinate frame conversions (reborn_protocol.coords).

The negative/boundary cases below are the ones this repo has actually shipped
bugs on (props frame, door frame, edge-warp frame), so they are asserted
directly rather than left to a round-trip property.
"""

import math

import pytest
from hypothesis import given, strategies as st

from reborn_protocol.coords import (
    LEVEL_SIZE,
    LEVEL_TILE_COUNT,
    camera_origin,
    gmap_extent,
    in_level_bounds,
    level_index,
    local_coord,
    local_to_world,
    screen_to_world,
    segment_at,
    segment_index,
    segment_origin,
    visible_tile_range,
    world_to_local,
    world_to_screen,
)


def test_level_size_constants():
    assert LEVEL_SIZE == 64
    assert LEVEL_TILE_COUNT == 4096


# =============================================================================
# Negative coordinates: floor(), not int()/truncation
# =============================================================================

@pytest.mark.parametrize("world,expected", [
    (0, 0), (0.0, 0), (1, 0), (63, 0), (63.9375, 0),
    (64, 1), (64.0, 1), (65, 1), (127, 1), (128, 2),
    (-0.5, -1), (-1, -1), (-1.5, -1), (-63, -1), (-64, -1),
    (-64.0, -1), (-64.5, -2), (-65, -2), (-128, -2), (-129, -3),
])
def test_segment_index_floors(world, expected):
    assert segment_index(world) == expected


@pytest.mark.parametrize("world", [-0.5, -1, -1.5, -63.9, -64, -64.5, -129])
def test_segment_index_disagrees_with_truncation_for_negatives(world):
    """The contract the tree's `int(x / 64)` spellings would have broken."""
    assert segment_index(world) == math.floor(world / LEVEL_SIZE)
    if world % LEVEL_SIZE != 0:
        assert segment_index(world) != int(world / LEVEL_SIZE)


@pytest.mark.parametrize("world", [0, 0.5, 63.9, 64, -0.5, -1.5, -64, -64.5, -129])
def test_segment_index_matches_floor_division(world):
    """`//` was the other spelling in the tree; it agrees, unlike `int()`."""
    assert segment_index(world) == world // LEVEL_SIZE


@pytest.mark.parametrize("world,expected", [
    (0, 0.0), (0.5, 0.5), (63.5, 63.5),
    (64, 0.0), (64.25, 0.25), (127.5, 63.5), (128, 0.0),
    (-0.5, 63.5), (-1, 63.0), (-1.5, 62.5), (-64, 0.0), (-64.5, 63.5),
])
def test_local_coord_wraps_positive(world, expected):
    """Python's `%` keeps the result in [0, 64) even for negatives: this is
    what makes `local + segment*64` reconstruct the original position."""
    assert local_coord(world) == expected
    assert 0 <= local_coord(world) < LEVEL_SIZE


# =============================================================================
# Segment boundaries and the 63 -> 64 rollover
# =============================================================================

def test_boundary_exactly_on_segment_edge():
    assert segment_at(64.0, 128.0) == (1, 2)
    assert world_to_local(64.0, 128.0) == (0.0, 0.0)
    # ...and the tile just before it stays in the previous segment.
    assert segment_at(63.9375, 127.9375) == (0, 1)


def test_rollover_63_to_64():
    assert (segment_index(63), local_coord(63)) == (0, 63)
    assert (segment_index(63.5), local_coord(63.5)) == (0, 63.5)
    assert (segment_index(64), local_coord(64)) == (1, 0)
    assert (segment_index(64.5), local_coord(64.5)) == (1, 0.5)


def test_negative_rollover_is_the_mirror_image():
    """-1 is the last tile of segment -1, not tile -1 of segment 0."""
    assert (segment_index(-1), local_coord(-1)) == (-1, 63)
    assert (segment_index(-64), local_coord(-64)) == (-1, 0)
    assert (segment_index(-65), local_coord(-65)) == (-2, 63)


# =============================================================================
# world <-> local round trip
# =============================================================================

@pytest.mark.parametrize("wx,wy", [
    (0, 0), (35.5, 30.25), (63.9375, 0), (64, 64), (191.5, 448.75),
    (-1.5, -0.25), (-64, -64), (-129.5, 200.5),
])
def test_world_local_round_trip_is_exact(wx, wy):
    gx, gy = segment_at(wx, wy)
    lx, ly = world_to_local(wx, wy)
    assert local_to_world(lx, ly, gx, gy) == (wx, wy)


# The wire carries positions as pixels/16 (props X2/Y2), so that is the real
# domain: every value is an exact binary fraction and the arithmetic below is
# exact. See test_local_coord_float_artifact_near_zero for what falls outside it.
_WIRE_COORDS = st.integers(-4096 * 16, 4096 * 16).map(lambda px: px / 16)


@given(_WIRE_COORDS, _WIRE_COORDS)
def test_world_local_round_trip_property(wx, wy):
    gx, gy = segment_at(wx, wy)
    lx, ly = world_to_local(wx, wy)
    assert 0 <= lx < LEVEL_SIZE and 0 <= ly < LEVEL_SIZE
    assert local_to_world(lx, ly, gx, gy) == (wx, wy)


def test_local_coord_float_artifact_near_zero():
    """Documented limitation, not a design choice: for a negative magnitude
    below one ULP of 64, `x % 64` rounds up to exactly 64.0 instead of landing
    in [0, 64). Pinned here because every `% 64` in the tree (which this module
    replaces verbatim) has always behaved this way."""
    tiny = -1e-16
    assert local_coord(tiny) == float(LEVEL_SIZE)
    assert not in_level_bounds(local_coord(tiny), 0)


def test_segment_origin_is_local_zero_in_that_cell():
    assert segment_origin(0, 0) == (0, 0)
    assert segment_origin(3, 7) == (192, 448)
    assert segment_origin(-1, -2) == (-64, -128)
    assert local_to_world(0, 0, 3, 7) == segment_origin(3, 7)


def test_segment_origin_round_trips_through_segment_at():
    for gx, gy in ((0, 0), (2, 5), (-1, 3)):
        ox, oy = segment_origin(gx, gy)
        assert segment_at(ox, oy) == (gx, gy)
        assert segment_at(ox + LEVEL_SIZE - 0.5, oy) == (gx, gy)


def test_gmap_extent():
    assert gmap_extent(1, 1) == (64, 64)
    assert gmap_extent(16, 12) == (1024, 768)
    assert gmap_extent(0, 0) == (0, 0)


# =============================================================================
# Level board indexing
# =============================================================================

def test_level_index_row_major():
    assert level_index(0, 0) == 0
    assert level_index(63, 0) == 63
    assert level_index(0, 1) == 64
    assert level_index(63, 63) == LEVEL_TILE_COUNT - 1


def test_level_index_covers_every_tile_once():
    seen = {level_index(x, y) for y in range(LEVEL_SIZE)
            for x in range(LEVEL_SIZE)}
    assert seen == set(range(LEVEL_TILE_COUNT))


@pytest.mark.parametrize("lx,ly,expected", [
    (0, 0, True), (63, 63, True), (63.5, 63.5, True),
    (-1, 0, False), (0, -1, False), (-0.5, 0, False),
    (64, 0, False), (0, 64, False), (64.0, 64.0, False),
])
def test_in_level_bounds(lx, ly, expected):
    assert in_level_bounds(lx, ly) is expected


# =============================================================================
# Camera transform and its inverse
# =============================================================================

def test_camera_origin_centres_the_view():
    origin = camera_origin(32, 32, 640, 480, 16.0)
    assert world_to_screen(32, 32, origin, 16.0) == (320.0, 240.0)


def test_camera_render_offset_shifts_without_moving_the_centre():
    plain = camera_origin(32, 32, 640, 480, 16.0)
    shaken = camera_origin(32, 32, 640, 480, 16.0, 3.0, -2.0)
    assert shaken == (plain[0] + 3.0, plain[1] - 2.0)


@pytest.mark.parametrize("scale", [8.0, 16.0, 32.0])
@pytest.mark.parametrize("wx,wy", [(0, 0), (32, 32), (63.5, 1.25), (-4, 200)])
def test_screen_to_world_inverts_world_to_screen(scale, wx, wy):
    origin = camera_origin(35.5, 30.5, 640, 480, scale)
    sx, sy = world_to_screen(wx, wy, origin, scale)
    rx, ry = screen_to_world(sx, sy, origin, scale)
    assert rx == pytest.approx(wx)
    assert ry == pytest.approx(wy)


def test_visible_tile_range_brackets_the_screen_rect():
    scale = 16.0
    origin = camera_origin(32, 32, 640, 480, scale)
    min_tx, min_ty, max_tx, max_ty = visible_tile_range(origin, scale, 640, 480)
    # 640px / 16px-per-tile = 40 tiles wide, centred on 32 -> [12, 52].
    assert (min_tx, max_tx) == (12, 52)
    assert (min_ty, max_ty) == (17, 47)
    # The bracket is inclusive: the corner tiles map inside the screen rect.
    assert world_to_screen(min_tx, min_ty, origin, scale) <= (0.0, 0.0)
    assert world_to_screen(max_tx, max_ty, origin, scale) >= (640.0, 480.0)


def test_visible_tile_range_handles_negative_world_coords():
    """A camera near the gmap origin sees negative tiles; floor(), not int(),
    keeps the left/top edge from clipping a column into view."""
    scale = 16.0
    origin = camera_origin(2, 2, 640, 480, scale)
    min_tx, min_ty, _, _ = visible_tile_range(origin, scale, 640, 480)
    assert (min_tx, min_ty) == (-18, -13)
