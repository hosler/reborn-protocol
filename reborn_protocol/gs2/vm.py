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

from .container import GS2Container, parse_container
from .disasm import Instruction, decode
from .opcodes import Op
from .values import (
    ARRAY_START, GS2Object, LValue, VarRef,
    gs2_eq, to_bool, to_num, to_str, fmt_num,
)

logger = logging.getLogger(__name__)

#: sentinel returned by hosts for "builtin not handled here"
NOT_HANDLED = object()

#: cap on any single array allocation/index-driven growth (arr[100000000]=1
#: must not try to allocate a 100M-element list). Applied wherever a script
#: index controls array size: OP_ARRAY_NEW(_MULTIDIM), OP_SETARRAY,
#: OP_ARRAY_ASSIGN, OP_OBJ_REPLACESTRING.
MAX_ARRAY_INDEX = 1 << 20


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
                py = f"%{width}d" % int(to_num(v))
            elif spec in "oxX":
                py = f"%{width}{spec}" % int(to_num(v))
            elif spec == "c":
                n = to_num(v)
                py = chr(int(n)) if isinstance(v, (int, float)) or _looks_numeric(v) else to_str(v)[:1]
                if width:
                    py = f"%{width}s" % py
            elif spec in "feEgG":
                p = prec if prec is not None else "6"
                py = f"%{width}.{p}{spec}" % to_num(v)
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
                    return done.value
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
                    secs = to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
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
        return None

    def _assign_name(self, name: str, value: Any, frame: "_Frame") -> None:
        key = name.lower()
        if frame.with_stack:
            wobj = frame.with_stack[-1]
            if isinstance(wobj, GS2Object):
                wobj.set(key, value)
                return
        if frame.temps.has(key):
            frame.temps.set(key, value)
            return
        self.globals[key] = value

    def _write_ref(self, target: Any, value: Any, frame: "_Frame") -> None:
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
                return args
            args.append(v)
        return args

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
        if to_bool(v):
            return int(instr.operand.value)
        return None

    def _op_if(self, frame, instr):
        v = self.deref(frame.stack.pop(), frame) if frame.stack else None
        if not to_bool(v):
            return int(instr.operand.value)
        return None

    def _op_or(self, frame, instr):
        v = self.deref(frame.stack.pop(), frame) if frame.stack else None
        if to_bool(v):
            frame.stack.append(True)
            return int(instr.operand.value)
        return None

    def _op_and(self, frame, instr):
        # symmetric with OP_OR (GS2Engine omits the push here, which would
        # underflow the OP_INLINE_CONDITIONAL that follows -- compiler layout
        # requires exactly one value at the merge point)
        v = self.deref(frame.stack.pop(), frame) if frame.stack else None
        if not to_bool(v):
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
        secs = to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
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
        frame.stack.append(None)

    def _op_pi(self, frame, instr):
        frame.stack.append(math.pi)

    def _op_this(self, frame, instr):
        frame.stack.append(self.this)

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

    # --- conversions / member access ---

    def _op_conv_to_float(self, frame, instr):
        v = self.deref(frame.stack.pop(), frame) if frame.stack else None
        # Arrays/objects pass through unchanged: the compiler emits this op
        # in front of e.g. arraylen()'s OP_OBJ_SIZE (sig-driven conversion),
        # which only works on the official client if the array survives.
        if isinstance(v, (list, GS2Object)):
            frame.stack.append(v)
        else:
            frame.stack.append(to_num(v))

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
            frame.stack.append(v)
        elif isinstance(raw, LValue):
            frame.stack.append(raw.get())
        else:
            frame.stack.append(raw)

    def _op_member_access(self, frame, instr):
        namev = frame.stack.pop() if frame.stack else None
        base = frame.stack.pop() if frame.stack else None
        name = namev.name if isinstance(namev, VarRef) else to_str(self.deref(namev, frame))
        base = self.deref(base, frame) if isinstance(base, (VarRef, LValue)) else base
        if isinstance(base, GS2Object):
            frame.stack.append(LValue(base, name))
        else:
            frame.stack.append(LValue(None, name))

    # --- objects / arrays ---

    def _op_array_end(self, frame, instr):
        vals = [self.deref(v, frame) for v in self._pop_args(frame)]
        frame.stack.append(vals)

    def _op_array_new(self, frame, instr):
        size = int(to_num(self.deref(frame.stack.pop(), frame))) if frame.stack else 0
        frame.stack.append([0.0] * max(0, min(size, MAX_ARRAY_INDEX)))

    def _op_array_new_multidim(self, frame, instr):
        size = int(to_num(self.deref(frame.stack.pop(), frame))) if frame.stack else 0
        size = max(0, min(size, MAX_ARRAY_INDEX))
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
        size = int(to_num(self.deref(frame.stack.pop(), frame))) if frame.stack else 0
        target = frame.stack.pop() if frame.stack else None
        size = max(0, min(size, MAX_ARRAY_INDEX))
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
        frame.stack.append(1.0 if to_bool(v) else 0.0)

    # --- assignment / params ---

    def _op_assign(self, frame, instr):
        value = self.deref(frame.stack.pop(), frame) if frame.stack else None
        target = frame.stack.pop() if frame.stack else None
        self._write_ref(target, value, frame)

    def _op_func_params_end(self, frame, instr):
        # param names were pushed in reverse declaration order, so pop order
        # is declaration order; bind against caller args positionally
        names = self._pop_args(frame)
        for i, nv in enumerate(names):
            name = nv.name if isinstance(nv, VarRef) else to_str(nv)
            frame.temps.set(name, frame.args[i] if i < len(frame.args) else None)
        return None

    def _op_inc(self, frame, instr):
        target = frame.stack.pop() if frame.stack else None
        n = to_num(self.deref(target, frame)) + 1
        if isinstance(target, (VarRef, LValue)):
            self._write_ref(target, n, frame)
            frame.stack.append(target)
        else:
            # plain value on the stack (e.g. the foreach loop index)
            frame.stack.append(n)

    def _op_dec(self, frame, instr):
        target = frame.stack.pop() if frame.stack else None
        n = to_num(self.deref(target, frame)) - 1
        if isinstance(target, (VarRef, LValue)):
            self._write_ref(target, n, frame)
            frame.stack.append(target)
        else:
            frame.stack.append(n)

    # --- arithmetic / comparison / logic (operands already converted by
    #     compiler-emitted OP_CONV_TO_FLOAT where applicable) ---

    def _pop2num(self, frame) -> Tuple[float, float]:
        b = to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
        a = to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
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
        a, b = self._pop2num(frame)
        frame.stack.append(math.fmod(a, b) if b != 0 else 0.0)

    def _op_pow(self, frame, instr):
        a, b = self._pop2num(frame)
        try:
            frame.stack.append(float(a ** b))
        except (ValueError, OverflowError, ZeroDivisionError):
            frame.stack.append(0.0)

    def _op_not(self, frame, instr):
        v = self.deref(frame.stack.pop(), frame) if frame.stack else None
        frame.stack.append(not to_bool(v))

    def _op_unarysub(self, frame, instr):
        v = self.deref(frame.stack.pop(), frame) if frame.stack else None
        frame.stack.append(-to_num(v))

    def _op_eq(self, frame, instr):
        b = self.deref(frame.stack.pop(), frame) if frame.stack else None
        a = self.deref(frame.stack.pop(), frame) if frame.stack else None
        frame.stack.append(gs2_eq(a, b))

    def _op_neq(self, frame, instr):
        b = self.deref(frame.stack.pop(), frame) if frame.stack else None
        a = self.deref(frame.stack.pop(), frame) if frame.stack else None
        frame.stack.append(not gs2_eq(a, b))

    def _op_lt(self, frame, instr):
        a, b = self._pop2num(frame)
        frame.stack.append(a < b)

    def _op_gt(self, frame, instr):
        a, b = self._pop2num(frame)
        frame.stack.append(a > b)

    def _op_lte(self, frame, instr):
        a, b = self._pop2num(frame)
        frame.stack.append(a <= b)

    def _op_gte(self, frame, instr):
        a, b = self._pop2num(frame)
        frame.stack.append(a >= b)

    def _op_bwo(self, frame, instr):
        a, b = self._pop2num(frame)
        frame.stack.append(float(int(a) | int(b)))

    def _op_bwa(self, frame, instr):
        a, b = self._pop2num(frame)
        frame.stack.append(float(int(a) & int(b)))

    def _op_bwx(self, frame, instr):
        a, b = self._pop2num(frame)
        frame.stack.append(float(int(a) ^ int(b)))

    def _op_bwi(self, frame, instr):
        v = to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
        frame.stack.append(float(~int(v)))

    def _op_bw_leftshift(self, frame, instr):
        a, b = self._pop2num(frame)
        frame.stack.append(float(int(a) << max(0, min(int(b), 63))))

    def _op_bw_rightshift(self, frame, instr):
        a, b = self._pop2num(frame)
        frame.stack.append(float(int(a) >> max(0, min(int(b), 63))))

    def _op_in_range(self, frame, instr):
        hi = to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
        lo = to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
        v = to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
        frame.stack.append(lo <= v <= hi)

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
        v = to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
        frame.stack.append(float(math.trunc(v)))

    def _op_abs(self, frame, instr):
        v = to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
        frame.stack.append(abs(v))

    def _op_random(self, frame, instr):
        a, b = self._pop2num(frame)
        lo, hi = min(a, b), max(a, b)
        frame.stack.append(lo + _random.random() * (hi - lo))

    def _op_sin(self, frame, instr):
        v = to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
        r = math.sin(v)
        frame.stack.append(0.0 if abs(r) < 1e-6 else r)

    def _op_cos(self, frame, instr):
        v = to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
        r = math.cos(v)
        frame.stack.append(0.0 if abs(r) < 1e-6 else r)

    def _op_arctan(self, frame, instr):
        v = to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
        frame.stack.append(math.atan(v))

    def _op_exp(self, frame, instr):
        v = to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
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

    def _op_min(self, frame, instr):
        a, b = self._pop2num(frame)
        frame.stack.append(min(a, b))

    def _op_max(self, frame, instr):
        a, b = self._pop2num(frame)
        frame.stack.append(max(a, b))

    def _op_getangle(self, frame, instr):
        dx, dy = self._pop2num(frame)
        frame.stack.append(math.atan2(-dy, dx) % (2 * math.pi))

    def _op_getdir(self, frame, instr):
        dx, dy = self._pop2num(frame)
        # dominant axis -> direction (0 up, 1 left, 2 down, 3 right)
        if abs(dx) >= abs(dy):
            frame.stack.append(3.0 if dx > 0 else (1.0 if dx < 0 else 2.0))
        else:
            frame.stack.append(2.0 if dy > 0 else 0.0)

    def _op_vecx(self, frame, instr):
        d = int(to_num(self.deref(frame.stack.pop(), frame))) if frame.stack else 0
        frame.stack.append({1: -1.0, 3: 1.0}.get(d % 4, 0.0))

    def _op_vecy(self, frame, instr):
        d = int(to_num(self.deref(frame.stack.pop(), frame))) if frame.stack else 0
        frame.stack.append({0: -1.0, 2: 1.0}.get(d % 4, 0.0))

    def _op_char(self, frame, instr):
        v = to_num(self.deref(frame.stack.pop(), frame)) if frame.stack else 0.0
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
        idx = int(to_num(self.deref(frame.stack.pop(), frame))) if frame.stack else 0
        s = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else ""
        frame.stack.append(s[idx] if 0 <= idx < len(s) else "")

    def _op_obj_substr(self, frame, instr):
        length = int(to_num(self.deref(frame.stack.pop(), frame))) if frame.stack else 0
        start = int(to_num(self.deref(frame.stack.pop(), frame))) if frame.stack else 0
        s = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else ""
        start = max(0, start)
        frame.stack.append(s[start:] if length < 0 else s[start:start + length])

    def _op_obj_starts(self, frame, instr):
        prefix = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else ""
        s = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else ""
        frame.stack.append(s.startswith(prefix))

    def _op_obj_ends(self, frame, instr):
        suffix = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else ""
        s = to_str(self.deref(frame.stack.pop(), frame)) if frame.stack else ""
        frame.stack.append(s.endswith(suffix))

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
            i = int(to_num(idx))
            frame.stack.append(arr[i] if 0 <= i < len(arr) else None)
        elif isinstance(arr, GS2Object):
            frame.stack.append(arr.get(to_str(idx)))
        elif isinstance(arr, str):
            i = int(to_num(idx))
            frame.stack.append(arr[i] if 0 <= i < len(arr) else "")
        else:
            frame.stack.append(None)

    def _op_array_assign(self, frame, instr):
        value = self.deref(frame.stack.pop(), frame) if frame.stack else None
        idx = self.deref(frame.stack.pop(), frame) if frame.stack else 0
        target = frame.stack.pop() if frame.stack else None
        arr = self.deref(target, frame)
        if isinstance(arr, list):
            i = int(to_num(idx))
            if 0 <= i <= MAX_ARRAY_INDEX:
                if i >= len(arr):
                    arr.extend([0.0] * (i + 1 - len(arr)))
                arr[i] = value
        elif isinstance(arr, GS2Object):
            arr.set(to_str(idx), value)
        elif arr is None and isinstance(target, (LValue, VarRef)):
            # auto-vivify: this.arr[0] = x on an unset member
            new = []
            i = int(to_num(idx))
            if 0 <= i <= MAX_ARRAY_INDEX:
                new.extend([0.0] * (i + 1))
                new[i] = value
            self._write_ref(target, new, frame)

    def _op_array_multidim(self, frame, instr):
        # a[i][j] read -- indices beyond the first are chained OP_ARRAY-like;
        # the compiler pushes all indices then this op. We support 2D (the
        # overwhelmingly common case).
        j = int(to_num(self.deref(frame.stack.pop(), frame))) if frame.stack else 0
        i = int(to_num(self.deref(frame.stack.pop(), frame))) if frame.stack else 0
        arr = self.deref(frame.stack.pop(), frame) if frame.stack else None
        row = arr[i] if isinstance(arr, list) and 0 <= i < len(arr) else None
        frame.stack.append(row[j] if isinstance(row, list) and 0 <= j < len(row) else None)

    def _op_array_multidim_assign(self, frame, instr):
        value = self.deref(frame.stack.pop(), frame) if frame.stack else None
        j = int(to_num(self.deref(frame.stack.pop(), frame))) if frame.stack else 0
        i = int(to_num(self.deref(frame.stack.pop(), frame))) if frame.stack else 0
        arr = self.deref(frame.stack.pop(), frame) if frame.stack else None
        if isinstance(arr, list) and 0 <= i < len(arr) and isinstance(arr[i], list):
            row = arr[i]
            if j >= len(row):
                row.extend([0.0] * (j + 1 - len(row)))
            if j >= 0:
                row[j] = value

    def _op_obj_subarray(self, frame, instr):
        # obj.subarray(start, length): default flags (no OBJECT_FIRST) put
        # the object on top: stack is [length, start, obj]
        arr = self.deref(frame.stack.pop(), frame) if frame.stack else None
        start = int(to_num(self.deref(frame.stack.pop(), frame))) if frame.stack else 0
        length = int(to_num(self.deref(frame.stack.pop(), frame))) if frame.stack else -1
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
        idx = int(to_num(self.deref(frame.stack.pop(), frame))) if frame.stack else 0
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
        idx = int(to_num(self.deref(frame.stack.pop(), frame))) if frame.stack else 0
        value = self.deref(frame.stack.pop(), frame) if frame.stack else None
        arr = self.deref(frame.stack.pop(), frame) if frame.stack else None
        if isinstance(arr, list) and 0 <= idx <= MAX_ARRAY_INDEX:
            if idx >= len(arr):
                arr.extend([0.0] * (idx + 1 - len(arr)))
            arr[idx] = value

    def _op_obj_insertstring(self, frame, instr):
        # obj.insert(index, value) with CMD_REVERSE_ARGS: stack [obj, value, index]
        idx = int(to_num(self.deref(frame.stack.pop(), frame))) if frame.stack else 0
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

    def _op_obj_type(self, frame, instr):
        # float 0, string 1, object 2, array 3 (opcodes.h comment)
        v = self.deref(frame.stack.pop(), frame) if frame.stack else None
        if isinstance(v, list):
            frame.stack.append(3.0)
        elif isinstance(v, GS2Object):
            frame.stack.append(2.0)
        elif isinstance(v, str):
            frame.stack.append(1.0)
        else:
            frame.stack.append(0.0)

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
        idx = int(to_num(self.deref(idx_entry, frame)))
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
            if self.host is not None and obj is not None:
                res = self.host.call_builtin(self, name, args, obj=obj)
                if res is not NOT_HANDLED:
                    cls.builtins_called[name] = cls.builtins_called.get(name, 0) + 1
                    return res
            cls.builtins_missing[name] = cls.builtins_missing.get(name, 0) + 1
            self._log_once(("method", name), "GS2 %s: unknown method %s()", self.name, name)
            return 0.0

        if isinstance(target, VarRef):
            name = target.name.lower()
            # with-scope function member
            if frame.with_stack:
                for wobj in reversed(frame.with_stack):
                    m = wobj.get(name)
                    if callable(m):
                        cls.builtins_called[name] = cls.builtins_called.get(name, 0) + 1
                        return m(*args)
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


class _Frame:
    __slots__ = ("ip", "stack", "temps", "args", "with_stack")

    def __init__(self, ip: int, args: List[Any]):
        self.ip = ip
        self.stack: List[Any] = []
        self.temps = GS2Object(name="temp")
        self.args = args
        self.with_stack: List[GS2Object] = []


class _ReturnValue(Exception):
    __slots__ = ("value",)

    def __init__(self, value: Any):
        self.value = value
