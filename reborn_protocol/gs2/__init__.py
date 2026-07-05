"""GS2 (Reborn Script 2) bytecode container parser, disassembler and VM.

Mirrors the layout/style of the sibling `reborn_protocol.gs1` package. See
memory for the build log; ground-truth sources for the bytecode format:

- GServer-v2/build/dependencies/fc/gs2parser-src/src/opcodes.h (opcode enum)
- .../src/codegen/GS2Bytecode.{h,cpp} (container format + operand encoding)
- .../src/compiler/GS2CompilerVisitor.cpp (which opcodes carry operands, and
  how -- every operand-bearing opcode is enumerated in opcodes.OPERAND_OPS,
  derived from this file's emit() call sites)
- .../tests/baselines/**/*.bytecode (golden test vectors)
"""

from .opcodes import Op, op_name
from .container import GS2Container, FunctionEntry, GS2ContainerError, parse_container
from .disasm import Instruction, Operand, GS2DecodeError, decode, format_listing
from .values import GS2Object, LValue, VarRef, to_num, to_str, to_bool, gs2_eq
from .vm import GS2VM, GS2Host, NOT_HANDLED, printf_format

__all__ = [
    "Op", "op_name",
    "GS2Container", "FunctionEntry", "GS2ContainerError", "parse_container",
    "Instruction", "Operand", "GS2DecodeError", "decode", "format_listing",
    "GS2Object", "LValue", "VarRef", "to_num", "to_str", "to_bool", "gs2_eq",
    "GS2VM", "GS2Host", "NOT_HANDLED", "printf_format",
]
