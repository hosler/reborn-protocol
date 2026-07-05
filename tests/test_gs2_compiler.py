"""Compiler conformance: recompile our vendored .gs2 fixtures with the REAL
GServer-v2 GS2 compiler (`gs2test`) and assert the output is byte-identical to
the checked-in .bytecode.

This is the ground-truth anchor for the whole GS2 corpus: it proves the
.bytecode files our disassembler/VM tests run against are exactly what the
production toolchain emits, and it guards against a fixture (or the compiler)
drifting. It also documents how to mint new .gs2 -> .bytecode vectors.

The compiler is a native binary, not a Python dep. Build it once with
tests/tools/build_gs2test.sh (or point GS2TEST_BIN at an existing one). When no
binary is available (e.g. CI without the toolchain) the whole module skips, so
it never blocks the pure-Python suite.
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "gs2_baselines"


def _find_gs2test():
    """Locate the gs2test binary: GS2TEST_BIN env, the cached build output, or
    anything on PATH. Returns None if none is runnable."""
    candidates = [
        os.environ.get("GS2TEST_BIN"),
        str(Path(__file__).parent / "tools" / "gs2test"),
        shutil.which("gs2test"),
    ]
    for c in candidates:
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return c
    return None


GS2TEST = _find_gs2test()

# Every fixture that ships BOTH a .gs2 source and its expected .bytecode.
PAIRS = sorted(
    (src, src.with_suffix(".bytecode"))
    for src in FIXTURES.rglob("*.gs2")
    if src.with_suffix(".bytecode").exists()
)


@pytest.mark.skipif(GS2TEST is None,
                    reason="gs2test compiler not built (run tests/tools/build_gs2test.sh "
                           "or set GS2TEST_BIN)")
@pytest.mark.skipif(not PAIRS, reason="no .gs2/.bytecode fixture pairs vendored")
@pytest.mark.parametrize("src,expected",
                         PAIRS,
                         ids=[f"{s.parent.name}/{s.stem}" for s, _ in PAIRS])
def test_gs2_source_recompiles_to_baseline(src, expected, tmp_path):
    out = tmp_path / "out.gs2bc"
    proc = subprocess.run([GS2TEST, str(src), "-o", str(out)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"gs2test failed: {proc.stdout}\n{proc.stderr}"
    assert out.exists(), f"gs2test produced no output for {src.name}"
    got = out.read_bytes()
    want = expected.read_bytes()
    assert got == want, (
        f"{src.relative_to(FIXTURES)}: recompiled bytecode differs from the "
        f"checked-in baseline ({len(got)} vs {len(want)} bytes) — the fixture "
        f"or the pinned compiler has drifted"
    )


def test_at_least_one_pair_when_compiler_present():
    """Guard: if the compiler is available we must actually have vectors to run,
    so a fixture-path regression can't silently reduce this to zero tests."""
    if GS2TEST is None:
        pytest.skip("compiler not built")
    assert PAIRS, "gs2test is present but no .gs2/.bytecode fixture pairs found"
