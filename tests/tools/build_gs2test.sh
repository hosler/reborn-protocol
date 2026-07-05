#!/usr/bin/env bash
# Build the real GS2 compiler (`gs2test`) from xtjoeytx/gs2-parser, pinned to
# the same commit GServer-v2 uses, so we can recompile our .gs2 fixtures with
# the production toolchain and diff against the checked-in .bytecode
# (tests/test_gs2_compiler.py). One-time; the binary is cached and gitignored.
#
#   ./tests/tools/build_gs2test.sh          # clone + build, print binary path
#   GS2TEST_BIN=/path/to/gs2test pytest ... # or point tests at an existing one
#
# Needs: cmake, a C++ compiler, flex, bison, git. gs2-parser vendors ANTLR.
set -euo pipefail

# Pin: keep in sync with GServer-v2/server/CMakeLists.txt FetchContent_Declare.
GS2PARSER_REPO="https://github.com/xtjoeytx/gs2-parser.git"
GS2PARSER_COMMIT="73e7ea8d6ed88112967547c5e6941bfa035fec6c"  # main 2026-07-02

here="$(cd "$(dirname "$0")" && pwd)"
work="$here/.gs2test-build"
out="$here/gs2test"

# Reuse an existing GServer-v2 FetchContent checkout if it's already on disk
# (avoids a network clone); else clone the pinned commit fresh.
gserver_src="$here/../../../GServer-v2/build/dependencies/fc/gs2parser-src"
if [ -f "$gserver_src/CMakeLists.txt" ]; then
    src="$gserver_src"
    echo "[build_gs2test] using existing gs2parser source: $src"
else
    src="$work/gs2-parser"
    if [ ! -d "$src/.git" ]; then
        echo "[build_gs2test] cloning $GS2PARSER_REPO @ ${GS2PARSER_COMMIT:0:12}"
        mkdir -p "$work"
        git clone "$GS2PARSER_REPO" "$src"
        git -C "$src" checkout --quiet "$GS2PARSER_COMMIT"
    fi
fi

echo "[build_gs2test] configuring (gs2test target on)"
cmake -S "$src" -B "$work/build" \
    -DGS2PARSER_BUILD_GS2TEST=ON \
    -DCMAKE_BUILD_TYPE=Release >/dev/null

echo "[build_gs2test] building"
cmake --build "$work/build" --target gs2test -j"$(nproc)" >/dev/null

bin="$(find "$work/build" -name gs2test -type f -perm -u+x | head -1)"
if [ -z "$bin" ]; then
    echo "[build_gs2test] ERROR: gs2test binary not found after build" >&2
    exit 1
fi
cp "$bin" "$out"
echo "[build_gs2test] done -> $out"
"$out" --help | head -1
