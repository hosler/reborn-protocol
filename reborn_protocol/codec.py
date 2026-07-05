"""
reborn_protocol.codec - Packet encoding/decoding utilities

Provides classes for reading and writing Reborn protocol data types,
packet framing, and the Gen5 codec for encryption/compression.

G-Type Encoding:
- GCHAR: Single byte with value + 32 (printable ASCII range)
- GSHORT: Two bytes with 7-bit shift encoding
- GINT3: Three bytes with 7-bit shift encoding
- GINT5: Five bytes for large values (timestamps, CRC32)
"""

import bz2
import logging
import struct
import zlib
from typing import Optional, List, Tuple

from .encryption import (
    CompressionType,
    RebornEncryption,
    compress_data,
    decompress_data,
)

logger = logging.getLogger(__name__)

# struct.pack('>H', ...) length-prefix field is 16 bits; a bundle bigger than
# this would raise an unhandled struct.error. Reference (gs2lib
# CFileQueue.cpp:244-248) tosses oversize packets rather than sending them.
MAX_PACKET_LEN = 0xFFFF


def _frame(payload: bytes) -> Optional[bytes]:
    """Length-prefix `payload` (u16 big-endian), or None if it's too big to
    fit the length field. Callers should drop the send on None."""
    if len(payload) > MAX_PACKET_LEN:
        logger.warning("dropping oversize packet bundle (%d bytes > %d)",
                        len(payload), MAX_PACKET_LEN)
        return None
    return struct.pack('>H', len(payload)) + payload


# =============================================================================
# PacketReader - Read protocol data types
# =============================================================================

class PacketReader:
    """
    Utility for reading packet data with Reborn protocol encodings.

    All G-type values are encoded with +32 offset for printable ASCII.
    """

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read_byte(self) -> int:
        """Read a raw byte."""
        if self.pos >= len(self.data):
            return 0
        value = self.data[self.pos]
        self.pos += 1
        return value

    def read_gchar(self) -> int:
        """Read a GCHAR (byte - 32). Range: 0-223."""
        return max(0, self.read_byte() - 32)

    def read_gshort(self) -> int:
        """
        Read a 2-byte GSHORT value.
        Encoding: ((b1 - 32) << 7) + (b2 - 32)
        Range: 0-28767 (reference: gs2lib CString::readGShort, CString.cpp:1619)
        """
        if self.pos + 1 >= len(self.data):
            self.pos = len(self.data)  # avoid infinite loop in has_data() readers
            return 0
        b1 = self.data[self.pos] - 32
        b2 = self.data[self.pos + 1] - 32
        self.pos += 2
        # Junk bytes below the +32 offset would go negative; a negative value
        # fed to read_gstring_short's length drove self.pos negative and
        # corrupted the reader (has_data() true forever, then IndexError).
        return max(0, (b1 << 7) + b2)

    def read_gint3(self) -> int:
        """
        Read a 3-byte GINT value.
        Encoding: ((b1 - 32) << 14) + ((b2 - 32) << 7) + (b3 - 32)
        Range: 0-3682399 (reference: gs2lib CString::readGInt, CString.cpp:1627).
        Must be + (not |): b2's high bit can overlap b1's low bit at bit 14.
        """
        if self.pos + 2 >= len(self.data):
            self.pos = len(self.data)  # avoid infinite loop in has_data() readers
            return 0
        b1 = self.data[self.pos] - 32
        b2 = self.data[self.pos + 1] - 32
        b3 = self.data[self.pos + 2] - 32
        self.pos += 3
        return max(0, (b1 << 14) + (b2 << 7) + b3)

    def read_gint5(self) -> int:
        """
        Read a 5-byte GINT5 value (32-bit range, for timestamps/CRC32).
        Encoding: 4/7/7/7/7-bit big-endian packing, each byte -32 before
        folding (reference: gs2lib CString::readGInt5, CString.cpp:1644 —
        that version folds raw bytes and subtracts a single 0x4081020
        constant; this is the algebraically equivalent per-byte-offset form,
        masked to 32 bits to match its unsigned int return).
        """
        if self.pos + 4 >= len(self.data):
            self.pos = len(self.data)  # avoid infinite loop in has_data() readers
            return 0
        b0 = self.data[self.pos] - 32
        b1 = self.data[self.pos + 1] - 32
        b2 = self.data[self.pos + 2] - 32
        b3 = self.data[self.pos + 3] - 32
        b4 = self.data[self.pos + 4] - 32
        self.pos += 5
        return ((b0 << 28) + (b1 << 21) + (b2 << 14) + (b3 << 7) + b4) & 0xFFFFFFFF

    def read_string(self, length: int) -> str:
        """Read a fixed-length string."""
        length = max(0, length)  # negative length would rewind pos past 0
        if self.pos + length > len(self.data):
            length = len(self.data) - self.pos
        data = self.data[self.pos:self.pos + length]
        self.pos += length
        return data.decode('latin-1', errors='replace')

    def read_gstring(self) -> str:
        """Read a length-prefixed string (GCHAR length prefix)."""
        length = self.read_gchar()
        return self.read_string(length)

    def read_gstring_short(self) -> str:
        """Read a length-prefixed string (GSHORT length prefix)."""
        length = self.read_gshort()
        return self.read_string(length)

    def read_position2(self) -> float:
        """
        Read a 2-byte high-precision position value.
        Used for PLPROP_X2/Y2 (prop 78/79).

        Encoding: value = (b1 << 7) | b2, sign in LSB
        Returns: position in tiles (pixels / 16)
        """
        if self.pos + 1 >= len(self.data):
            self.pos = len(self.data)  # avoid infinite loop in has_data() readers
            return 0.0
        b1 = self.data[self.pos] - 32
        b2 = self.data[self.pos + 1] - 32
        self.pos += 2
        raw = (b1 << 7) + b2
        pixels = raw >> 1
        if raw & 1:  # Sign bit
            pixels = -pixels
        return pixels / 16.0

    def remaining(self) -> bytes:
        """Get remaining unread data."""
        return self.data[self.pos:]

    def has_data(self) -> bool:
        """Check if more data is available."""
        return self.pos < len(self.data)

    def skip(self, count: int) -> None:
        """Skip count bytes."""
        self.pos = min(self.pos + count, len(self.data))

    def peek_byte(self) -> int:
        """Peek at next byte without advancing position."""
        if self.pos >= len(self.data):
            return 0
        return self.data[self.pos]


# =============================================================================
# PacketBuilder - Write protocol data types
# =============================================================================

class PacketBuilder:
    """
    Utility for building packet data with Reborn protocol encodings.

    All G-type values are encoded with +32 offset for printable ASCII.
    """

    def __init__(self):
        self._data = bytearray()

    def write_byte(self, value: int) -> 'PacketBuilder':
        """Write a raw byte."""
        self._data.append(value & 0xFF)
        return self

    def write_gchar(self, value: int) -> 'PacketBuilder':
        """Write a GCHAR (value + 32, clamped to 223 before the offset).
        Reference: gs2lib CString::writeGChar (CString.cpp:1519) — values
        >= 223 pass through WITHOUT the +32 (so 234 must clamp to 223, not
        wrap mod 256 into '\\n'/0x0A and corrupt newline-framed bundles)."""
        v = max(0, min(int(value), 223))
        self._data.append((v + 32) & 0xFF)
        return self

    def write_gshort(self, value: int) -> 'PacketBuilder':
        """Write a 2-byte GSHORT value. Max 28767.
        Reference: gs2lib CString::writeGShort (CString.cpp:1526)."""
        t = max(0, min(int(value), 28767))
        b0 = t >> 7
        if b0 > 223:
            b0 = 223
        b1 = t - (b0 << 7)
        self._data.append((b0 + 32) & 0xFF)
        self._data.append((b1 + 32) & 0xFF)
        return self

    def write_gint3(self, value: int) -> 'PacketBuilder':
        """Write a 3-byte GINT value. Max 3682399.
        Reference: gs2lib CString::writeGInt (CString.cpp:1543)."""
        t = max(0, min(int(value), 3682399))
        b0 = t >> 14
        if b0 > 223:
            b0 = 223
        t -= b0 << 14
        b1 = t >> 7
        if b1 > 223:
            b1 = 223
        b2 = t - (b1 << 7)
        self._data.append((b0 + 32) & 0xFF)
        self._data.append((b1 + 32) & 0xFF)
        self._data.append((b2 + 32) & 0xFF)
        return self

    def write_gint5(self, value: int) -> 'PacketBuilder':
        """Write a 5-byte GINT5 value (32-bit range, 4/7/7/7/7-bit packing).
        Reference: gs2lib CString::writeGInt5 (CString.cpp:1584)."""
        t = max(0, min(int(value), 0xFFFFFFFF))
        b0 = (t >> 28) & 0xFF
        if b0 > 15:  # capped low: higher would exceed 0xFFFFFFFF
            b0 = 15
        t -= b0 << 28
        b1 = (t >> 21) & 0xFF
        if b1 > 223:
            b1 = 223
        t -= b1 << 21
        b2 = (t >> 14) & 0xFF
        if b2 > 223:
            b2 = 223
        t -= b2 << 14
        b3 = (t >> 7) & 0xFF
        if b3 > 223:
            b3 = 223
        b4 = (t - (b3 << 7)) & 0xFF
        for b in (b0, b1, b2, b3, b4):
            self._data.append((b + 32) & 0xFF)
        return self

    def write_string(self, value: str) -> 'PacketBuilder':
        """Write a raw string (no length prefix)."""
        self._data.extend(value.encode('latin-1', errors='replace'))
        return self

    def write_gstring(self, value: str) -> 'PacketBuilder':
        """Write a GCHAR length-prefixed string."""
        encoded = value.encode('latin-1', errors='replace')
        self.write_gchar(len(encoded))
        self._data.extend(encoded)
        return self

    def write_gstring_short(self, value: str) -> 'PacketBuilder':
        """Write a GSHORT length-prefixed string."""
        encoded = value.encode('latin-1', errors='replace')
        self.write_gshort(len(encoded))
        self._data.extend(encoded)
        return self

    def write_position2(self, tiles: float) -> 'PacketBuilder':
        """
        Write a 2-byte high-precision position value.
        Used for PLPROP_X2/Y2 (prop 78/79).

        Args:
            tiles: Position in tiles (will be converted to pixels * 2)
        """
        pixels = int(tiles * 16)
        if pixels < 0:
            raw = ((-pixels) << 1) | 1
        else:
            raw = pixels << 1
        self.write_gshort(raw)
        return self

    def write_bytes(self, data: bytes) -> 'PacketBuilder':
        """Write raw bytes."""
        self._data.extend(data)
        return self

    def build(self) -> bytes:
        """Get the built packet data."""
        return bytes(self._data)

    def __len__(self) -> int:
        return len(self._data)


# =============================================================================
# PacketBuffer - Buffer and extract framed packets
# =============================================================================

class PacketBuffer:
    """
    Buffer for accumulating TCP data and extracting complete packets.

    Reborn packets are framed with a 2-byte big-endian length prefix.
    """

    def __init__(self):
        self._buffer = bytearray()

    def add_data(self, data: bytes) -> None:
        """Add received data to the buffer."""
        self._buffer.extend(data)

    def get_packets(self) -> List[bytes]:
        """
        Extract all complete packets from the buffer.

        Returns:
            List of packet data (without length prefix)
        """
        packets = []

        while len(self._buffer) >= 2:
            # Read length prefix (big-endian)
            length = struct.unpack('>H', bytes(self._buffer[:2]))[0]

            if len(self._buffer) < 2 + length:
                break  # Incomplete packet

            # Extract packet data
            packet = bytes(self._buffer[2:2 + length])
            del self._buffer[:2 + length]
            packets.append(packet)

        return packets

    def clear(self) -> None:
        """Clear the buffer."""
        self._buffer.clear()

    def __len__(self) -> int:
        return len(self._buffer)


# =============================================================================
# Gen5Codec - Client-side encryption codec
# =============================================================================

class Gen5Codec:
    """
    ENCRYPT_GEN_5 codec for client-side use.

    Handles packet encryption/decryption with dynamic compression
    selection based on data size. Used by pyReborn client.
    """

    def __init__(self, encryption_key: int = 0):
        self.encryption_key = encryption_key
        self.in_codec = RebornEncryption(encryption_key)
        self.out_codec = RebornEncryption(encryption_key)

    def set_key(self, key: int) -> None:
        """Update encryption key for both directions."""
        self.encryption_key = key
        self.in_codec.reset(key)
        self.out_codec.reset(key)

    def send_packet(self, data: bytes) -> bytes:
        """
        Encode packet for sending (returns with length prefix).

        Args:
            data: Packet data to send

        Returns:
            Length-prefixed encrypted packet
        """
        # Choose compression based on size
        compressed, compression_type = compress_data(data)

        # Encrypt (in place on the persistent codec, which owns the LCG
        # iterator across calls)
        self.out_codec.limit_from_type(compression_type)
        encrypted = self.out_codec.encrypt(compressed)

        # Build packet with compression type byte
        packet = bytes([compression_type]) + encrypted
        framed = _frame(packet)
        return framed if framed is not None else b""

    def recv_packet(self, data: bytes) -> Optional[bytes]:
        """
        Decode received packet.

        Args:
            data: Packet data (without length prefix)

        Returns:
            Decrypted and decompressed data, or None on error
        """
        if not data or len(data) == 0:
            return None

        compression_type = data[0]

        # Check for plain zlib (first response from server)
        if compression_type == 0x78:
            try:
                return zlib.decompress(data)
            except (zlib.error, OSError):  # corrupt compressed data
                return None

        encrypted_data = data[1:]

        if compression_type not in [CompressionType.UNCOMPRESSED,
                                    CompressionType.ZLIB,
                                    CompressionType.BZ2]:
            return None

        # Decrypt (in place on the persistent codec)
        self.in_codec.limit_from_type(compression_type)
        decrypted = self.in_codec.decrypt(encrypted_data)

        # Decompress
        try:
            return decompress_data(decrypted, compression_type)
        except (zlib.error, OSError):  # corrupt compressed data
            return None


# =============================================================================
# ServerCodec - Server-side encryption codec
# =============================================================================

class ServerCodec:
    """
    ENCRYPT_GEN_5 codec for server-side use.

    Similar to Gen5Codec but with additional handling for:
    - Login packets (plain zlib)
    - Login responses (plain zlib)
    - Server-side packet building

    Used by pygserver.
    """

    def __init__(self, encryption_key: int = 0):
        self.encryption_key = encryption_key
        self.in_codec = RebornEncryption(encryption_key)
        self.out_codec = RebornEncryption(encryption_key)
        self._first_decode = True

    def set_key(self, key: int) -> None:
        """Update encryption key for both directions."""
        self.encryption_key = key
        self.in_codec.reset(key)
        self.out_codec.reset(key)

    def encode_packet(self, data: bytes, is_login_response: bool = False) -> bytes:
        """
        Encode packet for sending to client.

        Args:
            data: Packet data to send
            is_login_response: True for first response (plain zlib)

        Returns:
            Length-prefixed encoded packet
        """
        if is_login_response:
            # First response is plain zlib compressed
            compressed = zlib.compress(data)
            framed = _frame(compressed)
            return framed if framed is not None else b""

        # Normal packet: compress, encrypt, add type byte
        compressed, compression_type = compress_data(data)

        # Encrypt (in place on the persistent codec, which owns the LCG
        # iterator across calls)
        self.out_codec.limit_from_type(compression_type)
        encrypted = self.out_codec.encrypt(compressed)

        # Build packet
        packet = bytes([compression_type]) + encrypted
        framed = _frame(packet)
        return framed if framed is not None else b""

    def decode_packet(self, data: bytes) -> Optional[bytes]:
        """
        Decode received packet from client.

        Args:
            data: Packet data (without length prefix)

        Returns:
            Decrypted and decompressed data, or None on error
        """
        if not data:
            return None

        # First packet is plain zlib (login packet)
        if self._first_decode:
            self._first_decode = False
            try:
                return zlib.decompress(data)
            except (zlib.error, OSError):  # corrupt compressed data
                return None

        # Normal packet
        compression_type = data[0]

        if compression_type not in [CompressionType.UNCOMPRESSED,
                                    CompressionType.ZLIB,
                                    CompressionType.BZ2]:
            return None

        encrypted_data = data[1:]

        # Decrypt (in place on the persistent codec)
        self.in_codec.limit_from_type(compression_type)
        decrypted = self.in_codec.decrypt(encrypted_data)

        # Decompress
        try:
            return decompress_data(decrypted, compression_type)
        except (zlib.error, OSError):  # corrupt compressed data
            return None

    def reset_decode_state(self) -> None:
        """Reset decode state (e.g., for reconnection handling)."""
        self._first_decode = True


# =============================================================================
# Gen3Codec - Single-byte-insertion encryption + zlib compression
# =============================================================================

class Gen3Codec:
    """
    ENCRYPT_GEN_3 codec (client side) - Reborn 1.41 - 2.18 era clients.

    Wire behavior (authoritative: gs2lib CEncryption.cpp + GServer
    IPacketHandler.h/CFileQueue.cpp):

    - client -> server: each packet (WITHOUT its trailing newline) gets ONE
      byte INSERTED at pos = (iterator & 0xFFFF) % len(packet_with_insertion);
      iterator advances once per packet (iterator = iterator*0x8088405 + key).
      The server strips it in parsePacketsFromBundle via CEncryption::decrypt
      (removeI at pos computed over the received length). Packets are then
      newline-joined, the bundle zlib-compressed and length-prefixed.
      The filler must not be '\\n' (would break the server's newline framing);
      we use ')' like CEncryption::encrypt does.
    - server -> client: plain zlib bundles, NO per-packet encryption
      (CFileQueue's ENCRYPT_GEN_3 case compresses but never calls encrypt()).
    """

    ITERATOR_START = 0x04A80B38
    MULTIPLIER = 0x8088405

    def __init__(self, encryption_key: int = 0):
        self.encryption_key = encryption_key & 0xFF
        self.out_iterator = self.ITERATOR_START

    def set_key(self, key: int) -> None:
        self.encryption_key = key & 0xFF
        self.out_iterator = self.ITERATOR_START

    def send_packet(self, data: bytes) -> bytes:
        """Encode one packet ({id+32}{payload}\\n) into a length-prefixed
        zlib bundle with the gen-3 insertion applied."""
        body = data[:-1] if data.endswith(b'\n') else data

        # Advance the LCG once per packet and insert the filler byte. The
        # server computes the removal position over the RECEIVED length
        # (len(body) + 1), so the insertion position must use that too.
        self.out_iterator = (self.out_iterator * self.MULTIPLIER
                             + self.encryption_key) & 0xFFFFFFFF
        pos = (self.out_iterator & 0xFFFF) % (len(body) + 1)
        body = body[:pos] + b')' + body[pos:]

        compressed = zlib.compress(body + b'\n')
        framed = _frame(compressed)
        return framed if framed is not None else b""

    def recv_packet(self, data: bytes) -> Optional[bytes]:
        """Decode a server bundle: plain zlib, no decryption."""
        if not data:
            return None
        try:
            return zlib.decompress(data)
        except Exception:
            return None


# =============================================================================
# Gen4Codec - Partial packet encryption + bz2 compression
# =============================================================================

class Gen4Codec:
    """
    ENCRYPT_GEN_4 codec (client side) - Reborn 2.19 - 2.21 / 3.x era clients.

    Both directions: bundle -> bz2 compress -> XOR-encrypt with the GEN_5 LCG
    (iterator start 0x4A80B38, limit 4 iterations = first 16 bytes, limit
    re-armed per bundle via limitFromType(COMPRESS_BZ2)) -> {u16 len}{data}.
    Unlike GEN_5 there is NO compression-type byte: GEN_4 is always bz2
    (gs2lib CFileQueue.cpp ENCRYPT_GEN_4 / GServer IPacketHandler.h
    processPacketBundle).
    """

    def __init__(self, encryption_key: int = 0):
        self.encryption_key = encryption_key
        self.in_codec = RebornEncryption(encryption_key)
        self.out_codec = RebornEncryption(encryption_key)

    def set_key(self, key: int) -> None:
        self.encryption_key = key
        self.in_codec.reset(key)
        self.out_codec.reset(key)

    def send_packet(self, data: bytes) -> bytes:
        """Encode packet bundle: bz2 + partial XOR, no type byte."""
        compressed = bz2.compress(data)
        self.out_codec.limit_from_type(CompressionType.BZ2)
        encrypted = self.out_codec.encrypt(compressed)
        framed = _frame(encrypted)
        return framed if framed is not None else b""

    def recv_packet(self, data: bytes) -> Optional[bytes]:
        """Decode a server bundle: partial XOR then bz2 decompress."""
        if not data:
            return None
        self.in_codec.limit_from_type(CompressionType.BZ2)
        decrypted = self.in_codec.decrypt(data)
        try:
            return bz2.decompress(decrypted)
        except Exception:
            return None


# =============================================================================
# Gen2Codec - List server codec
# =============================================================================

class Gen2Codec:
    """
    ENCRYPT_GEN_2 codec for list server communication.

    No encryption, only zlib compression. Used for server-to-listserver
    communication after the initial REGISTERV3 packet.
    """

    def __init__(self):
        pass

    def send_packet(self, data: bytes) -> bytes:
        """
        Encode packet for sending (returns with length prefix).

        Args:
            data: Packet data to send

        Returns:
            Length-prefixed zlib-compressed packet
        """
        # Compress with zlib
        compressed = zlib.compress(data)

        # Return with length prefix
        framed = _frame(compressed)
        return framed if framed is not None else b""

    def recv_packet(self, data: bytes) -> Optional[bytes]:
        """
        Decode received packet.

        Args:
            data: Packet data (without length prefix)

        Returns:
            Decompressed data, or None on error
        """
        if not data:
            return None

        # Try zlib decompression first
        try:
            return zlib.decompress(data)
        except (zlib.error, OSError):  # corrupt compressed data
            # If decompression fails, return raw data (might be uncompressed)
            return data


# =============================================================================
# Gen1Codec - Plain codec (no compression, no encryption)
# =============================================================================

class Gen1Codec:
    """
    ENCRYPT_GEN_1 codec for initial list server registration.

    No encryption, no compression, NO LENGTH PREFIX.
    Used for the REGISTERV3 packet - sends raw data directly.
    """

    def __init__(self):
        pass

    def send_packet(self, data: bytes) -> bytes:
        """
        Encode packet for sending (raw data, no length prefix).

        Args:
            data: Packet data to send

        Returns:
            Raw packet data (no compression/encryption/length prefix)
        """
        # Return raw data - NO length prefix for Gen1
        return data

    def recv_packet(self, data: bytes) -> Optional[bytes]:
        """
        Decode received packet.

        Args:
            data: Packet data (without length prefix)

        Returns:
            Raw data
        """
        return data if data else None
