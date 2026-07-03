"""
reborn_protocol - Shared protocol library for Reborn Online

This library provides common protocol components used by both
pygserver (server) and pyReborn (client) implementations.

Components:
- Encryption: ENCRYPT_GEN_5 XOR cipher
- Constants: PLI/PLO packet IDs, PLPROP/NPCPROP property IDs
- Codec: Packet encoding/decoding, framing, compression
"""

from .encryption import (
    CompressionType,
    RebornEncryption,
    compress_data,
    decompress_data,
)

from .constants import (
    PLI,
    PLO,
    PLPROP,
    NPCPROP,
    BDPROP,
    BDMODE,
    LevelItemType,
    PLTYPE,
    PLSTATUS,
    PLFLAG,
    PLPERM,
    NPCVISFLAG,
    NPCBLOCKFLAG,
    SVI,
    SVO,
)

from .codec import (
    PacketReader,
    PacketBuilder,
    PacketBuffer,
    Gen1Codec,
    Gen2Codec,
    Gen3Codec,
    Gen4Codec,
    Gen5Codec,
    ServerCodec,
)

__version__ = "0.1.0"
__all__ = [
    # Encryption
    "CompressionType",
    "RebornEncryption",
    "compress_data",
    "decompress_data",
    # Constants
    "PLI",
    "PLO",
    "PLPROP",
    "NPCPROP",
    "BDPROP",
    "BDMODE",
    "LevelItemType",
    "PLTYPE",
    "PLSTATUS",
    "PLFLAG",
    "PLPERM",
    "NPCVISFLAG",
    "NPCBLOCKFLAG",
    "SVI",
    "SVO",
    # Codec
    "PacketReader",
    "PacketBuilder",
    "PacketBuffer",
    "Gen1Codec",
    "Gen2Codec",
    "Gen3Codec",
    "Gen4Codec",
    "Gen5Codec",
    "ServerCodec",
]
