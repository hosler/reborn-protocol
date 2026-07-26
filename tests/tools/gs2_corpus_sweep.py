#!/usr/bin/env python3
"""GS2 corpus sweep: compile, disassemble and RUN every GS2 script in the
vendored third-party server checkouts, and report what our implementation
still does not know.

This is a gap FINDER, not a test: it never asserts, it produces a ranked
report. Nothing here is wired into pytest, because it needs both a native
compiler (tests/tools/gs2test) and the third-party server directories, neither
of which ships with this repo.

What it sweeps, per server directory:

  weapons/*.txt      GRAWP001 wrapper; script body between the SCRIPT and
                     SCRIPTEND lines
  npcs/*.txt         GRNPC001 wrapper; body between NPCSCRIPT/NPCSCRIPTEND
  scripts/*.txt      raw script-class source (no wrapper)
  weapon_bytecode/*  PRECOMPILED blobs produced by the OFFICIAL compiler, in
                     PLO_NPCWEAPONSCRIPT payload form ({GSHORT header_len}
                     {header CSV}{container}) -- the highest-value inputs,
                     since only these can carry opcodes our own compiler
                     never emits

Sources are split into serverside/clientside halves the way the server does
(GServer-v2/server/src/scripting/Script.cpp: `Script::split`, terminator
"//#CLIENTSIDE", clientside starts after the END of that line) and each half
is compiled separately with gs2test, so a name can be attributed to the side
it is used from. The `join <class>;` inlining Script.cpp does on the clientside
half (performClientSideJoinHack) is NOT reproduced: it needs the server's class
store, and an un-inlined join only costs us the joined class's own names, which
the sweep picks up separately from scripts/.

Execution uses a recording stub host that answers EVERY builtin, named object
and property with a benign value while logging the name -- the point is to
harvest names, so a silent refusal would lose exactly the data we came for.
"Missing" is then decided by set-differencing the harvested names against
pyReborn's real host surface (GS2ClientHost.host_surface() / its named-object
registry), not by whether the stub answered.

Usage:

    /usr/bin/python3.13 reborn-protocol/tests/tools/gs2_corpus_sweep.py \
        --out /tmp/gs2_corpus_report.json

Add --corpus to point at a different checkout root, --servers to restrict the
sweep, and --modes to change how the stub answers calls (see STUB_MODES).
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

_TOOLS = Path(__file__).resolve().parent
_RP_ROOT = _TOOLS.parents[1]              # reborn-protocol/
_WORKSPACE = _RP_ROOT.parent               # opengraal2/
sys.path.insert(0, str(_RP_ROOT))
sys.path.insert(0, str(_WORKSPACE / "pyReborn"))

from reborn_protocol.gs2 import (  # noqa: E402
    GS2ContainerError, GS2DecodeError, GS2Host, GS2Object, GS2VM,
    NOT_HANDLED, Op, decode, op_name, parse_container,
)
from reborn_protocol.gs2.opcodes import OPERAND_OPS  # noqa: E402

#: Third-party server checkouts to sweep, relative to --corpus. These are
#: literal sibling directory names (see the repo naming policy: paths into
#: third-party checkouts are exempt).
DEFAULT_SERVERS = (
    "graal-lttp",
    "graal-loginserver",
    "graal-loginserver-dev",
    "graal-loginserver-mobile",
    "graal-bomber-gs2",
    "graal-gta",
)

#: How the stub answers a builtin call. Every mode is swept and the harvested
#: names unioned, because the answer decides which branches the script takes:
#: an object is falsy and compares by pointer, 1.0 opens `if (getx())` guards,
#: 0.0 opens their else-branches. No single mode reaches every call site.
STUB_MODES = ("object", "truthy", "falsy")

CLIENTSIDE_TERMINATOR = "//#CLIENTSIDE"

#: Opcodes we deliberately do NOT model: the newer official compiler's
#: 200-242 range plus 54. They also SKIP the following 1-2 instructions, so a
#: script containing one is mis-disassembled from that point on -- they are
#: reported as their own category rather than as ordinary unknown bytes.
DEFERRED_OPCODES = frozenset({54}) | frozenset(range(200, 243))

_LOG_ERROR_MSG = "GS2 %s: error at op#%d (%s): %s"

#: Universal array methods the VM answers itself when the receiver really is
#: a list (vm.py GS2VM._list_method). The stub receiver is an object, so those
#: calls reach the host here and would otherwise read as host gaps.
VM_NATIVE_METHODS = frozenset({"add", "addarray", "size", "clear", "index",
                               "sortbyvalue"})


# ---------------------------------------------------------------------------
# corpus enumeration
# ---------------------------------------------------------------------------


@dataclass
class ScriptSource:
    """One compilable half of one source file."""
    server: str
    kind: str        # "weapon" | "npc" | "class"
    path: Path
    name: str
    side: str        # "clientside" | "serverside"
    source: str

    @property
    def label(self) -> str:
        return f"{self.server}/{self.name}[{self.side}]"


@dataclass
class Blob:
    """One GS2 container blob to disassemble and run."""
    server: str
    origin: str      # "compiled" | "precompiled"
    name: str
    side: str
    path: str
    data: bytes

    @property
    def label(self) -> str:
        return f"{self.server}/{self.name}[{self.side}]"


#: file-format magic -> (body start line, body end line)
_WRAPPERS = {
    "GRAWP001": ("SCRIPT", "SCRIPTEND"),
    "GRNPC001": ("NPCSCRIPT", "NPCSCRIPTEND"),
}


def extract_script_body(text: str) -> str:
    """Strip the GRAWP001/GRNPC001 wrapper off a weapon/NPC file.

    Both wrappers are line-oriented key/value headers followed by a lone
    SCRIPT (weapons) or NPCSCRIPT (NPCs) line, the body, and the matching END
    line. A wrapper with no such line is a BYTECODE-only weapon (the source
    lives in weapon_bytecode/, which the sweep loads directly) and yields no
    source. Anything without a wrapper is already raw source -- scripts/
    class files.

    CR is dropped first, exactly as the server does before compiling
    (GServer-v2 Script.cpp performClientSideJoinHack / minify), because a
    good third of the corpus is CRLF and "SCRIPT\\r" matches nothing.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    wrapper = _WRAPPERS.get(lines[0].strip() if lines else "")
    if wrapper is None:
        return "\n".join(lines)
    start, end = wrapper
    try:
        first = lines.index(start)
    except ValueError:
        return ""
    try:
        last = lines.index(end, first + 1)
    except ValueError:
        last = len(lines)
    return "\n".join(lines[first + 1:last])


def split_clientside(source: str) -> Tuple[str, str]:
    """Split into (serverside, clientside) exactly as Script::split does:
    everything before the "//#CLIENTSIDE" marker is serverside, everything
    after the END OF THAT LINE is clientside. No marker means the whole file
    is serverside."""
    idx = source.find(CLIENTSIDE_TERMINATOR)
    if idx < 0:
        return source.strip(), ""
    eol = source.find("\n", idx)
    if eol < 0:
        return source[:idx].strip(), ""
    return source[:idx].strip(), source[eol + 1:].strip()


def enumerate_sources(root: Path, servers: List[str]) -> List[ScriptSource]:
    out: List[ScriptSource] = []
    for server in servers:
        base = root / server
        if not base.is_dir():
            continue
        for subdir, kind in (("weapons", "weapon"), ("npcs", "npc"),
                             ("scripts", "class")):
            d = base / subdir
            if not d.is_dir():
                continue
            for path in sorted(d.iterdir()):
                if not path.is_file() or path.suffix.lower() != ".txt":
                    continue
                text = path.read_text(encoding="utf-8", errors="surrogateescape")
                body = extract_script_body(text)
                serverside, clientside = split_clientside(body)
                for side, code in (("serverside", serverside),
                                   ("clientside", clientside)):
                    if code.strip():
                        out.append(ScriptSource(server=server, kind=kind,
                                                path=path, name=path.stem,
                                                side=side, source=code))
    return out


def looks_like_container(data: bytes) -> bool:
    """True if `data` starts on a plausible GS2 container segment header
    (segment id 1-4, declared length within the blob)."""
    if len(data) < 8:
        return False
    seg_id = int.from_bytes(data[0:4], "big")
    seg_len = int.from_bytes(data[4:8], "big")
    return 1 <= seg_id <= 4 and 8 + seg_len <= len(data)


#: how far into a captured file to look for the container start
MAX_HEADER_PROBE = 512

#: sidecar formats that live alongside the blobs in weapon_bytecode/ and are
#: not bytecode at all (archives, disassembly listings, tooling)
NON_BLOB_SUFFIXES = frozenset({".zip", ".jar", ".gasm", ".dump", ".md", ".log"})


def find_container(data: bytes) -> int:
    """Byte offset the GS2 container starts at, or -1.

    The vendored blobs are captured at different points in the pipeline:
    some are bare `.gs2bc` compiler output (container at 0), the rest are
    PLO_NPCWEAPONSCRIPT payloads -- {GSHORT header_len}{header CSV}{container}
    (Weapon.cpp sendByteCodeToPlayer) -- and some of THOSE still carry the
    leading packet-id byte. Probing for the first offset that parses cleanly
    handles all three without having to guess which capture this is.
    """
    for offset in range(0, min(len(data), MAX_HEADER_PROBE)):
        if not looks_like_container(data[offset:]):
            continue
        try:
            container = parse_container(data[offset:])
        except GS2ContainerError:
            continue
        if container.code:
            return offset
    return -1


def enumerate_precompiled(root: Path, servers: List[str]
                          ) -> Tuple[List[Blob], List[Dict[str, str]]]:
    """(blobs, files skipped as non-bytecode)."""
    out: List[Blob] = []
    skipped: List[Dict[str, str]] = []
    for server in servers:
        d = root / server / "weapon_bytecode"
        if not d.is_dir():
            continue
        for path in sorted(d.iterdir()):
            if not path.is_file() or path.suffix.lower() in NON_BLOB_SUFFIXES:
                continue
            data = path.read_bytes()
            offset = find_container(data)
            if offset < 0:
                skipped.append({"path": str(path), "reason": "no GS2 container found",
                                "bytes": str(len(data))})
                continue
            out.append(Blob(server=server, origin="precompiled",
                            name=path.stem, side="clientside",
                            path=str(path), data=data[offset:]))
    return out, skipped


# ---------------------------------------------------------------------------
# compilation
# ---------------------------------------------------------------------------


def find_gs2test(explicit: Optional[str]) -> Optional[str]:
    for candidate in (explicit, str(_TOOLS / "gs2test"), shutil.which("gs2test")):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


class Compiler:
    """gs2test driver. Keeps one scratch dir for the whole sweep."""

    def __init__(self, binary: str, timeout: float = 30.0):
        self.binary = binary
        self.timeout = timeout
        self._dir = tempfile.TemporaryDirectory(prefix="gs2sweep-")

    def compile(self, source: str) -> Tuple[Optional[bytes], str]:
        """Return (bytecode, error). Exactly one is meaningful."""
        src = Path(self._dir.name) / "in.gs2"
        out = Path(self._dir.name) / "out.gs2bc"
        src.write_text(source, encoding="utf-8", errors="surrogateescape")
        if out.exists():
            out.unlink()
        try:
            proc = subprocess.run([self.binary, str(src), "-o", str(out)],
                                  capture_output=True, text=True,
                                  timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return None, "compiler timeout"
        except OSError as exc:
            return None, f"compiler failed to run: {exc}"
        if proc.returncode != 0 or not out.exists():
            msg = (proc.stderr or proc.stdout or "").strip()
            return None, first_error_line(msg) or f"exit {proc.returncode}"
        return out.read_bytes(), ""


def first_error_line(text: str) -> str:
    """Condense a compiler diagnostic to its first meaningful line, with the
    source line/column stripped so identical errors group together."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("Compiling") or line.startswith("Wrote"):
            continue
        return re.sub(r"\b\d+\b", "N", line)[:200]
    return ""


# ---------------------------------------------------------------------------
# recording stub host
# ---------------------------------------------------------------------------


class _StubObject(GS2Object):
    """Host object that answers any member read with a child stub and records
    the name. Reads do NOT land in the member dict (`has()` stays honest), so
    a script's own writes still route through the VM's normal scope chain."""

    __slots__ = ("_rec", "_children", "_path")

    def __init__(self, rec: "Recorder", path: str):
        super().__init__(name=path)
        self._rec = rec
        self._children: Dict[str, "_StubObject"] = {}
        self._path = path

    def get(self, key: str) -> Any:
        if super().has(key):
            return super().get(key)
        k = key.lower()
        self._rec.property_read(self._path, k)
        child = self._children.get(k)
        if child is None:
            child = _StubObject(self._rec, f"{self._path}.{k}")
            self._children[k] = child
        return child

    def set(self, key: str, value: Any) -> None:
        self._rec.property_write(self._path, key.lower())
        super().set(key, value)

    def __str__(self) -> str:
        # to_str() falls through to str() for objects; the default repr would
        # otherwise be pasted into every name a script builds by concatenation
        # (`"tab_" @ getText()`), producing unreadable harvested names.
        return ""


@dataclass
class Site:
    server: str
    script: str
    side: str
    function: str

    def as_tuple(self) -> Tuple[str, str, str, str]:
        return (self.server, self.script, self.side, self.function)


class Recorder:
    """Accumulates every name the corpus asked for, with call sites."""

    MAX_SITES = 4

    def __init__(self) -> None:
        self.calls: Counter = Counter()
        self.call_forms: Dict[str, Set[str]] = defaultdict(set)
        self.call_sides: Dict[str, Set[str]] = defaultdict(set)
        self.call_sites: Dict[str, List[Tuple[str, str, str, str]]] = defaultdict(list)
        self.props: Counter = Counter()
        self.prop_sites: Dict[str, List[Tuple[str, str, str, str]]] = defaultdict(list)
        self.method_keys: Set[str] = set()
        self.objects: Counter = Counter()
        self.object_sites: Dict[str, List[Tuple[str, str, str, str]]] = defaultdict(list)
        self.created: Counter = Counter()
        #: ctor args of `new GuiXCtrl(Name)` -- the engine binds those as bare
        #: globals, so they are NOT unresolved names
        self.constructed_names: Set[str] = set()
        self.site: Site = Site("", "", "", "")
        #: name -> distinct "<server>/<script>" that asked for it. Raw counts
        #: are loop-weighted (one `for` over the player list bills a property
        #: a thousand times); this is the breadth measure.
        self.call_scripts: Dict[str, Set[str]] = defaultdict(set)
        self.prop_scripts: Dict[str, Set[str]] = defaultdict(set)
        self.object_scripts: Dict[str, Set[str]] = defaultdict(set)

    def _add_site(self, table: Dict[str, List[Tuple[str, str, str, str]]],
                  scripts: Dict[str, Set[str]], key: str) -> None:
        sites = table[key]
        entry = self.site.as_tuple()
        if entry not in sites and len(sites) < self.MAX_SITES:
            sites.append(entry)
        scripts[key].add(f"{self.site.server}/{self.site.script}")

    def call(self, name: str, form: str) -> None:
        self.calls[name] += 1
        self.call_forms[name].add(form)
        self.call_sides[name].add(self.site.side)
        self._add_site(self.call_sites, self.call_scripts, name)

    def property_read(self, path: str, key: str) -> None:
        full = f"{path}.{key}"
        self.props[full] += 1
        self._add_site(self.prop_sites, self.prop_scripts, full)

    def property_write(self, path: str, key: str) -> None:
        self.property_read(path, key)

    def method_call(self, path: str, key: str) -> None:
        self.method_keys.add(f"{path}.{key}")

    def named_object(self, name: str) -> None:
        self.objects[name] += 1
        self._add_site(self.object_sites, self.object_scripts, name)


@dataclass
class SharedWorld:
    """Globals + named objects shared by every script of one server under one
    stub mode. Reborn clients share one global scope across all loaded weapon
    scripts, and the corpus relies on it: `plfunc = this;` in one weapon is
    how every other weapon on that server reaches it. A per-script scope
    reports those publications as unresolved names that are not."""
    globals: Dict[str, Any] = field(default_factory=dict)
    objects: Dict[str, "_StubObject"] = field(default_factory=dict)


class RecordingHost(GS2Host):
    """Answers everything, refuses nothing, logs every name."""

    def __init__(self, rec: Recorder, mode: str, shared: "SharedWorld"):
        self.rec = rec
        self.mode = mode
        self._globals = shared.globals
        self._objects = shared.objects

    # -- infrastructure ----------------------------------------------------

    def get_globals(self) -> Dict[str, Any]:
        return self._globals

    def sleep(self, vm: GS2VM, seconds: float) -> None:
        return None

    # -- surface -----------------------------------------------------------

    def call_builtin(self, vm: GS2VM, name: str, args: List[Any],
                     obj: Optional[GS2Object] = None) -> Any:
        # The VM consults with-scope host methods BEFORE the script's own
        # function table (vm._call_target), so answering a name the script
        # itself defines would shadow real script code and hide every name
        # inside it. Hand those back.
        if vm.has_function(name):
            return NOT_HANDLED
        if isinstance(obj, _StubObject):
            # the member read that produced this callee was recorded as a
            # property; re-tag it so the report does not double-count
            self.rec.method_call(obj._path, name)
            self.rec.call(name, "method")
        else:
            self.rec.call(name, "method" if obj is not None else "bare")
        return self._answer(name)

    def get_object(self, name: str) -> Optional[GS2Object]:
        """Always RECORD, but only `object` mode answers.

        get_object is the VM's last resort for any bare name it could not
        resolve, so a script's own un-initialised array (`expl[i] = ...`)
        arrives here too. Handing back a stub in every mode would stop the VM
        auto-vivifying those, which changes what the rest of the script does;
        the other two modes let the VM keep its native behaviour while the
        name is still harvested.
        """
        # OP_CONV_TO_OBJECT hands us the name UNFOLDED (vm.py
        # _op_conv_to_object), while _lookup lowercases first; the real host
        # lowercases on entry, so fold here or the same name is harvested
        # twice and the "*Profile" exemption misses.
        name = name.lower()
        self.rec.named_object(name)
        if self.mode != "object":
            return None
        stub = self._objects.get(name)
        if stub is None:
            stub = _StubObject(self.rec, name)
            self._objects[name] = stub
        return stub

    def create_object(self, classname: str, arg: Any) -> GS2Object:
        self.rec.created[classname] += 1
        if isinstance(arg, str) and arg:
            self.rec.constructed_names.add(arg.lower())
        return _StubObject(self.rec, f"new {classname}")

    def _answer(self, name: str) -> Any:
        if self.mode == "truthy":
            return 1.0
        if self.mode == "falsy":
            return 0.0
        return _StubObject(self.rec, name + "()")


# ---------------------------------------------------------------------------
# VM error capture
# ---------------------------------------------------------------------------


class VMErrorCollector(logging.Handler):
    """Captures the per-opcode error the VM logs from its own guard rail
    (GS2VM._execute's except clause -> _log_once), so the sweep can attribute
    a crash to a script/opcode without touching VM semantics."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.errors: List[Dict[str, Any]] = []
        self.other: Counter = Counter()
        self.site: Site = Site("", "", "", "")

    def emit(self, record: logging.LogRecord) -> None:
        if record.msg == _LOG_ERROR_MSG:
            script, pc, op, exc = record.args
            self.errors.append({
                "script": str(script),
                "pc": int(pc),
                "op": str(op),
                "error": str(exc),
                "site": self.site.as_tuple(),
            })
        else:
            try:
                self.other[record.getMessage()[:200]] += 1
            except Exception:
                self.other[str(record.msg)[:200]] += 1


# ---------------------------------------------------------------------------
# per-blob analysis
# ---------------------------------------------------------------------------


@dataclass
class BlobResult:
    label: str
    server: str
    origin: str
    side: str
    functions: int = 0
    instructions: int = 0
    code_bytes: int = 0
    container_error: str = ""
    decode_error: str = ""
    unknown_ops: Counter = field(default_factory=Counter)
    deferred_ops: Counter = field(default_factory=Counter)
    orphan_operands: Counter = field(default_factory=Counter)
    skipped_ops: Counter = field(default_factory=Counter)
    vm_errors: int = 0


def analyse_blob(blob: Blob, rec: Recorder, collector: VMErrorCollector,
                 modes: List[str], max_ops: int,
                 worlds: Dict[Tuple[str, str], SharedWorld]) -> BlobResult:
    res = BlobResult(label=blob.label, server=blob.server, origin=blob.origin,
                     side=blob.side)
    try:
        container = parse_container(blob.data)
    except (GS2ContainerError, Exception) as exc:  # noqa: BLE001 - report, never abort
        res.container_error = f"{type(exc).__name__}: {exc}"
        return res

    res.functions = len(container.functions)
    res.code_bytes = len(container.code)

    try:
        instrs = decode(container.code)
        res.instructions = len(instrs)
        for instr in instrs:
            if instr.opnum in DEFERRED_OPCODES:
                res.deferred_ops[instr.opnum] += 1
            elif instr.opnum not in Op._value2member_map_:
                res.unknown_ops[instr.opnum] += 1
            # A 0xF0-0xF6 record attached to an opcode that takes no operand.
            # disasm.decode() folds those into the previous instruction so the
            # stream cannot desync, which is right for the operand-register
            # model -- but it also means opcode bytes 240/241/242 (inside the
            # deliberately-unmodelled 200-242 range) would be swallowed as
            # markers rather than reported. Count them so the "no unmodelled
            # opcodes found" result is falsifiable.
            if instr.operand is not None and instr.operand.marker >= 0 \
                    and instr.op not in OPERAND_OPS:
                res.orphan_operands[instr.opnum] += 1
    except (GS2DecodeError, Exception) as exc:  # noqa: BLE001
        res.decode_error = f"{type(exc).__name__}: {exc}"
        return res

    for mode in modes:
        world = worlds.setdefault((blob.server, mode), SharedWorld())
        run_blob(container, blob, rec, collector, mode, max_ops, res, world)
    return res


def run_blob(container, blob: Blob, rec: Recorder, collector: VMErrorCollector,
             mode: str, max_ops: int, res: BlobResult,
             world: SharedWorld) -> None:
    """Run the top level and then every function the container declares, on a
    single VM so top-level state (`this.x = ...`, published globals) is in
    place exactly as it would be at runtime."""
    before_skipped = dict(GS2VM.ops_skipped)
    host = RecordingHost(rec, mode, world)
    try:
        vm = GS2VM(container, name=blob.name, host=host)
    except Exception as exc:  # noqa: BLE001
        res.container_error = res.container_error or f"VM init: {type(exc).__name__}: {exc}"
        return
    vm.max_ops = max_ops
    # per-blob so every script's own errors surface (the VM dedups warnings
    # class-wide through _logged_once)
    GS2VM._logged_once = set()

    errors_before = len(collector.errors)

    rec.site = Site(blob.server, blob.name, blob.side, "<toplevel>")
    collector.site = rec.site
    vm.run_toplevel()

    for fname in sorted(vm.functions):
        rec.site = Site(blob.server, blob.name, blob.side, fname)
        collector.site = rec.site
        try:
            vm.call(fname)
        except Exception as exc:  # noqa: BLE001 - GS2VM.call claims never to raise
            collector.errors.append({
                "script": blob.name, "pc": -1, "op": "<call>",
                "error": f"{type(exc).__name__}: {exc}",
                "site": rec.site.as_tuple(),
            })

    res.vm_errors += len(collector.errors) - errors_before
    for opnum, count in GS2VM.ops_skipped.items():
        delta = count - before_skipped.get(opnum, 0)
        if delta:
            res.skipped_ops[opnum] += delta


# ---------------------------------------------------------------------------
# reference host surface (pyReborn)
# ---------------------------------------------------------------------------


def load_host_surface() -> Tuple[Optional[Set[str]], Optional[Set[str]], str]:
    """(builtin names, named-object names, error) from the real client host.

    The named-object side is GS2ClientHost.get_object's static registry plus
    the GS1 builtin-variable tables it falls through to (gs2_client.py
    get_object tail -> gs1._host.get_builtin), so a bare `graalversion` or
    `screenwidth` read is not reported as unresolved when it is not.
    """
    try:
        from pyreborn.gs2_client import GS2ClientHost, _GS2_OBJECTS
        from pyreborn.gs1_client import (_GS1_BUILTINS, _GS1_NPC_BUILTINS,
                                         _GS1_PLAYER_BUILTINS)
    except Exception as exc:  # noqa: BLE001
        return None, None, f"{type(exc).__name__}: {exc}"
    try:
        objects = (set(_GS2_OBJECTS) | set(_GS1_BUILTINS)
                   | set(_GS1_NPC_BUILTINS) | set(_GS1_PLAYER_BUILTINS))
        return set(GS2ClientHost.host_surface()), objects, ""
    except Exception as exc:  # noqa: BLE001
        return None, None, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def rank(counter: Counter, sites: Dict[str, List[Tuple[str, str, str, str]]],
         scripts: Dict[str, Set[str]],
         extra: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Rank by raw call count (what the task asks for) but carry the
    distinct-script breadth alongside: a name billed 1100 times by one loop
    is not more wanted than one billed twice by twenty scripts."""
    out = []
    for name, count in counter.most_common():
        used_by = sorted(scripts.get(name, ()))
        entry: Dict[str, Any] = {"name": name, "count": count,
                                 "scripts": len(used_by),
                                 "used_by": used_by[:12],
                                 "sites": [list(s) for s in sites.get(name, [])]}
        if extra:
            for key, table in extra.items():
                value = table.get(name)
                if value is not None:
                    entry[key] = sorted(value) if isinstance(value, set) else value
        out.append(entry)
    return out


def group_vm_errors(errors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group by (opcode, normalised message): the root cause is the opcode
    handler plus the Python exception it raised, not the individual site."""
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for err in errors:
        norm = re.sub(r"0x[0-9a-fA-F]+|\b\d+\b", "N", err["error"])[:160]
        key = (err["op"], norm)
        g = groups.setdefault(key, {"op": err["op"], "error": norm, "count": 0,
                                    "examples": [], "scripts": set()})
        g["count"] += 1
        g["scripts"].add(err["site"][0] + "/" + err["site"][1])
        if len(g["examples"]) < 4:
            g["examples"].append({"script": err["script"], "pc": err["pc"],
                                  "error": err["error"], "site": err["site"]})
    out = []
    for g in sorted(groups.values(), key=lambda g: -g["count"]):
        g["scripts"] = sorted(g["scripts"])[:8]
        out.append(g)
    return out


def print_summary(report: Dict[str, Any], top: int) -> None:
    c = report["corpus"]
    print("=" * 72)
    print("  GS2 CORPUS SWEEP")
    print("=" * 72)
    print(f"  servers            : {', '.join(c['servers'])}")
    print(f"  source halves      : {c['source_halves']} "
          f"(compiled {c['compiled_ok']}, failed {c['compile_failed']})")
    print(f"  precompiled blobs  : {c['precompiled_blobs']}")
    print(f"  blobs analysed     : {c['blobs_analysed']}  "
          f"({c['instructions']} instructions, {c['code_bytes']} code bytes)")
    print(f"  stub modes         : {', '.join(report['modes'])}")
    print(f"  elapsed            : {report['elapsed_sec']:.1f}s")

    print("\n-- unknown opcodes (no entry in opcodes.Op) " + "-" * 28)
    if not report["unknown_opcodes"]:
        print("  none")
    for e in report["unknown_opcodes"][:top]:
        print(f"  {e['opcode']:>4}  x{e['count']:<7} {', '.join(e['scripts'][:3])}")

    print("\n-- deferred opcodes (newer compiler: 54, 200-242) " + "-" * 22)
    if not report["deferred_opcodes"]:
        print("  none")
    for e in report["deferred_opcodes"][:top]:
        print(f"  {e['opcode']:>4}  x{e['count']:<7} {', '.join(e['scripts'][:3])}")

    print("\n-- operand markers on non-operand opcodes " + "-" * 30)
    if not report["orphan_operand_markers"]:
        print("  none")
    for e in report["orphan_operand_markers"][:top]:
        print(f"  {e['opcode']:>4} {e['name']:<24} x{e['count']:<7} "
              f"{', '.join(e['scripts'][:2])}")

    print("\n-- known opcodes with no VM handler " + "-" * 36)
    if not report["unimplemented_executed"]:
        print("  none")
    for e in report["unimplemented_executed"][:top]:
        print(f"  {e['opcode']:>4} {e['name']:<24} x{e['count']}")

    print(f"\n-- missing builtins (top {top}; calls / distinct scripts) " + "-" * 12)
    for e in report["missing_builtins"][:top]:
        sides = ",".join(e.get("sides", []))
        used = "; ".join(e["used_by"][:2])
        print(f"  {e['count']:>7} {e['scripts']:>4}  {e['name']:<30} "
              f"{sides:<22} {used}")

    print(f"\n-- unresolved named objects (top {top}) " + "-" * 32)
    for e in report["missing_objects"][:top]:
        used = "; ".join(e["used_by"][:2])
        print(f"  {e['count']:>7} {e['scripts']:>4}  {e['name']:<30} {used}")

    print(f"\n-- properties on ENGINE objects (top {top}) " + "-" * 28)
    for e in report["engine_object_properties"][:top]:
        used = "; ".join(e["used_by"][:2])
        print(f"  {e['count']:>7} {e['scripts']:>4}  {e['name']:<30} {used}")

    print(f"\n-- all properties requested (top {top}) " + "-" * 32)
    for e in report["properties"][:top]:
        used = "; ".join(e["used_by"][:2])
        print(f"  {e['count']:>7} {e['scripts']:>4}  {e['name']:<30} {used}")

    print("\n-- VM error groups " + "-" * 53)
    if not report["vm_errors"]:
        print("  none")
    for g in report["vm_errors"][:top]:
        print(f"  {g['count']:>7}  {g['op']:<22} {g['error'][:60]}")
        print(f"           scripts: {', '.join(g['scripts'][:4])}")

    print("\n-- compile results by server/side " + "-" * 38)
    for e in report["compile_by_bucket"]:
        print(f"  {e['bucket']:<40} ok {e['ok']:>4}  failed {e['failed']:>4}")

    print("\n-- compile failures by diagnostic " + "-" * 38)
    for e in report["compile_failures"][:top]:
        print(f"  {e['count']:>7}  {e['error'][:80]}")
        print(f"           e.g. {', '.join(e['scripts'][:3])}")
    print()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--corpus", default=str(_WORKSPACE / "Preagonal"),
                    help="root holding the third-party server checkouts")
    ap.add_argument("--servers", default=",".join(DEFAULT_SERVERS),
                    help="comma-separated server directory names")
    ap.add_argument("--gs2test", default=None, help="path to the gs2test compiler")
    ap.add_argument("--modes", default=",".join(STUB_MODES),
                    help=f"stub answer modes to sweep ({'/'.join(STUB_MODES)})")
    ap.add_argument("--max-ops", type=int, default=50_000,
                    help="per-invocation instruction budget (GS2VM.max_ops)")
    ap.add_argument("--out", default="gs2_corpus_report.json",
                    help="where to write the JSON report")
    ap.add_argument("--top", type=int, default=40, help="rows per human table")
    ap.add_argument("--seed", type=int, default=0, help="RNG seed for OP_RANDOM")
    ap.add_argument("--no-compile", action="store_true",
                    help="skip source compilation; sweep precompiled blobs only")
    args = ap.parse_args(argv)

    root = Path(args.corpus)
    servers = [s for s in args.servers.split(",") if s]
    modes = [m for m in args.modes.split(",") if m]
    for mode in modes:
        if mode not in STUB_MODES:
            ap.error(f"unknown stub mode {mode!r}; pick from {STUB_MODES}")
    if not root.is_dir():
        ap.error(f"corpus root {root} does not exist")

    started = time.time()
    # OP_RANDOM draws from the stdlib RNG (vm.py _op_random), and scripts
    # branch on it -- seed so two sweeps of the same corpus are comparable.
    random.seed(args.seed)
    logging.disable(logging.NOTSET)
    vm_logger = logging.getLogger("reborn_protocol.gs2.vm")
    vm_logger.setLevel(logging.WARNING)
    vm_logger.propagate = False
    collector = VMErrorCollector()
    vm_logger.addHandler(collector)

    GS2VM.reset_coverage()
    rec = Recorder()

    sources = enumerate_sources(root, servers)
    blobs, skipped_files = enumerate_precompiled(root, servers)
    precompiled_count = len(blobs)

    compile_failures: Counter = Counter()
    compile_failure_scripts: Dict[str, List[str]] = defaultdict(list)
    compile_failed_by_bucket: Counter = Counter()
    compiled_by_bucket: Counter = Counter()
    compiled_ok = 0

    binary = None if args.no_compile else find_gs2test(args.gs2test)
    if binary is None and not args.no_compile:
        print("WARNING: gs2test not found; sweeping precompiled blobs only",
              file=sys.stderr)
    if binary is not None:
        compiler = Compiler(binary)
        for src in sources:
            bucket = f"{src.server}/{src.side}"
            code, error = compiler.compile(src.source)
            if code is None:
                compile_failures[error] += 1
                compile_failed_by_bucket[bucket] += 1
                scripts = compile_failure_scripts[error]
                if len(scripts) < 8:
                    scripts.append(src.label)
                continue
            compiled_ok += 1
            compiled_by_bucket[bucket] += 1
            blobs.append(Blob(server=src.server, origin="compiled",
                              name=src.name, side=src.side,
                              path=str(src.path), data=code))

    results: List[BlobResult] = []
    worlds: Dict[Tuple[str, str], SharedWorld] = {}
    for blob in blobs:
        results.append(analyse_blob(blob, rec, collector, modes, args.max_ops,
                                    worlds))

    # ---- aggregate -------------------------------------------------------
    unknown: Counter = Counter()
    unknown_scripts: Dict[int, Set[str]] = defaultdict(set)
    deferred: Counter = Counter()
    deferred_scripts: Dict[int, Set[str]] = defaultdict(set)
    orphan: Counter = Counter()
    orphan_scripts: Dict[int, Set[str]] = defaultdict(set)
    skipped: Counter = Counter()
    container_failures: List[Dict[str, str]] = []
    total_instructions = 0
    total_code_bytes = 0

    for res in results:
        total_instructions += res.instructions
        total_code_bytes += res.code_bytes
        for opnum, count in res.unknown_ops.items():
            unknown[opnum] += count
            unknown_scripts[opnum].add(res.label)
        for opnum, count in res.deferred_ops.items():
            deferred[opnum] += count
            deferred_scripts[opnum].add(res.label)
        for opnum, count in res.orphan_operands.items():
            orphan[opnum] += count
            orphan_scripts[opnum].add(res.label)
        skipped.update(res.skipped_ops)
        if res.container_error or res.decode_error:
            container_failures.append({
                "script": res.label, "origin": res.origin,
                "container_error": res.container_error,
                "decode_error": res.decode_error,
            })

    surface, object_surface, surface_error = load_host_surface()

    def missing(counter: Counter, known: Optional[Set[str]]) -> Counter:
        if known is None:
            return counter
        return Counter({k: v for k, v in counter.items() if k not in known})

    missing_calls = missing(rec.calls, surface)
    missing_calls = Counter({k: v for k, v in missing_calls.items()
                             if k not in VM_NATIVE_METHODS})
    # a property name that later dispatched as a method is a CALL, not a
    # property: drop it from the property ranking to avoid double counting
    props = Counter({k: v for k, v in rec.props.items()
                     if k not in rec.method_keys})
    # Properties whose ROOT is an engine object (player, level, server, ...)
    # are the ones a host can actually implement; the rest hang off GUI
    # controls the script created itself.
    engine_props = Counter({
        k: v for k, v in props.items()
        if object_surface is not None and k.split(".", 1)[0] in object_surface
    })

    # Named-object misses need three more exemptions before they mean
    # anything, all of them real host resolution paths that no static table
    # can express (pyReborn gs2_client.py GS2ClientHost.get_object):
    #   * "<something>profile" is an engine-builtin GUI profile
    #   * a loaded weapon's own name resolves to that weapon's script object
    #   * a control constructed as `new GuiXCtrl(Name)` becomes a bare global
    corpus_script_names: Set[str] = set()
    for name in [s.name for s in sources] + [b.name for b in blobs]:
        low = name.lower()
        corpus_script_names.add(low)
        # weapons/ files are named "weapon<realname>.txt"; the runtime name a
        # script addresses is the REALNAME, without that prefix
        if low.startswith("weapon"):
            corpus_script_names.add(low[len("weapon"):])
            corpus_script_names.add(low[len("weapon"):].lstrip("-@"))
    constructed_names = {n.lower() for n in rec.constructed_names}
    # Names the corpus assigns to itself. get_object is the VM's LAST resort,
    # so a script's own global read before its first write lands here too;
    # sweeping functions in isolation makes that common. A name the corpus
    # writes is script state, not a host gap.
    script_globals: Set[str] = set()
    for world in worlds.values():
        script_globals |= {k.lower() for k in world.globals}

    unresolved_all = rec.objects
    missing_objects = missing(rec.objects, object_surface)
    if surface is not None:
        missing_objects = Counter({k: v for k, v in missing_objects.items()
                                   if k not in surface})
    missing_objects = Counter({
        k: v for k, v in missing_objects.items()
        if not k.endswith("profile")
        and k not in corpus_script_names
        and k.lstrip("-") not in corpus_script_names
        and k not in constructed_names
        and k not in script_globals
    })

    report: Dict[str, Any] = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_sec": time.time() - started,
        "modes": modes,
        "max_ops": args.max_ops,
        "host_surface": {
            "builtins": len(surface) if surface else 0,
            "named_objects": len(object_surface) if object_surface else 0,
            "error": surface_error,
        },
        "corpus": {
            "root": str(root),
            "servers": servers,
            "source_files": len({s.path for s in sources}),
            "source_halves": len(sources),
            "compiled_ok": compiled_ok,
            "compile_failed": sum(compile_failures.values()),
            "precompiled_blobs": precompiled_count,
            "precompiled_skipped": skipped_files,
            "blobs_analysed": len(blobs),
            "instructions": total_instructions,
            "code_bytes": total_code_bytes,
        },
        "unknown_opcodes": [
            {"opcode": op, "count": n, "scripts": sorted(unknown_scripts[op])[:8]}
            for op, n in unknown.most_common()
        ],
        "deferred_opcodes": [
            {"opcode": op, "count": n, "scripts": sorted(deferred_scripts[op])[:8]}
            for op, n in deferred.most_common()
        ],
        "orphan_operand_markers": [
            {"opcode": op, "name": op_name(op), "count": n,
             "scripts": sorted(orphan_scripts[op])[:8]}
            for op, n in orphan.most_common()
        ],
        "unimplemented_executed": [
            {"opcode": op, "name": op_name(op), "count": n}
            for op, n in skipped.most_common()
        ],
        "missing_builtins": rank(missing_calls, rec.call_sites, rec.call_scripts,
                                 {"sides": rec.call_sides, "forms": rec.call_forms}),
        "all_builtins_called": rank(rec.calls, rec.call_sites, rec.call_scripts,
                                    {"sides": rec.call_sides}),
        "missing_objects": rank(missing_objects, rec.object_sites,
                                rec.object_scripts),
        "unresolved_names_all": rank(unresolved_all, rec.object_sites,
                                     rec.object_scripts),
        "properties": rank(props, rec.prop_sites, rec.prop_scripts),
        "engine_object_properties": rank(engine_props, rec.prop_sites,
                                         rec.prop_scripts),
        "constructed_classes": [{"name": k, "count": v}
                                for k, v in rec.created.most_common()],
        "vm_errors": group_vm_errors(collector.errors),
        "vm_warnings": [{"message": k, "count": v}
                        for k, v in collector.other.most_common(60)],
        "container_failures": container_failures,
        "compile_failures": [
            {"error": err, "count": n, "scripts": compile_failure_scripts[err]}
            for err, n in compile_failures.most_common()
        ],
        "compile_by_bucket": [
            {"bucket": b, "ok": compiled_by_bucket.get(b, 0),
             "failed": compile_failed_by_bucket.get(b, 0)}
            for b in sorted(set(compiled_by_bucket) | set(compile_failed_by_bucket))
        ],
        "per_blob": [
            {"label": r.label, "server": r.server, "origin": r.origin,
             "side": r.side, "functions": r.functions,
             "instructions": r.instructions, "vm_errors": r.vm_errors,
             "unknown_ops": {str(k): v for k, v in r.unknown_ops.items()},
             "deferred_ops": {str(k): v for k, v in r.deferred_ops.items()},
             "container_error": r.container_error,
             "decode_error": r.decode_error}
            for r in results
        ],
    }

    Path(args.out).write_text(json.dumps(report, indent=1), encoding="utf-8")
    print_summary(report, args.top)
    print(f"JSON report: {Path(args.out).resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
