import bz2
import zlib

import pytest

import reborn_protocol.codec as codec_module
from reborn_protocol.codec import Gen4Codec, Gen5Codec, PacketBuilder, ServerCodec
from reborn_protocol.encryption import (
    CompressionType,
    MAX_DECOMPRESSED_SIZE,
    decompress_data,
)
from reborn_protocol.gs1 import parse


def test_ternary_followed_by_colon_idiom_in_later_statement():
    source = 'if (x) {\n  y=true?1:2;\n}\nif (!Spar:xyz) {\n  say("hi");\n}\n'

    program = parse(source)

    assert len(program.body) == 2


def test_gstring_writers_truncate_body_to_encoded_header_length():
    assert len(PacketBuilder().write_gstring("x" * 224).build()) == 1 + 223
    assert len(PacketBuilder().write_gstring_short("x" * 28768).build()) == 2 + 28767


@pytest.mark.parametrize("codec_class", [Gen5Codec, ServerCodec])
def test_oversize_gen5_send_does_not_advance_cipher(monkeypatch, codec_class):
    instance = codec_class(17)
    initial_iterator = instance.out_codec.iterator
    monkeypatch.setattr(codec_module, "compress_data", lambda data: (
        b"x" * codec_module.MAX_PACKET_LEN, CompressionType.ZLIB
    ))

    send = instance.send_packet if codec_class is Gen5Codec else instance.encode_packet
    assert send(b"payload") == b""
    assert instance.out_codec.iterator == initial_iterator


def test_oversize_gen4_send_does_not_advance_cipher(monkeypatch):
    instance = Gen4Codec(17)
    initial_iterator = instance.out_codec.iterator
    monkeypatch.setattr(codec_module.bz2, "compress", lambda data: (
        b"x" * (codec_module.MAX_PACKET_LEN + 1)
    ))

    assert instance.send_packet(b"payload") == b""
    assert instance.out_codec.iterator == initial_iterator


@pytest.mark.parametrize(
    ("compression_type", "compressed", "error"),
    [
        (CompressionType.ZLIB, zlib.compress(b"x" * (MAX_DECOMPRESSED_SIZE + 1)), zlib.error),
        (CompressionType.BZ2, bz2.compress(b"x" * (MAX_DECOMPRESSED_SIZE + 1)), OSError),
    ],
)
def test_decompression_limit_rejects_oversize_output(compression_type, compressed, error):
    with pytest.raises(error):
        decompress_data(compressed, compression_type)


def test_corrupt_first_packet_can_be_retried():
    codec = ServerCodec()

    assert codec.decode_packet(b"not compressed") is None
    assert codec.decode_packet(zlib.compress(b"login")) == b"login"
