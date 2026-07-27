"""GS2 bytecode virtual machine.

A stack machine executing the instruction stream decoded by disasm.decode().
Semantics are derived from (in priority order, per the build ground rules):

1. The gs2parser compiler emitter (GS2CompilerVisitor.cpp) -- what stack
   layout each construct actually produces (argument order, ArrayStart
   markers, param binding, jump labels, with/foreach protocols, builtin
   signatures in GS2BuiltInFunctions.cpp).
2. The C# client's GS2Engine (ScriptMachine.cs) as the runtime tiebreaker --
   confirmed OP_JMP is a runtime no-op, OP_FUNC_PARAMS_END binds param names
   (pushed in reverse) against caller args, OP_CALL collects args down to the
   ArrayStart marker and recurses, operands 0xF0-0xF6 attach to the previous
   opcode, and jump operands are instruction indices (not byte offsets).
   Where GS2Engine is visibly buggy/asymmetric (e.g. OP_AND not pushing on
   the short-circuit jump while OP_OR does, OP_OBJ_STARTS popping operands
   in the wrong order vs the compiler's OBJECT_FIRST layout) the compiler's
   stack layout wins.

Safety contract (QA requirement): nothing raises out of the VM. Unknown
opcodes are logged once and skipped; handler exceptions are logged once per
site and abort only the current event call; every run is bounded by
max_ops. Coverage (ops seen/implemented/skipped, builtins called/missing)
is tracked on the class so a corpus run can print an honest progress report.
"""
from __future__ import annotations

import logging
import math
import random as _random
import re
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from ..gs1.csv import gs1_csv_split
from .container import GS2Container, parse_container
from .disasm import Instruction, decode
from .opcodes import Op
from .values import (  # noqa: F401 -- the coercion policy lives in values.py;
    # array_index/array_size/to_int32/MAX_ARRAY_SIZE/ARRAY_INDEX_EPSILON are
    # re-exported here because importers (and gs2/__init__) know them as
    # gs2.vm names.
    ARRAY_INDEX_EPSILON, ARRAY_START, GS2_NULL, ElemRef, GS2Object, LValue,
    MAX_ARRAY_SIZE, SCRIPT_EPSILON, VarRef, _f32, array_index, array_size,
    casefold, copy_value, fmt_num, gs2_compare, gs2_eq, gs2_to_num,
    gs2_truthy, strtofloat, to_bool, to_int32, to_num, to_str, wrap_int32,
)

logger = logging.getLogger(__name__)

#: sentinel returned by hosts for "builtin not handled here"
NOT_HANDLED = object()

#: cap on any single array allocation/index-driven growth (arr[100000000]=1
#: must not try to allocate a 100M-element list). Applied wherever a script
#: index controls array size: OP_ARRAY_ASSIGN, OP_OBJ_REPLACESTRING.
MAX_ARRAY_INDEX = 1 << 20

#: register-file bound for OP_REG_STORE/OP_REG_LOAD -- the official
#: interpreter rejects indices >= 0x400 (getCallStackRegister,
#: src/TScriptMachine.cpp:2160-2169; the registers live on the CALL-STACK
#: entry, which is why ours are per-frame).
MAX_REGISTER_INDEX = 0x3FF


def _denull(value: Any) -> Any:
    """Collapse the GS2_NULL sentinel back to None.

    GS2_NULL exists so `x == null` can take the OBJECT/OBJECT row of
    gs2_compare instead of the NUMBER row (values._GS2Null). It is an
    expression-stack value only: the instant it would be stored in a variable
    or handed to a host builtin it becomes plain None, so pyReborn's host
    surface -- which tests `arg is None` all over -- never has to learn about
    it. The two are interchangeable everywhere else (both are falsy, both
    to_num to 0.0, both to_str to "")."""
    return None if value is GS2_NULL else value


def _value_in_range(value: float, lo: float, hi: float, mode: int) -> bool:
    """ValueInRange (Preagonal/FourPlay/quattroplay/src/unsorted.cpp:
    1359-1376): each end is inclusive or exclusive per the mode bits, and
    both carry the 0.0001 tolerance. A mode outside 0..3 is `return false`,
    not "behave like mode 0"."""
    if not 0 <= mode <= 3:
        return False
    eps = ARRAY_INDEX_EPSILON
    lo_ok = value - lo > (eps if mode in (2, 3) else -eps)
    hi_ok = (-eps if mode in (1, 3) else eps) > value - hi
    return lo_ok and hi_ok


class GS2Host:
    """Host interface the VM calls out to. Default implementation is inert;
    pyReborn provides a real bridge (routing to the same client host surface
    the GS1 engine uses) in phase 3."""

    def call_builtin(self, vm: "GS2VM", name: str, args: List[Any],
                     obj: Optional[GS2Object] = None) -> Any:
        """Handle a builtin/global function call (or obj method call when
        obj is not None). Return NOT_HANDLED if unknown."""
        return NOT_HANDLED

    def get_object(self, name: str) -> Optional[GS2Object]:
        """Resolve a named object (player, level, weapon names...)."""
        return None

    def create_object(self, classname: str, arg: Any) -> GS2Object:
        """new <classname>(arg)"""
        return GS2Object(name=classname)

    def sleep(self, vm: "GS2VM", seconds: float) -> None:
        """OP_SLEEP / sleep(n). Default: no-op (the VM never blocks)."""

    def get_globals(self) -> Dict[str, Any]:
        """Storage for unqualified variable writes. Hosts may share one dict
        across scripts (Reborn client globals are shared)."""
        raise NotImplementedError


class GS2ScriptFunction:
    """A callable bound to one of a script's own functions.

    This is what a member read of ``this.<script function name>`` evaluates
    to (see _ScriptFnRef). It is the value `onAction = function(){...}` puts
    in the control's onAction slot, so a host can simply call whatever it
    finds there. Calling it runs the function synchronously through
    GS2VM.call().
    """

    __slots__ = ("vm", "name")

    def __init__(self, vm: "GS2VM", name: str):
        self.vm = vm
        self.name = name

    def __call__(self, *args: Any) -> Any:
        return self.vm.call(self.name, *args)

    def __repr__(self) -> str:
        return f"<GS2ScriptFunction {self.vm.name}.{self.name}>"


class _ScriptFnRef(LValue):
    """Member slot on a this-object whose name is also one of the script's
    own functions, and which holds no stored value.

    `onAction = function(){...};` compiles to a *read* of
    `this.<generated-name>`: the compiler declares the lambda body as a
    public script function and then emits
    `OP_THIS; OP_TYPE_VAR <name>; OP_MEMBER_ACCESS; OP_CONV_TO_OBJECT`
    (GS2CompilerVisitor.cpp:970-994, name from
    ParserContext::generateLambdaFuncName, Parser.cpp:133 -> "function_1NN_1";
    ExpressionFnObject's ctor marks the decl public, ast.h:855). The same
    shape backs the plain `x = function(){...}; x();` lambda idiom.

    """

    __slots__ = ("_vm",)

    def __init__(self, vm: "GS2VM", obj: Any, key: str):
        super().__init__(obj, key)
        self._vm = vm

    def get(self) -> Any:
        value = super().get()
        if value is None:
            return self._vm.script_function(self.key)
        return value


class _NameVivifyRef(LValue):
    """Member slot behind an UNDEFINED (or non-object) bare global/local
    name. Write semantics match GS2Engine's variable collection: the first
    member WRITE auto-creates the holder object and assigns it through the
    normal scope chain (temps -> this -> globals, honoring with-scope and
    host this-object claims via _assign_name); reads before any write yield
    None and create nothing. Without this, `tmp.node = <treenode>` in the
    live Login serverlist builder silently dropped every node id/sortgroup/
    icon write (tmp is a plain identifier there, not the temp. prefix)."""

    __slots__ = ("_vm", "_frame", "_varname")

    def __init__(self, vm: "GS2VM", frame, varname: str, key: str):
        super().__init__(None, key)
        self._vm = vm
        self._frame = frame
        self._varname = varname

    def set(self, value) -> None:
        if not isinstance(self.obj, GS2Object):
            holder = self._vm._lookup(self._varname, self._frame)
            if not isinstance(holder, GS2Object):
                holder = GS2Object(name=self._varname)
                self._vm._assign_name(self._varname, holder, self._frame)
            self.obj = holder
        self.obj.set(self.key, value)


def _csv_tokens(value: str) -> Optional[List[str]]:
    """Official OP_CONV_TO_OBJECT string-property rule
    (TScriptStackEntry::switchTypeObject, quattroplay): a string-valued
    property converts to a temporary token ARRAY iff it contains a comma or
    is fully quoted (first and last char '"'); any other string stays a
    non-object (null-object entry). The token dialect is the shared engine
    CSV format (gs1_csv_split). The conversion result is a TEMP -- writes
    into it never reach the source variable -- which a fresh list models
    exactly."""
    if "," in value or (len(value) >= 2 and value[0] == '"' and value[-1] == '"'):
        return gs1_csv_split(value)
    return None


_FMT_RE = re.compile(r"%(-?\d*)(?:\.(\d+))?([dioxXucsfeEgG%])")


def printf_format(fmt: str, args: List[Any]) -> str:
    """C-style format used by format() (OP_FORMAT). Supports the common
    subset: %s %d %i %o %x %X %u %c %f %e %E %g %G %% with width/precision."""
    out: List[str] = []
    pos = 0
    argi = 0

    def next_arg() -> Any:
        nonlocal argi
        if argi < len(args):
            v = args[argi]
            argi += 1
            return v
        return ""

    for m in _FMT_RE.finditer(fmt):
        out.append(fmt[pos:m.start(0)])
        pos = m.end(0)
        width, prec, spec = m.group(1), m.group(2), m.group(3)
        if spec == "%":
            out.append("%")
            continue
        v = next_arg()
        try:
            if spec in "diu":
                py = f"%{width}d" % int(gs2_to_num(v))
            elif spec in "oxX":
                py = f"%{width}{spec}" % int(gs2_to_num(v))
            elif spec == "c":
                n = gs2_to_num(v)
                py = chr(int(n)) if isinstance(v, (int, float)) or _looks_numeric(v) else to_str(v)[:1]
                if width:
                    py = f"%{width}s" % py
            elif spec in "feEgG":
                p = prec if prec is not None else "6"
                py = f"%{width}.{p}{spec}" % gs2_to_num(v)
            else:  # %s
                s = to_str(v)
                if prec is not None:
                    s = s[: int(prec)]
                py = f"%{width}s" % s if width else s
        except (ValueError, TypeError, OverflowError):
            py = to_str(v)
        out.append(py)
    out.append(fmt[pos:])
    return "".join(out)


def _looks_numeric(v: Any) -> bool:
    if isinstance(v, str):
        try:
            float(v)
            return True
        except ValueError:
            return False
    return isinstance(v, (int, float, bool))


class GS2VM:
    """One VM instance per loaded script (weapon/npc/class/gani). All state
    (this-object, per-frame temps) is per-instance; globals come from the
    host (or a per-VM dict when no host provides one)."""

    # ---- class-level coverage accounting (aggregated across instances) ----
    ops_seen: Dict[int, int] = {}
    ops_skipped: Dict[int, int] = {}
    builtins_called: Dict[str, int] = {}
    builtins_missing: Dict[str, int] = {}
    _logged_once: set = set()

    #: default instruction budget per event invocation (incl. nested calls)
    max_ops = 500_000
    #: abort the current event after this many handler errors
    max_errors = 50

    def __init__(self, data: Union[bytes, GS2Container], name: str = "",
                 host: Optional[GS2Host] = None):
        container = data if isinstance(data, GS2Container) else parse_container(data)
        self.container = container
        self.name = name
        self.host = host
        self.instructions: List[Instruction] = decode(container.code)
        self.strings = container.strings
        # function name (lowercased) -> entry instruction index
        self.functions: Dict[str, int] = {}
        # public. prefix marks cross-script-callable functions; universe
        # functions arrive as "name,objname.name" (see StatementFnDeclNode)
        self.public_functions: set = set()
        for f in container.functions:
            fname = f.name
            for alias in fname.split(","):
                is_public = alias.startswith("public.")
                if is_public:
                    alias = alias[len("public."):]
                self.functions[alias.lower()] = f.op_index
                if is_public:
                    self.public_functions.add(alias.lower())

        self.this = GS2Object(name=name or "this")
        self.thiso = self.this
        self._globals: Optional[Dict[str, Any]] = None
        # joined-class VMs get their function tables merged in (phase 4)
        self.joined: List["GS2VM"] = []
        self._ops_used = 0
        self._errors = 0
        self._dispatch = self._build_dispatch()

    # ------------------------------------------------------------- publics

    def has_function(self, name: str) -> bool:
        return name.lower() in self.functions or any(
            j.has_function(name) for j in self.joined)

    @property
    def this(self) -> Any:
        return self._this

    @this.setter
    def this(self, value: Any) -> None:
        """Register this VM on the object's script_vms, for cross-script calls.

        Hosts swap `this` in after construction, so it cannot be done in __init__.
        """
        self._this = value
        owners = getattr(value, "script_vms", None)
        if owners is not None and self not in owners:
            owners.append(self)

    def has_public_function(self, name: str) -> bool:
        """Declared `public function` here or in a joined class -- the engine's only
        cross-script call surface.
        """
        return name.lower() in self.public_functions or any(
            j.has_public_function(name) for j in self.joined)

    def script_function(self, name: str) -> Optional[GS2ScriptFunction]:
        """The script's own function `name` as a plain Python callable, or
        None if this script (or any class it joined) does not declare it.
        """
        if self.has_function(name):
            return GS2ScriptFunction(self, name.lower())
        return None

    def call(self, name: str, *args: Any) -> Any:
        """Invoke a script function by name (event entry point). Returns the
        script's return value, or None if the function does not exist.
        Never raises."""
        key = name.lower()
        idx = self.functions.get(key)
        if idx is None:
            for j in self.joined:
                if j.has_function(name):
                    return j.call(name, *args)
            return None
        try:
            gen = self._start_execution(idx, list(args), coro_mode=False)
            while True:
                try:
                    next(gen)
                except StopIteration as done:
                    # `return null;` must reach the host as None, not the
                    # expression-stack sentinel -- see _denull.
                    return _denull(done.value)
        except Exception as e:  # absolute backstop; _execute already guards
            self._log_once(("call", self.name, key, type(e).__name__),
                           "GS2 %s.%s aborted: %s", self.name, name, e)
            return None

    def iter_call(self, name: str, *args: Any):
        """Invoke a script function as a coroutine yielding sleep durations."""
        key = name.lower()
        idx = self.functions.get(key)
        if idx is None:
            for j in self.joined:
                if j.has_function(name):
                    return j.iter_call(name, *args)

            def empty():
                if False:
                    yield None
                return None
            return empty()
        return self._start_execution(idx, list(args), coro_mode=True)

    def _start_execution(self, idx: int, args: List[Any], coro_mode: bool):
        self._ops_used = 0
        self._errors = 0
        return self._execute(idx, args, coro_mode)

    def run_toplevel(self) -> None:
        """Execute the script from instruction 0: runs any statements outside
        function bodies (function bodies are skipped by the compiler's
        OP_SET_INDEX prejumps). Safe on scripts that are functions-only."""
        if not self.instructions:
            return
        self._ops_used = 0
        self._errors = 0
        try:
            gen = self._start_execution(0, [], coro_mode=False)
            for _ in gen:
                pass
        except Exception as e:
            self._log_once(("toplevel", self.name, type(e).__name__),
                           "GS2 %s toplevel aborted: %s", self.name, e)

    @property
    def globals(self) -> Dict[str, Any]:
        if self.host is not None:
            try:
                return self.host.get_globals()
            except NotImplementedError:
                pass
        if self._globals is None:
            self._globals = {}
        return self._globals

    # ------------------------------------------------------------ coverage

    @classmethod
    def coverage_report(cls) -> Dict[str, Any]:
        implemented = set(cls._implemented_ops())
        seen = set(cls.ops_seen)
        return {
            "implemented_ops": sorted(implemented),
            "seen_ops": dict(sorted(cls.ops_seen.items())),
            "executed_unimplemented": dict(sorted(cls.ops_skipped.items())),
            "seen_not_implemented": sorted(seen - implemented),
            "builtins_called": dict(sorted(cls.builtins_called.items())),
            "builtins_missing": dict(sorted(cls.builtins_missing.items())),
        }

    @classmethod
    def coverage_summary(cls) -> str:
        rep = cls.coverage_report()
        lines = [
            f"GS2 VM coverage: {len(rep['implemented_ops'])} ops implemented, "
            f"{len(rep['seen_ops'])} distinct ops seen, "
            f"{len(rep['seen_not_implemented'])} seen-but-unimplemented",
        ]
        if rep["seen_not_implemented"]:
            from .opcodes import op_name
            lines.append("  unimplemented ops encountered: " +
                         ", ".join(op_name(o) for o in rep["seen_not_implemented"]))
        if rep["builtins_missing"]:
            lines.append("  missing builtins: " +
                         ", ".join(f"{k}({v})" for k, v in rep["builtins_missing"].items()))
        return "\n".join(lines)

    @classmethod
    def reset_coverage(cls) -> None:
        cls.ops_seen = {}
        cls.ops_skipped = {}
        cls.builtins_called = {}
        cls.builtins_missing = {}
        cls._logged_once = set()

    @classmethod
    def _implemented_ops(cls) -> List[int]:
        return [op for op in Op if getattr(cls, f"_op_{op.name[3:].lower()}", None)]

    # ------------------------------------------------------------ core loop

    def _execute(self, start_idx: int, args: List[Any], coro_mode: bool):
        """Run one frame starting at instruction start_idx. args are the
        caller-supplied parameters bound by OP_FUNC_PARAMS_END."""
        frame = _Frame(start_idx, args)
        instrs = self.instructions
        n = len(instrs)
        dispatch = self._dispatch
        cls = type(self)

        while 0 <= frame.ip < n:
            if self._ops_used >= self.max_ops:
                self._log_once(("budget", self.name),
                               "GS2 %s: instruction budget exhausted (%d)",
                               self.name, self.max_ops)
                return None
            self._ops_used += 1

            instr = instrs[frame.ip]
            frame.ip += 1
            opnum = instr.opnum

            cls.ops_seen[opnum] = cls.ops_seen.get(opnum, 0) + 1

            handler = dispatch.get(opnum)
            if handler is None:
                cls.ops_skipped[opnum] = cls.ops_skipped.get(opnum, 0) + 1
                self._log_once(("op", opnum),
                               "GS2 %s: unimplemented opcode %d, skipping",
                               self.name, opnum)
                continue

            try:
                if opnum == Op.OP_SLEEP and coro_mode:
                    secs = gs2_to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
                    if secs > 0:
                        yield float(secs)
                        self._ops_used = 0
                    result = None
                elif opnum == Op.OP_CALL:
                    target = frame.stack.pop() if frame.stack else None
                    call_args = [self.deref(a, frame) for a in self._pop_args(frame)]
                    value = yield from self._gcall_target(
                        target, call_args, frame, coro_mode)
                    frame.stack.append(value)
                    result = None
                else:
                    result = handler(frame, instr)
            except _ReturnValue as rv:
                return rv.value
            except Exception as e:
                self._errors += 1
                self._log_once(("err", self.name, frame.ip - 1, type(e).__name__),
                               "GS2 %s: error at op#%d (%s): %s",
                               self.name, frame.ip - 1, Op(opnum).name if opnum in Op._value2member_map_ else opnum, e)
                if self._errors > self.max_errors:
                    return None
                continue
            if result is not None:  # jump request
                frame.ip = result

        return None

    # -------------------------------------------------------------- helpers

    def deref(self, v: Any, frame: "_Frame") -> Any:
        """Resolve VarRef/LValue to a concrete value."""
        if isinstance(v, VarRef):
            return self._lookup(v.name, frame)
        if isinstance(v, LValue):
            return v.get()
        return v

    def _lookup(self, name: str, frame: "_Frame") -> Any:
        key = name.lower()
        if frame.with_stack:
            for wobj in reversed(frame.with_stack):
                if isinstance(wobj, GS2Object) and wobj.has(key):
                    return wobj.get(key)
        if frame.temps.has(key):
            return frame.temps.get(key)
        if self.this.has(key):
            return self.this.get(key)
        g = self.globals
        if key in g:
            return g[key]
        if key == "params":
            # event-parameter array: the compiler emits a plain named var
            # (TYPE_VAR 'params'), not OP_PARAMS, so resolve it here; an
            # explicit script variable of the same name shadows it above
            return list(frame.args)
        if self.host is not None:
            obj = self.host.get_object(key)
            if obj is not None:
                return obj
        if "." in key:
            # A DOTTED name as one string: what OP_MAKEVAR pushes for
            # `makevar("temp.creds." @ temp.field)` (compiled as
            # OP_CONV_TO_STRING; OP_MAKEVAR -- gs2test-verified, no Call op),
            # resolved here at deref. Walk head through the normal chain
            # ("temp" is the frame scope), then members. Read-only: a dotted
            # WRITE stays unresolved, same as before.
            head, _, rest = key.partition(".")
            if head == "temp":
                node: Any = frame.temps
            elif head in ("this", "thiso"):
                node = self.this if head == "this" else self.thiso
            else:
                node = self._lookup(head, frame)   # head has no dot
            for part in rest.split("."):
                if not isinstance(node, GS2Object):
                    return None
                node = node.get(part)
            return node
        return None

    def _assign_name(self, name: str, value: Any, frame: "_Frame") -> None:
        key = name.lower()
        if frame.with_stack:
            for wobj in reversed(frame.with_stack):
                if isinstance(wobj, GS2Object) and wobj.has(key):
                    wobj.set(key, value)
                    return
        if frame.temps.has(key):
            frame.temps.set(key, value)
            return
        if self.this.has(key):
            # Mirror _lookup's resolution order (temps -> this -> globals):
            # a bare name the this-object claims must WRITE there too, or a
            # script's own read-after-write comes back with the stale
            # this-value. Host this-objects that bridge to engine state (an
            # NPC's x/y/nick etc.) rely on this so `y = 12.5;` moves the NPC
            # instead of landing in the VM-shared globals dict.
            self.this.set(key, value)
            return
        self.globals[key] = value

    def _write_ref(self, target: Any, value: Any, frame: "_Frame") -> None:
        value = _denull(value)
        if isinstance(target, LValue):
            target.set(value)
        elif isinstance(target, VarRef):
            self._assign_name(target.name, value, frame)
        # else: assignment into a computed value -- dropped (GS2Engine
        # mutates a dead entry here, same net effect)

    def _pop_args(self, frame: "_Frame") -> List[Any]:
        """Pop stack values down to (and including) the ARRAY_START marker.
        Args were pushed in reverse source order, so pop order == source
        order."""
        args: List[Any] = []
        stack = frame.stack
        while stack:
            v = stack.pop()
            if v is ARRAY_START:
                return [_denull(a) for a in args]
            args.append(v)
        return [_denull(a) for a in args]

    def _log_once(self, key: Tuple, msg: str, *fmt: Any) -> None:
        if key not in type(self)._logged_once:
            type(self)._logged_once.add(key)
            logger.warning(msg, *fmt)

    # ------------------------------------------------------------- dispatch

    def _build_dispatch(self) -> Dict[int, Callable]:
        table: Dict[int, Callable] = {}
        for op in Op:
            m = getattr(self, f"_op_{op.name[3:].lower()}", None)
            if m is not None:
                table[op.value] = m
        return table

    # --- control flow ---

    def _op_none(self, frame, instr):
        return None

    def _op_set_index(self, frame, instr):
        return int(instr.operand.value)

    def _op_set_index_true(self, frame, instr):
        v = self.deref(frame.stack.pop(), frame) if frame.stack else None
        if gs2_truthy(v):
            return int(instr.operand.value)
        return None

    def _op_if(self, frame, instr):
        v = self.deref(frame.stack.pop(), frame) if frame.stack else None
        if not gs2_truthy(v):
            return int(instr.operand.value)
        return None

    def _op_or(self, frame, instr):
        v = self.deref(frame.stack.pop(), frame) if frame.stack else None
        if gs2_truthy(v):
            frame.stack.append(True)
            return int(instr.operand.value)
        return None

    def _op_and(self, frame, instr):
        # symmetric with OP_OR (GS2Engine omits the push here, which would
        # underflow the OP_INLINE_CONDITIONAL that follows -- compiler layout
        # requires exactly one value at the merge point)
        v = self.deref(frame.stack.pop(), frame) if frame.stack else None
        if not gs2_truthy(v):
            frame.stack.append(False)
            return int(instr.operand.value)
        return None

    def _op_jmp(self, frame, instr):
        return None  # runtime no-op (GS2Engine ScriptMachine.cs)

    def _op_cmd_call(self, frame, instr):
        return None  # loop bookkeeping; our budget lives in _execute

    def _op_ret(self, frame, instr):
        v = self.deref(frame.stack.pop(), frame) if frame.stack else None
        raise _ReturnValue(v)

    def _op_sleep(self, frame, instr):
        secs = gs2_to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
        if self.host is not None:
            self.host.sleep(self, secs)
        return None

    def _op_waitfor(self, frame, instr):
        # waitfor(obj, event, timeout): sig "xssf" -- pop 3 args; not
        # supported client-side, push 0
        for _ in range(3):
            if frame.stack:
                frame.stack.pop()
        frame.stack.append(0.0)
        return None

    # --- literals / scope roots ---

    def _op_type_number(self, frame, instr):
        frame.stack.append(float(instr.operand.value))

    def _op_type_string(self, frame, instr):
        idx = instr.operand.value
        frame.stack.append(self.strings[idx] if 0 <= idx < len(self.strings) else "")

    def _op_type_var(self, frame, instr):
        idx = instr.operand.value
        frame.stack.append(VarRef(self.strings[idx] if 0 <= idx < len(self.strings) else ""))

    def _op_type_array(self, frame, instr):
        frame.stack.append(ARRAY_START)

    def _op_type_true(self, frame, instr):
        frame.stack.append(True)

    def _op_type_false(self, frame, instr):
        frame.stack.append(False)

    def _op_type_null(self, frame, instr):
        # The `null` keyword is an OBJECT entry with a nullptr pointer, not an
        # unset variable (TScriptMachine.cpp:2605-2609) -- see values._GS2Null.
        frame.stack.append(GS2_NULL)

    def _op_pi(self, frame, instr):
        frame.stack.append(math.pi)

    def _op_this(self, frame, instr):
        frame.stack.append(self._current_this(frame))

    def _current_this(self, frame: "_Frame") -> Any:
        """Official semantics (TScriptMachine.cpp:3589 for OP_WITH and :3615
        for OP_WITHEND): inside `with (obj)` blocks -- which includes
        inline-new construction blocks -- OP_THIS yields the innermost
        with-target, so `this.field = x` inside `new Ctrl() {...}` lands on
        the object under construction. OP_THISO is untouched by with
        (machine slot 0x70 vs this at 0x78)."""
        if frame.with_stack:
            player = self.host.get_object("player") if self.host is not None else None
            for wobj in reversed(frame.with_stack):
                if wobj is not player:
                    return wobj
        return self.this

    def _op_thiso(self, frame, instr):
        frame.stack.append(self.thiso)

    def _op_player(self, frame, instr):
        frame.stack.append(self.host.get_object("player") if self.host else None)

    def _op_playero(self, frame, instr):
        frame.stack.append(self.host.get_object("player") if self.host else None)

    def _op_level(self, frame, instr):
        frame.stack.append(self.host.get_object("level") if self.host else None)

    def _op_temp(self, frame, instr):
        frame.stack.append(frame.temps)

    def _op_params(self, frame, instr):
        frame.stack.append(list(frame.args))

    # --- stack shuffling ---

    def _op_copy_last_op(self, frame, instr):
        if frame.stack:
            frame.stack.append(frame.stack[-1])

    def _op_swap_last_ops(self, frame, instr):
        if len(frame.stack) > 1:
            frame.stack[-1], frame.stack[-2] = frame.stack[-2], frame.stack[-1]

    def _op_index_dec(self, frame, instr):
        if frame.stack:
            frame.stack.pop()

    # --- expression-cache registers (official-compiler-only ops 45-47; see
    #     opcodes.py Op enum comment for the reversed-interpreter evidence).
    #     Emitted as `expr; OP_CONV_TO_PROPERTY; OP_REG_STORE n;
    #     OP_INDEX_DEC` to cache, and `OP_REG_LOAD n` to recall -- notably
    #     for foreach loop variables/collections and repeated params. ---

    def _op_conv_to_property(self, frame, instr):
        # official: TScriptStackEntry::switchTypeProperty(machine, true) on
        # the stack top, in place. Our VarRef/LValue entries already ARE
        # property references, so only a bare name string needs converting.
        if frame.stack and isinstance(frame.stack[-1], str):
            frame.stack[-1] = VarRef(frame.stack[-1])
        return None

    def _op_reg_store(self, frame, instr):
        # store the stack top (usually a reference -- the preceding
        # OP_CONV_TO_PROPERTY guarantees it) WITHOUT popping; the compiler
        # emits OP_INDEX_DEC right after to drop it.
        n = int(instr.operand.value) if instr.operand else 0
        if frame.stack and 0 <= n <= MAX_REGISTER_INDEX:
            frame.registers[n] = frame.stack[-1]
        return None

    def _op_reg_load(self, frame, instr):
        n = int(instr.operand.value) if instr.operand else 0
        frame.stack.append(frame.registers.get(n))
        return None

    # --- conversions / member access ---

    def _op_conv_to_float(self, frame, instr):
        v = self.deref(frame.stack.pop(), frame) if frame.stack else None
        # Arrays/objects pass through unchanged -- a DELIBERATE divergence
        # from switchTypeFloat (which reads an object var's float slot,
        # 0.0 -- values.gs2_to_num models that): gs2parser's sig-driven
        # conversion emits this op in front of arraylen()'s OP_OBJ_SIZE,
        # and that bytecode (which pygserver serves us) only works if the
        # array survives. Everything scalar takes the faithful path, so
        # `if ("word")` still sees strtofloat's -1.0.
        if isinstance(v, (list, GS2Object)):
            frame.stack.append(v)
        else:
            frame.stack.append(gs2_to_num(v))

    def _op_conv_to_string(self, frame, instr):
        v = self.deref(frame.stack.pop(), frame) if frame.stack else None
        frame.stack.append(to_str(v))

    def _op_conv_to_object(self, frame, instr):
        raw = frame.stack.pop() if frame.stack else None
        if isinstance(raw, (VarRef, str)):
            name = raw.name if isinstance(raw, VarRef) else raw
            # with-scope member first (GS2Engine), then normal chain
            v = self._lookup(name, frame)
            if v is None and self.host is not None:
                v = self.host.get_object(name)
            if isinstance(v, (list, GS2Object)):
                frame.stack.append(v)
                return
            # CSV-shaped string property -> temp token array (official
            # switchTypeObject; live serverlist rows depend on this).
            # Applies only to resolved property values (VarRef), not to
            # computed string entries -- official checks the property slot.
            if isinstance(raw, VarRef) and isinstance(v, str):
                toks = _csv_tokens(v)
                if toks is not None:
                    frame.stack.append(toks)
                    return
            frame.stack.append(raw)
        elif isinstance(raw, LValue):
            value = raw.get()
            if isinstance(value, (list, GS2Object)):
                frame.stack.append(value)
                return
            if isinstance(value, str):
                toks = _csv_tokens(value)
                if toks is not None:
                    frame.stack.append(toks)
                    return
            frame.stack.append(raw)
        else:
            frame.stack.append(raw)

    def _is_script_fn_slot(self, base: GS2Object, name: str,
                           frame: "_Frame") -> bool:
        """Should `base.name` fall back to the script's own function table?

        Only for reads off the CURRENT this-object (plain `this`, thiso, or
        the with-rebound this OP_THIS would push right now) whose member is
        unset -- that is precisely the shape the compiler emits for an
        anonymous `function(){...}`. Restricting it to this-objects keeps a
        miss on some unrelated host object (`player.onwall`) reading None as
        before. has() is only the cheap gate; _ScriptFnRef.get() re-checks
        the real stored value, so a host object with a computed member it
        does not report through has() still wins.
        """
        if base.has(name) or not self.has_function(name):
            return False
        if base is self.this or base is self.thiso:
            return True
        return bool(frame.with_stack) and base is self._current_this(frame)

    def _op_member_access(self, frame, instr):
        namev = frame.stack.pop() if frame.stack else None
        base_entry = frame.stack.pop() if frame.stack else None
        name = namev.name if isinstance(namev, VarRef) else to_str(self.deref(namev, frame))
        base = (self.deref(base_entry, frame)
                if isinstance(base_entry, (VarRef, LValue)) else base_entry)
        if isinstance(base, GS2Object) and self._is_script_fn_slot(base, name, frame):
            frame.stack.append(_ScriptFnRef(self, base, name))
        elif isinstance(base, (GS2Object, list)):
            # lists ride along so array method calls can dispatch (LValue
            # reads/writes on a list base are still dead -- see values.py)
            frame.stack.append(LValue(base, name))
        elif base is None and isinstance(base_entry, VarRef):
            # Member access through a bare name holding no object yet
            # (`tmp.node = x` with no prior `tmp`): GS2Engine's variable
            # collection auto-creates the holder object on member WRITE
            # (live Login -Rescripted/Serverlist stores its serverlist tree
            # nodes under exactly this shape), while a plain READ stays None
            # and creates nothing -- see _NameVivifyRef.
            frame.stack.append(_NameVivifyRef(self, frame, base_entry.name, name))
        elif isinstance(base_entry, str):
            # A string LITERAL that survived OP_CONV_TO_OBJECT unresolved
            # rides along as the base, so OP_CALL can hand it to the host --
            # `"-Serverlist_Options".showOptions()` is a universe-name
            # method call whose object may need installing first (the host's
            # weapon-fetch stand-in). Reads/writes on it stay dead
            # (LValue.get/set gate on GS2Object). A string held in a
            # PROPERTY stays a dead reference: the official conversion only
            # name-resolves literals -- the property case reads the slot's
            # object and falls back to CSV-tokens-or-null
            # (TScriptStackEntry::switchTypeObject/makeProperty,
            # TScriptStackEntry.cpp:299-353).
            frame.stack.append(LValue(base_entry, name))
        else:
            frame.stack.append(LValue(None, name))

    # --- objects / arrays ---

    def _op_array_end(self, frame, instr):
        vals = [self.deref(v, frame) for v in self._pop_args(frame)]
        frame.stack.append(vals)

    def _op_array_new(self, frame, instr):
        size = array_size(self.deref(frame.stack.pop(), frame)) if frame.stack else 0
        frame.stack.append([0.0] * size)

    def _op_array_new_multidim(self, frame, instr):
        size = array_size(self.deref(frame.stack.pop(), frame)) if frame.stack else 0
        arr = self.deref(frame.stack[-1], frame) if frame.stack else None
        if isinstance(arr, list):
            for i, v in enumerate(arr):
                if isinstance(v, list):
                    self._op_array_new_multidim_inner(v, size)
                else:
                    arr[i] = [0.0] * size
        return None

    @staticmethod
    def _op_array_new_multidim_inner(arr: list, size: int) -> None:
        for i, v in enumerate(arr):
            if isinstance(v, list):
                GS2VM._op_array_new_multidim_inner(v, size)
            else:
                arr[i] = [0.0] * size

    def _op_setarray(self, frame, instr):
        size = array_size(self.deref(frame.stack.pop(), frame)) if frame.stack else 0
        target = frame.stack.pop() if frame.stack else None
        cur = self.deref(target, frame)
        arr = list(cur) if isinstance(cur, list) else []
        if len(arr) < size:
            arr.extend([0.0] * (size - len(arr)))
        else:
            arr = arr[:size]
        self._write_ref(target, arr, frame)

    def _op_inline_new(self, frame, instr):
        # marker between the ctor arg and the classname; identity for us
        return None

    def _op_makevar(self, frame, instr):
        name = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else ""
        frame.stack.append(VarRef(name))

    def _op_new_object(self, frame, instr):
        classname = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else ""
        arg = self.deref(frame.stack.pop(), frame) if frame.stack else None
        if self.host is not None:
            frame.stack.append(self.host.create_object(classname, arg))
        else:
            frame.stack.append(GS2Object(name=classname))

    def _op_inline_conditional(self, frame, instr):
        v = self.deref(frame.stack.pop(), frame) if frame.stack else None
        frame.stack.append(1.0 if gs2_truthy(v) else 0.0)

    # --- assignment / params ---

    def _op_assign(self, frame, instr):
        value = self.deref(frame.stack.pop(), frame) if frame.stack else None
        target = frame.stack.pop() if frame.stack else None
        self._write_ref(target, value, frame)

    def _op_func_params_end(self, frame, instr):
        # param names were pushed in reverse declaration order, so pop order
        # is declaration order; bind against caller args positionally.
        # gs2test pushes bare VarRefs, but the official compiler pushes each
        # param as a temp.<name> reference (OP_TEMP; OP_TYPE_VAR;
        # OP_MEMBER_ACCESS -> LValue on the frame temps object) -- seen in
        # every live Login-server weapon. Bind through the reference in that
        # case or all params silently become None.
        names = self._pop_args(frame)
        for i, nv in enumerate(names):
            value = frame.args[i] if i < len(frame.args) else None
            if isinstance(nv, LValue) and nv.obj is not None:
                nv.set(value)
                continue
            name = nv.name if isinstance(nv, VarRef) else to_str(nv)
            frame.temps.set(name, value)
        return None

    def _op_inc(self, frame, instr):
        target = frame.stack.pop() if frame.stack else None
        n = gs2_to_num(self.deref(target, frame)) + 1
        if isinstance(target, (VarRef, LValue)):
            self._write_ref(target, n, frame)
            frame.stack.append(target)
        else:
            # plain value on the stack (e.g. the foreach loop index)
            frame.stack.append(n)

    def _op_dec(self, frame, instr):
        target = frame.stack.pop() if frame.stack else None
        n = gs2_to_num(self.deref(target, frame)) - 1
        if isinstance(target, (VarRef, LValue)):
            self._write_ref(target, n, frame)
            frame.stack.append(target)
        else:
            frame.stack.append(n)

    # --- arithmetic / comparison / logic (operands already converted by
    #     compiler-emitted OP_CONV_TO_FLOAT where applicable) ---

    def _pop2num(self, frame) -> Tuple[float, float]:
        b = gs2_to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
        a = gs2_to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
        return a, b

    def _op_add(self, frame, instr):
        a, b = self._pop2num(frame)
        frame.stack.append(a + b)

    def _op_sub(self, frame, instr):
        a, b = self._pop2num(frame)
        frame.stack.append(a - b)

    def _op_mul(self, frame, instr):
        a, b = self._pop2num(frame)
        frame.stack.append(a * b)

    def _op_div(self, frame, instr):
        a, b = self._pop2num(frame)
        frame.stack.append(a / b if b != 0 else 0.0)

    def _op_mod(self, frame, instr):
        # FLOORED modulo (`left - right * floor(left / right)`), not C fmod:
        # src/TScriptMachine.cpp:3091 (and the same expression in the fused
        # immediate helper, :2195). -7 % 3 is 2 on the real client, not -1.
        a, b = self._pop2num(frame)
        frame.stack.append(a - b * math.floor(a / b) if b != 0 else 0.0)

    def _op_pow(self, frame, instr):
        a, b = self._pop2num(frame)
        try:
            frame.stack.append(float(a ** b))
        except (ValueError, OverflowError, ZeroDivisionError):
            frame.stack.append(0.0)

    # 66/67: non-short-circuiting logical AND/OR (TScriptMachine.cpp:3127-3151); see opcodes.py.

    def _op_unknown_66(self, frame, instr):
        a, b = self._pop2num(frame)
        frame.stack.append(1.0 if (a != 0.0 and b != 0.0) else 0.0)

    def _op_unknown_67(self, frame, instr):
        a, b = self._pop2num(frame)
        frame.stack.append(1.0 if (a != 0.0 or b != 0.0) else 0.0)

    def _op_dynamic_add(self, frame, instr):
        # Runtime-typed +: concat if either side is a string (TScriptMachine.cpp:3452-3475).
        a, b = self._pop2(frame)
        if isinstance(a, str) or isinstance(b, str):
            frame.stack.append(to_str(a) + to_str(b))
        else:
            frame.stack.append(gs2_to_num(a) + gs2_to_num(b))

    def _op_not(self, frame, instr):
        v = self.deref(frame.stack.pop(), frame) if frame.stack else None
        frame.stack.append(not gs2_truthy(v))

    def _op_unarysub(self, frame, instr):
        v = self.deref(frame.stack.pop(), frame) if frame.stack else None
        frame.stack.append(-gs2_to_num(v))

    # OP_EQ..OP_GTE all funnel through the one 3-way TScriptMachine::compare()
    # (src/TScriptMachine.cpp:3164-3190), so they inherit its string
    # comparison and 1e-4 numeric tolerance -- see values.gs2_compare.

    def _pop2(self, frame) -> Tuple[Any, Any]:
        b = self.deref(frame.stack.pop(), frame) if frame.stack else None
        a = self.deref(frame.stack.pop(), frame) if frame.stack else None
        return a, b

    def _op_eq(self, frame, instr):
        a, b = self._pop2(frame)
        frame.stack.append(gs2_compare(a, b) == 0)

    def _op_neq(self, frame, instr):
        a, b = self._pop2(frame)
        frame.stack.append(gs2_compare(a, b) != 0)

    def _op_lt(self, frame, instr):
        a, b = self._pop2(frame)
        frame.stack.append(gs2_compare(a, b) < 0)

    def _op_gt(self, frame, instr):
        a, b = self._pop2(frame)
        frame.stack.append(gs2_compare(a, b) > 0)

    def _op_lte(self, frame, instr):
        a, b = self._pop2(frame)
        frame.stack.append(gs2_compare(a, b) <= 0)

    def _op_gte(self, frame, instr):
        a, b = self._pop2(frame)
        frame.stack.append(gs2_compare(a, b) >= 0)

    def _op_obj_compare(self, frame, instr):
        # 3-way compare pushed as a number (src/TScriptMachine.cpp:3352-3358).
        # Never emitted by the open-source compiler; the official one does.
        a, b = self._pop2(frame)
        frame.stack.append(float(gs2_compare(a, b)))

    # Bitwise ops convert with a C int32 cast, so they wrap/saturate where
    # Python's unbounded ints would not (src/TScriptMachine.cpp:3098-3111).

    def _op_bwo(self, frame, instr):
        a, b = self._pop2(frame)
        frame.stack.append(float(to_int32(a) | to_int32(b)))

    def _op_bwa(self, frame, instr):
        a, b = self._pop2(frame)
        frame.stack.append(float(to_int32(a) & to_int32(b)))

    def _op_bwx(self, frame, instr):
        a, b = self._pop2(frame)
        frame.stack.append(float(to_int32(a) ^ to_int32(b)))

    def _op_bwi(self, frame, instr):
        # NB: OP_BWI is the one bitwise op that rounds with the index epsilon
        # instead of truncating (`~floorScriptIndex(v)`, :3121-3124).
        v = self.deref(frame.stack.pop(), frame) if frame.stack else 0.0
        frame.stack.append(float(~array_index(v)))

    def _op_bw_leftshift(self, frame, instr):
        # shift count masked to 5 bits and the result is an int32 (:3108-3111)
        a, b = self._pop2(frame)
        frame.stack.append(float(wrap_int32(to_int32(a) << (to_int32(b) & 31))))

    def _op_bw_rightshift(self, frame, instr):
        a, b = self._pop2(frame)
        frame.stack.append(float(to_int32(a) >> (to_int32(b) & 31)))

    def _op_in_range(self, frame, instr):
        # `v in |lo,hi|`. The opcode carries a *mode* operand selecting which
        # ends are inclusive (ValueInRange, src/unsorted.cpp:1359-1376); the
        # open-source compiler only ever emits mode 0 (both ends inclusive,
        # each with the 1e-4 tolerance), and leaves the operand off entirely.
        # An array-valued left operand tests EVERY cell
        # (TScriptMachine::inRange, src/TScriptMachine.cpp:92-135).
        hi = gs2_to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
        lo = gs2_to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
        v = self.deref(frame.stack.pop(), frame) if frame.stack else 0.0
        mode = int(instr.operand.value) if instr is not None and instr.operand else 0
        if isinstance(v, list) and v:
            ok = all(_value_in_range(gs2_to_num(x), lo, hi, mode) for x in v)
        else:
            # empty array / scalar: converted with switchTypeFloat first
            ok = _value_in_range(gs2_to_num(v), lo, hi, mode)
        frame.stack.append(ok)

    def _op_in_obj(self, frame, instr):
        obj = self.deref(frame.stack.pop(), frame) if frame.stack else None
        v = self.deref(frame.stack.pop(), frame) if frame.stack else None
        if isinstance(obj, list):
            frame.stack.append(any(gs2_eq(v, x) for x in obj))
        elif isinstance(obj, str):
            frame.stack.append(to_str(v) in obj)
        elif isinstance(obj, GS2Object):
            frame.stack.append(obj.has(to_str(v)))
        else:
            frame.stack.append(False)

    # --- math builtin opcodes ---

    def _op_int(self, frame, instr):
        # int() is floorScriptIndex(), i.e. floor(v + 0.0001) -- NOT
        # truncation (src/TScriptMachine.cpp:3221-3225 with the helper at :60-67).
        # int(-2.5) is -3 on the real client, and int(2.99999) is 3.
        v = self.deref(frame.stack.pop(), frame) if frame.stack else 0.0
        frame.stack.append(float(array_index(v)))

    def _op_abs(self, frame, instr):
        v = gs2_to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
        frame.stack.append(abs(v))

    def _op_random(self, frame, instr):
        a, b = self._pop2num(frame)
        lo, hi = min(a, b), max(a, b)
        frame.stack.append(lo + _random.random() * (hi - lo))

    # GS2Engine's 1e-6 snap-to-zero is its own invention; the official machine has none.

    def _op_sin(self, frame, instr):
        v = gs2_to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
        frame.stack.append(math.sin(v))

    def _op_cos(self, frame, instr):
        v = gs2_to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
        frame.stack.append(math.cos(v))

    def _op_arctan(self, frame, instr):
        v = gs2_to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
        frame.stack.append(math.atan(v))

    def _op_exp(self, frame, instr):
        v = gs2_to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
        try:
            frame.stack.append(math.exp(v))
        except OverflowError:
            frame.stack.append(0.0)

    def _op_log(self, frame, instr):
        # log(base, x)
        base, x = self._pop2num(frame)
        try:
            frame.stack.append(math.log(x, base) if x > 0 and base > 0 and base != 1 else 0.0)
        except (ValueError, ZeroDivisionError):
            frame.stack.append(0.0)

    # min/max go through compare() too and keep the winning operand's TYPE
    # (src/TScriptMachine.cpp:3286-3295), so min("b","a") is the string "a".

    def _op_min(self, frame, instr):
        a, b = self._pop2(frame)
        frame.stack.append(b if gs2_compare(a, b) > 0 else a)

    def _op_max(self, frame, instr):
        a, b = self._pop2(frame)
        frame.stack.append(b if gs2_compare(a, b) < 0 else a)

    def _op_getangle(self, frame, instr):
        # src/unsorted.cpp:1298-1309. atan2 agrees everywhere except x == 0,
        # where the official code hardcodes 3*pi/2 for y > 0 and pi/2
        # otherwise (so getangle(0,0) is pi/2, not 0).
        dx, dy = self._pop2num(frame)
        if dx == 0.0:
            frame.stack.append(4.7123889803846899 if dy > 0.0 else math.pi / 2)
            return
        frame.stack.append(math.atan2(-dy, dx) % (2 * math.pi))

    def _op_getdir(self, frame, instr):
        # TScriptMachine::getDir (src/TScriptMachine.cpp:1278-1304): dominant
        # axis wins, and a TIE goes to the vertical axis (getdir(1,1) is 2,
        # not 3). Magnitudes are compared as floats, not doubles.
        dx, dy = self._pop2num(frame)
        if abs(_f32(dx)) > abs(_f32(dy)):
            frame.stack.append(3.0 if dx >= 0.0 else 1.0)
        else:
            frame.stack.append(2.0 if dy >= 0.0 else 0.0)

    # vecx/vecy index TInput::movevec[dir*2 (+1)] after floorScriptIndex, and
    # return 0 for dir > 3 -- there is NO wrap-around (src/TScriptMachine.cpp:
    # 3313-3326), so vecx(5) is 0, not vecx(1).

    def _op_vecx(self, frame, instr):
        d = array_index(self.deref(frame.stack.pop(), frame)) if frame.stack else 0
        frame.stack.append(0.0 if d > 3 else {1: -1.0, 3: 1.0}.get(d, 0.0))

    def _op_vecy(self, frame, instr):
        d = array_index(self.deref(frame.stack.pop(), frame)) if frame.stack else 0
        frame.stack.append(0.0 if d > 3 else {0: -1.0, 2: 1.0}.get(d, 0.0))

    def _op_char(self, frame, instr):
        v = gs2_to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
        try:
            frame.stack.append(chr(int(v)))
        except (ValueError, OverflowError):
            frame.stack.append("")

    def _op_format(self, frame, instr):
        fmt = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else ""
        args = [self.deref(a, frame) for a in self._pop_args(frame)]
        frame.stack.append(printf_format(fmt, args))

    def _op_translate(self, frame, instr):
        v = self.deref(frame.stack.pop(), frame) if frame.stack else None
        frame.stack.append(to_str(v))

    # --- string ops (compiler layout: object pushed first for OBJECT_FIRST
    #     builtins, so the argument(s) are on top) ---

    def _op_obj_trim(self, frame, instr):
        s = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else ""
        frame.stack.append(s.strip())

    def _op_obj_length(self, frame, instr):
        s = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else ""
        frame.stack.append(float(len(s)))

    def _op_obj_pos(self, frame, instr):
        sub = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else ""
        s = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else ""
        frame.stack.append(float(s.find(sub)))

    def _op_join(self, frame, instr):
        b = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else ""
        a = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else ""
        frame.stack.append(a + b)

    def _op_obj_charat(self, frame, instr):
        idx = array_index(self.deref(frame.stack.pop(), frame)) if frame.stack else 0
        s = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else ""
        frame.stack.append(s[idx] if 0 <= idx < len(s) else "")

    def _op_obj_substr(self, frame, instr):
        # subString converts BOTH bounds with the index epsilon
        # and clamps start at 0 (TScriptMachine::subString,
        # src/TScriptMachine.cpp:1194-1241).
        length = array_index(self.deref(frame.stack.pop(), frame)) if frame.stack else 0
        start = array_index(self.deref(frame.stack.pop(), frame)) if frame.stack else 0
        s = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else ""
        start = max(0, start)
        frame.stack.append(s[start:] if length < 0 else s[start:start + length])

    def _op_obj_starts(self, frame, instr):
        # CASE-INSENSITIVE: the reference VM's OP_OBJ_STARTS handler calls
        # TString::startsIgnoreCase (TScriptMachine.cpp:3416-3428;
        # TString.cpp:961) -- Login's isLoginServer() relies on
        # "Login".starts("login") being true. startsIgnoreCase compares with
        # TString::strncasecmp, so it is values.casefold's ASCII-only folding.
        prefix = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else ""
        s = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else ""
        frame.stack.append(casefold(s).startswith(casefold(prefix)))

    def _op_obj_ends(self, frame, instr):
        # CASE-INSENSITIVE like OP_OBJ_STARTS (TString::endsIgnoreCase in
        # the same reference dispatch, same strncasecmp folding).
        suffix = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else ""
        s = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else ""
        frame.stack.append(casefold(s).endswith(casefold(suffix)))

    def _op_obj_tokenize(self, frame, instr):
        delims = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else " ,"
        s = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else ""
        if not delims:
            delims = " ,"
        toks = re.split("[" + re.escape(delims) + "]+", s)
        frame.stack.append([t for t in toks if t != ""])

    def _op_obj_positions(self, frame, instr):
        sub = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else ""
        s = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else ""
        out: List[float] = []
        if sub:
            i = s.find(sub)
            while i != -1:
                out.append(float(i))
                i = s.find(sub, i + 1)
        frame.stack.append(out)

    # --- array/object ops ---

    def _op_obj_size(self, frame, instr):
        v = self.deref(frame.stack.pop(), frame) if frame.stack else None
        if isinstance(v, list):
            frame.stack.append(float(len(v)))
        elif isinstance(v, GS2Object):
            frame.stack.append(float(len(v)))
        else:
            frame.stack.append(0.0)

    def _op_array(self, frame, instr):
        idx = self.deref(frame.stack.pop(), frame) if frame.stack else 0
        arr = self.deref(frame.stack.pop(), frame) if frame.stack else None
        if isinstance(arr, list):
            i = array_index(idx)
            # Push a REFERENCE, not a value copy: GS2Engine's array access
            # yields a variable slot, so `this.data[1]++` / `arr[i] += x`
            # must write back into the list. ElemRef subclasses LValue, so
            # every consumer that already derefs LValue sees the value as
            # before; only the write-path ops behave differently (correctly).
            frame.stack.append(ElemRef(arr, i) if 0 <= i < len(arr) else None)
        elif isinstance(arr, GS2Object):
            frame.stack.append(arr.get(to_str(idx)))
        elif isinstance(arr, str):
            # Official getArrayCell: a string that survived OP_CONV_TO_OBJECT
            # unconverted (no comma, not fully quoted) is a null-object entry
            # and indexing it pushes 0.0 -- NOT a character (charAt is a
            # separate op). CSV-shaped strings never reach here: the
            # preceding OP_CONV_TO_OBJECT already tokenized them.
            frame.stack.append(0.0)
        else:
            frame.stack.append(None)

    def _op_array_assign(self, frame, instr):
        value = self.deref(frame.stack.pop(), frame) if frame.stack else None
        idx = self.deref(frame.stack.pop(), frame) if frame.stack else 0
        target = frame.stack.pop() if frame.stack else None
        arr = self.deref(target, frame)
        if isinstance(arr, list):
            i = array_index(idx)
            if 0 <= i <= MAX_ARRAY_INDEX:
                if i >= len(arr):
                    arr.extend([0.0] * (i + 1 - len(arr)))
                arr[i] = value
        elif isinstance(arr, GS2Object):
            arr.set(to_str(idx), value)
        elif arr is None and isinstance(target, (LValue, VarRef)):
            # auto-vivify: this.arr[0] = x on an unset member
            new = []
            i = array_index(idx)
            if 0 <= i <= MAX_ARRAY_INDEX:
                new.extend([0.0] * (i + 1))
                new[i] = value
            self._write_ref(target, new, frame)

    def _op_array_multidim(self, frame, instr):
        # `a[i, j]` read. STRICTLY two-dimensional, and deliberately so: the
        # compiler emits ONE OP_ARRAY_MULTIDIM for an index list of any
        # length > 1 (GS2CompilerVisitor.cpp:663-669 with ast.h:277), but the
        # official machine's handler getArrayCell2 pops exactly two indices
        # and one base -- TScriptMachine::getArrayCell2,
        # src/TScriptMachine.cpp:1930-1971. For `a[i, j, k]` that makes `i`
        # the base (so the read yields nothing) and leaves `a` on the stack --
        # the real client mis-executes a 3-index read the same way, so matching
        # it beats fixing it.
        j = array_index(self.deref(frame.stack.pop(), frame)) if frame.stack else 0
        i = array_index(self.deref(frame.stack.pop(), frame)) if frame.stack else 0
        arr = self.deref(frame.stack.pop(), frame) if frame.stack else None
        row = arr[i] if isinstance(arr, list) and 0 <= i < len(arr) else None
        frame.stack.append(row[j] if isinstance(row, list) and 0 <= j < len(row) else None)

    def _op_array_multidim_assign(self, frame, instr):
        # `a[i, j] = v`; stack [obj, i, j, value] -- setArrayCell2 skips the
        # value entry, converts the next two as indices and takes the fourth
        # as the base (src/TScriptMachine.cpp:2040-2115). Two dimensions
        # only, like the reader.
        value = self.deref(frame.stack.pop(), frame) if frame.stack else None
        j = array_index(self.deref(frame.stack.pop(), frame)) if frame.stack else 0
        i = array_index(self.deref(frame.stack.pop(), frame)) if frame.stack else 0
        arr = self.deref(frame.stack.pop(), frame) if frame.stack else None
        if isinstance(arr, list) and 0 <= i < len(arr) and isinstance(arr[i], list):
            row = arr[i]
            if j >= len(row):
                row.extend([0.0] * (j + 1 - len(row)))
            if j >= 0:
                row[j] = value

    def _op_obj_subarray(self, frame, instr):
        # obj.subarray(start, length): default flags (no OBJECT_FIRST) put
        # the object on top: stack is [length, start, obj]. Both bounds go
        # through floorScriptIndex like every other index -- subArray() at
        # src/TScriptMachine.cpp:1844 (length) and :1848 (start).
        arr = self.deref(frame.stack.pop(), frame) if frame.stack else None
        start = array_index(self.deref(frame.stack.pop(), frame)) if frame.stack else 0
        length = array_index(self.deref(frame.stack.pop(), frame)) if frame.stack else -1
        if isinstance(arr, list):
            start = max(0, start)
            frame.stack.append(arr[start:] if length < 0 else arr[start:start + length])
        else:
            frame.stack.append([])

    def _op_obj_addstring(self, frame, instr):
        value = self.deref(frame.stack.pop(), frame) if frame.stack else None
        target = frame.stack.pop() if frame.stack else None
        arr = self.deref(target, frame)
        if isinstance(arr, list):
            arr.append(value)
        elif arr is None and isinstance(target, (LValue, VarRef)):
            self._write_ref(target, [value], frame)

    def _op_obj_deletestring(self, frame, instr):
        idx = array_index(self.deref(frame.stack.pop(), frame)) if frame.stack else 0
        arr = self.deref(frame.stack.pop(), frame) if frame.stack else None
        if isinstance(arr, list) and 0 <= idx < len(arr):
            del arr[idx]

    def _op_obj_removestring(self, frame, instr):
        value = self.deref(frame.stack.pop(), frame) if frame.stack else None
        arr = self.deref(frame.stack.pop(), frame) if frame.stack else None
        if isinstance(arr, list):
            for i, x in enumerate(arr):
                if gs2_eq(x, value):
                    del arr[i]
                    break

    def _op_obj_replacestring(self, frame, instr):
        # obj.replace(index, value) with CMD_REVERSE_ARGS: stack [obj, value, index]
        idx = array_index(self.deref(frame.stack.pop(), frame)) if frame.stack else 0
        value = self.deref(frame.stack.pop(), frame) if frame.stack else None
        arr = self.deref(frame.stack.pop(), frame) if frame.stack else None
        if isinstance(arr, list) and 0 <= idx <= MAX_ARRAY_INDEX:
            if idx >= len(arr):
                arr.extend([0.0] * (idx + 1 - len(arr)))
            arr[idx] = value

    def _op_obj_insertstring(self, frame, instr):
        # obj.insert(index, value) with CMD_REVERSE_ARGS: stack [obj, value, index]
        idx = array_index(self.deref(frame.stack.pop(), frame)) if frame.stack else 0
        value = self.deref(frame.stack.pop(), frame) if frame.stack else None
        arr = self.deref(frame.stack.pop(), frame) if frame.stack else None
        if isinstance(arr, list) and idx >= 0:
            arr.insert(idx, value)

    def _op_obj_clear(self, frame, instr):
        v = self.deref(frame.stack.pop(), frame) if frame.stack else None
        if isinstance(v, list):
            v.clear()
        elif isinstance(v, GS2Object):
            v.clear()

    def _op_obj_index(self, frame, instr):
        # obj.index(value): OBJECT_FIRST -> stack [obj, value]
        value = self.deref(frame.stack.pop(), frame) if frame.stack else None
        arr = self.deref(frame.stack.pop(), frame) if frame.stack else None
        if isinstance(arr, list):
            for i, x in enumerate(arr):
                if gs2_eq(x, value):
                    frame.stack.append(float(i))
                    return None
        frame.stack.append(-1.0)

    def _op_obj_indices(self, frame, instr):
        v = self.deref(frame.stack.pop(), frame) if frame.stack else None
        if isinstance(v, GS2Object):
            frame.stack.append(list(v.keys()))
        elif isinstance(v, list):
            frame.stack.append([float(i) for i in range(len(v))])
        else:
            frame.stack.append([])

    def _op_obj_link(self, frame, instr):
        # `x.link()` (GS2BuiltInFunctions.cpp:172-175) swaps the entry's
        # object for a link-var ALIASING it rather than a copy
        # (TScriptEnvironment::makeLinkVar -> linkValueTo,
        # src/TScriptEnvironment.cpp:175-193); Python object values are
        # already references, so the link is the identity, and a receiver
        # with no object links nullptr and reads back as null.
        # The decompiled C++ then retypes the entry to the arg-list start
        # marker (`type = Array`, src/TScriptMachine.cpp:3339), which would
        # truncate the enclosing call's arguments; its own asm stores the
        # object type (`movl $0x3,(%rax)`, asm/TScriptMachine/
        # _ZN14TScriptMachine13executeScriptEv.s_decomped:3235) and wins --
        # -ScriptedRC_GuiEditor passes `script.link()` as a non-final arg.
        v = self.deref(frame.stack.pop(), frame) if frame.stack else None
        frame.stack.append(v if isinstance(v, (GS2Object, list)) else GS2_NULL)

    def _op_obj_type(self, frame, instr):
        # `x.type()` is NOT the 0/1/2/3 type tag opcodes.h guesses at. The
        # official handler pushes 3.0 when the (already OP_CONV_TO_OBJECT'd)
        # entry holds an object with array cells and 0.0 for everything else
        # -- numbers, plain strings and member-only objects all read 0
        # (src/TScriptMachine.cpp:3207-3213, constant DOUBLE_00402540 = 3.0
        # from src/TInitStatics.cpp:1249). A CSV-shaped string still reads 3
        # because OP_CONV_TO_OBJECT tokenized it into an array first.
        v = self.deref(frame.stack.pop(), frame) if frame.stack else None
        frame.stack.append(3.0 if isinstance(v, list) and v else 0.0)

    # --- with / foreach ---

    def _op_with(self, frame, instr):
        obj = self.deref(frame.stack.pop(), frame) if frame.stack else None
        if isinstance(obj, GS2Object):
            frame.with_stack.append(obj)
            return None
        # invalid target: skip the block (operand = op index after WITHEND)
        return int(instr.operand.value)

    def _op_withend(self, frame, instr):
        if frame.with_stack:
            frame.with_stack.pop()
        return None

    def _op_foreach(self, frame, instr):
        # stack: [varref, obj, index]
        if len(frame.stack) < 3:
            return int(instr.operand.value)
        idx_entry = frame.stack.pop()
        obj_entry = frame.stack.pop()
        var_entry = frame.stack.pop()
        idx = int(gs2_to_num(self.deref(idx_entry, frame)))
        obj = self.deref(obj_entry, frame) if isinstance(obj_entry, (VarRef, LValue)) else obj_entry

        if isinstance(obj, GS2Object):
            items = list(obj.keys())
        elif isinstance(obj, list):
            items = obj
        else:
            items = []

        if idx >= len(items):
            # loop done: leave the var entry for the trailing OP_INDEX_DEC
            frame.stack.append(var_entry)
            return int(instr.operand.value)

        self._write_ref(var_entry, items[idx], frame)
        frame.stack.append(var_entry)
        frame.stack.append(obj_entry)
        frame.stack.append(float(idx))
        return None

    # --- calls ---

    def _op_call(self, frame, instr):
        target = frame.stack.pop() if frame.stack else None
        args = [self.deref(a, frame) for a in self._pop_args(frame)]
        frame.stack.append(self._call_target(target, args, frame))
        return None

    def _gcall_target(self, target: Any, args: List[Any], frame: "_Frame",
                      coro_mode: bool):
        if (isinstance(target, LValue) and target.obj is self.this
                and self.has_function(target.key)):
            return (yield from self._gcall_script(
                target.key.lower(), args, coro_mode))
        if isinstance(target, VarRef):
            name = target.name.lower()
            if self.has_function(name):
                return (yield from self._gcall_script(name, args, coro_mode))
        return self._call_target(target, args, frame)

    def _gcall_script(self, name: str, args: List[Any], coro_mode: bool):
        idx = self.functions.get(name)
        if idx is not None:
            return (yield from self._execute(idx, args, coro_mode))
        for joined in self.joined:
            if joined.has_function(name):
                joined._ops_used = self._ops_used
                value = yield from joined._gcall_script(name, args, coro_mode)
                self._ops_used = joined._ops_used
                return value
        return None

    def _call_target(self, target: Any, args: List[Any], frame: "_Frame") -> Any:
        cls = type(self)

        # method call: obj.func(args)
        if isinstance(target, LValue):
            name = target.key.lower()
            obj = target.obj
            member = target.get()
            if callable(member):
                cls.builtins_called[name] = cls.builtins_called.get(name, 0) + 1
                return member(*args)
            if isinstance(member, GS2VM):
                return member.call(name, *args)
            # A method call on ANOTHER script's `this`. Scripts publish
            # themselves into a bare global (`plfunc = this;` in Zelda's
            # -Player/Functions, `Movement = this;` in -Player/Movement) and
            # the rest of the world calls them as `plfunc.ModifyClientR(...)`.
            # Only `public` functions are reachable this way, same as the engine.
            owners = [vm for vm in reversed(getattr(obj, "script_vms", ()) or ())
                      if vm is not self]
            for other in owners:
                if other.has_public_function(name):
                    cls.builtins_called[name] = cls.builtins_called.get(name, 0) + 1
                    return other.call(name, *args)
            # `<script object>.trigger("onSomeEvent", params)` fires an EVENT
            # on that script -- the engine method, not a script function, so
            # it is not gated on `public`.
            #   findweapon(...).trigger("onweaponfired", null) is Zelda's only weapon-fire
            #   path (weapon-Player_Movement.txt:473).
            if owners and name == "trigger" and args:
                event = str(args[0])
                cls.builtins_called[name] = cls.builtins_called.get(name, 0) + 1
                for other in owners:
                    if other.has_function(event):
                        return other.call(event, *args[1:])
                return 0.0
            if self.host is not None and obj is not None:
                res = self.host.call_builtin(self, name, args, obj=obj)
                if res is not NOT_HANDLED:
                    cls.builtins_called[name] = cls.builtins_called.get(name, 0) + 1
                    return res
            res = self._root_object_method(obj, name, args)
            if res is not NOT_HANDLED:
                cls.builtins_called[name] = cls.builtins_called.get(name, 0) + 1
                return res
            # No fall-through to the bare-name builtins: resolveObjectMember
            # with an explicit object consults that object's property table
            # and its own vars, then gives up -- it never reaches the level
            # or the universe globals (src/TScriptMachine.cpp:5283-5322).
            cls.builtins_missing[name] = cls.builtins_missing.get(name, 0) + 1
            self._log_once(("method", name), "GS2 %s: unknown method %s()", self.name, name)
            return 0.0

        if isinstance(target, VarRef):
            name = target.name.lower()
            # with-scope resolution first (official machine: bare-name calls
            # inside with-blocks resolve against the with-list innermost-out
            # before anything else). This is how construction blocks parent
            # nested controls: the compiler emits `addcontrol(<child name>)`
            # after each nested new's WITHEND, resolved as a METHOD of the
            # enclosing with-target -- so host object methods must be
            # reachable here (host.call_builtin with obj=), not only
            # callable members.
            if frame.with_stack:
                for wobj in reversed(frame.with_stack):
                    m = wobj.get(name)
                    if callable(m):
                        cls.builtins_called[name] = cls.builtins_called.get(name, 0) + 1
                        return m(*args)
                    if self.host is not None:
                        res = self.host.call_builtin(self, name, args, obj=wobj)
                        if res is not NOT_HANDLED:
                            cls.builtins_called[name] = cls.builtins_called.get(name, 0) + 1
                            return res
            # script's own functions (incl. joined classes)
            idx = self.functions.get(name)
            if idx is not None:
                gen = self._start_execution(idx, args, coro_mode=False)
                try:
                    while True:
                        next(gen)
                except StopIteration as done:
                    return done.value
            for j in self.joined:
                if j.has_function(name):
                    return j.call(name, *args)
            # host builtins
            if self.host is not None:
                res = self.host.call_builtin(self, name, args)
                if res is not NOT_HANDLED:
                    cls.builtins_called[name] = cls.builtins_called.get(name, 0) + 1
                    return res
            # a variable holding a callable / function object
            v = self._lookup(name, frame)
            if callable(v):
                cls.builtins_called[name] = cls.builtins_called.get(name, 0) + 1
                return v(*args)
            if isinstance(v, GS2VM):
                return v.run_toplevel()
            cls.builtins_missing[name] = cls.builtins_missing.get(name, 0) + 1
            self._log_once(("builtin", name), "GS2 %s: unknown function %s()", self.name, name)
            return 0.0

        if callable(target):
            return target(*args)
        return 0.0

    #: Names in _list_method that the reference registers on the root object
    #: class -- i.e. on EVERY object, not only on arrays (`addarray`
    #: src/TGraalVarProperties.cpp:233, `sortbyvalue` :575). The rest of
    #: _list_method mirrors compiled opcodes (OP_OBJ_ADDSTRING /
    #: OP_OBJ_SIZE / OP_OBJ_CLEAR / OP_OBJ_INDEX) and is not a registered
    #: name, so it must stay array-only.
    _ROOT_OBJECT_METHODS = frozenset({"addarray", "sortbyvalue"})

    def _root_object_method(self, obj: Any, name: str, args: List[Any]) -> Any:
        if isinstance(obj, list):
            return self._list_method(obj, name, args)
        if name == "copyfrom" and isinstance(obj, GS2Object):
            # obj.copyfrom(o): registered on the root object class
            # (src/TGraalVarProperties.cpp:278-285, 'v' "o"), so it reaches
            # every object -- the live Scripted_RC weapon calls it. The
            # host is consulted first (above), so engine-backed objects
            # can keep the reference's gated no-op by overriding copy_from.
            obj.copy_from(args[0] if args else None)
            return None
        if obj is not None and name in self._ROOT_OBJECT_METHODS:
            return None  # object with no array cells: nothing to extend/sort
        return NOT_HANDLED

    @staticmethod
    def _list_method(arr: list, name: str, args: List[Any]) -> Any:
        """Universal array methods dispatched at runtime (not compiled to
        opcodes): the subset the live Login-server corpus exercises plus the
        trivial aliases. All mutations are in place (official TGraalVar
        methods mutate the underlying var). Host builtins are consulted
        first, so a richer host can override any of these."""
        if name == "add":
            arr.append(args[0] if args else None)
            return None
        if name == "addarray":
            other = args[0] if args else None
            if isinstance(other, list):
                arr.extend(other)
            return None
        if name == "size":
            return float(len(arr))
        if name == "clear":
            arr.clear()
            return None
        if name == "index":
            value = args[0] if args else None
            for i, x in enumerate(arr):
                if gs2_eq(x, value):
                    return float(i)
            return -1.0
        if name == "copyfrom":
            # TGraalVar::copyFrom on an array-valued target: the source's
            # array replaces the target's, cells CLONED (src/TGraalVar.cpp:
            # 2248-2257); a null/non-array source clears (:2216-2221).
            other = args[0] if args else None
            arr[:] = copy_value(other) if isinstance(other, list) else []
            return None
        if name == "sortbyvalue":
            # sortbyvalue(fieldindex, "float"|"string", ascending): entries
            # are CSV-row strings (or nested lists); sort by field
            # <fieldindex>, numerically for "float". Seen live in
            # -Mobile/Serverlist sortServers().
            idx = int(gs2_to_num(args[0])) if len(args) > 0 else 0
            as_float = to_str(args[1] if len(args) > 1 else "") == "float"
            ascending = gs2_truthy(args[2]) if len(args) > 2 else True

            def field(row: Any) -> Any:
                if isinstance(row, list):
                    cell = row[idx] if 0 <= idx < len(row) else ""
                else:
                    toks = gs1_csv_split(to_str(row))
                    cell = toks[idx] if 0 <= idx < len(toks) else ""
                return gs2_to_num(cell) if as_float else to_str(cell)

            try:
                arr.sort(key=field, reverse=not ascending)
            except TypeError:
                pass
            return None
        return NOT_HANDLED


class _Frame:
    __slots__ = ("ip", "stack", "temps", "args", "with_stack", "registers")

    def __init__(self, ip: int, args: List[Any]):
        self.ip = ip
        self.stack: List[Any] = []
        self.temps = GS2Object(name="temp")
        self.args = args
        self.with_stack: List[GS2Object] = []
        # expression-cache register file (OP_REG_STORE/OP_REG_LOAD). The
        # official machine's file lives on the machine, but slot indices are
        # compiler-assigned per function body, so per-frame keeps nested
        # script-function calls from clobbering their caller's slots.
        self.registers: Dict[int, Any] = {}


class _ReturnValue(Exception):
    __slots__ = ("value",)

    def __init__(self, value: Any):
        self.value = value
