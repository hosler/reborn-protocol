"""Differential tests: our GS1 interpreter vs the GServer-v2 oracle binary.

The oracle is an untracked Catch2 driver (GServer-v2/Catch_tests/Oracle/) built
with -DTESTS=ON; it runs each corpus case through the real C++ GS1 engine and
dumps the resulting variable stores as JSONL.  We run the same cases through
reborn_protocol.gs1 and compare the `this.*` results (oracle "npc" store).

Skipped entirely when the oracle binary is not built.  Set GS1_ORACLE_BIN to
override the default location.

Corpus format: see tests/gs1_oracle_corpus.cases (=====CASE / =====MATH /
=====STR markers; =====EVENTS overrides the default `created` event list).
"""

import json
import math
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from reborn_protocol.gs1 import Context, MemoryHost, run_event
from reborn_protocol.gs1.parser import parse

TESTS_DIR = Path(__file__).resolve().parent
CORPUS = TESTS_DIR / "gs1_oracle_corpus.cases"
DEFAULT_BIN = TESTS_DIR.parent.parent / "GServer-v2" / "bin" / "Oracle"
ORACLE_BIN = Path(os.environ.get("GS1_ORACLE_BIN", DEFAULT_BIN))

# Cases where the C++ engine's *own* behavior is known-broken (their test
# suite fails on it too) and our port deliberately follows the documented
# semantics instead of the bug.
KNOWN_UPSTREAM_BUGS = {
    # 66813d8b implemented `pi` for plain reads, but constants do not resolve
    # inside call-argument expressions upstream (sin(pi/2) -> sin(0)); the
    # upstream Catch2 test "reserved constants cannot be used as variables"
    # fails in their own suite.  We resolve constants everywhere.
    "pi-in-call": "upstream constants don't resolve inside call arguments",
    # fn_getdir (9e759e9d) only computes its angle
    # `if (!DoubleIsZero(dx) || DoubleIsZero(dy))`, so a pure-vertical delta
    # (dx==0, dy!=0) skips the atan2 and falls back to angle=0.0 ("right"),
    # i.e. getdir(0,-1) AND getdir(0,1) both come out 3.0. This contradicts
    # GServer's own docs (bin/docs/scripting-gs1-functions.md tables
    # (0,-1)->0 up, (0,1)->2 down). Deliberate decision: gameplay fidelity
    # over bug-for-bug reference fidelity -- we always compute the real
    # angle, matching the docs table instead of this guard typo.
    "getdir-vertical": "upstream skips the angle calc for pure-vertical deltas (typo'd DoubleIsZero guard)",
}

TOLERANCE = 1e-6


@dataclass
class Case:
    id: str
    kind: str  # "script" | "math" | "str"
    events: list = field(default_factory=lambda: ["created"])
    body: str = ""


def parse_corpus(path: Path):
    cases, current, saw_events = [], None, False
    for line in path.read_text().splitlines():
        if line.startswith("=====CASE "):
            current = Case(id=line[10:].strip(), kind="script")
            cases.append(current)
            saw_events = False
        elif line.startswith("=====MATH "):
            current = Case(id=line[10:].strip(), kind="math")
            cases.append(current)
        elif line.startswith("=====STR "):
            current = Case(id=line[9:].strip(), kind="str")
            cases.append(current)
        elif line.startswith("=====EVENTS ") and current and not saw_events and not current.body:
            current.events = [e.strip() for e in line[12:].split(",") if e.strip()]
            saw_events = True
        elif current is not None:
            current.body += line + "\n"
    return cases


CASES = parse_corpus(CORPUS) if CORPUS.exists() else []


@pytest.fixture(scope="module")
def oracle_results(tmp_path_factory):
    if not ORACLE_BIN.is_file():
        pytest.skip(f"oracle binary not built: {ORACLE_BIN} (cmake -DTESTS=ON)")
    out = tmp_path_factory.mktemp("oracle") / "results.jsonl"
    subprocess.run(
        [str(ORACLE_BIN), "[oracle]"],
        cwd=ORACLE_BIN.parent,  # Server("test") resolves its config relative to cwd
        env={**os.environ, "GS1_ORACLE_IN": str(CORPUS), "GS1_ORACLE_OUT": str(out)},
        check=True,
        capture_output=True,
        timeout=120,
    )
    results = {}
    for line in out.read_text().splitlines():
        obj = json.loads(line)
        results[obj["id"]] = obj
    return results


class OracleHost(MemoryHost):
    """MemoryHost plus the few host-level builtins the corpus touches."""

    def get_builtin(self, name, indices, ctx):
        if name == "tokenscount":
            return float(len(ctx.tokenize_tokens))
        return super().get_builtin(name, indices, ctx)


def run_ours(case: Case) -> Context:
    ctx = Context(OracleHost())
    program = parse(case.body)
    from reborn_protocol.gs1.interp import Interpreter

    for event in case.events:
        Interpreter(ctx).run_event(program, event)
    return ctx


def oracle_value(entry):
    """Collapse an oracle store entry to a comparable Python value.

    The oracle dumps every GameValue field that's actually SET (OracleMain.cpp
    dumpValue uses GameValue::get<T>, not getCopy<T>, so a key is only present
    when that field genuinely holds something) -- but newly-created GS1
    variables are apparently seeded with a default m_number=0.0 that setstring
    never clears, so a pure-string variable (e.g. one only ever touched by
    setstring) can still show up with a "num":0 alongside its "text". That
    stray default is dump noise, not real script-observable behavior (a
    string never numeric-coerces anyway, see gs1_num), so "text" -- even an
    empty one -- always wins over an incidental "num":0.
    """
    if "array" in entry:
        return list(entry["array"])
    if "text" in entry:
        return entry["text"]
    if "num" in entry:
        return entry["num"]
    if "bool" in entry:
        return 1.0 if entry["bool"] else 0.0
    if "text" in entry:
        return entry["text"]
    return None


def our_value(v):
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (list, tuple)):
        return [float(x) for x in v]
    return v


def compare(name, oracle_entry, ours):
    expected = oracle_value(oracle_entry)
    got = our_value(ours)
    if isinstance(expected, list):
        assert isinstance(got, list), f"{name}: oracle array {expected}, ours {got!r}"
        assert len(expected) == len(got), f"{name}: oracle {expected}, ours {got}"
        for i, (e, g) in enumerate(zip(expected, got)):
            assert math.isclose(e, float(g), abs_tol=TOLERANCE), \
                f"{name}[{i}]: oracle {e}, ours {g}"
    elif isinstance(expected, str):
        assert str(got) == expected, f"{name}: oracle {expected!r}, ours {got!r}"
    elif expected is None:
        pytest.fail(f"{name}: oracle entry not comparable: {oracle_entry}")
    else:
        assert isinstance(got, (int, float)), \
            f"{name}: oracle number {expected}, ours {got!r}"
        assert math.isclose(float(expected), float(got), abs_tol=TOLERANCE), \
            f"{name}: oracle {expected}, ours {got}"


SCRIPT_CASES = [c for c in CASES if c.kind == "script"]
EXPR_CASES = [c for c in CASES if c.kind in ("math", "str")]


@pytest.mark.parametrize("case", SCRIPT_CASES, ids=[c.id for c in SCRIPT_CASES])
def test_script_case(case, oracle_results):
    if case.id in KNOWN_UPSTREAM_BUGS:
        pytest.xfail(KNOWN_UPSTREAM_BUGS[case.id])
    oracle = oracle_results[case.id]
    assert oracle.get("compile_error") is None, \
        f"oracle failed to compile: {oracle.get('compile_error')}"

    ctx = run_ours(case)
    this_scope = ctx.vars.scopes["this"]

    npc_store = oracle["stores"]["npc"]
    checked = 0
    for name, entry in sorted(npc_store.items()):
        if entry.get("live"):
            continue
        assert name in this_scope, \
            f"this.{name}: oracle has {entry}, ours never set it"
        compare(f"this.{name}", entry, this_scope[name])
        checked += 1
    assert checked > 0, "case produced nothing to compare"


@pytest.mark.parametrize("case", EXPR_CASES, ids=[c.id for c in EXPR_CASES])
def test_expr_case(case, oracle_results):
    if case.id in KNOWN_UPSTREAM_BUGS:
        pytest.xfail(KNOWN_UPSTREAM_BUGS[case.id])
    oracle = oracle_results[case.id]
    expected = oracle["result"]
    body = case.body.strip()

    if case.kind == "math":
        wrapped = f"if (created) {{ this.__r = {body}; }}"
    else:
        wrapped = f"if (created) {{ setstring this.__r,{body}; }}"
    ctx = run_event(wrapped, "created", MemoryHost())
    got = ctx.vars.scopes["this"].get("__r")
    assert got is not None, "our engine produced no result"

    if case.kind == "math":
        assert math.isclose(float(expected), float(our_value(got)), abs_tol=TOLERANCE), \
            f"math: oracle {expected}, ours {got}"
    else:
        assert str(got) == expected, f"str: oracle {expected!r}, ours {got!r}"
