"""Property descriptor table + table-driven codec (reborn_protocol.props)."""

import pytest

from reborn_protocol import PacketBuilder, PacketReader
from reborn_protocol.props import (
    BADDY_PROPS,
    COLORS_CLASSIC,
    COLORS_NEWWORLD,
    NPC_PROPS,
    PLAYER_PROPS,
    PropDesc,
    StreamPolicy,
    Wire,
    decode_value,
    encode_value,
    parse_prop_stream,
    payload_len,
    preset_power_image,
    with_gif_fallback,
)


def gchar(value: int) -> bytes:
    return bytes(((value + 32) & 0xFF,))


def gstring(text: str) -> bytes:
    return gchar(len(text)) + text.encode('latin-1')


# =============================================================================
# The G-encoding primitives must agree with the shared codec
# =============================================================================

_READERS = {1: 'read_gchar', 2: 'read_gshort', 3: 'read_gint3',
            4: 'read_gint4', 5: 'read_gint5'}


@pytest.mark.parametrize("width", sorted(_READERS))
def test_gbyte_decode_matches_packet_reader(width):
    """props' fold must be PacketReader's fold, lane carries included."""
    desc = PropDesc(0, f"GBYTE{width}", Wire.GBYTE, width)
    for lane in (0, 1, 63, 127, 128, 200, 223):
        raw = bytes(((lane + 32) & 0xFF,)) * width
        value, pos = decode_value(desc, raw, 0)
        assert pos == width
        assert value == getattr(PacketReader(raw), _READERS[width])()


@pytest.mark.parametrize("width", sorted(_READERS))
def test_gbyte_encode_matches_packet_builder(width):
    writers = {1: 'write_gchar', 2: 'write_gshort', 3: 'write_gint3',
               4: 'write_gint4', 5: 'write_gint5'}
    desc = PropDesc(0, f"GBYTE{width}", Wire.GBYTE, width)
    for value in (0, 1, 127, 223, 1000, 100000):
        expected = getattr(PacketBuilder(), writers[width])(value).build()
        assert encode_value(desc, value) == expected


# =============================================================================
# Individual wire forms
# =============================================================================

def test_string_empty_decodes_as_none():
    """Empty and truncated both give None; call sites decide what that means."""
    desc = PLAYER_PROPS[12]  # CURCHAT
    assert decode_value(desc, gchar(0), 0) == (None, 1)
    assert decode_value(desc, gstring("hi"), 0) == ("hi", 3)


def test_signed_half_tile_coordinate():
    desc = PLAYER_PROPS[15]  # X (PropertyTileCoordinate)
    assert decode_value(desc, gchar(63), 0)[0] == 31.5
    assert decode_value(desc, gchar(220), 0)[0] == -18.0


def test_tile_coordinate_avoids_223():
    assert encode_value(PLAYER_PROPS[15], 223 / 2.0) == gchar(224)


def test_pixel_coordinate_round_trip():
    desc = PLAYER_PROPS[78]  # X2
    for tiles in (0.0, 30.5, 63.9375, -2.5):
        encoded = encode_value(desc, tiles)
        assert decode_value(desc, encoded, 0) == (tiles, 2)


def test_colors_width_is_policy_not_wire():
    desc = PLAYER_PROPS[13]
    raw = b"".join(gchar(i) for i in range(8))
    assert decode_value(desc, raw, 0, COLORS_CLASSIC) == ([0, 1, 2, 3, 4], 5)
    assert decode_value(desc, raw, 0, COLORS_NEWWORLD) == (
        [0, 1, 2, 3, 4, 5, 6, 7], 8)


def test_effect_colors_stop_if_first_zero():
    desc = PLAYER_PROPS[23]
    assert decode_value(desc, gchar(0) + gchar(9), 0) == ([0], 1)
    raw = b"".join(gchar(i) for i in (1, 2, 3, 4, 5))
    assert decode_value(desc, raw, 0) == ([1, 2, 3, 4, 5], 5)


def test_sword_power_bias_and_bare_range():
    sword, shield = PLAYER_PROPS[8], PLAYER_PROPS[9]
    assert decode_value(sword, gchar(3), 0) == ((3, None), 1)
    assert decode_value(sword, gchar(33) + gstring("blade.png"), 0)[0] == (
        3, "blade.png")
    # 7 is above the bare preset range but below the bias: still a bare power.
    assert decode_value(sword, gchar(7), 0) == ((7, None), 1)
    assert decode_value(shield, gchar(12) + gstring("guard.png"), 0)[0] == (
        2, "guard.png")


def test_biased_power_with_no_image_bytes_is_not_bare():
    """PropertyShieldPower::deserialize's bytesLeft()==0 early return: the image
    field is absent, but the power was still biased, so it is not the bare form
    and must not pick up a synthesised default name."""
    assert decode_value(PLAYER_PROPS[9], gchar(12), 0) == ((2, ''), 1)


def test_sword_power_encode_round_trip():
    sword = PLAYER_PROPS[8]
    assert decode_value(sword, encode_value(sword, 2), 0)[0] == (2, None)
    assert decode_value(sword, encode_value(sword, (2, "b.png")), 0)[0] == (
        2, "b.png")


def test_headgif_preset_vs_custom():
    desc = PLAYER_PROPS[11]
    assert decode_value(desc, gchar(5), 0) == (5, 1)
    name = "myhead.png"
    assert decode_value(desc, gchar(100 + len(name)) + name.encode(), 0) == (
        name, 1 + len(name))
    assert encode_value(desc, 5) == gchar(5)
    assert encode_value(desc, name) == gchar(100 + len(name)) + name.encode()


def test_elo_rating_packs_into_gint3():
    desc = PLAYER_PROPS[36]
    encoded = encode_value(desc, (1500, 60))
    assert len(encoded) == 3
    assert decode_value(desc, encoded, 0)[0] == (1500, 60)


def test_attachnpc_is_byte_plus_gint3():
    desc = PLAYER_PROPS[42]
    encoded = encode_value(desc, (1, 4096))
    assert len(encoded) == 4
    assert decode_value(desc, encoded, 0)[0] == (1, 4096)


def test_void_prop_consumes_nothing():
    assert decode_value(PLAYER_PROPS[51], b"", 0) == (None, 0)
    assert encode_value(PLAYER_PROPS[51], None) == b""


def test_imagepart_round_trip():
    desc = NPC_PROPS[34]
    encoded = encode_value(desc, (128, 256, 32, 48))
    assert len(encoded) == 6
    assert decode_value(desc, encoded, 0)[0] == (128, 256, 32, 48)


def test_baddy_power_image():
    desc = BADDY_PROPS[4]
    encoded = encode_value(desc, (2, "baddy.png"))
    assert decode_value(desc, encoded, 0)[0] == (2, "baddy.png")


def test_fixed_width_payload_len_survives_truncation():
    """A short tail must still report the declared width, or a truncated packet
    parses as clean (which is how a wrong COLORS width slipped through)."""
    for prop_id, expected in ((22, 1), (3, 3), (30, 5), (78, 2), (36, 3),
                              (42, 4), (45, 1), (13, 8)):
        assert payload_len(PLAYER_PROPS[prop_id], b"", 0, COLORS_NEWWORLD) == expected


def test_truncated_variable_width_payload_overruns_the_packet():
    """A declared length past the end must leave pos > len(data) so the stream
    walker can flag the desync, not silently measure as "fits exactly"."""
    for desc, payload in ((PLAYER_PROPS[0], gchar(20) + b"short"),
                          (PLAYER_PROPS[11], gchar(120) + b"short"),
                          (NPC_PROPS[1], gchar(0) + gchar(90) + b"short")):
        value, pos = decode_value(desc, payload, 0)
        assert pos > len(payload)


def test_payload_len_agrees_with_decode():
    """The skip width and the decoder are one implementation, not two."""
    samples = {
        0: gstring("nick"),
        3: gchar(1) + gchar(2) + gchar(3),
        8: gchar(33) + gstring("blade.png"),
        11: gchar(100 + len("myhead.png")) + b"myhead.png",
        13: b"".join(gchar(i) for i in range(8)),
        23: gchar(0),
        42: gchar(1) + gchar(0) + gchar(0) + gchar(1),
        51: b"",
        78: gchar(1) + gchar(2),
    }
    for prop_id, payload in samples.items():
        desc = PLAYER_PROPS[prop_id]
        assert payload_len(desc, payload, 0, COLORS_NEWWORLD) == len(payload)


# =============================================================================
# Table completeness
# =============================================================================

def test_player_table_is_contiguous_through_83():
    assert sorted(PLAYER_PROPS) == list(range(84))


def test_npc_table_is_contiguous_through_77():
    assert sorted(NPC_PROPS) == list(range(78))


def test_baddy_table_is_contiguous():
    assert sorted(BADDY_PROPS) == list(range(11))


def test_npc_gattribs_are_not_contiguous():
    """GMAPLEVELX/Y and Z sit between GATTRIB5 and GATTRIB6 in NPCProp."""
    assert NPC_PROPS[40].name == "GATTRIB5"
    assert [NPC_PROPS[i].name for i in (41, 42, 43)] == [
        "GMAPLEVELX", "GMAPLEVELY", "Z"]
    assert NPC_PROPS[44].name == "GATTRIB6"


def test_every_descriptor_round_trips_a_sample():
    """No wire form is missing from encode_value or decode_value."""
    samples = {
        Wire.GBYTE: 3, Wire.RAW: b"ab", Wire.STRING: "s",
        Wire.LONGSTRING: "s", Wire.VOID: None, Wire.TILE: 4.5,
        Wire.HALFTILE: 4.5, Wire.TILEZ: 2, Wire.PIXEL: 4.5,
        Wire.COLORS: [1, 2, 3, 4, 5], Wire.EFFECTCOLORS: [1, 2, 3, 4, 5],
        Wire.SWORDPOWER: (2, "i.png"), Wire.POWERIMAGE: (2, "i.png"),
        Wire.HEADGIF: 5, Wire.ELORATING: (100, 20),
        Wire.ATTACHNPC: (1, 2), Wire.IMAGEPART: (1, 2, 3, 4),
    }
    assert set(samples) == set(Wire)
    for table in (PLAYER_PROPS, NPC_PROPS, BADDY_PROPS):
        for desc in table.values():
            encoded = encode_value(desc, samples[desc.wire], COLORS_CLASSIC)
            assert payload_len(desc, encoded, 0, COLORS_CLASSIC) == len(encoded)


# =============================================================================
# Stream walking + policy
# =============================================================================

_ASCENDING = StreamPolicy(
    table=PLAYER_PROPS, max_prop_id=83, colors_len=COLORS_NEWWORLD,
    require_ascending=True, ascending_exempt=frozenset({50}),
    check_alignment=True)


def _handlers():
    return {
        15: lambda out, v: out.__setitem__('x', v),
        16: lambda out, v: out.__setitem__('y', v),
        20: lambda out, v: out.__setitem__('level', v),
    }


def test_stream_skips_undecoded_props_by_table_width():
    data = (gchar(13) + b"".join(gchar(i) for i in range(8))     # COLORS, 8 wide
            + gchar(15) + gchar(60)
            + gchar(20) + gstring("start.nw"))
    out, clean, pos = parse_prop_stream(data, 0, _ASCENDING, _handlers())
    assert clean and pos == len(data)
    assert out == {'x': 30.0, 'level': "start.nw"}


def test_wrong_colors_width_is_detected_as_unclean():
    data = (gchar(13) + b"".join(gchar(i) for i in range(8))
            + gchar(15) + gchar(60)
            + gchar(20) + gstring("start.nw"))
    classic = _ASCENDING.with_colors_len(COLORS_CLASSIC)
    _, clean, _ = parse_prop_stream(data, 0, classic, _handlers())
    assert not clean


def test_descending_prop_id_stops_the_parse():
    data = gchar(16) + gchar(60) + gchar(15) + gchar(20)
    out, clean, _ = parse_prop_stream(data, 0, _ASCENDING, _handlers())
    assert not clean
    assert out == {'y': 30.0}


def test_joinleavelvl_header_is_exempt_from_ascending_order():
    data = gchar(50) + gchar(1) + gchar(15) + gchar(60)
    handlers = _handlers()
    handlers[50] = lambda out, v: out.__setitem__('joinleave', v)
    out, clean, _ = parse_prop_stream(data, 0, _ASCENDING, handlers)
    assert clean
    assert out == {'joinleave': 1, 'x': 30.0}


def test_full_consume_policy():
    trailing = gchar(15) + gchar(60) + b"\x00"
    strict = StreamPolicy(table=PLAYER_PROPS, max_prop_id=83,
                          require_full_consume=True)
    lenient = StreamPolicy(table=PLAYER_PROPS, max_prop_id=83)
    assert not parse_prop_stream(trailing, 0, strict, _handlers())[1]
    assert parse_prop_stream(gchar(15) + gchar(60), 0, strict, _handlers())[1]
    assert not parse_prop_stream(trailing, 0, lenient, _handlers())[1] is None


# =============================================================================
# Sword/shield image conventions (opt-in, not part of the decode)
# =============================================================================

def test_preset_power_image():
    assert preset_power_image('sword', 3) == "sword3.png"
    assert preset_power_image('sword', 3, classic=True) == "sword3.gif"
    assert preset_power_image('shield', 4) == "shield4.png"
    assert preset_power_image('sword', 0) == ""
    assert preset_power_image('sword', 5) == ""


def test_with_gif_fallback():
    assert with_gif_fallback("blade") == "blade.gif"
    assert with_gif_fallback("blade.png") == "blade.png"
    assert with_gif_fallback("") == ""
