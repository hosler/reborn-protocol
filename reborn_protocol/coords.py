"""Coordinate frames: level-local, gmap-world, level-array index, screen pixels.

This module owns the vocabulary that pygserver and pyReborn both re-derived
inline at ~40 sites. The frames are:

* **local** — 0..63 tiles inside one level segment. Everything the wire carries
  (movement props, links, chests, signs, baddies) is local.
* **world** — ``local + grid * LEVEL_SIZE`` across a whole gmap. What pyReborn
  stores in ``player.x/y`` and renders in.
* **level index** — the flat ``ly * LEVEL_SIZE + lx`` offset into a board list.
* **screen** — pixels, via a camera centre/scale (see `camera_origin`).

The rule from the repo's own spec (CLAUDE.md "GMAP Coordinate System",
CLAUDE.md:252-255) is that the world->segment step uses ``math.floor()``, not
``int()``: for a negative world coordinate ``int()`` truncates toward zero and
lands one segment too far right/down. ``x // 64`` — the other spelling found in
the tree — agrees with ``floor(x / 64)`` over every coordinate the wire can
carry (positions are multiples of 1/16 tile), so both forms were correct.
``int(x / 64)`` is the one that is not.

Because LEVEL_SIZE is a power of two, ``%`` and ``*`` are exact on floats, so
``local_to_world(*world_to_local(w), *segment_at(w))`` round-trips bit-exactly
over the wire's pixels/16 domain. The one exception is a negative magnitude
below one ULP of 64, where ``x % 64`` rounds up to exactly 64.0 — see
tests/test_coords.py::test_local_coord_float_artifact_near_zero.
"""

import math
from typing import Tuple

# Tiles per level segment axis. A segment is always square.
LEVEL_SIZE = 64
LEVEL_TILE_COUNT = LEVEL_SIZE * LEVEL_SIZE


# -- world <-> segment / local -------------------------------------------------

def segment_index(world: float) -> int:
    """Gmap grid index of the segment containing a single world axis value."""
    return math.floor(world / LEVEL_SIZE)


def segment_at(world_x: float, world_y: float) -> Tuple[int, int]:
    """Gmap grid cell ``(grid_x, grid_y)`` containing world position (x, y)."""
    return (segment_index(world_x), segment_index(world_y))


def local_coord(world: float) -> float:
    """Level-local (0..63) value of a single world axis value."""
    return world % LEVEL_SIZE


def world_to_local(world_x: float, world_y: float) -> Tuple[float, float]:
    """Level-local (0..63) position of a world position, dropping the segment."""
    return (world_x % LEVEL_SIZE, world_y % LEVEL_SIZE)


def local_to_world(local_x: float, local_y: float,
                   grid_x: int, grid_y: int) -> Tuple[float, float]:
    """World position of a level-local position in gmap cell (grid_x, grid_y)."""
    return (local_x + grid_x * LEVEL_SIZE, local_y + grid_y * LEVEL_SIZE)


def segment_origin(grid_x: int, grid_y: int) -> Tuple[float, float]:
    """World position of the top-left tile of gmap cell (grid_x, grid_y)."""
    return (grid_x * LEVEL_SIZE, grid_y * LEVEL_SIZE)


def gmap_extent(width_segments: int, height_segments: int) -> Tuple[int, int]:
    """Full gmap size in world tiles, from its size in segments."""
    return (width_segments * LEVEL_SIZE, height_segments * LEVEL_SIZE)


# -- level board indexing -----------------------------------------------------

def level_index(local_x: int, local_y: int) -> int:
    """Flat board offset of a level-local tile. Caller bounds-checks first."""
    return local_y * LEVEL_SIZE + local_x


def in_level_bounds(local_x: float, local_y: float) -> bool:
    """Whether a level-local position lies on the 64x64 board."""
    return 0 <= local_x < LEVEL_SIZE and 0 <= local_y < LEVEL_SIZE


# -- world <-> screen ---------------------------------------------------------
#
# The transform is an origin plus a uniform scale, so it is fully described by
# the screen-space position of world tile (0, 0) and pixels-per-tile. Splitting
# it that way lets a caller cache the origin per frame (see Camera2D) instead of
# re-deriving the centre term per drawn entity.

def camera_origin(center_x: float, center_y: float,
                  screen_w: float, screen_h: float, scale: float,
                  offset_x: float = 0.0,
                  offset_y: float = 0.0) -> Tuple[float, float]:
    """Screen-space position of world tile (0, 0) for a camera centred on
    (center_x, center_y) at `scale` pixels per tile, shifted by a pixel offset
    (screen shake) that does not move the centre."""
    return (screen_w * 0.5 - center_x * scale + offset_x,
            screen_h * 0.5 - center_y * scale + offset_y)


def world_to_screen(world_x: float, world_y: float,
                    origin: Tuple[float, float],
                    scale: float) -> Tuple[float, float]:
    """World tile coords -> screen pixels."""
    return (world_x * scale + origin[0], world_y * scale + origin[1])


def screen_to_world(screen_x: float, screen_y: float,
                    origin: Tuple[float, float],
                    scale: float) -> Tuple[float, float]:
    """Screen pixels -> world tile coords (mouse picking). Inverse of
    `world_to_screen` for the same origin/scale."""
    return ((screen_x - origin[0]) / scale, (screen_y - origin[1]) / scale)


def visible_tile_range(origin: Tuple[float, float], scale: float,
                       screen_w: float,
                       screen_h: float) -> Tuple[int, int, int, int]:
    """Inclusive ``(min_tx, min_ty, max_tx, max_ty)`` world tiles touching the
    screen rect, for culling."""
    left, top = screen_to_world(0, 0, origin, scale)
    right, bottom = screen_to_world(screen_w, screen_h, origin, scale)
    return (math.floor(left), math.floor(top),
            math.ceil(right), math.ceil(bottom))
