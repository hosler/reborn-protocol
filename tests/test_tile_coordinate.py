"""One-byte tile coordinate (PropertyTileCoordinate): PLPROP X/Y 15/16,
NPCPROP X/Y 2/3 and the X/Y bytes of PLO_PLAYERWARP / PLAYERWARP2.

Oracle: GServer-v2 origin/beta4 512b8918,
server/src/utilities/PropertySerializers.cpp:484-497 (serialize) and
:499-509 (deserialize), rule introduced by upstream commit 18cf7639. Constants
MAX_LENGTH_BEFORE_OVERFLOW = 223 and BAD_BYTE_CAUSES_PACKET_CORRUPTION_SIGNED =
-22 are server/include/utilities/CommonTypes.h:535,537. The reference client
reads the byte as plain ``byte - 0x20`` half-tiles
(Preagonal/FourPlay/quattroplay/src/TServerPlayer.cpp:38-41, 1680-1692) and
writes ``(char)(floorToInt(tiles * 2.0 + 0.5) + 0x20)``
(TPlayer.cpp:279-282, 3213-3216).
"""

import math

import pytest
from hypothesis import given, strategies as st

from reborn_protocol import (
    PacketBuilder,
    PacketReader,
    decode_tile_coordinate,
    encode_client_tile_coordinate,
    encode_tile_coordinate,
)
from reborn_protocol.props import NPC_PROPS, PLAYER_PROPS, decode_value, encode_value

# deserialize (PropertySerializers.cpp:499-509): half = (uint8)(b - 32),
# minus 256 when > 223; the client's byte - 0x20 gives the same value.
READER_VECTORS = [
    (0x00, -16.0), (0x01, -15.5), (0x0A, -11.0), (0x0B, -10.5),
    (0x1F, -0.5), (0x20, 0.0), (0x21, 0.5), (0xDF, 95.5), (0xE0, 96.0),
    (0xF7, 107.5), (0xF8, 108.0), (0xFE, 111.0), (0xFF, 111.5),
]

# serialize (PropertySerializers.cpp:484-497): int16(tiles*16), clamp to
# [-256, 1784], -176 px -> -168 px, /8 truncating toward zero, +32 wrapped.
WRITER_VECTORS = [
    (-20.0, 0x00), (-16.0, 0x00), (-15.5, 0x01), (-11.0, 0x0B),
    (-10.5, 0x0B), (-10.0, 0x0C), (-0.75, 0x1F), (-0.5, 0x1F),
    (-0.25, 0x20), (0.0, 0x20), (0.25, 0x20), (0.5, 0x21), (0.75, 0x21),
    (95.5, 0xDF), (108.0, 0xF8), (111.5, 0xFF), (112.0, 0xFF),
    (127.5, 0xFF),
]


@pytest.mark.parametrize("byte,tiles", READER_VECTORS)
def test_reader_boundary_bytes(byte, tiles):
    """Bytes 0xF8..0xFF are 108..111.5 tiles; the old >= 216 rule (GServer-v2
    25396a18) made them -20..-16.5 and disagreed with the client."""
    assert decode_tile_coordinate(byte) == tiles
    assert PacketReader(bytes((byte,))).read_tile_coordinate() == tiles
    for table, prop_id in ((PLAYER_PROPS, 15), (PLAYER_PROPS, 16),
                           (NPC_PROPS, 2), (NPC_PROPS, 3)):
        assert decode_value(table[prop_id], bytes((byte,)), 0) == (tiles, 1)


@pytest.mark.parametrize("tiles,byte", WRITER_VECTORS)
def test_writer_boundary_tiles(tiles, byte):
    """111.5 is 0xFF (the old writer turned it into 0x00, i.e. -16) and -11.0
    is nudged to 0x0B (the old writer sent 0x0A, a newline)."""
    assert encode_tile_coordinate(tiles) == byte
    assert PacketBuilder().write_tile_coordinate(tiles).build() == bytes((byte,))
    for table, prop_id in ((PLAYER_PROPS, 15), (PLAYER_PROPS, 16),
                           (NPC_PROPS, 2), (NPC_PROPS, 3)):
        assert encode_value(table[prop_id], tiles) == bytes((byte,))


def test_reader_past_end_does_not_advance():
    reader = PacketReader(b"")
    assert reader.read_tile_coordinate() == 0.0
    assert reader.pos == 0


def test_round_trip_every_half_tile_except_minus_eleven():
    """Every half-tile in -16..111.5 survives encode -> decode except -11.0,
    which the reference moves to -10.5 (PropertySerializers.cpp:490-491)."""
    for half in range(-32, 224):
        tiles = half / 2.0
        expected = -10.5 if half == -22 else tiles
        assert decode_tile_coordinate(encode_tile_coordinate(tiles)) == expected


def test_writer_never_emits_newline_for_pixels_the_reference_guard_misses():
    """Departure from the reference, see encode_tile_coordinate: pixels
    -183..-177 truncate to half -22 (0x0A) and -191..-184 to half -23, where
    gs2lib writeGCharUnsafe throws (CString.cpp:1730-1736). We nudge the
    first group to 0x0B and write 0x09 for the second."""
    assert {encode_tile_coordinate(p / 16) for p in range(-183, -176)} == {0x0B}
    assert {encode_tile_coordinate(p / 16) for p in range(-191, -183)} == {0x09}


@pytest.mark.parametrize("tiles,byte", [
    (0.0, 0x20), (0.24, 0x20), (0.25, 0x21), (0.5, 0x21), (-0.25, 0x20),
    (-0.26, 0x1F), (-16.0, 0x00), (111.5, 0xFF),
    (112.0, 0x00),       # no clamp, wraps like (char)(224 + 0x20)
    (-11.0, 0x0B),       # 0x0A would end the packet: the server's +1 nudge
    (117.0, 0x0B),       # half 234 wraps to 0x0A as well
])
def test_client_writer_rounds_and_wraps(tiles, byte):
    """TPlayer.cpp:3213-3216 rounds half up; netInt (TPlayer.cpp:279-282)
    wraps without a clamp."""
    assert encode_client_tile_coordinate(tiles) == byte


# Hypothesis properties


@given(byte=st.integers(min_value=0, max_value=255))
def test_reader_matches_client_and_server_formulas(byte):
    """Server: uint8(b-32), > 223 -> -256. Client: b - 0x20. Both * 0.5."""
    half = (byte - 32) & 0xFF
    if half > 223:
        half -= 256
    assert decode_tile_coordinate(byte) == half / 2.0 == (byte - 0x20) * 0.5


@given(byte=st.integers(min_value=0, max_value=255).filter(lambda b: b != 0x0A))
def test_decode_then_encode_is_identity_on_bytes(byte):
    """Every byte but the newline is the image of exactly its decoded value."""
    tiles = decode_tile_coordinate(byte)
    assert encode_tile_coordinate(tiles) == byte
    assert encode_client_tile_coordinate(tiles) == byte


@given(pixels=st.integers(min_value=-4096, max_value=4096))
def test_writer_matches_reference_arithmetic(pixels):
    """Clamp, -176 nudge and truncating division as written at
    PropertySerializers.cpp:489-494; never 0x0A."""
    byte = encode_tile_coordinate(pixels / 16)
    assert byte != 0x0A
    clamped = max(-256, min(1784, pixels))
    if clamped == -176:
        clamped += 8
    half = math.trunc(clamped / 8)
    if half == -22:          # the documented departure, see above
        half = -21
    assert byte == (half + 32) & 0xFF
    assert -16.0 <= decode_tile_coordinate(byte) <= 111.5


@given(tiles=st.floats(min_value=-1000, max_value=1000, allow_nan=False))
def test_client_writer_never_emits_newline(tiles):
    byte = encode_client_tile_coordinate(tiles)
    assert 0 <= byte <= 0xFF and byte != 0x0A
