"""GEN_5 outbound compression policies.

The policy is part of a session's wire contract: `limit_from_type` derives the
encryption limit from the compression-type byte, so a peer that never expects
bz2 must never be sent bz2. pyReborn's list-server client has always chosen
only UNCOMPRESSED/ZLIB; the game-server sessions add a bz2 tier above 8192
bytes. This pins both, and pins that the default stayed the game one.
"""

import bz2
import zlib

import pytest

from reborn_protocol.codec import Gen5Codec, ServerCodec
from reborn_protocol.encryption import (
    GAME_COMPRESSION,
    LIST_SERVER_COMPRESSION,
    CompressionPolicy,
    CompressionType,
    compress_data,
)

# 0/1 byte, the uncompressed/zlib boundary, and the zlib/bz2 boundary.
SIZES = [0, 1, 40, 55, 56, 57, 4096, 0x1FFF, 0x2000, 0x2001, 9000, 40000]


def _payload(size):
    """Poorly compressible so the two tiers produce visibly different bytes."""
    state = 0x4A80B38
    out = bytearray()
    while len(out) < size:
        state = (state * 0x8088405 + 1) & 0xFFFFFFFF
        out.append(32 + ((state >> 16) % 96))
    return bytes(out[:size])


@pytest.mark.parametrize("size", SIZES)
def test_game_policy_is_exactly_the_historical_compress_data(size):
    data = _payload(size)
    assert GAME_COMPRESSION.compress(data) == compress_data(data)


@pytest.mark.parametrize("size", SIZES)
def test_list_server_policy_never_selects_bz2(size):
    data = _payload(size)
    compressed, kind = LIST_SERVER_COMPRESSION.compress(data)
    assert kind != CompressionType.BZ2
    if size <= 55:
        assert (compressed, kind) == (data, CompressionType.UNCOMPRESSED)
    else:
        assert kind == CompressionType.ZLIB
        assert zlib.decompress(compressed) == data


def test_the_policies_diverge_only_above_the_bz2_threshold():
    small = _payload(0x2000)
    assert GAME_COMPRESSION.compress(small) == LIST_SERVER_COMPRESSION.compress(small)

    big = _payload(0x2001)
    assert GAME_COMPRESSION.compress(big)[1] == CompressionType.BZ2
    assert LIST_SERVER_COMPRESSION.compress(big)[1] == CompressionType.ZLIB


def test_gen5_codec_defaults_to_the_game_policy():
    assert Gen5Codec().compression is GAME_COMPRESSION
    frame = Gen5Codec(48).send_packet(_payload(9000))
    assert frame[2] == CompressionType.BZ2


def test_gen5_codec_honours_an_explicit_policy():
    frame = Gen5Codec(48, LIST_SERVER_COMPRESSION).send_packet(_payload(9000))
    assert frame[2] == CompressionType.ZLIB


@pytest.mark.parametrize("policy", [GAME_COMPRESSION, LIST_SERVER_COMPRESSION])
@pytest.mark.parametrize("size", SIZES)
def test_either_policy_round_trips_through_a_peer_that_reads_the_type_byte(
        policy, size):
    """A peer decodes purely from the type byte, so both policies survive a
    round trip - the constraint is what the peer *implements*, not what it can
    in principle parse."""
    data = _payload(size)
    codec = Gen5Codec(48, policy)
    peer = ServerCodec(48)
    peer.reset_decode_state()
    peer._first_decode = False  # this is not a login packet

    frame = codec.send_packet(data)
    assert frame[:2] == len(frame[2:]).to_bytes(2, "big")
    assert peer.decode_packet(frame[2:]) == data


def test_a_custom_policy_can_shift_the_uncompressed_boundary():
    policy = CompressionPolicy('test', uncompressed_max=0)
    assert policy.compress(b"")[1] == CompressionType.UNCOMPRESSED
    compressed, kind = policy.compress(b"x")
    assert kind == CompressionType.ZLIB and zlib.decompress(compressed) == b"x"


def test_bz2_tier_is_really_bz2_when_the_policy_allows_it():
    data = _payload(9000)
    compressed, kind = GAME_COMPRESSION.compress(data)
    assert kind == CompressionType.BZ2
    assert bz2.decompress(compressed) == data


def test_policy_repr_names_the_session():
    assert repr(LIST_SERVER_COMPRESSION) == "CompressionPolicy('list_server')"
