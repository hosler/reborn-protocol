"""GS2 bytecode container (segment) format.

Mirrors GS2Bytecode::getByteCode() in
GServer-v2/build/dependencies/fc/gs2parser-src/src/codegen/GS2Bytecode.cpp:

    repeated { uint32 segment_id BE, uint32 segment_len BE, payload }
    followed by a single trailing '\\n' byte (outside any segment's declared
    length -- GS2Bytecode.cpp writes it manually after the bytecode segment).

All header/length integers use plain big-endian encoding (encoding::Int32 /
encoding::Int16 in graalencoding.h), NOT the Graal "+32" variable-length byte
encoding used elsewhere in the wire protocol (GraalByte/GraalShort/...) --
that family is never used inside the GS2 container or bytecode stream.

Segment IDs (from GS2Bytecode.cpp's anonymous enum):
    1 = GS1FLAGS      -- a single uint32 bitflag for legacy GS1 event hooks
    2 = FUNCTIONTABLE  -- repeated {uint32 op_index BE, NUL-terminated name}
    3 = STRINGTABLE    -- repeated NUL-terminated strings (string-const /
                          variable-name table, referenced by index from the
                          bytecode stream)
    4 = BYTECODE       -- raw opcode stream (see opcodes.py, disasm.py)

Byte-for-byte verified against every *.bytecode file in
GServer-v2/build/dependencies/fc/gs2parser-src/tests/baselines/.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional

SEGMENT_GS1FLAGS = 1
SEGMENT_FUNCTIONTABLE = 2
SEGMENT_STRINGTABLE = 3
SEGMENT_BYTECODE = 4


class GS2ContainerError(ValueError):
    """Raised when a blob does not parse as a well-formed GS2 container."""


@dataclass
class FunctionEntry:
    name: str
    op_index: int


@dataclass
class GS2Container:
    gs1_flags: int = 0
    functions: List[FunctionEntry] = field(default_factory=list)
    strings: List[str] = field(default_factory=list)
    code: bytes = b""
    #: Any segment IDs not recognized above, kept verbatim for forward compat.
    unknown_segments: Dict[int, bytes] = field(default_factory=dict)
    #: True if a trailing '\n' byte followed the last declared segment, as
    #: GS2Bytecode.cpp always emits (present here mainly for round-trip/debug
    #: purposes, not required for parsing).
    trailing_newline: bool = False

    def function_by_index(self, op_index: int) -> Optional[str]:
        for f in self.functions:
            if f.op_index == op_index:
                return f.name
        return None


def _read_u32(data: bytes, pos: int) -> int:
    if pos + 4 > len(data):
        raise GS2ContainerError(f"truncated u32 at offset {pos} (len={len(data)})")
    return struct.unpack_from(">I", data, pos)[0]


def _read_cstr(data: bytes, pos: int) -> (str, int):
    end = data.find(b"\x00", pos)
    if end == -1:
        raise GS2ContainerError(f"unterminated string at offset {pos}")
    # GS2 source strings are not guaranteed strict ASCII/UTF-8 (player input,
    # etc can end up in string constants) -- decode leniently.
    return data[pos:end].decode("utf-8", errors="surrogateescape"), end + 1


def parse_container(data: bytes) -> GS2Container:
    """Parse a raw GS2 bytecode blob (as received in PLO_NPCBYTECODE /
    PLO_GANISCRIPT / PLO_NPCWEAPONSCRIPT / etc, or read from GS2Bytecode's
    on-disk output) into a GS2Container.

    Raises GS2ContainerError on malformed input rather than silently
    returning partial data.
    """
    container = GS2Container()
    pos = 0
    n = len(data)

    while pos < n:
        # Tolerate a lone trailing '\n' after the last segment (always
        # present in bytecode emitted by GS2Bytecode::getByteCode()).
        if n - pos == 1 and data[pos:pos + 1] == b"\n":
            container.trailing_newline = True
            pos += 1
            break

        if pos + 8 > n:
            raise GS2ContainerError(
                f"truncated segment header at offset {pos} (len={n})"
            )

        seg_id = _read_u32(data, pos)
        seg_len = _read_u32(data, pos + 4)
        pos += 8

        if pos + seg_len > n:
            raise GS2ContainerError(
                f"segment {seg_id} claims length {seg_len} but only "
                f"{n - pos} bytes remain at offset {pos}"
            )

        payload = data[pos:pos + seg_len]
        pos += seg_len

        if seg_id == SEGMENT_GS1FLAGS:
            container.gs1_flags = _read_u32(payload, 0) if len(payload) >= 4 else 0
        elif seg_id == SEGMENT_FUNCTIONTABLE:
            p = 0
            while p < len(payload):
                op_index = _read_u32(payload, p)
                p += 4
                name, p = _read_cstr(payload, p)
                container.functions.append(FunctionEntry(name=name, op_index=op_index))
        elif seg_id == SEGMENT_STRINGTABLE:
            p = 0
            while p < len(payload):
                s, p = _read_cstr(payload, p)
                container.strings.append(s)
        elif seg_id == SEGMENT_BYTECODE:
            container.code = payload
        else:
            container.unknown_segments[seg_id] = payload

    return container
