"""GS2 container/disassembler regression tests.

Two corpora:
  1. The gs2parser compiler's own golden baselines (paired source -> bytecode
     vectors covering expressions/statements/functions/classes/advanced/
     edge_cases/basic) -- every *.bytecode file there must parse and decode
     with exact byte consumption and zero unknown-operand-marker errors.
  2. Live blobs captured from the local GServer-v2 fixtures (qa_gs2weapon,
     qa_gs2class, qa_script gani) via PLI_UPDATESCRIPT/UPDATECLASS/
     UPDATEGANI, saved under tests/fixtures/gs2/ -- proves the disassembler
     also handles bytecode as it actually arrives over the wire, not just
     the compiler's test suite output.

Note on the weapon fixture: upstream GServer-v2's Weapon::sendByteCodeToPlayer
(server/src/object/Weapon.cpp) sent PLO_NPCWEAPONSCRIPT as a plain packet,
unlike ScriptClass/GameAni which wrap their bytecode packets in PLO_RAWDATA
(Level.cpp:1356, GameAni.cpp:117). Without RAWDATA framing, any raw 0x0a byte
inside the bytecode truncated the packet at normal newline-delimited packet
boundaries. This is fixed in the local GServer-v2 working tree (Weapon.cpp now
wraps the packet in PLO_RAWDATA); against an unfixed upstream server, weapon
blobs containing 0x0a still arrive truncated, and parse_container correctly
raises GS2ContainerError rather than mis-parsing -- exercised below.
"""
from __future__ import annotations

import glob
import os

import pytest

from reborn_protocol.gs2.container import parse_container, GS2ContainerError
from reborn_protocol.gs2.disasm import decode, format_listing, GS2DecodeError

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "gs2")

BASELINES_ROOT = os.path.join(
    os.path.dirname(__file__), "..", "..", "GServer-v2", "build", "dependencies",
    "fc", "gs2parser-src", "tests", "baselines",
)
BASELINE_FILES = sorted(glob.glob(os.path.join(BASELINES_ROOT, "**", "*.bytecode"), recursive=True))


def _decode_fully(data: bytes):
    container = parse_container(data)
    instrs = decode(container.code)
    last = instrs[-1] if instrs else None
    consumed = (last.offset + last.length) if last else 0
    assert consumed == len(container.code), (
        f"decoder consumed {consumed} bytes but code segment is {len(container.code)} bytes"
    )
    return container, instrs


@pytest.mark.skipif(not BASELINE_FILES, reason="gs2parser baselines not present in this checkout")
@pytest.mark.parametrize("path", BASELINE_FILES, ids=[os.path.relpath(p, BASELINES_ROOT) for p in BASELINE_FILES])
def test_baseline_decodes_cleanly(path):
    with open(path, "rb") as fh:
        data = fh.read()
    container, instrs = _decode_fully(data)
    # Every instruction must render without raising (exercises the operand
    # formatter / jump-target resolver, not just the raw decoder).
    format_listing(container)


def test_baseline_corpus_present():
    # Guards against a silent glob/path typo hiding the whole test set.
    assert len(BASELINE_FILES) >= 20


def test_live_weapon_qa_gs2weapon_decodes():
    with open(os.path.join(FIXTURES_DIR, "live_weapon_qa_gs2weapon.bin"), "rb") as fh:
        data = fh.read()
    container, instrs = _decode_fully(data)
    assert [f.name for f in container.functions] == ["onCreated"]
    assert "counter" in container.strings


def test_live_class_qa_gs2class_decodes():
    with open(os.path.join(FIXTURES_DIR, "live_class_qa_gs2class.bin"), "rb") as fh:
        data = fh.read()
    container, instrs = _decode_fully(data)
    assert [f.name for f in container.functions] == ["qaHelper"]


def test_live_gani_qa_script_decodes():
    with open(os.path.join(FIXTURES_DIR, "live_gani_qa_script.bin"), "rb") as fh:
        data = fh.read()
    container, instrs = _decode_fully(data)
    assert [f.name for f in container.functions] == ["onCreated"]
    assert "qa" in container.strings


def test_truncated_blob_raises_cleanly():
    """A blob whose bytecode segment is cut short (as happens for
    PLO_NPCWEAPONSCRIPT from unfixed upstream servers, see module docstring)
    must raise GS2ContainerError, never silently misparse or crash with an
    unrelated exception."""
    # 4 well-formed segments, but the bytecode segment's declared length
    # exceeds what's actually present.
    import struct
    blob = b""
    blob += struct.pack(">II", 1, 4) + struct.pack(">I", 0)
    blob += struct.pack(">II", 2, 0)
    blob += struct.pack(">II", 3, 0)
    blob += struct.pack(">II", 4, 21) + b"\x01\xf4\x00\x0c\x17\x33"  # only 6 of 21 bytes
    with pytest.raises(GS2ContainerError):
        parse_container(blob)
