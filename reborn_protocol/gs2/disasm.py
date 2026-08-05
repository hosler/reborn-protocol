"""GS2 bytecode disassembler.

Decodes the raw opcode stream (GS2Container.code) into a linear list of
Instruction objects and renders a readable listing, mirroring the style of
`GServer-v2 dependencies/fc/gs2parser-src/src/decompiler` output closely
enough to cross-check by eye.

Key fact (see container.py / opcodes.py docstrings for how this was derived
and verified): the compiler's "opIndex" -- referenced by the function table
and by every jump operand -- counts *instructions emitted*, not bytes. Only
GS2Bytecode::emit(opcode::Opcode) increments it; the emit(char/short/int/str)
overloads used for operands do not. So a jump/function-table target of N
means "the Nth opcode encountered while decoding linearly", not byte offset
N. This module tracks that mapping while decoding so operands can be
resolved to byte offsets for display.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from typing import List, Optional, Tuple, Union

from .container import GS2Container, FunctionEntry
from .opcodes import Op, op_name, NUMBER_OPS, INDEX_OPS, OPERAND_OPS

# Jump-target-carrying ops (subset of NUMBER_OPS): the dynamic-number operand
# here is an instruction index to branch to, not a literal.
JUMP_OPS = frozenset({
    Op.OP_SET_INDEX, Op.OP_SET_INDEX_TRUE, Op.OP_OR, Op.OP_IF, Op.OP_AND,
    Op.OP_WITH, Op.OP_FOREACH,
})


class GS2DecodeError(ValueError):
    pass


_STRTOD_PREFIX = re.compile(
    r"[ \t\r\n\f\v]*[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_HEX_PREFIX = re.compile(r"[0-9a-fA-F]+")


def strtofloat(text: str) -> float:
    """The reference loader's conversion for textual (0xF6) number literals.

    The mobile reference feeds the operand text to strtofloat
    (TScript.cpp:257-270 -> TInitStatics.cpp:4335-4386): "true" -> 1.0,
    "false" -> 0.0, a "0x" prefix through strtoul(16), and everything else
    through strtod -- and when strtod consumes NOTHING the result is -1.0
    (DOUBLE_004023f0, TInitStatics.cpp:1269), not 0.0. strtod also parses
    the longest valid numeric PREFIX, so trailing junk does not void the
    number ("1.5x" -> 1.5).

    Real content hits the unparseable branch: LTTP's -Player/Movement
    weapon carries the literal '--0.5' (the compiler textualized a double
    unary minus), which float() refuses -- an earlier blanket 0.0 fallback
    here silently mis-decoded it; the reference reads -1.0.
    """
    if not text:
        return 0.0
    if text == "true":
        return 1.0
    if text == "false":
        return 0.0
    if text.startswith("0x"):
        m = _HEX_PREFIX.match(text[2:])
        return float(int(m.group(0), 16)) if m else 0.0
    m = _STRTOD_PREFIX.match(text)
    if m is None:
        return -1.0
    try:
        return float(m.group(0))
    except ValueError:  # pragma: no cover - prefix regex should not fail
        return -1.0


@dataclass
class Operand:
    kind: str          # "index" | "number" | "float" | "jump"
    marker: int         # the 0xF0-0xF6 marker byte
    width: str           # "u8"/"u16"/"u32"/"i8"/"i16"/"i32"/"cstr"
    value: Union[int, float]
    raw_text: Optional[str] = None   # original text for float-string operands
    nbytes: int = 0                   # bytes consumed after the marker byte


@dataclass
class Instruction:
    idx: int             # instruction index (opIndex in the compiler)
    offset: int           # byte offset of the opcode byte within the code segment
    opnum: int             # raw opcode byte value
    operand: Optional[Operand] = None

    @property
    def op(self) -> Op:
        try:
            return Op(self.opnum)
        except ValueError:
            return None  # type: ignore[return-value]

    @property
    def length(self) -> int:
        if not self.operand:
            return 1
        marker_bytes = 0 if self.operand.marker < 0 else 1
        return 1 + marker_bytes + self.operand.nbytes


def _read(fmt: str, code: bytes, pos: int, what: str) -> int:
    size = struct.calcsize(fmt)
    if pos + size > len(code):
        raise GS2DecodeError(f"truncated {what} operand at offset {pos} (len={len(code)})")
    return struct.unpack_from(fmt, code, pos)[0]


def _operand_kind(op: Optional[Op]) -> str:
    return "jump" if op in JUMP_OPS else ("index" if op in INDEX_OPS else "number")


def _parse_typed_operand(op: Optional[Op], marker: int, code: bytes,
                         pos: int) -> Tuple[Operand, int]:
    """Parse one typed operand record; *pos* points just past the marker
    byte. Returns the Operand and the position after the operand data."""
    start = pos
    if marker == 0xF0:
        value = _read(">B", code, pos, "u8"); pos += 1
        operand = Operand("jump" if op in JUMP_OPS else "index", marker, "u8", value)
    elif marker == 0xF1:
        value = _read(">H", code, pos, "u16"); pos += 2
        operand = Operand("jump" if op in JUMP_OPS else "index", marker, "u16", value)
    elif marker == 0xF2:
        value = _read(">I", code, pos, "u32"); pos += 4
        operand = Operand("jump" if op in JUMP_OPS else "index", marker, "u32", value)
    elif marker == 0xF3:
        value = _read(">b", code, pos, "i8"); pos += 1
        operand = Operand("jump" if op in JUMP_OPS else "number", marker, "i8", value)
    elif marker == 0xF4:
        value = _read(">h", code, pos, "i16"); pos += 2
        operand = Operand("jump" if op in JUMP_OPS else "number", marker, "i16", value)
    elif marker == 0xF5:
        value = _read(">i", code, pos, "i32"); pos += 4
        operand = Operand("jump" if op in JUMP_OPS else "number", marker, "i32", value)
    else:  # 0xF6
        end = code.find(b"\x00", pos)
        if end == -1:
            raise GS2DecodeError(f"unterminated float literal at offset {pos}")
        text = code[pos:end].decode("ascii", errors="replace")
        pos = end + 1
        # strtofloat, not float(): the reference's loader semantics (prefix
        # parse, and -1.0 -- not 0.0 -- for unparseable text like '--0.5').
        operand = Operand("float", marker, "cstr", strtofloat(text),
                          raw_text=text)
    operand.nbytes = pos - start
    return operand, pos


def decode(code: bytes) -> List[Instruction]:
    """Decode a raw GS2 opcode stream into a flat instruction list.

    Raises GS2DecodeError on truncated/malformed operand data. Unknown
    opcode *numbers* (no case in Op) are not an error here: they decode as
    bare instructions. Operand markers (0xF0-0xF6) are separate stream
    records attached to the preceding opcode -- both the C# client's loader
    and the official interpreter (quattroplay executeScript has jumptable
    cases for 0xF0-0xF6 that merely load the operand register) treat them
    that way, so a marker byte is NEVER an instruction: for ops in
    OPERAND_OPS we attach it eagerly, and for any other opcode a trailing
    marker record still attaches to it instead of desyncing the stream.
    """
    instrs: List[Instruction] = []
    pos = 0
    idx = 0
    n = len(code)

    while pos < n:
        opnum = code[pos]
        offset = pos

        try:
            op = Op(opnum)
        except ValueError:
            op = None

        if 0xF0 <= opnum <= 0xF6:
            # Operand record in opcode position: belongs to the previous
            # instruction (whatever it was -- see docstring). Consuming it
            # here guarantees a marker byte is never executed as an opcode.
            pos += 1
            prev = instrs[-1] if instrs else None
            prev_op = prev.op if prev is not None else None
            operand, pos = _parse_typed_operand(prev_op, opnum, code, pos)
            if prev is not None:
                prev.operand = operand  # last record wins (operand register)
            continue

        pos += 1
        operand = None

        if op in OPERAND_OPS:
            marker = code[pos] if pos < n else -1
            if not 0xF0 <= marker <= 0xF6:
                # No typed record follows: the operand keeps its
                # zero-initialized value (implicit-zero record).
                operand = Operand(_operand_kind(op), -1, "implicit", 0)
            else:
                pos += 1
                operand, pos = _parse_typed_operand(op, marker, code, pos)

        instrs.append(Instruction(idx=idx, offset=offset, opnum=opnum, operand=operand))
        idx += 1

    return instrs


def format_listing(container: GS2Container) -> str:
    """Render a full human-readable listing: GS1 flags, function table,
    string table, and the disassembled instruction stream with jump/function
    targets resolved to instruction index + byte offset."""
    lines: List[str] = []

    lines.append(f"; gs1_flags = 0x{container.gs1_flags:08X}")

    if container.unknown_segments:
        for seg_id in container.unknown_segments:
            lines.append(f"; WARNING: unknown segment id={seg_id} ({len(container.unknown_segments[seg_id])} bytes) preserved but not interpreted")

    lines.append(f"; functions ({len(container.functions)}):")
    for f in container.functions:
        lines.append(f";   op#{f.op_index:<5} {f.name}")

    lines.append(f"; strings ({len(container.strings)}):")
    for i, s in enumerate(container.strings):
        lines.append(f";   [{i}] {s!r}")

    instrs = decode(container.code)
    by_idx = {i.idx: i for i in instrs}
    func_by_idx = {f.op_index: f.name for f in container.functions}

    lines.append(";")
    lines.append("; disassembly:")
    for instr in instrs:
        fn = func_by_idx.get(instr.idx)
        if fn is not None:
            lines.append(f"{fn}:")

        mnem = op_name(instr.opnum)
        operand_text = ""
        if instr.operand:
            o = instr.operand
            if o.kind == "jump":
                target = by_idx.get(o.value)
                if target is not None:
                    target_off = f"0x{target.offset:04X}"
                elif o.value == len(instrs):
                    target_off = f"0x{len(container.code):04X} (end)"
                else:
                    target_off = "??? (out of range)"
                operand_text = f" -> #{o.value} (offset {target_off})"
            elif o.kind == "index":
                sval = container.strings[o.value] if 0 <= o.value < len(container.strings) else None
                operand_text = f" [{o.value}]" + (f" {sval!r}" if sval is not None else "")
            elif o.kind == "float":
                operand_text = f" {o.value} ({o.raw_text!r})"
            else:
                operand_text = f" {o.value}"

        lines.append(f"  {instr.idx:5d}  0x{instr.offset:04X}  {mnem}{operand_text}")

    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    from .container import parse_container

    parser = argparse.ArgumentParser(description="Disassemble a GS2 bytecode blob")
    parser.add_argument("file", help="path to a raw GS2 bytecode blob (container format)")
    args = parser.parse_args(argv)

    with open(args.file, "rb") as fh:
        data = fh.read()

    container = parse_container(data)
    print(format_listing(container))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
