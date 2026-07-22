"""
reborn_protocol.encryption - ENCRYPT_GEN_5 implementation

Provides XOR-based encryption/decryption and compression utilities
for the Reborn Online protocol (versions 2.22 to 6.037).

The encryption uses a Linear Congruential Generator (LCG) to produce
a pseudorandom byte stream that is XORed with the data. The same
algorithm is used for both encryption and decryption.
"""

import struct
import zlib
import bz2
import logging
from typing import Tuple


logger = logging.getLogger(__name__)

MAX_DECOMPRESSED_SIZE = 8 * 1024 * 1024


class CompressionType:
    """Compression type identifiers for Gen5 protocol."""
    UNCOMPRESSED = 0x02
    ZLIB = 0x04
    BZ2 = 0x06


class RebornEncryption:
    """
    ENCRYPT_GEN_5 XOR cipher implementation.

    Uses a Linear Congruential Generator (LCG) state machine with:
    - key: The encryption key (from login packet)
    - iterator: LCG state (initial value 0x4A80B38)
    - multiplier: LCG multiplier (0x8088405)
    - limit: Number of 4-byte blocks to encrypt (-1 = unlimited)

    The encryption limit varies by compression type:
    - UNCOMPRESSED: 12 iterations (48 bytes)
    - ZLIB: 4 iterations (16 bytes)
    - BZ2: 4 iterations (16 bytes)
    """

    # LCG constants
    INITIAL_ITERATOR = 0x4A80B38
    MULTIPLIER = 0x8088405

    def __init__(self, key: int = 0):
        """
        Initialize encryption with key.

        Args:
            key: Encryption key from login packet (0-127)
        """
        self.key = key
        self.iterator = self.INITIAL_ITERATOR
        self.limit = -1  # -1 = encrypt all bytes
        self.multiplier = self.MULTIPLIER

    def reset(self, key: int) -> None:
        """
        Reset encryption state with new key.

        Args:
            key: New encryption key
        """
        self.key = key
        self.iterator = self.INITIAL_ITERATOR
        self.limit = -1

    def limit_from_type(self, compression_type: int) -> None:
        """
        Set encryption limit based on compression type.

        Args:
            compression_type: One of CompressionType values
        """
        if compression_type == CompressionType.UNCOMPRESSED:
            self.limit = 0x0C  # 12 iterations = 48 bytes
        elif compression_type == CompressionType.ZLIB:
            self.limit = 0x04  # 4 iterations = 16 bytes
        elif compression_type == CompressionType.BZ2:
            self.limit = 0x04  # 4 iterations = 16 bytes

    def encrypt(self, data: bytes) -> bytes:
        """
        Encrypt data using XOR cipher.

        The encryption XORs data with bytes from the LCG state.
        Every 4 bytes, the LCG state is advanced:
            iterator = (iterator * multiplier + key) mod 2^32

        The number of bytes encrypted depends on the limit setting:
        - limit < 0: encrypt all bytes
        - limit == 0: encrypt nothing (return data unchanged)
        - limit > 0: encrypt up to limit * 4 bytes

        Args:
            data: Data to encrypt

        Returns:
            Encrypted data
        """
        result = bytearray(data)

        if self.limit < 0:
            bytes_to_encrypt = len(data)
        elif self.limit == 0:
            return bytes(result)
        else:
            bytes_to_encrypt = min(len(data), self.limit * 4)

        # Pack the iterator once per 4-byte group instead of once per byte
        # (struct.pack was being called on every iteration even though the
        # value it produces only changes every 4th byte).
        iterator_bytes = b""
        for i in range(bytes_to_encrypt):
            if i % 4 == 0:
                # Advance LCG state (the self.limit == 0 case already
                # returned above, so this branch is never dead here)
                self.iterator = (self.iterator * self.multiplier + self.key) & 0xFFFFFFFF
                if self.limit > 0:
                    self.limit -= 1
                iterator_bytes = struct.pack('<I', self.iterator)

            # XOR with iterator byte
            result[i] ^= iterator_bytes[i % 4]

        return bytes(result)

    def decrypt(self, data: bytes) -> bytes:
        """
        Decrypt data (same as encrypt due to XOR symmetry).

        Args:
            data: Data to decrypt

        Returns:
            Decrypted data
        """
        return self.encrypt(data)


def compress_data(data: bytes) -> Tuple[bytes, int]:
    """
    Compress data using appropriate method based on size.

    Compression method is selected based on data size:
    - <= 55 bytes: No compression (UNCOMPRESSED)
    - <= 8192 bytes: zlib compression (ZLIB)
    - > 8192 bytes: bz2 compression (BZ2)

    Args:
        data: Data to compress

    Returns:
        Tuple of (compressed_data, compression_type)
    """
    if len(data) <= 55:
        return data, CompressionType.UNCOMPRESSED
    elif len(data) > 0x2000:  # 8192 bytes
        return bz2.compress(data), CompressionType.BZ2
    else:
        return zlib.compress(data), CompressionType.ZLIB


def decompress_data(data: bytes, compression_type: int) -> bytes:
    """
    Decompress data based on compression type.

    Args:
        data: Compressed data
        compression_type: One of CompressionType values

    Returns:
        Decompressed data

    Raises:
        zlib.error: If zlib decompression fails
        OSError: If bz2 decompression fails
    """
    if compression_type == CompressionType.ZLIB:
        decompressor = zlib.decompressobj()
        result = decompressor.decompress(data, max_length=MAX_DECOMPRESSED_SIZE)
        if decompressor.unconsumed_tail:
            logger.warning("rejecting zlib packet exceeding decompression limit (%d bytes)",
                           MAX_DECOMPRESSED_SIZE)
            raise zlib.error("decompressed packet exceeds size limit")
        if not decompressor.eof:
            raise zlib.error("incomplete or invalid compressed packet")
        return result
    elif compression_type == CompressionType.BZ2:
        decompressor = bz2.BZ2Decompressor()
        result = decompressor.decompress(data, max_length=MAX_DECOMPRESSED_SIZE)
        if not decompressor.eof and not decompressor.needs_input:
            logger.warning("rejecting bz2 packet exceeding decompression limit (%d bytes)",
                           MAX_DECOMPRESSED_SIZE)
            raise OSError("decompressed packet exceeds size limit")
        if not decompressor.eof:
            raise OSError("incomplete or invalid compressed packet")
        return result
    else:
        return data
