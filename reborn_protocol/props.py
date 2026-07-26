"""
reborn_protocol.props - Declarative property-stream descriptors + codec.

A property stream is a flat run of ``[gchar prop_id][payload]`` pairs. One table
per property enum (PlayerProp / NPCProp / BaddyProp) records how each id is
encoded; the reader, the writer and the "how many bytes does this prop occupy"
skipper are all driven from those tables, so a width only ever exists in one
place. Widths and encodings come from GServer-v2:

- ``server/include/object/Player.h:613``   FOR_LIST_OF_PLAYER_PROPS
- ``server/include/object/NPC.h:580``      FOR_LIST_OF_NPC_PROPS
- ``server/src/level/LevelBaddy.cpp:124``  LevelBaddy::getProp
- ``server/src/utilities/PropertySerializers.cpp``  the serializers those name

Getting one width wrong misaligns every following prop in the packet (the
classic "Y position suddenly jumps" symptom), which is why the hand-rolled
copies this replaces kept drifting: pygserver read PLPROP_COLORS as 5 bytes
while writing 8, and read SWORDPOWER without removing its +30 bias.

What is deliberately *not* unified: which props a given call site surfaces,
under which dict key, and with what precedence. Those differ on purpose between
contexts (a client's own props vs another player's, a server reading client
input) so they stay with the call site as an explicit handler map, and the
version-dependent COLORS width stays a caller-supplied policy value.
"""

from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

__all__ = [
    "Wire", "PropDesc", "StreamPolicy",
    "PLAYER_PROPS", "NPC_PROPS", "BADDY_PROPS",
    "COLORS_CLASSIC", "COLORS_NEWWORLD",
    "decode_value", "encode_value", "payload_len", "parse_prop_stream",
    "preset_power_image", "with_gif_fallback",
]


# PLPROP_COLORS' width is a server-wide mode switch (GServer-v2
# PropertyColors::getColorCount -> Server::isNewWorldMode), not something
# derivable from the negotiated client version, so it is always passed in.
COLORS_CLASSIC = 5
COLORS_NEWWORLD = 8


class Wire(Enum):
    """Payload shapes a property can take on the wire."""

    GBYTE = auto()        # `width` G-encoded bytes, 7 bits each -> int
    RAW = auto()          # `width` opaque bytes -> bytes
    STRING = auto()       # gchar length + chars -> str (None when empty)
    LONGSTRING = auto()   # gshort length + chars -> str
    VOID = auto()         # no payload
    TILE = auto()         # signed half-tile gchar -> tiles float
    HALFTILE = auto()     # unsigned half-tile gchar -> tiles float
    TILEZ = auto()        # gchar biased by 50 -> tiles int
    PIXEL = auto()        # gshort of (abs(pixels) << 1 | sign) -> tiles float
    COLORS = auto()       # colors_len gchars -> list[int]
    EFFECTCOLORS = auto()  # 1 gchar if the first is 0, else 5 -> list[int]
    SWORDPOWER = auto()   # biased power, plus an image string at/above `bias`
    POWERIMAGE = auto()   # gchar power + gchar-length image string
    HEADGIF = auto()      # preset id below 100, else a (length - 100) char name
    ELORATING = auto()    # gbyte3 of (rating << 9 | deviation)
    ATTACHNPC = auto()    # gchar type + gbyte3 npc id
    IMAGEPART = auto()    # gshort x, gshort y, gchar w, gchar h


@dataclass(frozen=True)
class PropDesc:
    """One property's wire contract. `name` is the GServer-v2 enum name."""

    id: int
    name: str
    wire: Wire
    width: int = 0
    bias: int = 0


def _desc(id_: int, name: str, wire: Wire, width: int = 0, bias: int = 0) -> PropDesc:
    return PropDesc(id_, name, wire, width, bias)


def _table(descs) -> Dict[int, PropDesc]:
    return {d.id: d for d in descs}


def _gattribs(first_id: int, first_index: int, count: int) -> List[PropDesc]:
    return [_desc(first_id + i, f"GATTRIB{first_index + i}", Wire.STRING)
            for i in range(count)]


# =============================================================================
# Tables
# =============================================================================

# PlayerProp. Ids and serializers from FOR_LIST_OF_PLAYER_PROPS; the enum names
# are GServer-v2's, which differ slightly from reborn_protocol.PLPROP (HEADGIF /
# BODYIMG / EFFECTCOLORS / DISCONNECT / LANGUAGE / PLAYERLISTSTATUS).
PLAYER_PROPS: Dict[int, PropDesc] = _table([
    _desc(0, "NICKNAME", Wire.STRING),
    _desc(1, "MAXPOWER", Wire.GBYTE, 1),
    _desc(2, "CURPOWER", Wire.GBYTE, 1),
    _desc(3, "RUPEESCOUNT", Wire.GBYTE, 3),
    _desc(4, "ARROWSCOUNT", Wire.GBYTE, 1),
    _desc(5, "BOMBSCOUNT", Wire.GBYTE, 1),
    _desc(6, "GLOVEPOWER", Wire.GBYTE, 1),
    _desc(7, "BOMBPOWER", Wire.GBYTE, 1),
    _desc(8, "SWORDPOWER", Wire.SWORDPOWER, bias=30),
    _desc(9, "SHIELDPOWER", Wire.SWORDPOWER, bias=10),
    # PropertyGaniOrBowGif: a plain string for 2.x+ clients, a bow preset/image
    # for 1.411 and earlier. Only the modern form is modelled.
    _desc(10, "GANI", Wire.STRING),
    _desc(11, "HEADGIF", Wire.HEADGIF),
    _desc(12, "CURCHAT", Wire.STRING),
    _desc(13, "COLORS", Wire.COLORS),
    _desc(14, "ID", Wire.GBYTE, 2),
    _desc(15, "X", Wire.TILE),
    _desc(16, "Y", Wire.TILE),
    _desc(17, "SPRITE", Wire.GBYTE, 1),      # PropertySprite: sprite << 2 | direction
    _desc(18, "STATUS", Wire.GBYTE, 1),
    _desc(19, "CARRYSPRITE", Wire.GBYTE, 1),
    _desc(20, "CURLEVEL", Wire.STRING),
    _desc(21, "HORSEGIF", Wire.STRING),
    _desc(22, "HORSEBUSHES", Wire.GBYTE, 1),
    _desc(23, "EFFECTCOLORS", Wire.EFFECTCOLORS),
    _desc(24, "CARRYNPC", Wire.GBYTE, 3),
    _desc(25, "APCOUNTER", Wire.GBYTE, 2),
    _desc(26, "MAGICPOINTS", Wire.GBYTE, 1),
    _desc(27, "KILLSCOUNT", Wire.GBYTE, 3),
    _desc(28, "DEATHSCOUNT", Wire.GBYTE, 3),
    _desc(29, "ONLINESECS", Wire.GBYTE, 3),
    _desc(30, "IPADDR", Wire.GBYTE, 5),
    _desc(31, "UDPPORT", Wire.GBYTE, 3),
    _desc(32, "ALIGNMENT", Wire.GBYTE, 1),
    _desc(33, "ADDITFLAGS", Wire.GBYTE, 1),
    _desc(34, "ACCOUNTNAME", Wire.STRING),
    _desc(35, "BODYIMG", Wire.STRING),
    _desc(36, "RATING", Wire.ELORATING),
    *_gattribs(37, 1, 5),
    _desc(42, "ATTACHNPC", Wire.ATTACHNPC),
    _desc(43, "GMAPLEVELX", Wire.GBYTE, 1),
    _desc(44, "GMAPLEVELY", Wire.GBYTE, 1),
    _desc(45, "Z", Wire.TILEZ),
    *_gattribs(46, 6, 4),
    _desc(50, "JOINLEAVELVL", Wire.GBYTE, 1),
    _desc(51, "DISCONNECT", Wire.VOID),
    _desc(52, "LANGUAGE", Wire.STRING),
    _desc(53, "PLAYERLISTSTATUS", Wire.GBYTE, 1),
    *_gattribs(54, 10, 21),
    _desc(75, "OSTYPE", Wire.STRING),
    _desc(76, "TEXTCODEPAGE", Wire.GBYTE, 3),
    _desc(77, "ONLINESECS2", Wire.GBYTE, 5),
    _desc(78, "X2", Wire.PIXEL),
    _desc(79, "Y2", Wire.PIXEL),
    _desc(80, "Z2", Wire.PIXEL),
    _desc(81, "PLAYERLISTCATEGORY", Wire.GBYTE, 1),
    _desc(82, "COMMUNITYNAME", Wire.STRING),
    # Absent from FOR_LIST_OF_PLAYER_PROPS; pyReborn has always skipped it as a
    # GBYTE5 against live v6 servers, so the width is kept rather than guessed at.
    _desc(83, "UNKNOWN83", Wire.GBYTE, 5),
])

# NPCProp. Ids and serializers from FOR_LIST_OF_NPC_PROPS. Note the GATTRIBs are
# NOT contiguous: GMAPLEVELX/GMAPLEVELY/Z (41-43) sit between GATTRIB5 and
# GATTRIB6, and the ids do not line up with PlayerProp's (75-77 are X2/Y2/Z2
# here, not OSTYPE/TEXTCODEPAGE/ONLINESECS2).
NPC_PROPS: Dict[int, PropDesc] = _table([
    _desc(0, "IMAGE", Wire.STRING),
    _desc(1, "SCRIPT", Wire.LONGSTRING),   # PropertyGS1Script
    _desc(2, "X", Wire.TILE),
    _desc(3, "Y", Wire.TILE),
    _desc(4, "POWER", Wire.GBYTE, 1),
    _desc(5, "RUPEES", Wire.GBYTE, 3),
    _desc(6, "ARROWS", Wire.GBYTE, 1),
    _desc(7, "BOMBS", Wire.GBYTE, 1),
    _desc(8, "GLOVEPOWER", Wire.GBYTE, 1),
    _desc(9, "BOMBPOWER", Wire.GBYTE, 1),
    _desc(10, "SWORDIMAGE", Wire.SWORDPOWER, bias=30),
    _desc(11, "SHIELDIMAGE", Wire.SWORDPOWER, bias=10),
    _desc(12, "GANI", Wire.STRING),
    _desc(13, "VISFLAGS", Wire.GBYTE, 1),
    _desc(14, "BLOCKFLAGS", Wire.GBYTE, 1),
    _desc(15, "MESSAGE", Wire.STRING),
    _desc(16, "HURTDXDY", Wire.RAW, 2),
    _desc(17, "ID", Wire.GBYTE, 3),
    _desc(18, "SPRITE", Wire.GBYTE, 1),
    _desc(19, "COLORS", Wire.COLORS),
    _desc(20, "NICKNAME", Wire.STRING),
    _desc(21, "HORSEIMAGE", Wire.STRING),
    _desc(22, "HEADIMAGE", Wire.HEADGIF),
    *[_desc(23 + i, f"SAVE{i}", Wire.GBYTE, 1) for i in range(10)],
    _desc(33, "ALIGNMENT", Wire.GBYTE, 1),
    _desc(34, "IMAGEPART", Wire.IMAGEPART),
    _desc(35, "BODYIMAGE", Wire.STRING),
    *_gattribs(36, 1, 5),
    _desc(41, "GMAPLEVELX", Wire.GBYTE, 1),
    _desc(42, "GMAPLEVELY", Wire.GBYTE, 1),
    _desc(43, "Z", Wire.TILEZ),
    *_gattribs(44, 6, 4),
    _desc(48, "UNKNOWN48", Wire.VOID),
    _desc(49, "SCRIPTER", Wire.STRING),
    _desc(50, "NAME", Wire.STRING),
    _desc(51, "TYPE", Wire.STRING),
    _desc(52, "CURLEVEL", Wire.STRING),
    *_gattribs(53, 10, 21),
    _desc(74, "CLASS", Wire.LONGSTRING),
    _desc(75, "X2", Wire.PIXEL),
    _desc(76, "Y2", Wire.PIXEL),
    # Z2 exists in FOR_LIST_OF_NPC_PROPS but not in reborn_protocol.NPCPROP,
    # whose enum stops at Y2 = 76.
    _desc(77, "Z2", Wire.PIXEL),
])

# BaddyProp. LevelBaddy::getProp only ever serializes (the reference server
# forwards inbound baddy packets without parsing them), so X/Y are the plain
# unsigned `position / 8` half-tiles it writes, not PropertyTileCoordinate.
BADDY_PROPS: Dict[int, PropDesc] = _table([
    _desc(0, "ID", Wire.GBYTE, 1),
    _desc(1, "X", Wire.HALFTILE),
    _desc(2, "Y", Wire.HALFTILE),
    _desc(3, "TYPE", Wire.GBYTE, 1),
    _desc(4, "POWERIMAGE", Wire.POWERIMAGE),
    _desc(5, "MODE", Wire.GBYTE, 1),
    _desc(6, "ANI", Wire.GBYTE, 1),
    _desc(7, "DIR", Wire.GBYTE, 1),        # headDirection << 2 | direction
    _desc(8, "VERSESIGHT", Wire.STRING),
    _desc(9, "VERSEHURT", Wire.STRING),
    _desc(10, "VERSEATTACK", Wire.STRING),
])


# =============================================================================
# Decoding
# =============================================================================

def _read_gbyte(data: bytes, pos: int, count: int):
    """`count` G-encoded bytes -> unsigned int, or None if truncated.

    Folds with + rather than |, and does not mask each lane to 7 bits: a lane may
    legitimately reach 223, so its top bit carries into the next lane. Same
    arithmetic as PacketReader.read_gshort/read_gint3/read_gint4/read_gint5
    (codec.py:79-147), cross-checked against them in tests/test_props.py.
    """
    if pos + count > len(data):
        return None, len(data)
    value = 0
    for i in range(count):
        value += (data[pos + i] - 32) << (7 * (count - 1 - i))
    if count == 5:
        # GBYTE5's top lane is 4 bits wide; read_gint5 returns an unsigned int.
        return value & 0xFFFFFFFF, pos + count
    return max(0, value), pos + count


def _read_string(data: bytes, pos: int, length_bytes: int):
    """Length-prefixed string. Returns None for an empty *or* truncated value.

    Callers that need to tell "cleared to empty" from "absent" rely on the
    None: parse_other_player surfaces an empty CURCHAT as '' so a chat bubble
    can be cleared, while the self-props parser leaves the key absent.
    """
    str_len, pos = _read_gbyte(data, pos, length_bytes)
    if str_len is None or str_len <= 0:
        return None, pos
    end = pos + str_len
    if end > len(data):
        # Report the position the declared length asked for, past the end of the
        # packet: the caller uses pos > len(data) to mark the stream desynced.
        return None, end
    return data[pos:end].decode('latin-1', errors='replace'), end


def _read_signed_halftile(data: bytes, pos: int):
    """PropertyTileCoordinate: a gchar of half-tiles where >= 216 is negative."""
    raw = data[pos] - 32
    if raw >= 216:
        raw -= 256
    return raw / 2.0, pos + 1


def decode_value(desc: PropDesc, data: bytes, pos: int,
                 colors_len: int = COLORS_CLASSIC) -> Tuple[Any, int]:
    """Decode one prop payload -> (value, new_pos).

    A value of None means "nothing usable was decoded" - either a truncated
    payload, an empty string, or a genuinely payload-less prop (Wire.VOID).
    """
    wire = desc.wire
    n = len(data)

    if wire is Wire.VOID:
        return None, pos
    if wire is Wire.GBYTE:
        return _read_gbyte(data, pos, desc.width)
    if wire is Wire.RAW:
        if pos + desc.width > n:
            return None, n
        return data[pos:pos + desc.width], pos + desc.width
    if wire is Wire.STRING:
        return _read_string(data, pos, 1)
    if wire is Wire.LONGSTRING:
        length, pos = _read_gbyte(data, pos, 2)
        if length is None:
            return None, n
        end = min(pos + length, n)
        return data[pos:end].decode('latin-1', errors='replace'), pos + length
    if wire is Wire.TILE:
        if pos >= n:
            return None, pos
        return _read_signed_halftile(data, pos)
    if wire is Wire.HALFTILE:
        if pos >= n:
            return None, pos
        return (data[pos] - 32) / 2.0, pos + 1
    if wire is Wire.TILEZ:
        if pos >= n:
            return None, pos
        return (data[pos] - 32) - 50, pos + 1
    if wire is Wire.PIXEL:
        raw, pos = _read_gbyte(data, pos, 2)
        if raw is None:
            return None, pos
        pixels = raw >> 1
        return (-pixels if raw & 1 else pixels) / 16.0, pos
    if wire is Wire.COLORS:
        end = min(pos + colors_len, n)
        return [max(0, data[i] - 32) for i in range(pos, end)], pos + colors_len
    if wire is Wire.EFFECTCOLORS:
        # PropertyArray<GBYTE1, 5, StopIfFirstZero>: a leading 0 ends the array.
        if pos >= n:
            return None, pos
        if data[pos] - 32 == 0:
            return [0], pos + 1
        end = min(pos + 5, n)
        return [max(0, data[i] - 32) for i in range(pos, end)], pos + 5
    if wire is Wire.SWORDPOWER:
        if pos >= n:
            return (0, None), pos
        raw = data[pos] - 32
        pos += 1
        if raw < desc.bias:
            # The bare form: no image field on the wire at all. An image of None
            # rather than '' is what tells a caller that apart from a present
            # but empty name, which matters because the reference server
            # synthesises a default name only for the bare form.
            return (max(0, raw), None), pos
        image, pos = _read_string(data, pos, 1)
        return (max(0, raw - desc.bias), image or ''), pos
    if wire is Wire.POWERIMAGE:
        if pos >= n:
            return None, pos
        power = max(0, data[pos] - 32)
        image, pos = _read_string(data, pos + 1, 1)
        return (power, image), pos
    if wire is Wire.HEADGIF:
        if pos >= n:
            return None, pos
        length = data[pos] - 32
        pos += 1
        if length < 100:
            return length, pos
        end = pos + (length - 100)
        if end > n:
            # Like _read_string: report where the declared length pointed, so a
            # truncated name reads back as a desynced stream rather than as
            # having fit exactly.
            return None, end
        return data[pos:end].decode('latin-1', errors='replace'), end
    if wire is Wire.ELORATING:
        packed, pos = _read_gbyte(data, pos, 3)
        if packed is None:
            return None, pos
        return ((packed >> 9) & 0xFFF, packed & 0x1FF), pos
    if wire is Wire.ATTACHNPC:
        if pos >= n:
            return None, pos
        npc_type = data[pos] - 32
        npc_id, pos = _read_gbyte(data, pos + 1, 3)
        if npc_id is None:
            return None, pos
        return (npc_type, npc_id), pos
    if wire is Wire.IMAGEPART:
        if pos + 6 > n:
            return None, n
        px, pos = _read_gbyte(data, pos, 2)
        py, pos = _read_gbyte(data, pos, 2)
        return (px, py, data[pos] - 32, data[pos + 1] - 32), pos + 2

    raise ValueError(f"unhandled wire form {wire} for prop {desc.name}")


# Forms whose payload width does not depend on the bytes at hand.
_FIXED_PAYLOAD = {
    Wire.VOID: 0,
    Wire.TILE: 1,
    Wire.HALFTILE: 1,
    Wire.TILEZ: 1,
    Wire.PIXEL: 2,
    Wire.ELORATING: 3,
    Wire.ATTACHNPC: 4,
    Wire.IMAGEPART: 6,
}

# Every non-void form occupies at least its first byte, so a payload too short
# to hold even that is reported as that minimum rather than as whatever fits.
# Otherwise a prop id dangling at the very end of a packet measures as "exactly
# fits" and a desynced stream reads back as clean.
_MIN_PAYLOAD = {
    Wire.STRING: 1,
    Wire.LONGSTRING: 2,
    Wire.EFFECTCOLORS: 1,
    Wire.SWORDPOWER: 1,
    Wire.POWERIMAGE: 1,
    Wire.HEADGIF: 1,
}


def payload_len(desc: PropDesc, data: bytes, pos: int,
                colors_len: int = COLORS_CLASSIC) -> int:
    """Bytes this prop's payload occupies at `pos` (the id byte already read).

    Fixed-width forms report their *declared* width even when the packet is
    short, so a truncated tail is still detectable as truncated; variable-width
    forms are measured by decoding, so a length can never disagree with a reader.
    """
    if desc.wire in (Wire.GBYTE, Wire.RAW):
        return desc.width
    if desc.wire is Wire.COLORS:
        return colors_len
    fixed = _FIXED_PAYLOAD.get(desc.wire)
    if fixed is not None:
        return fixed
    derived = decode_value(desc, data, pos, colors_len)[1] - pos
    return max(_MIN_PAYLOAD.get(desc.wire, 0), derived)


# =============================================================================
# Encoding
# =============================================================================

def _gchar_unsafe(value: int) -> bytes:
    """gs2lib CString::writeGCharUnsafe: value + 32, no clamp. Used where a byte
    must round-trip a signed value, e.g. PropertyTileCoordinate's half-tiles."""
    return bytes(((int(value) + 32) & 0xFF,))


def _gbyte(value: int, count: int) -> bytes:
    """`count` G-encoded bytes, big-endian 7-bit lanes clamped at 223.

    Mirrors PacketBuilder.write_gchar/write_gshort/write_gint3/write_gint4/
    write_gint5 (codec.py:226-306): a lane above 223 would wrap mod 256 through
    '\\n' and cut the packet short, so it clamps and the remainder carries down.
    """
    t = max(0, int(value))
    lanes = []
    for i in range(count - 1):
        lane = min(t >> (7 * (count - 1 - i)), 223)
        lanes.append(lane)
        t -= lane << (7 * (count - 1 - i))
    lanes.append(min(t, 223))
    return bytes(((lane + 32) & 0xFF for lane in lanes))


def _gstring(value: str, length_bytes: int = 1) -> bytes:
    encoded = str(value).encode('latin-1', errors='replace')
    return _gbyte(len(encoded), length_bytes) + encoded


def _pixel(tiles: float) -> bytes:
    pixels = int(float(tiles) * 16)
    raw = ((-pixels) << 1) | 1 if pixels < 0 else pixels << 1
    return _gbyte(raw, 2)


def _power_image(value, bias: int) -> bytes:
    """SWORDPOWER/SHIELDPOWER payload from an int power or a (power, image) pair."""
    if isinstance(value, (tuple, list)):
        power, image = int(value[0]), str(value[1])
        return _gbyte(bias + power, 1) + _gstring(image)
    return _gbyte(value, 1)


def encode_value(desc: PropDesc, value: Any,
                 colors_len: int = COLORS_NEWWORLD) -> bytes:
    """Encode one prop payload (without the leading prop-id byte)."""
    wire = desc.wire

    if wire is Wire.VOID:
        return b""
    if wire is Wire.GBYTE:
        return _gbyte(value, desc.width)
    if wire is Wire.RAW:
        return bytes(value)[:desc.width].ljust(desc.width, b' ')
    if wire is Wire.STRING:
        return _gstring(value)
    if wire is Wire.LONGSTRING:
        return _gstring(value, 2)
    if wire is Wire.TILE:
        halftile = int(float(value) * 2) & 0xFF
        # PropertyTileCoordinate::serialize:410 nudges 223 to 224 ("223 will
        # break the packet flow"), i.e. -11 half-tiles becomes -10.5.
        return _gchar_unsafe(224 if halftile == 223 else halftile)
    if wire is Wire.HALFTILE:
        return _gbyte(int(float(value) * 2), 1)
    if wire is Wire.TILEZ:
        return _gbyte(max(-50, min(170, int(value))) + 50, 1)
    if wire is Wire.PIXEL:
        return _pixel(value)
    if wire is Wire.COLORS:
        colors = [int(c) for c in list(value)[:colors_len]]
        colors += [0] * (colors_len - len(colors))
        return b"".join(_gbyte(c, 1) for c in colors)
    if wire is Wire.EFFECTCOLORS:
        colors = [int(c) for c in list(value)[:5]]
        if not colors or colors[0] == 0:
            return _gbyte(0, 1)
        colors += [0] * (5 - len(colors))
        return b"".join(_gbyte(c, 1) for c in colors)
    if wire is Wire.SWORDPOWER:
        return _power_image(value, desc.bias)
    if wire is Wire.POWERIMAGE:
        power, image = value
        return _gbyte(power, 1) + _gstring(image)
    if wire is Wire.HEADGIF:
        # A preset id is a bare gchar; a custom name is gchar(100 + len) + chars.
        if isinstance(value, int):
            return _gbyte(min(99, value), 1)
        name = str(value).encode('latin-1', errors='replace')
        return _gbyte(100 + len(name), 1) + name
    if wire is Wire.ELORATING:
        rating, deviation = value
        return _gbyte(((int(rating) & 0xFFF) << 9) | (int(deviation) & 0x1FF), 3)
    if wire is Wire.ATTACHNPC:
        npc_type, npc_id = value
        return _gbyte(npc_type, 1) + _gbyte(npc_id, 3)
    if wire is Wire.IMAGEPART:
        px, py, pw, ph = (int(v) for v in value)
        return _gbyte(px, 2) + _gbyte(py, 2) + _gbyte(pw, 1) + _gbyte(ph, 1)

    raise ValueError(f"unhandled wire form {wire} for prop {desc.name}")


# =============================================================================
# Stream walking
# =============================================================================

# Handler signature: (out_dict, decoded_value) -> None. Called only for props
# the site cares about, and only when the value is not None.
PropHandler = Callable[[Dict[str, Any], Any], None]


@dataclass(frozen=True)
class StreamPolicy:
    """The context-specific parts of walking a prop stream.

    Everything here is a genuine per-call-site difference, not a wire fact:

    - `colors_len`: which PLPROP_COLORS width to assume (see COLORS_* above).
    - `require_ascending`: GServer-v2 emits props in strictly ascending id order
      (PlayerProps.cpp getPropsPacketFromList / getModifiedPropsPacket), so a
      descending id means the stream desynced - the signal a COLORS-width retry
      uses to tell a good parse from a corrupted one. Client->server streams
      make no such promise.
    - `ascending_exempt`: ids after which a descending id is still fine, e.g. the
      standalone JOINLEAVELVL header OTHERPLPROPS prepends to a props blob.
    - `check_alignment`: flag (without stopping) a prop whose payload runs past
      the end of the packet.
    - `require_full_consume`: only treat the parse as clean if it ended exactly
      at the end of the data.
    - `unknown_payload_len`: bytes to skip for an id with no descriptor, so the
      loop still makes progress.
    - `handle_empty`: ids whose handler runs even for a None value. An empty
      string is indistinguishable from an absent one otherwise, and some
      contexts need the difference - an empty CURCHAT from another player means
      "clear the chat bubble", while in a self-props packet it means nothing.
    """

    table: Mapping[int, PropDesc]
    max_prop_id: int
    colors_len: int = COLORS_CLASSIC
    require_ascending: bool = False
    ascending_exempt: frozenset = frozenset()
    check_alignment: bool = False
    require_full_consume: bool = False
    unknown_payload_len: int = 1
    handle_empty: frozenset = frozenset()

    def with_colors_len(self, colors_len: int) -> "StreamPolicy":
        return StreamPolicy(self.table, self.max_prop_id, colors_len,
                            self.require_ascending, self.ascending_exempt,
                            self.check_alignment, self.require_full_consume,
                            self.unknown_payload_len, self.handle_empty)


def parse_prop_stream(data: bytes, pos: int, policy: StreamPolicy,
                      handlers: Mapping[int, PropHandler],
                      out: Optional[Dict[str, Any]] = None):
    """Walk a `[gchar prop_id][payload]` run -> (out, clean, pos).

    `clean` is False if the stream stopped early or did not line up; callers use
    it to pick between candidate COLORS widths.
    """
    out = {} if out is None else out
    n = len(data)
    clean = True
    last_prop_id = -1

    while pos < n:
        prop_id = data[pos] - 32
        pos += 1

        if prop_id < 0 or prop_id > policy.max_prop_id:
            clean = False
            break

        desc = policy.table.get(prop_id)
        if policy.check_alignment and desc is not None:
            if pos + payload_len(desc, data, pos, policy.colors_len) > n:
                clean = False

        if (policy.require_ascending and prop_id <= last_prop_id
                and last_prop_id not in policy.ascending_exempt):
            clean = False
            break
        last_prop_id = prop_id

        if desc is None:
            pos += policy.unknown_payload_len
            continue

        value, pos = decode_value(desc, data, pos, policy.colors_len)
        handler = handlers.get(prop_id)
        if handler is not None and (value is not None
                                    or prop_id in policy.handle_empty):
            handler(out, value)

    if pos > n:
        clean = False
    if policy.require_full_consume and pos != n:
        clean = False
    return out, clean, pos


# =============================================================================
# Sword/shield image conventions
#
# Opt-in helpers rather than part of the SWORDPOWER decode: the reference server
# fills these in for its own account state, while a client wants to know whether
# an image was actually on the wire so it can fall back to its own sprite.
# =============================================================================

def preset_power_image(prefix: str, power: int, classic: bool = False) -> str:
    """GServer-v2's synthesised name for a bare sword/shield power.

    PropertySwordPower::deserialize:98 and PropertyShieldPower::deserialize:158.
    Note the asymmetry both keep and we do not "fix": shield *serialize* only
    emits the bare form up to power 3 (:142) while its deserialize hands out a
    default image up to power 4, same as sword.
    """
    if not 0 < power <= 4:
        return ""
    return f"{prefix}{power}.{'gif' if classic else 'png'}"


def with_gif_fallback(image: str) -> str:
    """An extensionless custom image name defaults to .gif (deserialize:116)."""
    if image and '.' not in image:
        return image + '.gif'
    return image
