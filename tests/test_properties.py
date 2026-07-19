"""Property-based tests for reborn_protocol wire encoding, framing and
encryption codecs (hypothesis-driven).

These complement the fixed-example tests elsewhere: instead of a handful of
hand-picked vectors, they grind the codec/encryption math over its full
legal input domain to catch off-by-one/carry-encoding bugs like the ones
hand-found in the G-type writers (see the writer docstrings in codec.py for
the exact carry-encoding rules being verified here -- clamp-to-223 leading
bytes, cap-at-15 top nibble for GINT5, etc).

Sections:
  1. Single-value round trips for every G-type writer/reader pair, over its
     documented legal domain.
  2. Mixed-type sequences (catches positional/consumption bugs that a
     single-value round trip can't -- e.g. a writer/reader pair that
     round-trips fine in isolation but consumes the wrong number of bytes
     when something else follows it).
  3. Encrypt<->decrypt round trips for every ENCRYPT_GEN_* codec, using
     paired sender/receiver instances (mirroring how a real client and
     server, seeded with the same key, would talk to each other).
  4. Robustness: PacketReader over arbitrary/truncated junk must never hang
     or raise -- this is a regression guard for the "avoid infinite loop in
     has_data() readers" bug class documented in codec.py.
  5. Compression round trips (compress_data/decompress_data), including the
     UNCOMPRESSED/ZLIB/BZ2 size-threshold boundaries, plus a guard that
     decompressing junk only ever raises the two documented exception types.
"""
from __future__ import annotations

import struct
import zlib

import pytest
from hypothesis import given, settings, strategies as st

from reborn_protocol.codec import (
    Gen1Codec,
    Gen2Codec,
    Gen3Codec,
    Gen4Codec,
    Gen5Codec,
    PacketBuilder,
    PacketReader,
    ServerCodec,
)
from reborn_protocol.encryption import (
    CompressionType,
    RebornEncryption,
    compress_data,
    decompress_data,
)

# =============================================================================
# 1. Single-value G-type round trips
# =============================================================================

@given(value=st.integers(min_value=0, max_value=223))
def test_gchar_roundtrip(value):
    data = PacketBuilder().write_gchar(value).build()
    assert PacketReader(data).read_gchar() == value


@given(value=st.integers(min_value=0, max_value=28767))
def test_gshort_roundtrip(value):
    data = PacketBuilder().write_gshort(value).build()
    assert PacketReader(data).read_gshort() == value


@given(value=st.integers(min_value=0, max_value=3682399))
def test_gint3_roundtrip(value):
    data = PacketBuilder().write_gint3(value).build()
    assert PacketReader(data).read_gint3() == value


@given(value=st.integers(min_value=0, max_value=471347295))
def test_gint4_roundtrip(value):
    data = PacketBuilder().write_gint4(value).build()
    assert PacketReader(data).read_gint4() == value


@given(value=st.integers(min_value=0, max_value=0xFFFFFFFF))
def test_gint5_roundtrip(value):
    data = PacketBuilder().write_gint5(value).build()
    assert PacketReader(data).read_gint5() == value


@given(raw=st.binary(max_size=223))
def test_gstring_roundtrip(raw):
    # latin-1 is a bijection over byte values 0-255, so decode/encode here
    # is lossless -- any mismatch is a bug in write_gstring/read_gstring,
    # not in the round trip through str.
    value = raw.decode("latin-1")
    data = PacketBuilder().write_gstring(value).build()
    assert PacketReader(data).read_gstring() == value


@given(raw=st.binary(max_size=4000))
@settings(max_examples=40)
def test_gstring_short_roundtrip(raw):
    value = raw.decode("latin-1")
    data = PacketBuilder().write_gstring_short(value).build()
    assert PacketReader(data).read_gstring_short() == value


@given(pixels=st.integers(min_value=-14383, max_value=14383))
def test_position2_roundtrip(pixels):
    # write_position2's raw value is `pixels << 1 | sign`, which must fit
    # in a GSHORT (0-28767) -- 14383 is the largest magnitude that does.
    tiles = pixels / 16.0
    data = PacketBuilder().write_position2(tiles).build()
    assert PacketReader(data).read_position2() == tiles


# =============================================================================
# 2. Mixed-type sequences (positional/consumption bugs)
# =============================================================================

_GTYPE_STRATEGIES = {
    "gchar": st.integers(min_value=0, max_value=223),
    "gshort": st.integers(min_value=0, max_value=28767),
    "gint3": st.integers(min_value=0, max_value=3682399),
    "gint4": st.integers(min_value=0, max_value=471347295),
    "gint5": st.integers(min_value=0, max_value=0xFFFFFFFF),
}
_GTYPE_METHODS = {
    "gchar": ("write_gchar", "read_gchar"),
    "gshort": ("write_gshort", "read_gshort"),
    "gint3": ("write_gint3", "read_gint3"),
    "gint4": ("write_gint4", "read_gint4"),
    "gint5": ("write_gint5", "read_gint5"),
}


@st.composite
def gtype_sequences(draw, max_size=15):
    kinds = draw(st.lists(
        st.sampled_from(list(_GTYPE_STRATEGIES) + ["gstring"]),
        min_size=1, max_size=max_size,
    ))
    seq = []
    for kind in kinds:
        if kind == "gstring":
            value = draw(st.binary(max_size=40)).decode("latin-1")
        else:
            value = draw(_GTYPE_STRATEGIES[kind])
        seq.append((kind, value))
    return seq


@given(seq=gtype_sequences())
def test_mixed_gtype_sequence_roundtrip(seq):
    builder = PacketBuilder()
    for kind, value in seq:
        if kind == "gstring":
            builder.write_gstring(value)
        else:
            write_name, _ = _GTYPE_METHODS[kind]
            getattr(builder, write_name)(value)

    reader = PacketReader(builder.build())
    for kind, expected in seq:
        if kind == "gstring":
            got = reader.read_gstring()
        else:
            _, read_name = _GTYPE_METHODS[kind]
            got = getattr(reader, read_name)()
        assert got == expected

    # Every byte written must be consumed by exactly the matching reads --
    # leftover or over-consumed data means a writer/reader pair disagreed
    # about its own width.
    assert not reader.has_data()


# =============================================================================
# 3. Encrypt <-> decrypt round trips per ENCRYPT_GEN_* codec
# =============================================================================

KEYS = st.integers(min_value=0, max_value=127)  # "key from login packet (0-127)"
PAYLOADS = st.binary(max_size=8300)  # spans all three compress_data() thresholds


def _unframe(framed: bytes) -> bytes:
    """Strip the 2-byte big-endian length prefix all send_packet()/
    encode_packet() outputs use, returning the payload recv_packet()/
    decode_packet() expect (per their own docstrings)."""
    assert len(framed) >= 2
    (length,) = struct.unpack(">H", framed[:2])
    payload = framed[2:2 + length]
    assert len(payload) == length
    return payload


@given(data=PAYLOADS)
@settings(max_examples=40, deadline=None)
def test_gen1_roundtrip(data):
    # Gen1 has no framing, no compression, no encryption at all.
    codec = Gen1Codec()
    sent = codec.send_packet(data)
    assert sent == data
    if data:
        assert codec.recv_packet(sent) == data
    else:
        assert codec.recv_packet(sent) is None  # documented empty sentinel


@given(data=PAYLOADS)
@settings(max_examples=40, deadline=None)
def test_gen2_roundtrip(data):
    codec = Gen2Codec()
    payload = _unframe(codec.send_packet(data))
    assert codec.recv_packet(payload) == data


@given(
    data=st.binary(max_size=8300).filter(lambda d: b"\n" not in d),
    key=KEYS,
)
@settings(max_examples=40, deadline=None)
def test_gen3_send_wire_format_self_consistent(data, key):
    """Gen3Codec only implements the client -> server *encode* direction;
    the matching decrypt (stripping the inserted filler byte) happens
    server-side in GServer/pygserver, not in this library (see the class
    docstring). So there's no recv_packet counterpart to round-trip
    against here -- instead, verify send_packet's wire format is
    self-consistent by independently recomputing the documented insertion
    position and reversing it by hand."""
    codec = Gen3Codec(key)
    payload = _unframe(codec.send_packet(data))
    bundle = zlib.decompress(payload)
    assert bundle.endswith(b"\n")
    with_insertion = bundle[:-1]
    assert len(with_insertion) == len(data) + 1

    iterator = (Gen3Codec.ITERATOR_START * Gen3Codec.MULTIPLIER + (key & 0xFF)) & 0xFFFFFFFF
    pos = (iterator & 0xFFFF) % (len(data) + 1)
    assert with_insertion[pos:pos + 1] == b")"
    recovered = with_insertion[:pos] + with_insertion[pos + 1:]
    assert recovered == data


@given(data=PAYLOADS)
@settings(max_examples=40, deadline=None)
def test_gen3_recv_roundtrip(data):
    # server -> client direction is plain zlib, no encryption at all.
    assert Gen3Codec().recv_packet(zlib.compress(data)) == data


@given(data=PAYLOADS, key=KEYS)
@settings(max_examples=40, deadline=None)
def test_gen4_roundtrip(data, key):
    sender = Gen4Codec(key)
    receiver = Gen4Codec(key)
    payload = _unframe(sender.send_packet(data))
    assert receiver.recv_packet(payload) == data


@given(data=PAYLOADS, key=KEYS)
@settings(max_examples=40, deadline=None)
def test_gen5_client_to_server_roundtrip(data, key):
    client = Gen5Codec(key)
    server = ServerCodec(key)
    # Transition the server past its "first packet is the plain-zlib login
    # packet" special case using only the public API, so this test exercises
    # steady-state traffic (the login packet itself is covered separately).
    server.decode_packet(zlib.compress(b""))

    payload = _unframe(client.send_packet(data))
    assert server.decode_packet(payload) == data


@given(data=PAYLOADS, key=KEYS)
@settings(max_examples=40, deadline=None)
def test_gen5_server_to_client_roundtrip(data, key):
    server = ServerCodec(key)
    client = Gen5Codec(key)
    payload = _unframe(server.encode_packet(data, is_login_response=False))
    assert client.recv_packet(payload) == data


@given(data=PAYLOADS)
@settings(max_examples=25, deadline=None)
def test_gen5_login_response_roundtrip(data):
    """The very first server->client packet is plain zlib (no encryption);
    Gen5Codec.recv_packet detects this via the zlib magic byte (0x78)."""
    server = ServerCodec()
    client = Gen5Codec()
    payload = _unframe(server.encode_packet(data, is_login_response=True))
    assert client.recv_packet(payload) == data


@given(data=PAYLOADS, key=KEYS)
@settings(max_examples=25, deadline=None)
def test_server_codec_login_packet_is_plain_zlib(data, key):
    """The first packet FROM the client (the login packet) is plain zlib,
    per ServerCodec.decode_packet's _first_decode special case."""
    server = ServerCodec(key)
    assert server.decode_packet(zlib.compress(data)) == data
    assert server._first_decode is False


@given(
    data=st.binary(max_size=2000),
    key=KEYS,
    ctype=st.sampled_from([CompressionType.UNCOMPRESSED, CompressionType.ZLIB, CompressionType.BZ2]),
)
@settings(max_examples=60, deadline=None)
def test_reborn_encryption_symmetric(data, key, ctype):
    """The LCG XOR cipher itself (independent of the codec/compression
    layers): a fresh instance seeded with the same key and limit always
    reverses another fresh instance's encrypt()."""
    encryptor = RebornEncryption(key)
    encryptor.limit_from_type(ctype)
    encrypted = encryptor.encrypt(data)

    decryptor = RebornEncryption(key)
    decryptor.limit_from_type(ctype)
    assert decryptor.decrypt(encrypted) == data


# =============================================================================
# 4. Robustness: PacketReader over arbitrary/truncated junk
# =============================================================================

_READER_METHODS_NO_ARG = [
    "read_byte",
    "read_gchar",
    "read_gshort",
    "read_gint3",
    "read_gint5",
    "read_gstring",
    "read_gstring_short",
    "read_position2",
    # peek_byte excluded: it's documented to never advance pos, so it isn't a
    # "consuming read" this loop-termination property applies to (see
    # test_peek_byte_never_advances below instead).
]


@given(data=st.binary(max_size=64), method=st.sampled_from(_READER_METHODS_NO_ARG))
@settings(max_examples=300, deadline=None)
def test_reader_truncated_reads_terminate(data, method):
    """Regression guard for the infinite-loop-on-truncation bug class (see
    the "avoid infinite loop in has_data() readers" comments in codec.py):
    the caller pattern `while reader.has_data(): reader.read_x()` must
    always terminate, never raise, and pos must never overrun the buffer."""
    reader = PacketReader(data)
    max_iterations = len(data) + 8  # generous slack: every call must either
    # advance pos or exhaust the buffer, so this can never legitimately trip
    iterations = 0
    last_pos = reader.pos
    while reader.has_data():
        getattr(reader, method)()
        iterations += 1
        assert reader.pos <= len(reader.data)
        assert reader.pos >= last_pos
        last_pos = reader.pos
        assert iterations <= max_iterations, (
            f"{method} did not terminate on truncated data {data!r}"
        )


def test_peek_byte_never_advances():
    reader = PacketReader(b"\x2f\x30")
    for _ in range(5):
        assert reader.peek_byte() == 0x2F
        assert reader.pos == 0


def test_gstring_short_negative_length_corrupts_reader_state():
    """Regression: hypothesis-found bug (shrunk to b'\\x00\\x00'). Byte pairs
    below the +32 G-offset made read_gshort() return a NEGATIVE value, which
    read_gstring_short() fed to read_string() as a length; the truncation
    guard never fires for negative lengths, so pos went permanently negative
    (has_data() true forever, next read = unhandled IndexError). Fixed with a
    max(0, ...) floor in read_gshort/read_gint3 (matching read_gchar) plus a
    negative-length guard in read_string."""
    reader = PacketReader(b"\x00\x00")
    assert reader.has_data()
    assert reader.read_gstring_short() == ""
    assert 0 <= reader.pos <= len(reader.data)
    assert not reader.has_data()


@given(
    data=st.binary(max_size=64),
    lengths=st.lists(st.integers(min_value=0, max_value=300), max_size=20),
)
@settings(max_examples=100, deadline=None)
def test_reader_read_string_and_skip_never_overrun(data, lengths):
    reader = PacketReader(data)
    for length in lengths:
        s = reader.read_string(length)
        assert isinstance(s, str)
        assert reader.pos <= len(reader.data)

    reader = PacketReader(data)
    for length in lengths:
        reader.skip(length)
        assert reader.pos <= len(reader.data)


@given(junk=st.binary(max_size=300), key=KEYS)
@settings(max_examples=150, deadline=None)
def test_codec_recv_never_raises_on_junk(junk, key):
    """Every codec's decode path must handle arbitrary/corrupt input
    cleanly (return data or None), never let an exception escape -- this is
    the "decompressing junk must not crash" requirement, exercised at the
    level real callers actually use (Codec.recv_packet/decode_packet), not
    just the raw decompress_data() helper."""
    for codec in (Gen1Codec(), Gen2Codec(), Gen3Codec(key), Gen4Codec(key), Gen5Codec(key)):
        codec.recv_packet(junk)

    fresh_server = ServerCodec(key)
    fresh_server.decode_packet(junk)  # exercises the first-decode branch

    steady_server = ServerCodec(key)
    steady_server.decode_packet(zlib.compress(b""))  # transition past login
    steady_server.decode_packet(junk)


# =============================================================================
# 5. Compression paths (compress_data / decompress_data)
# =============================================================================

@given(data=st.binary(max_size=8500))
@settings(max_examples=60, deadline=None)
def test_compress_decompress_roundtrip(data):
    compressed, ctype = compress_data(data)
    assert decompress_data(compressed, ctype) == data


@pytest.mark.parametrize("size", [0, 1, 55, 56, 8192, 8193, 8500])
def test_compress_decompress_size_thresholds(size):
    # Non-repeating filler so zlib/bz2 can't cheat their way to a trivial
    # small output regardless of the branch taken.
    data = bytes((i * 131 + 7) % 256 for i in range(size))
    compressed, ctype = compress_data(data)
    if size <= 55:
        assert ctype == CompressionType.UNCOMPRESSED
    elif size <= 0x2000:
        assert ctype == CompressionType.ZLIB
    else:
        assert ctype == CompressionType.BZ2
    assert decompress_data(compressed, ctype) == data


@given(junk=st.binary(max_size=500))
@settings(max_examples=100, deadline=None)
def test_decompress_junk_raises_only_documented_exceptions(junk):
    """decompress_data() is documented to raise zlib.error/BZ2DecompressError
    (a subclass of OSError) on corrupt input -- callers (the codecs above)
    are responsible for catching it. Verify garbage never raises anything
    outside that documented contract."""
    for ctype in (CompressionType.ZLIB, CompressionType.BZ2):
        try:
            decompress_data(junk, ctype)
        except (zlib.error, OSError):
            pass
