# GS2 tooling

Two things live here: the pinned GS2 compiler (`gs2test`) and the corpus sweep
that drives it over real server content (`gs2_corpus_sweep.py`).

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

# GS2 corpus sweep

`gs2_corpus_sweep.py` compiles, disassembles and RUNS every GS2 script in the
vendored third-party server checkouts and reports what our container parser,
opcode table, VM and client host still do not know. It is a gap finder, not a
test: it asserts nothing and is not collected by pytest (it needs both the
compiler and the server directories, neither of which ships with this repo).

```bash
/usr/bin/python3.13 tests/tools/gs2_corpus_sweep.py --out /tmp/gs2_corpus_report.json
```

Roughly 10s for the whole corpus. Useful flags: `--corpus` (checkout root),
`--servers` (comma-separated dir names), `--modes` (how the stub host answers
calls -- `object`/`truthy`/`falsy`, all three by default and unioned),
`--max-ops`, `--no-compile` (precompiled blobs only), `--seed`.

The JSON report holds the detail; stdout is a ranked human summary. Key
sections: `unknown_opcodes`, `deferred_opcodes` (the unmodelled 54 / 200-242
range), `orphan_operand_markers`, `unimplemented_executed`, `missing_builtins`,
`missing_objects`, `engine_object_properties`, `vm_errors`, `compile_failures`.

Two things the numbers do NOT mean:

- Raw counts are loop-weighted -- a name read inside a `for` over the player
  list bills thousands of hits from one call site. Every ranked entry carries a
  `scripts` count (distinct server/script) as the breadth measure; prefer it.
- A serverside half that fails to compile is usually not a gap: several servers'
  serverside is V8 JavaScript or GS1, not GS2. `compile_by_bucket` breaks the
  failures down by server and side so that is visible at a glance.
