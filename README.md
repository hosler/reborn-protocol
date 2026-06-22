# reborn-protocol

Shared protocol library for Reborn Online server and client implementations.

## Overview

This library provides common protocol components used by both:
- **pygserver** - Python game server
- **pyReborn** - Python game client

## Components

### Encryption (`reborn_protocol.encryption`)

- `RebornEncryption` - ENCRYPT_GEN_5 XOR cipher implementation
- `CompressionType` - Compression type constants (UNCOMPRESSED, ZLIB, BZ2)
- `compress_data()` - Auto-select compression based on data size
- `decompress_data()` - Decompress based on type

### Constants (`reborn_protocol.constants`)

**Game Protocol:**
- `PLI` - Client → Server packet IDs (Player Input) - 125 types
- `PLO` - Server → Client packet IDs (Player Output) - 149 types
- `PLPROP` - Player property IDs - 83 properties
- `NPCPROP` - NPC property IDs - 77 properties
- `BDPROP` / `BDMODE` - Baddy property/mode IDs - 11 / 10
- `LevelItemType` - Ground item types
- `PLTYPE` / `PLSTATUS` / `PLFLAG` / `PLPERM` - Player flags and permissions
- `NPCVISFLAG` / `NPCBLOCKFLAG` - NPC visibility and blocking flags

**List Server Protocol:**
- `SVO` - Server → ListServer packet IDs (Server Output) - 33 types
- `SVI` - ListServer → Server packet IDs (Server Input) - 26 types

Total: **500+ packet and property types defined**

### Codec (`reborn_protocol.codec`)

- `PacketReader` - Read G-type encoded data (GCHAR, GSHORT, GINT3, GINT5)
- `PacketBuilder` - Build G-type encoded packets
- `PacketBuffer` - Buffer and extract length-prefixed packets
- `Gen5Codec` - Client-side encryption codec
- `ServerCodec` - Server-side encryption codec

## Installation

### Development (editable install)

```bash
cd reborn-protocol
pip install -e .
```

### From parent project

```bash
# Install all projects with shared library
cd reborn
pip install -e ./reborn-protocol
pip install -e ./pygserver
pip install -e ./pyReborn
```

## Usage

```python
from reborn_protocol import (
    # Encryption
    RebornEncryption,
    CompressionType,

    # Game Protocol Constants
    PLI, PLO, PLPROP, NPCPROP,

    # List Server Protocol Constants
    SVI, SVO,

    # Codec
    PacketReader,
    PacketBuilder,
    Gen5Codec,
    ServerCodec,
)

# Build a packet
builder = PacketBuilder()
builder.write_gchar(PLI.TOALL)
builder.write_gstring("Hello world!")
packet = builder.build()

# Read a packet
reader = PacketReader(data)
packet_id = reader.read_gchar()
message = reader.read_gstring()
```

## Protocol Details

### G-Type Encoding

All values use +32 offset for printable ASCII range:

- **GCHAR**: 1 byte, value + 32 (range 0-223)
- **GSHORT**: 2 bytes, `((v >> 7) + 32, (v & 0x7F) + 32)` (range 0-16383)
- **GINT3**: 3 bytes, 7-bit per byte encoding (range 0-2097151)
- **GINT5**: 5 bytes, for large values like timestamps

### Encryption

ENCRYPT_GEN_5 uses a Linear Congruential Generator (LCG):

```
iterator = (iterator * 0x8088405 + key) mod 2^32
```

Encryption limit varies by compression type:
- UNCOMPRESSED: 48 bytes
- ZLIB/BZ2: 16 bytes

### Packet Framing

Packets are framed with a 2-byte big-endian length prefix:

```
[LENGTH:2 bytes][PACKET_DATA:LENGTH bytes]
```

## License

MIT License - see LICENSE file.
