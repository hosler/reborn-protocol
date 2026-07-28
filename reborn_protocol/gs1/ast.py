"""GS1 AST node definitions.

These mirror GS1Parser.g4's structure/precedence but are shaped for execution by
the Phase-3 visitor rather than to reproduce ANTLR's parse tree verbatim.
See memory: gs1-python-port.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# -- top level / statements -------------------------------------------------
@dataclass
class Program:
    body: list


@dataclass
class Block:
    body: list


@dataclass
class If:
    cond: Any
    then: list
    els: Optional[list] = None


@dataclass
class For:
    init: Any
    cond: Any
    post: Any
    body: list


@dataclass
class While:
    cond: Any
    body: list


@dataclass
class With:
    obj: Any
    body: list


@dataclass
class Switch:
    value: Any
    cases: list
    default: Optional[list] = None


@dataclass
class FuncDef:
    name: str
    body: list
    # Era-2006 "new GS1" allows GS2-style parameter lists on function
    # declarations (era's -System/-MoveSystem/-BulletSystem clientside all
    # declare `function onActionX(a, b, c)` in otherwise-classic GS1 —
    # shipped content is the oracle; GServer-v2's grammar predates it).
    params: list = field(default_factory=list)


@dataclass
class Flow:
    kind: str  # 'return' | 'break' | 'continue'


@dataclass
class Command:
    name: str
    args: list


@dataclass
class UserCall:
    name: str
    args: list = field(default_factory=list)


@dataclass
class MethodCall:
    # era new-GS1 GS2-ism: `clientr.jail.tokenize()`, `client.message.add(x)`,
    # `player.level.name.pos("gym")`, and `<call>.method(...)` chains.
    # base is a VarRef (flag/prop path) or another expression node.
    base: Any
    name: str
    args: list = field(default_factory=list)


@dataclass
class Assign:
    target: Any  # VarRef
    op: str
    value: Any


@dataclass
class ExprStmt:
    expr: Any


# -- expressions ------------------------------------------------------------
@dataclass
class Ternary:
    cond: Any
    a: Any
    b: Any


@dataclass
class BinOp:
    op: str
    left: Any
    right: Any


@dataclass
class UnaryOp:
    op: str
    operand: Any


@dataclass
class Postfix:
    op: str
    operand: Any


@dataclass
class InExpr:
    values: list  # one or more exponentiation-level expressions (`a,b in ...`)
    rng: Any       # RangeLit, or an expression evaluating to an array


@dataclass
class Call:
    name: str  # builtin function
    args: list


@dataclass
class MessageCode:
    code: str
    args: list = field(default_factory=list)


@dataclass
class StrConcat:
    parts: list  # list of Str | MessageCode


@dataclass
class Str:
    value: str


@dataclass
class Num:
    value: float


@dataclass
class Bool:
    value: bool


@dataclass
class PathPart:
    name: str  # static name, or "" when the segment is dynamic (see atoms)
    index: list = field(default_factory=list)  # 0, 1 or 2 index expressions
    atoms: list = field(default_factory=list)  # Str|MessageCode parts for dynamic names


@dataclass
class VarRef:
    parts: list  # list[PathPart]


@dataclass
class ArrayLit:
    elements: list


@dataclass
class SpecialLit:
    kind: str  # ITEM | CARRY | DIRECTION | GENDER | COLOR | BADDY
    value: str


@dataclass
class RangeLit:
    lo: Any
    hi: Any
    lo_incl: bool = True  # opening delimiter: '|' inclusive, '<' exclusive
    hi_incl: bool = True  # closing delimiter: '|' inclusive, '>' exclusive
