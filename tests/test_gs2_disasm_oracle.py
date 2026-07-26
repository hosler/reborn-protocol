"""Cross-check the disassembler against an INDEPENDENT disassembly listing.

`Preagonal/graal-loginserver*/weapon_bytecode/` ships `weapon-Serverlist.gasm`
next to the blob it was produced from. The listing is NOT official-toolchain
output: it is emitted by `gs2-analysis`, a separate reverse-engineering tool
whose source sits in the same checkout
(`Preagonal/GraalNetwork/gs2-analysis/`, `ir/Disassembler.kt`), and whose jar
sits in the same directory. That still makes it a genuinely independent
decoder of the same 32 KB of production bytecode, which is what this test
uses it for.

What the comparison settles, instruction for instruction over all 9442
instructions of the blob:

- jump operands and function-table entries are INSTRUCTION INDICES, not byte
  offsets. gs2-analysis resolves every jump into `instructions[target]` and
  throws if the index is out of bounds (`file/GS2File.kt:183-197`), so the
  listing could not have been produced at all under a byte-offset reading.
- operand-marker bytes 0xF0-0xF6 never occupy an instruction index: it
  attaches them to the preceding instruction exactly as we do
  (`file/GS2File.kt:145-180`).

Two classes of disagreement are expected and are asserted *exactly*, so a
third one fails the test:

1. Four-byte integer operands. gs2-analysis's `readInt` is written as
   `(b0 and 0xff) shl 24 or (b1 and 0xff) shl 16 or ...`; Kotlin gives `shl`,
   `or` and `and` equal precedence and left associativity, so it evaluates as
   `((((((b0 shl 24) or b1) shl 16) or b2) shl 8) or b3)`
   (`file/GraalReadWriter.kt:202-211`). It happens to be right whenever the
   top three bytes are zero, which is why it survived. `_listing_readint`
   reproduces it.
2. Two same-length string constants edited in the blob after the listing was
   taken (KNOWN_BLOB_EDITS).
"""
from __future__ import annotations

import glob
import os
import re
import struct

import pytest

from reborn_protocol.gs2.container import parse_container
from reborn_protocol.gs2.disasm import decode

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
# Third-party checkouts; literal sibling paths, exempt from the naming policy.
_CORPUS = os.path.join(_ROOT, "Preagonal")
_OPCODE_KT = os.path.join(
    _CORPUS, "GraalNetwork", "gs2-analysis", "src", "main", "kotlin", "com",
    "cerne", "xyz", "instruction", "Opcode.kt",
)

LISTINGS = sorted(
    path for path in glob.glob(os.path.join(_CORPUS, "*", "weapon_bytecode", "*.gasm"))
    if os.path.isfile(path[: -len(".gasm")] + ".gs2bc")
)

#: String-table entries the blob no longer agrees with the listing on: a
#: platform token and the query part of a web URL, each overwritten in place
#: with same-length text after the listing was taken. The index set and the
#: equal-length check are the guard -- any other index, or an edit that
#: changes the length, is a decoder divergence and fails.
KNOWN_BLOB_EDITS = frozenset({327, 329})


def _listing_readint(value: int) -> int:
    """The value gs2-analysis prints for a 4-byte operand -- see the module
    docstring for the precedence bug this reproduces."""
    b0, b1, b2, b3 = struct.pack(">i", value)
    return ((((((b0 << 24) | b1) << 16) | b2) << 8) | b3)


def _mnemonic_opcodes() -> dict:
    """mnemonic -> opcode number, read from gs2-analysis's own enum rather
    than transcribed here."""
    table = {}
    with open(_OPCODE_KT, "r", encoding="utf-8") as fh:
        for line in fh:
            m = re.match(r"\s*([A-Z_0-9x]+)\(0x([0-9a-fA-F]+)\)", line)
            if m:
                table[m.group(1)] = int(m.group(2), 16)
    return table


def _parse_listing(path: str):
    """(records, labels, functions) from a .gasm.

    A record is (mnemonic, operand-kind, operand-text); operand-kind is None
    for a bare instruction, "LABEL" for a resolved jump, else the storage
    section gs2-analysis printed. String operands may span lines, because the
    listing quotes the constant verbatim.
    """
    with open(path, "r", encoding="utf-8", errors="surrogateescape") as fh:
        lines = fh.read().split("\n")

    # gs2-analysis prints its header parse to stdout ahead of the listing
    start = next(i for i, l in enumerate(lines)
                 if l.startswith("Save script to file:")) + 1

    records, labels, functions = [], {}, {}
    i, n = start, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith(".function "):
            functions.setdefault(len(records), []).append(line[len(".function "):])
            i += 1
            continue
        if line.startswith(".label "):
            labels[len(records)] = line[len(".label "):].strip()
            i += 1
            continue
        m = re.match(r"^(\S+)\s+LABEL\s+(\S+)\s*$", line)
        if m:
            records.append((m.group(1), "LABEL", m.group(2)))
            i += 1
            continue
        m = re.match(r'^(\S+)\s+(SECTION_\S+)\s+"(.*)$', line, re.S)
        if m:
            mnemonic, section, rest = m.groups()
            text = rest
            while not text.endswith('"'):
                i += 1
                assert i < n, f"{path}: unterminated string operand"
                text += "\n" + lines[i]
            records.append((mnemonic, section, text[:-1]))
            i += 1
            continue
        m = re.match(r"^(\S+)\s*$", line)
        assert m, f"{path}:{i + 1}: unparsed listing line {line!r}"
        records.append((m.group(1), None, None))
        i += 1
    return records, labels, functions


@pytest.mark.skipif(not LISTINGS, reason="third-party .gasm listings not present")
@pytest.mark.skipif(not os.path.isfile(_OPCODE_KT),
                    reason="gs2-analysis opcode table not present")
@pytest.mark.parametrize("listing_path", LISTINGS,
                         ids=lambda p: os.path.relpath(p, _CORPUS))
def test_disassembly_matches_independent_listing(listing_path):
    with open(listing_path[: -len(".gasm")] + ".gs2bc", "rb") as fh:
        data = fh.read()
    # PLO_NPCWEAPONSCRIPT payload: {GSHORT header_len}{header CSV}{container}
    header_len = ((data[0] << 7) | data[1]) - 0x1020
    container = parse_container(data[2 + header_len:])
    instrs = decode(container.code)

    records, labels, functions = _parse_listing(listing_path)
    opcode_of = _mnemonic_opcodes()

    assert len(instrs) == len(records)
    # the trailing label gs2-analysis emits is the instruction count, and
    # jumps past the last instruction resolve to it
    assert labels.get(len(records)) == f"lbl_{len(records)}"

    for instr, (mnemonic, section, text) in zip(instrs, records):
        where = f"{listing_path}#{instr.idx}"
        assert opcode_of[mnemonic] == instr.opnum, where

        operand = instr.operand
        if section is None:
            assert operand is None or operand.marker < 0, where
            continue
        assert operand is not None and operand.marker >= 0, where

        if section == "LABEL":
            target = int(text.rsplit("_", 1)[1])
            assert operand.value == target, where
            assert labels.get(target) == text, where
        elif section == "SECTION_STRINGS":
            ours = container.strings[operand.value]
            if ours != text:
                assert operand.value in KNOWN_BLOB_EDITS, \
                    f"{where}: string[{operand.value}] {text!r} vs {ours!r}"
                assert len(text) == len(ours), where
        elif section == "SECTION_TEXT_INT":
            if operand.marker == 0xF5:
                assert int(text) == _listing_readint(int(operand.value)), where
            else:
                assert int(text) == int(operand.value), where
        elif section == "SECTION_TEXT_STRING":
            assert operand.raw_text == text, where
        else:
            pytest.fail(f"{where}: unknown listing section {section}")

    by_index = {}
    for entry in container.functions:
        by_index.setdefault(entry.op_index, []).append(entry.name)
    for index, names in functions.items():
        assert names[0] in by_index.get(index, []), f"{listing_path}#{index}"


@pytest.mark.skipif(not os.path.isdir(_CORPUS),
                    reason="third-party server checkouts not present")
def test_listing_corpus_present():
    # Guards against a glob/path typo silently skipping the whole cross-check.
    assert LISTINGS
