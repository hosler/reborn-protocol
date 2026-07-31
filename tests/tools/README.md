# GS2 tooling

This directory contains the pinned GS2 compiler (`gs2test`) and the corpus
sweep (`gs2_corpus_sweep.py`). The corpus sweep processes real server content.

# GS2 compiler harness

`gs2test` is the real GServer-v2 GS2 compiler (from
[xtjoeytx/gs2-parser](https://github.com/xtjoeytx/gs2-parser), pinned to the
same commit GServer-v2 uses). It is the reference `.gs2` → `.gs2bc` toolchain.

## Build it (one-time)

```bash
./tests/tools/build_gs2test.sh        # clones (or reuses a GServer-v2 checkout) + builds
```

The build needs `cmake`, a C++ compiler, `flex`, `bison`, and `git`. The build
caches the binary at `tests/tools/gs2test` (gitignored).
`tests/test_gs2_compiler.py` finds it there, through `GS2TEST_BIN`, or on
`PATH`. If the binary is not present, the compiler test skips. Thus, CI passes
without the toolchain.

## What it anchors

`tests/test_gs2_compiler.py` recompiles every vendored `.gs2` in
`tests/fixtures/gs2_baselines/**` and asserts the output is byte-identical to the
committed `.bytecode`. This check proves that the disassembler and VM use the
exact production output. It also detects fixture or compiler drift.

## Mint a new test vector

```bash
./tests/tools/gs2test path/to/script.gs2 -o out.gs2bc
# drop script.gs2 + out.gs2bc (renamed .bytecode) into tests/fixtures/gs2_baselines/<category>/
```

The `advanced/weapon-*.bytecode` fixtures contain captured real weapons with no
`.gs2` source. The disasm and VM tests use them, but the recompile check does
not use them.

# GS2 corpus sweep

`gs2_corpus_sweep.py` compiles, disassembles, and runs every GS2 script in the
vendored third-party server checkouts. It reports gaps in the container
parser, opcode table, VM, and client host. It finds gaps and is not a test.
It has no assertions, and pytest does not collect it. It needs the compiler
and the server directories. This repository does not include either item.

```bash
/usr/bin/python3.13 tests/tools/gs2_corpus_sweep.py --out /tmp/gs2_corpus_report.json
```

The complete corpus takes about 10 seconds. Useful flags: `--corpus` (checkout root),
`--servers` (comma-separated dir names), `--modes` (how the stub host answers
calls -- `object`/`truthy`/`falsy`; the default uses and combines all three),
`--max-ops`, `--no-compile` (precompiled blobs only), `--seed`.

The JSON report contains the details. Standard output shows a ranked summary.
Key sections: `unknown_opcodes`, `deferred_opcodes` (the unmodeled 54 / 200-242
range), `orphan_operand_markers`, `unimplemented_executed`, `missing_builtins`,
`missing_objects`, `engine_object_properties`, `vm_errors`, `compile_failures`.

The numbers do not have these two meanings:

- Raw counts have loop weighting. A name read inside a `for` over the player
  list records thousands of hits from one call site. Every ranked entry has a
  `scripts` count (distinct server/script) as the breadth measure. Prefer it.
- A serverside half that fails to compile is usually not a gap: several servers'
  serverside is V8 JavaScript or GS1, not GS2. `compile_by_bucket` breaks the
  failures down by server and side. This breakdown shows the cause directly.
