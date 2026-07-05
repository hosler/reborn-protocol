# GS2 compiler harness

`gs2test` is the real GServer-v2 GS2 compiler (from
[xtjoeytx/gs2-parser](https://github.com/xtjoeytx/gs2-parser), pinned to the
same commit GServer-v2 uses). It's our ground-truth `.gs2` → `.gs2bc` toolchain.

## Build it (one-time)

```bash
./tests/tools/build_gs2test.sh        # clones (or reuses a GServer-v2 checkout) + builds
```

Needs `cmake`, a C++ compiler, `flex`, `bison`, `git`. The binary is cached at
`tests/tools/gs2test` (gitignored). `tests/test_gs2_compiler.py` finds it there,
via `GS2TEST_BIN`, or on `PATH`; with no binary the compiler test skips, so CI
without the toolchain stays green.

## What it anchors

`tests/test_gs2_compiler.py` recompiles every vendored `.gs2` in
`tests/fixtures/gs2_baselines/**` and asserts the output is byte-identical to the
committed `.bytecode`. This proves the corpus our disassembler/VM run against is
exactly what production emits, and catches fixture or compiler drift.

## Mint a new test vector

```bash
./tests/tools/gs2test path/to/script.gs2 -o out.gs2bc
# drop script.gs2 + out.gs2bc (renamed .bytecode) into tests/fixtures/gs2_baselines/<category>/
```

The `advanced/weapon-*.bytecode` fixtures are captured real weapons with no
`.gs2` source, so they're exercised by the disasm/VM tests but not this
recompile check.
