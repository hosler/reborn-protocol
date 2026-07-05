"""GS2 (Reborn Script 2) bytecode opcode table.

Mirrors GServer-v2/build/dependencies/fc/gs2parser-src/src/opcodes.h exactly
(same names, same numeric values) -- that header is the authoritative source
since it is the compiler GServer itself uses to produce the bytecode we
receive over the wire. See memory: none yet (see task-agent report).

Values with no case in opcode::OpcodeToString() are commented "reserved" or
left unnamed in the C++ source; we still assign them a symbolic name here
(OP_<n>) purely so the disassembler has something to print. Do not treat an
unnamed op's presence in this table as evidence of understood semantics.
"""
from __future__ import annotations

from enum import IntEnum


class Op(IntEnum):
    OP_NONE = 0
    OP_SET_INDEX = 1
    OP_SET_INDEX_TRUE = 2
    OP_OR = 3
    OP_IF = 4
    OP_AND = 5
    OP_CALL = 6
    OP_RET = 7
    OP_SLEEP = 8
    OP_CMD_CALL = 9
    OP_JMP = 10
    OP_WAITFOR = 11

    OP_TYPE_NUMBER = 20
    OP_TYPE_STRING = 21
    OP_TYPE_VAR = 22
    OP_TYPE_ARRAY = 23
    OP_TYPE_TRUE = 24
    OP_TYPE_FALSE = 25
    OP_TYPE_NULL = 26
    OP_PI = 27

    OP_COPY_LAST_OP = 30
    OP_SWAP_LAST_OPS = 31
    OP_INDEX_DEC = 32
    OP_CONV_TO_FLOAT = 33
    OP_CONV_TO_STRING = 34
    OP_MEMBER_ACCESS = 35
    OP_CONV_TO_OBJECT = 36
    OP_ARRAY_END = 37
    OP_ARRAY_NEW = 38
    OP_SETARRAY = 39
    OP_INLINE_NEW = 40
    OP_MAKEVAR = 41
    OP_NEW_OBJECT = 42
    OP_OBJ_FROM_STR = 43
    OP_INLINE_CONDITIONAL = 44
    OP_UNKNOWN_45 = 45
    OP_UNKNOWN_46 = 46
    OP_UNKNOWN_47 = 47

    OP_ASSIGN = 50
    OP_FUNC_PARAMS_END = 51
    OP_INC = 52
    OP_DEC = 53
    OP_UNKNOWN_54 = 54

    OP_ADD = 60
    OP_SUB = 61
    OP_MUL = 62
    OP_DIV = 63
    OP_MOD = 64
    OP_POW = 65
    OP_UNKNOWN_66 = 66
    OP_UNKNOWN_67 = 67
    OP_NOT = 68
    OP_UNARYSUB = 69
    OP_EQ = 70
    OP_NEQ = 71
    OP_LT = 72
    OP_GT = 73
    OP_LTE = 74
    OP_GTE = 75
    OP_BWO = 76
    OP_BWA = 77
    OP_BWX = 78
    OP_BWI = 79
    OP_IN_RANGE = 80
    OP_IN_OBJ = 81
    OP_OBJ_INDEX = 82
    OP_OBJ_TYPE = 83
    OP_FORMAT = 84
    OP_INT = 85
    OP_ABS = 86
    OP_RANDOM = 87
    OP_SIN = 88
    OP_COS = 89
    OP_ARCTAN = 90
    OP_EXP = 91
    OP_LOG = 92
    OP_MIN = 93
    OP_MAX = 94
    OP_GETANGLE = 95
    OP_GETDIR = 96
    OP_VECX = 97
    OP_VECY = 98
    OP_OBJ_INDICES = 99
    OP_OBJ_LINK = 100
    OP_BW_LEFTSHIFT = 101
    OP_BW_RIGHTSHIFT = 102
    OP_CHAR = 103
    OP_OBJ_COMPARE = 104

    OP_OBJ_TRIM = 110
    OP_OBJ_LENGTH = 111
    OP_OBJ_POS = 112
    OP_JOIN = 113
    OP_OBJ_CHARAT = 114
    OP_OBJ_SUBSTR = 115
    OP_OBJ_STARTS = 116
    OP_OBJ_ENDS = 117
    OP_OBJ_TOKENIZE = 118
    OP_TRANSLATE = 119
    OP_OBJ_POSITIONS = 120

    OP_OBJ_SIZE = 130
    OP_ARRAY = 131
    OP_ARRAY_ASSIGN = 132
    OP_ARRAY_MULTIDIM = 133
    OP_ARRAY_MULTIDIM_ASSIGN = 134
    OP_OBJ_SUBARRAY = 135
    OP_OBJ_ADDSTRING = 136
    OP_OBJ_DELETESTRING = 137
    OP_OBJ_REMOVESTRING = 138
    OP_OBJ_REPLACESTRING = 139
    OP_OBJ_INSERTSTRING = 140
    OP_OBJ_CLEAR = 141
    OP_ARRAY_NEW_MULTIDIM = 142

    OP_WITH = 150
    OP_WITHEND = 151

    OP_FOREACH = 163

    OP_THIS = 180
    OP_THISO = 181
    OP_PLAYER = 182
    OP_PLAYERO = 183
    OP_LEVEL = 184
    OP_TEMP = 189
    OP_PARAMS = 190


#: Opcodes which are immediately followed by a "dynamic operand": a 1-byte
#: marker (0xF0-0xF6) selecting the operand's width/kind, then the operand
#: itself. Every other opcode carries zero operand bytes. Derived directly
#: from GS2Bytecode::emitDynamicNumber{,Unsigned}/emitDoubleNumber call sites
#: in GS2CompilerVisitor.cpp (the only places that emit a marker byte) and
#: hand-verified byte-for-byte against tests/baselines/basic/01_variables.
OPERAND_OPS = frozenset({
    Op.OP_SET_INDEX,
    Op.OP_SET_INDEX_TRUE,
    Op.OP_OR,
    Op.OP_IF,
    Op.OP_AND,
    Op.OP_TYPE_NUMBER,
    Op.OP_TYPE_STRING,
    Op.OP_TYPE_VAR,
    Op.OP_WITH,
    Op.OP_FOREACH,
})

#: Ops for which the dynamic operand is a signed number (literal constant or
#: a jump-target instruction-index -- disambiguated by which opcode owns it,
#: never by the marker itself). Markers 0xF3/0xF4/0xF5 -> int8/int16/int32,
#: 0xF6 -> NUL-terminated ASCII float string.
NUMBER_OPS = frozenset({
    Op.OP_SET_INDEX, Op.OP_SET_INDEX_TRUE, Op.OP_OR, Op.OP_IF, Op.OP_AND,
    Op.OP_TYPE_NUMBER, Op.OP_WITH, Op.OP_FOREACH,
})

#: Ops for which the dynamic operand is an unsigned index into the string
#: table (doubles as the variable-name table). Markers 0xF0/0xF1/0xF2 ->
#: uint8/uint16/uint32.
INDEX_OPS = frozenset({Op.OP_TYPE_STRING, Op.OP_TYPE_VAR})

#: Ops that push/reference a reserved identifier directly (no operand).
RESERVED_IDENT_OPS = frozenset({
    Op.OP_THIS, Op.OP_THISO, Op.OP_PLAYER, Op.OP_PLAYERO, Op.OP_LEVEL, Op.OP_TEMP,
})

#: Ops whose result is boolean (per opcode::IsBooleanReturningOp).
BOOLEAN_RETURNING_OPS = frozenset({
    Op.OP_NOT, Op.OP_EQ, Op.OP_NEQ, Op.OP_LT, Op.OP_GT, Op.OP_LTE, Op.OP_GTE,
    Op.OP_IN_RANGE, Op.OP_IN_OBJ,
})

#: Ops whose result is treated as an object reference (per
#: opcode::IsObjectReturningOp / IsReservedIdentOp -- same set in the C++).
OBJECT_RETURNING_OPS = RESERVED_IDENT_OPS


def op_name(value: int) -> str:
    """Best-effort opcode name, matching opcode::OpcodeToString()'s fallback
    of "OP <n>" for values with no enum case."""
    try:
        return Op(value).name
    except ValueError:
        return f"OP_{value}"
