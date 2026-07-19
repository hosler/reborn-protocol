"""GS1 interpreter — the tree-walking executor.

Owns control flow, expression evaluation and the flag/var stores; routes
built-in attributes, message codes and side-effecting commands to a Host
(runtime.py). Pure math/string functions are implemented here; game functions
(onwall, getnpc, playersays, ...) fall through to the host.

See memory: gs1-python-port. Phase 4 fleshes out the full command/function set
and diffs against the C++ oracle; this establishes the engine + core.
"""
from __future__ import annotations

import base64
import hashlib
import math
import random as _random

from . import ast
from .parser import parse
from .csv import gs1_csv_join, gs1_csv_split
from .runtime import (Context, Host, MemoryHost, VarStore, UNSET, NAMESPACES,
                      RESERVED_CONSTANTS,
                      BreakSignal, ContinueSignal, ReturnSignal)
from .values import (to_num, to_str, fmt_num,
                     gs1_num, gs1_truthy, is_double_zero)

# commands the interpreter handles itself (manipulate the var store)
_VAR_COMMANDS = {
    "set", "unset", "setstring", "setarray",
    "addstring", "deletestring", "insertstring", "removestring",
    "replacestring",
}
# commands whose first argument is a message code used as an assignment target
# (passed to the host as its raw code string, not its expanded value)
_MSGCODE_TARGET_COMMANDS = {"setcharprop", "setplayerprop"}


def _raw_msgcode(node):
    """Raw code (e.g. '#1') if node is a message code, else None. A lone M-type
    argument parses as a MessageCode or a StrConcat wrapping just one."""
    if isinstance(node, ast.MessageCode):
        return node.code
    if isinstance(node, ast.StrConcat):
        codes = [p for p in node.parts if isinstance(p, ast.MessageCode)]
        others = [p for p in node.parts
                  if not isinstance(p, ast.MessageCode)
                  and not (isinstance(p, ast.Str) and p.value == "")]
        if len(codes) == 1 and not others:
            return codes[0].code
    return None
_MAX_CALL_DEPTH = 100
# ScriptEngineGS1.h maximumLoopCount: while/for hard-caps at 10000 iterations
# and then silently falls out, regardless of the loop's own condition. This is
# PER LOOP (each while/for statement gets its own budget) and is separate
# from/in addition to ctx.max_steps, which remains a whole-script backstop.
_MAX_LOOP_ITERATIONS = 10000


def _tokenize(text, delimiters=""):
    """Split on standard/custom delimiters while preserving quoted chunks."""
    text = text.strip()
    if not text:
        return []

    tokens = []
    start = 0
    i = 0
    while i < len(text):
        char = text[i]
        if char == '"':
            start = i + 1
            end = text.find('"', start)
            if end == -1:
                tokens.append(text[start:])
                return tokens
            tokens.append(text[start:end])
            start = end + 1
            i = end
        elif char in " \t," or char in delimiters:
            if start != i:
                tokens.append(text[start:i])
            start = i + 1
        i += 1

    if start != len(text):
        tokens.append(text[start:])
    return tokens


class Interpreter:
    def __init__(self, ctx: Context, resumable: bool = False):
        self.ctx = ctx
        self._depth = 0
        self._loop_depth = 0   # enclosing while/for nesting (for sleep semantics)
        # Cooperative-coroutine mode: when True, `sleep N` SUSPENDS the script
        # (the generator yields N seconds; a scheduler resumes it later). When
        # False (pygserver / tests / expression-context calls) sleep can't
        # suspend, so it falls back to break-the-enclosing-loop / no-op (the
        # tradeoff this used to document unconditionally -- see
        # run_event_resumable / ResumableExecution below for the opt-in fix:
        # true suspend-and-resume across while/for/function bodies, matching
        # GServer-v2's m_sleepCallStack). Sync callers (run/run_event/exec,
        # the pygserver and expression-call paths) are UNCHANGED by that
        # addition -- they never set this, so they keep today's behavior.
        self._coro = resumable

    # -- entry -------------------------------------------------------------
    def run(self, program: ast.Program):
        # hoist user function definitions first
        for stmt in program.body:
            if isinstance(stmt, ast.FuncDef):
                self.ctx.functions[stmt.name] = stmt
        for stmt in program.body:
            if not isinstance(stmt, ast.FuncDef):
                try:
                    self.exec(stmt)
                except (BreakSignal, ContinueSignal, ReturnSignal):
                    pass  # stray control-flow at top level is a no-op

    def run_event(self, program: ast.Program, event: str):
        """Fire an event: set its flag true and run the whole script.

        In GS1 an event name (created, playerenters, playerchats, ...) is a flag
        that reads true only during that event, so `if (playerchats && ...)` and
        `if (created)` gate behaviour. We run every top-level statement; blocks
        for other events see their flag as false and skip themselves.
        """
        self.ctx.active_event = event
        self.ctx.tokens_count = 0
        self.ctx.vars.unset(None, "tokenscount")
        for stmt in program.body:
            if isinstance(stmt, ast.FuncDef):
                self.ctx.functions[stmt.name] = stmt
        for stmt in program.body:
            if isinstance(stmt, ast.FuncDef):
                continue
            try:
                self.exec(stmt)
            except (BreakSignal, ContinueSignal, ReturnSignal):
                pass  # stray control-flow outside a loop is a no-op

    def run_event_resumable(self, program: ast.Program, event: str) -> "ResumableExecution":
        """Opt-in entry point for hosts that want real suspend/resume `sleep`
        semantics instead of the sync break-the-loop fallback (see the _coro
        comment on __init__). Forces coro mode on THIS Interpreter -- create
        a fresh Interpreter per resumable run (don't share one across
        concurrent runs; _depth/_loop_depth and ctx.this_obj/charprop_source
        are live interpreter/ctx state for the duration a run is suspended).

        Returns a ResumableExecution already advanced to its first sleep (or
        to completion, if the script never sleeps). The caller drives it back
        to completion with repeated .resume() calls, e.g. from the host's own
        timeout-event scheduler -- see ResumableExecution and interp.py:186.
        """
        self._coro = True
        return ResumableExecution(self, self.iter_event(program, event))

    # -- statements --------------------------------------------------------
    def _step(self):
        self.ctx.steps += 1
        if self.ctx.steps > self.ctx.max_steps:
            raise RuntimeError("GS1 step budget exceeded (possible infinite loop)")

    # -- synchronous drains (pygserver / tests / non-suspending contexts) ---
    # In coro mode these still run to completion immediately; only the scheduler
    # (which pumps iter_event) honours the sleep-suspend yields.
    def exec(self, node):
        for _ in self._gx(node):
            pass

    def exec_block(self, body):
        for stmt in body:
            for _ in self._gx(stmt):
                pass

    # -- generator execution (cooperative sleep) ---------------------------
    def _gblock(self, body):
        for stmt in body:
            yield from self._gx(stmt)

    def _gx(self, node):
        """Execute one statement as a generator. Yields sleep-seconds when the
        script suspends (coro mode); control-flow recurses via `yield from`.
        Leaf statements run synchronously via their `_st_*` handlers."""
        self._step()
        t = type(node).__name__
        if t == "If":
            if gs1_truthy(self.eval(node.cond)):
                yield from self._gblock(node.then)
            elif node.els is not None:
                yield from self._gblock(node.els)
        elif t == "Block":
            yield from self._gblock(node.body)
        elif t == "While":
            self._loop_depth += 1
            iterations = 0
            try:
                while iterations < _MAX_LOOP_ITERATIONS and gs1_truthy(self.eval(node.cond)):
                    iterations += 1
                    self._step()  # guard empty-body loops too
                    try:
                        yield from self._gblock(node.body)
                    except BreakSignal:
                        break
                    except ContinueSignal:
                        continue
            finally:
                self._loop_depth -= 1
        elif t == "For":
            if node.init is not None:
                self.exec(node.init)
            self._loop_depth += 1
            iterations = 0
            try:
                while (iterations < _MAX_LOOP_ITERATIONS
                       and (node.cond is None or gs1_truthy(self.eval(node.cond)))):
                    iterations += 1
                    self._step()  # guard empty-body loops too
                    try:
                        yield from self._gblock(node.body)
                    except BreakSignal:
                        break
                    except ContinueSignal:
                        pass
                    if node.post is not None:
                        self.exec(node.post)
            finally:
                self._loop_depth -= 1
        elif t == "With":
            obj = self.eval(node.obj)
            prev = self.ctx.this_obj
            self.ctx.this_obj = obj
            try:
                yield from self._gblock(node.body)
            finally:
                self.ctx.this_obj = prev
        elif t == "UserCall":
            yield from self._gcall_user(node.name)
        elif t == "Command" and node.name == "sleep":
            secs = to_num(self.eval(node.args[0])) if node.args else 0.0
            if self._coro:
                if secs > 0:
                    yield secs          # suspend; the scheduler resumes us later
            elif self._loop_depth > 0:
                raise BreakSignal()     # sync: yield by breaking the wait-loop
            # sync, outside a loop: no-op (sequential `sleep 1; foo` runs foo)
        else:
            m = getattr(self, "_st_" + t, None)
            if m is None:
                raise RuntimeError(f"cannot execute node {t}")
            m(node)

    def _gcall_user(self, name):
        fn = self.ctx.functions.get(name)
        if fn is None:
            return
        if self._depth >= _MAX_CALL_DEPTH:
            raise RuntimeError("GS1 max call depth exceeded")
        self._depth += 1
        try:
            yield from self._gblock(fn.body)
        except ReturnSignal:
            pass
        finally:
            self._depth -= 1

    def iter_event(self, program: ast.Program, event: str):
        """Coroutine entry: run an event as a generator that yields sleep-seconds
        (coro mode). The scheduler drives this; sync callers use run_event."""
        self.ctx.active_event = event
        self.ctx.tokens_count = 0
        self.ctx.vars.unset(None, "tokenscount")
        for stmt in program.body:
            if isinstance(stmt, ast.FuncDef):
                self.ctx.functions[stmt.name] = stmt
        for stmt in program.body:
            if isinstance(stmt, ast.FuncDef):
                continue
            try:
                yield from self._gx(stmt)
            except (BreakSignal, ContinueSignal, ReturnSignal):
                pass  # stray control-flow outside a loop is a no-op

    def _st_ExprStmt(self, node):
        if node.expr is not None:
            self.eval(node.expr)

    def _st_FuncDef(self, node):
        self.ctx.functions[node.name] = node

    def _st_Flow(self, node):
        if node.kind == "break":
            raise BreakSignal()
        if node.kind == "continue":
            raise ContinueSignal()
        raise ReturnSignal()

    def _st_Assign(self, node):
        value = self.eval(node.value)
        if node.op != "=":
            cur = self.get_ref(node.target)
            value = self._compound(node.op, cur, value)
        elif self._is_bare_timeout(node.target):
            # `timeout = x` (plain assignment only -- upstream gates this on
            # OP_ASSIGN, so `timeout += 1` does NOT clear) erases any
            # resumable sleep pending on this ctx. See Context.sleep_cancelled.
            self.ctx.sleep_cancelled = True
        if node.op == "=" and not isinstance(value, (list, bool)):
            # Plain assignment is numeric; text requires setstring.
            value = to_num(value)
        self.set_ref(node.target, value)

    def _is_bare_timeout(self, ref):
        """True if `ref` is the unscoped, unindexed identifier `timeout`
        (not this.timeout/server.timeout/timeout[i]) -- the one GS1Visitor
        special-cases for the sleep-stack-clearing rule."""
        if len(ref.parts) != 1:
            return False
        part = ref.parts[0]
        return not part.index and self._part_name(part) == "timeout"

    def _st_Command(self, node):
        name = node.name
        if name == "sleep":
            return  # handled in _gx (suspends in coro mode / yields the loop)
        if name in _VAR_COMMANDS:
            self._exec_var_command(node)
            return
        if name == "tokenize":
            s = to_str(self.eval(node.args[0])) if node.args else ""
            self.ctx.tokenize_tokens = _tokenize(s)
            self.ctx.tokens_count = len(self.ctx.tokenize_tokens)
            self.ctx.vars.set("", "tokenscount",
                              float(len(self.ctx.tokenize_tokens)))
            return
        if name == "tokenize2":
            delimiters = to_str(self.eval(node.args[0])) if node.args else ""
            text = to_str(self.eval(node.args[1])) if len(node.args) > 1 else ""
            self.ctx.tokenize_tokens = _tokenize(text, delimiters)
            self.ctx.tokens_count = len(self.ctx.tokenize_tokens)
            self.ctx.vars.set("", "tokenscount",
                              float(len(self.ctx.tokenize_tokens)))
            return
        if name in _MSGCODE_TARGET_COMMANDS and node.args:
            # setcharprop/setplayerprop take a message code as an assignment
            # *target* (#1 sword, #3 head, ...), so pass its raw code rather
            # than the expanded value (mirrors GS1Visitor's prop-ref handling).
            code = _raw_msgcode(node.args[0])
            first = code if code is not None else self.eval(node.args[0])
            # While the VALUE args are evaluated, the command's own target is
            # the current source for bare context-sensitive message codes
            # (#C0..#C7): GServer's processBuiltInCommand pushSource()es the
            # NPC for setcharprop / the acting player for setplayerprop
            # BEFORE argument collection (GS1Commands.cpp:430-496). This is
            # what makes the corpus idiom `setcharprop #C0,#C0` a self
            # round-trip on the NPC rather than a player-colour read.
            prev = getattr(self.ctx, "charprop_source", None)
            self.ctx.charprop_source = ("npc" if name == "setcharprop"
                                        else "player")
            try:
                args = [first] + [self.eval(a) for a in node.args[1:]]
            finally:
                self.ctx.charprop_source = prev
            self.ctx.host.call_command(name, args, self.ctx)
            return
        args = [self.eval(a) for a in node.args]
        self.ctx.host.call_command(name, args, self.ctx)

    def _exec_var_command(self, node):
        # flags/strings/arrays live in the var store, NOT in host built-ins
        name, args = node.name, node.args
        if name == "set":
            # fn_set (GS1Commands.cpp): `flag->assign<bool>(true)` -- a real
            # bool, not 1.0 (a bare number is never truthy in `if (flag)`
            # under GS1's flag-condition semantics, see gs1_truthy).
            ref = args[0]
            if self._store_get(ref) is UNSET_VAL:  # set only marks presence
                self._store_set(ref, True)
        elif name == "unset":
            self.unset_ref(args[0])
        elif name == "setstring":
            # The value is the rest of the line; GS1 command args are split on
            # commas, so rejoin them with commas to reconstruct it (e.g.
            # `setstring client.b_temp,"a","b","",""` -> "a,b,," and
            # `setstring server.bombrm_N,Join <t>,<list>` keeps the player list).
            val = ",".join(to_str(self.eval(a)) for a in args[1:])
            # fn_setstring (GS1Commands.cpp): for client./server.(r) flags an
            # empty value DELETES the flag; every other scope (this., temp.,
            # local., level., global., bare) just assigns the empty string
            # like any other value (`var->assign<std::string>(text)`
            # unconditionally) -- it does NOT unset. this.empty="" is a real,
            # present empty string, distinct from an unset flag.
            scope, _key, _indices, _names = self._resolve(args[0])
            if val == "" and scope in ("client", "server"):
                self.unset_ref(args[0])
            else:
                self._store_set(args[0], val)
        elif name == "addstring":
            items = self._string_list(args[0])
            items.append(to_str(self.eval(args[1])) if len(args) > 1 else "")
            self._store_set(args[0], gs1_csv_join(items))
        elif name == "deletestring":
            items = self._string_list(args[0])
            index = max(0, int(math.floor(to_num(self.eval(args[1])))))
            if index < len(items):
                del items[index]
                self._store_set(args[0], gs1_csv_join(items))
        elif name == "insertstring":
            items = self._string_list(args[0])
            index = max(0, int(math.floor(to_num(self.eval(args[1])))))
            index = min(index, len(items))
            items.insert(index, to_str(self.eval(args[2])))
            self._store_set(args[0], gs1_csv_join(items))
        elif name == "removestring":
            current = self._store_get(args[0])
            if not isinstance(current, str) or current == "":
                return
            text = to_str(self.eval(args[1]))
            items = [item for item in gs1_csv_split(current)
                     if item != text]
            self._store_set(args[0], gs1_csv_join(items))
        elif name == "replacestring":
            items = self._string_list(args[0])
            index = max(0, int(math.floor(to_num(self.eval(args[1])))))
            text = to_str(self.eval(args[2]))
            if index >= len(items):
                items.append(text)
            else:
                items[index] = text
            self._store_set(args[0], gs1_csv_join(items))
        elif name == "setarray":
            # fn_setarray (GS1Commands.cpp, commit f6803352) resizes an
            # EXISTING array in place, preserving contents and zero-filling
            # only the new slots; a negative/zero size clamps to an empty
            # array. If the var doesn't currently hold an array (scalar,
            # string or unset), it's simply replaced with a fresh zero array.
            size = max(0, int(to_num(self.eval(args[1])))) if len(args) > 1 else 0
            cur = self._store_get(args[0])
            new = [0.0] * size
            if isinstance(cur, list):
                new[:min(size, len(cur))] = cur[:size]
            self._store_set(args[0], new)

    # -- expressions -------------------------------------------------------
    def eval(self, node):
        m = getattr(self, "_ex_" + type(node).__name__, None)
        if m is None:
            raise RuntimeError(f"cannot evaluate node {type(node).__name__}")
        return m(node)

    def _ex_Num(self, node):
        return node.value

    def _ex_Bool(self, node):
        return bool(node.value)

    def _ex_Str(self, node):
        return node.value

    def _ex_StrConcat(self, node):
        # compound strings are trimmed both sides by the engine
        # (GS1Visitor::visitCompoundString -> trimMutate), which strips the
        # command/argument separator space; internal spacing is preserved.
        s = "".join(self._str_part(p) for p in node.parts).strip()
        # Reborn `name=` value-of idiom applies to the ASSEMBLED string (e.g.
        # server.room#v(RoomID)= -> "server.room1=" -> room1's value).
        vo = self._value_of(s)
        return s if vo is None else vo

    def _str_part(self, p):
        if isinstance(p, ast.Str):
            return p.value
        if isinstance(p, ast.MessageCode):
            return self._eval_messagecode(p)
        return to_str(self.eval(p))

    def _value_of(self, s):
        """Reborn `name=` value-of idiom: a bareword ending in '=' resolves to the
        variable's VALUE, not the literal text (e.g. strlen(server.room1=) is the
        length of room1's value). Restricted to NAMESPACED refs that EXIST, so
        ordinary string literals ending in '=' (e.g. "Score=") are left alone.
        Returns the value string, or None if `s` isn't this idiom."""
        if len(s) < 3 or s[-1] != "=" or s[-2] == "=":
            return None
        name = s[:-1]
        dot = name.find(".")
        if dot <= 0:
            return None                      # must be namespaced (server.x, this.x)
        scope = NAMESPACES.get(name[:dot])
        if scope is None:
            return None
        key = name[dot + 1:]
        # key is a flat flag/var name (allow the 223-overflow '*' twin), no spaces
        if not key or any(c in key for c in " \t.="):
            return None
        v = self.ctx.vars.get(scope, key)
        return None if v is UNSET else to_str(v)

    def _ex_MessageCode(self, node):
        return self._eval_messagecode(node)

    def _eval_messagecode(self, node):
        code = node.code
        a = node.args
        # computed / string-manipulation codes (faithful to GS1MessageCodes.cpp)
        if code == "#s":
            # string-of: an UNSET flag is the empty string, NOT "0" (an unset
            # var otherwise evaluates to 0.0). The bomber room editor relies on
            # #s(server.roomN*) being "" when a room has no 223-overflow twin.
            if not a:
                return ""
            if isinstance(a[0], ast.VarRef):
                v = self.get_ref(a[0])
                return "" if v is UNSET_VAL else to_str(v)
            return to_str(self.eval(a[0]))
        if code == "#v":
            return to_str(to_num(self.eval(a[0]))) if a else "0"
        if code == "#U":
            return to_str(self.eval(a[0])) if a else ""
        if code == "#T":  # trim
            return to_str(self.eval(a[0])).strip() if a else ""
        if code == "#e":  # substr(start, len, str); negative len = to end
            if len(a) >= 3:
                start = int(math.floor(to_num(self.eval(a[0]))))
                length = int(math.floor(to_num(self.eval(a[1]))))
                s = to_str(self.eval(a[2]))
                if start < 0:
                    return ""
                return s[start:] if length < 0 else s[start:start + length]
            return ""
        if code == "#I":  # csv item by index
            if len(a) >= 2:
                csv = to_str(self.eval(a[0]))
                idx = int(math.floor(to_num(self.eval(a[1]))))
                items = gs1_csv_split(csv)
                return items[idx] if 0 <= idx < len(items) else ""
            return ""
        if code == "#K":  # char from ascii code
            c = min(255, max(0, int(math.floor(to_num(self.eval(a[0])))))) if a else 0
            return chr(c)
        if code == "#t":  # tokenize token by index
            idx = int(math.floor(to_num(self.eval(a[0])))) if a else 0
            toks = self.ctx.tokenize_tokens
            return toks[idx] if 0 <= idx < len(toks) else ""
        if code == "#R":  # random pick among args
            return to_str(self.eval(_random.choice(a))) if a else ""
        if code == "#E":  # base64-encoded SHA256 hash
            s = to_str(self.eval(a[0])) if a else ""
            return _base64_sha256(s)
        # character / context codes (#a account, #n nick, #c chat, #1-8, #C, ...)
        args = [self.eval(x) for x in a]
        return to_str(self.ctx.host.message_code(code, args, self.ctx))

    def _ex_VarRef(self, node):
        v = self.get_ref(node)
        return 0.0 if v is UNSET_VAL else v

    def _ex_ArrayLit(self, node):
        return [self.eval(e) for e in node.elements]

    def _ex_SpecialLit(self, node):
        return node.value

    def _ex_RangeLit(self, node):
        return (self.eval(node.lo), self.eval(node.hi))

    def _ex_UnaryOp(self, node):
        v = self.eval(node.operand)
        if node.op == "-":
            return -gs1_num(v)
        if node.op == "+":
            return gs1_num(v)
        if node.op == "!":
            # NOT a flag test: GS1's unary `!` always numeric-coerces its
            # operand and tests it near zero (DoubleIsZero), so it is
            # deliberately NOT De Morgan-consistent with `if`/`&&`/`||`
            # (which use gs1_truthy: a number is never truthy there, but
            # `!42` is still false, not true).
            return is_double_zero(gs1_num(v))
        return v

    def _ex_Postfix(self, node):
        cur = gs1_num(self.get_ref(node.operand))
        new = cur + 1 if node.op == "++" else cur - 1
        self.set_ref(node.operand, new)
        return cur

    def _ex_Ternary(self, node):
        return self.eval(node.a) if gs1_truthy(self.eval(node.cond)) else self.eval(node.b)

    def _ex_InExpr(self, node):
        # fn_visitExpressionIn (GS1Visitor.cpp): ALL values on the left must
        # satisfy the range/membership test (comma list is an "all of" test,
        # not "any of"); a range's bounds may be given in either order (a
        # reversed range like |5,1| just flips which comparison is <=/>=),
        # and each side is independently inclusive ('|') or exclusive
        # ('<'/'>').
        checks = [gs1_num(self.eval(v)) for v in node.values]
        rng = node.rng
        if isinstance(rng, ast.RangeLit):
            lo = gs1_num(self.eval(rng.lo))
            hi = gs1_num(self.eval(rng.hi))
            ascending = lo < hi
            for check in checks:
                if ascending:
                    left_ok = (lo <= check) if rng.lo_incl else (lo < check)
                    right_ok = (check <= hi) if rng.hi_incl else (check < hi)
                else:
                    left_ok = (lo >= check) if rng.lo_incl else (lo > check)
                    right_ok = (check >= hi) if rng.hi_incl else (check > hi)
                if not (left_ok and right_ok):
                    return False
            return True
        container = self.eval(rng)
        if not isinstance(container, (list, tuple)):
            return False
        for check in checks:
            if not any(gs1_num(x) == check for x in container):
                return False
        return True

    def _ex_BinOp(self, node):
        op = node.op
        if op == "&&":
            return gs1_truthy(self.eval(node.left)) and gs1_truthy(self.eval(node.right))
        if op == "||":
            return gs1_truthy(self.eval(node.left)) or gs1_truthy(self.eval(node.right))
        a = self.eval(node.left)
        b = self.eval(node.right)
        # GS1 arithmetic/comparison ALWAYS coerces via gs1_num: a bool is
        # 1.0/0.0, a number is itself, but a plain string NEVER numeric-parses
        # here (GameValue::getCopy<double> ignores text) -- `3 + this.s` where
        # this.s holds "25" is 3, not 28. String content compares go through
        # strequals()/strcontains(), not these operators.
        if op == "+":
            return gs1_num(a) + gs1_num(b)
        if op == "-":
            return gs1_num(a) - gs1_num(b)
        if op == "*":
            return gs1_num(a) * gs1_num(b)
        if op == "/":
            d = gs1_num(b)
            return gs1_num(a) / d if d != 0 else 0.0
        if op == "%":
            return self._mod(gs1_num(a), gs1_num(b))
        if op == "^":
            try:
                return float(gs1_num(a) ** gs1_num(b))
            except (ValueError, OverflowError):
                return 0.0
        if op in ("==", "="):
            return self._eq(a, b)
        if op == "!=":
            return not self._eq(a, b)
        if op == "<":
            return gs1_num(a) < gs1_num(b)
        if op == ">":
            return gs1_num(a) > gs1_num(b)
        if op == "<=":
            return gs1_num(a) <= gs1_num(b)
        if op == ">=":
            return gs1_num(a) >= gs1_num(b)
        raise RuntimeError(f"unknown operator {op}")

    @staticmethod
    def _mod(a, b):
        # Both operands truncate to an integer FIRST (C int64_t semantics:
        # `static_cast<int64_t>(result) % static_cast<int64_t>(right)`), THEN
        # modulo -- so 7.9 % 3 == 1 (7 % 3), not fmod(7.9, 3) == 1.9.
        # Python's int() truncates toward zero like the C++ cast; math.fmod
        # (not Python's `%`) then matches C's sign-of-dividend convention.
        ib = int(b)
        if ib == 0:
            return 0.0
        return math.fmod(int(a), ib)

    @staticmethod
    def _eq(a, b):
        # ExpressionEquality (GS1Visitor.cpp): array/array is a real
        # element-wise vector compare; everything else -- including a
        # string on either side -- coerces through gs1_num (DoublesAreSame,
        # epsilon 0.0001, CommonTypes.h), so plain string content is never
        # compared here (use strequals() for that).
        if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
            return list(a) == list(b)
        return abs(gs1_num(a) - gs1_num(b)) < 0.0001

    def _compound(self, op, cur, value):
        base = op[0]
        if base == "+":
            return gs1_num(cur) + gs1_num(value)
        if base == "-":
            return gs1_num(cur) - gs1_num(value)
        if base == "*":
            return gs1_num(cur) * gs1_num(value)
        if base == "/":
            d = gs1_num(value)
            return gs1_num(cur) / d if d != 0 else 0.0
        if base == "%":
            return self._mod(gs1_num(cur), gs1_num(value))
        if base == "^":
            try:
                return float(gs1_num(cur) ** gs1_num(value))
            except (ValueError, OverflowError):
                return 0.0
        return value

    # -- function calls ----------------------------------------------------
    def _ex_Call(self, node):
        name = node.name
        if name in self.ctx.functions:
            return self._call_user(name)
        fn = _PURE.get(name)
        if fn is not None:
            return fn(self, [self.eval(a) for a in node.args])
        v = self.ctx.host.call_function(name, [self.eval(a) for a in node.args], self.ctx)
        return 0.0 if v is UNSET else v

    def _call_user(self, name):
        # Sync call (expression context, e.g. x = myfunc()): drain the generator
        # body. A sleep inside a function called from an expression can't suspend
        # (no scheduler in expression eval), so it falls back to the sync no-op.
        for _ in self._gcall_user(name):
            pass
        return 0.0

    # -- variable resolution ----------------------------------------------
    def _part_name(self, part):
        if part.atoms:
            return "".join(
                p.value if isinstance(p, ast.Str) else self._eval_messagecode(p)
                for p in part.atoms)
        return part.name

    def _resolve(self, ref):
        names = [self._part_name(p) for p in ref.parts]
        # Evaluate ALL indices on the indexed part (2D access like tiles[x,y]).
        # The var store uses the first index; built-ins get the whole list.
        indices = []
        for p in ref.parts:
            if p.index:
                indices = [int(to_num(self.eval(e))) for e in p.index]
                break
        first = names[0]
        if first in NAMESPACES and len(names) > 1:
            return NAMESPACES[first], ".".join(names[1:]), indices, names
        return None, ".".join(names), indices, names

    def get_ref(self, ref):
        scope, key, indices, names = self._resolve(ref)
        index = indices[0] if indices else None
        if scope is not None:
            v = self.ctx.vars.get(scope, key, index)
            return UNSET_VAL if v is UNSET else v
        # bare reserved constant (pi, allstats, allfeatures) always wins over
        # any flag/attribute of the same name — see RESERVED_CONSTANTS.
        if key in RESERVED_CONSTANTS:
            return RESERVED_CONSTANTS[key]
        # bare name that matches the firing event -> its flag reads true.
        # Event names are FLAGS (GS1Visitor's flagStore.get(...)->getCopy<bool>),
        # so this must be a real bool -- gs1_truthy(1.0) is false, since a
        # bare number is never truthy in a condition (`if (created)` would
        # otherwise never fire).
        if key and key == self.ctx.active_event:
            return True
        # bare: built-in attribute first, then player flag/var
        v = self.ctx.host.get_builtin(key, indices, self.ctx)
        if v is not UNSET:
            return v
        v = self.ctx.vars.get(None, key, index)
        return UNSET_VAL if v is UNSET else v

    def set_ref(self, ref, value):
        scope, key, indices, names = self._resolve(ref)
        index = indices[0] if indices else None
        if scope is not None:
            if scope == "this" and key == "save" and index is not None:
                slots = self.ctx.vars.scopes["this"].setdefault("save", [0.0] * 10)
                i = int(index)
                if 0 <= i < 10:
                    slots[i] = float(min(220, max(0, int(to_num(value)))))
                return
            self.ctx.vars.set(scope, key, value, index)
            return
        # bare assignment to a reserved constant name is illegal upstream
        # (GS1Visitor throws "reserved keyword"); ignore rather than raise.
        if key in RESERVED_CONSTANTS:
            return
        if self.ctx.host.set_builtin(key, value, indices, self.ctx):
            return
        self.ctx.vars.set(None, key, value, index)

    def unset_ref(self, ref):
        scope, key, indices, names = self._resolve(ref)
        self.ctx.vars.unset(scope, key)

    def _store_get(self, ref):
        """Read a flag/var from the var store only (ignores host built-ins)."""
        scope, key, indices, names = self._resolve(ref)
        v = self.ctx.vars.get(scope, key, indices[0] if indices else None)
        return UNSET_VAL if v is UNSET else v

    def _store_set(self, ref, value):
        """Write a flag/var to the var store only (ignores host built-ins)."""
        scope, key, indices, names = self._resolve(ref)
        self.ctx.vars.set(scope, key, value, indices[0] if indices else None)

    def _string_list(self, ref):
        cur = self._store_get(ref)
        return gs1_csv_split(cur) if isinstance(cur, str) else []


class ResumableExecution:
    """One GS1 event execution that can suspend at `sleep` and be resumed
    later by the host -- the opt-in fix for the break-the-loop tradeoff
    documented on Interpreter.__init__ (interp.py:~62). Construct via
    Interpreter.run_event_resumable(); don't build directly.

    Design: GServer-v2 (GS1Visitor.cpp) implements this with an EXPLICIT
    sleep call-stack (`m_sleepCallStack`, a vector of (parse-node, child-index)
    pairs) that a Block/While/For visitor snapshots by hand when a
    sleep_exception unwinds through it, and restores by re-entering the tree
    at the saved node/index on the next TIMEOUT event. We get the same
    resume-in-the-middle-of-a-loop behaviour for free from Python's own
    generator machinery: `iter_event` is written as a tree-walking generator
    (`Interpreter._gx`/`_gblock`), so the position within nested
    while/for/with()/user-function bodies IS the suspended generator frame --
    calling `next()` on it again resumes exactly after the `sleep` statement,
    with loop counters and the with()-source/call-stack (both plain Python
    locals or ctx.this_obj, restored by the paused try/finally on resume)
    intact. No hand-rolled position bookkeeping needed; statement-level
    granularity comes for free from `sleep` only ever appearing as its own
    statement (matches upstream: "statement boundaries are fine, no need for
    sub-expression resume").

    Attributes:
      pending_sleep -- seconds the script asked to sleep for, or None if the
                        execution has finished (`done` is True).
      done           -- True once the generator is exhausted (StopIteration)
                        or this pending sleep was cancelled by a `timeout = x`
                        assignment elsewhere on the same ctx.
    """

    def __init__(self, interp: "Interpreter", gen):
        self._interp = interp
        self._gen = gen
        self.pending_sleep = None
        self.done = False
        self._advance()

    def _advance(self):
        ctx = self._interp.ctx
        try:
            self.pending_sleep = next(self._gen)
        except StopIteration:
            self.done = True
            self.pending_sleep = None
        else:
            # A `timeout = x` that happened DURING the statements we just ran
            # (e.g. `timeout = x; sleep 1;` in the same continuation) is this
            # execution's own doing, not a stale cross-execution cancel -- it
            # must not cancel the sleep it just produced. Upstream's own
            # clear is a no-op there too: by the time a script resumes,
            # whatever it might clear was already emptied by the act of
            # resuming (GS1Visitor::execute, m_sleepCallStack.clear() at the
            # top of the TIMEOUT-resume branch). Only a cancel that arrives
            # while we sit SUSPENDED, between this and the next resume() call,
            # is a real "something else reprogrammed my timer" signal.
            ctx.sleep_cancelled = False

    def resume(self):
        """Continue from the saved `sleep` point. A no-op once `done`. If a
        `timeout = x` assignment cancelled this pending sleep while we were
        suspended (see Context.sleep_cancelled), this call consumes the
        cancellation, marks the execution done and stops it -- the script
        never gets a chance to run past its sleep, matching GS1Visitor's
        cleared-stack behaviour."""
        if self.done:
            return
        if self._interp.ctx.sleep_cancelled:
            self._interp.ctx.sleep_cancelled = False
            self.done = True
            self.pending_sleep = None
            self._gen.close()
            return
        self._advance()


UNSET_VAL = UNSET  # alias for readability in this module


# -- pure built-in functions ------------------------------------------------
def _f_random(self, a):
    lo = to_num(a[0]) if a else 0.0
    hi = to_num(a[1]) if len(a) > 1 else lo
    if hi < lo:
        lo, hi = hi, lo
    return _random.uniform(lo, hi)


def _f1(fn):
    return lambda self, a: fn(to_num(a[0]) if a else 0.0)


def _safe(fn, default=0.0):
    def g(x):
        try:
            return fn(x)
        except (ValueError, OverflowError, ZeroDivisionError):
            return default
    return g


# vecx/vecy direction tables (dir 0=up,1=left,2=down,3=right)
_VECX = (0.0, -1.0, 0.0, 1.0)
_VECY = (-1.0, 0.0, 1.0, 0.0)


def _ascii(a):
    s = to_str(a[0]) if a else ""
    return float(ord(s[0]) & 0xFF) if s else 0.0


def _keycode(a):
    key = to_str(a[0]) if a else ""
    if not key:
        return 0.0
    char = key[0]
    if "a" <= char <= "z":
        char = char.upper()
    punctuation = {
        ";": 0xBA, ":": 0xBA, "=": 0xBB, "+": 0xBB,
        ",": 0xBC, "<": 0xBC, "-": 0xBD, "_": 0xBD,
        ".": 0xBE, ">": 0xBE, "/": 0xBF, "?": 0xBF,
        "`": 0xC0, "~": 0xC0, "[": 0xDB, "{": 0xDB,
        "\\": 0xDC, "|": 0xDC, "]": 0xDD, "}": 0xDD,
        "'": 0xDE, '"': 0xDE,
    }
    if char == "\t":
        return float(0x09)
    if char == " ":
        return float(0x20)
    if "0" <= char <= "9" or "A" <= char <= "Z":
        return float(ord(char))
    return float(punctuation.get(char, 0))


def _getangle(a):
    dx = to_num(a[0]) if a else 0.0
    dy = to_num(a[1]) if len(a) > 1 else 0.0
    if dx == 0 and dy == 0:
        return 0.0
    ang = math.atan2(-dy, dx)  # game flips Y
    if ang < 0:
        ang += 2 * math.pi
    return ang


def _getdir(a):
    # GServer-v2 fn_getdir (GS1Functions.cpp, commit 9e759e9d) is angle-based,
    # not a cardinal snap: atan2 over the full circle (Y flipped to match the
    # game's coordinate system), biased to up/down on the diagonals.
    #   right if angle <  pi/4        up    if angle <= 3*pi/4
    #   left  if angle <  5*pi/4      down  if angle <= 7*pi/4
    #   else (>= 7*pi/4)  right
    #
    # The compiled reference binary has a verbatim quirk here: its angle is
    # only computed `if (!DoubleIsZero(dx) || DoubleIsZero(dy))`, so a
    # pure-vertical delta (dx == 0, dy != 0 -- straight up/down) skips the
    # atan2 and falls back to the 0.0 default, i.e. BOTH getdir(0,-1) and
    # getdir(0,1) come out "right" (3.0). That contradicts GServer's own docs
    # (bin/docs/scripting-gs1-functions.md tables (0,-1)->0, (0,1)->2) and
    # real player-facing gameplay expectations. Decision: gameplay fidelity
    # wins over bug-for-bug reference fidelity here, so we always compute the
    # real angle (matching the docs table) rather than replicate that guard.
    # See tests/test_gs1_oracle.py KNOWN_UPSTREAM_BUGS["getdir-vertical"] and
    # corpus case getdir-vertical for the oracle-verified contrast.
    dx = to_num(a[0]) if a else 0.0
    dy = to_num(a[1]) if len(a) > 1 else 0.0
    angle = math.atan2(-dy, dx)  # game flips Y; atan2(0,0)==0.0 for dx=dy=0
    if angle < 0.0:
        angle += 2 * math.pi
    angle_ne = math.pi / 4.0
    angle_nw = math.pi * 3.0 / 4.0
    angle_sw = math.pi + angle_ne
    angle_se = math.pi + angle_nw
    if angle < angle_ne:
        return 3.0
    if angle <= angle_nw:
        return 0.0
    if angle < angle_sw:
        return 1.0
    if angle <= angle_se:
        return 2.0
    return 3.0


def _aindexof(a):
    if len(a) < 2 or not isinstance(a[1], (list, tuple)):
        return -1.0
    val = to_num(a[0])
    for i, x in enumerate(a[1]):
        if to_num(x) == val:
            return float(i)
    return -1.0


def _lindexof(a):
    # GServer-v2's fn_lindexof (GS1Functions.cpp) trims each item and the
    # needle and compares with plain `==` — no case folding (unlike
    # strequals/strcontains/startswith, which use findi/equalsi).
    if len(a) < 2:
        return -1.0
    needle = to_str(a[0]).strip()
    for i, item in enumerate(to_str(a[1]).split(",")):
        if item.strip() == needle:
            return float(i)
    return -1.0


def _sin(x):
    # GServer-v2's fn_sin (GS1Functions.cpp) only evaluates std::sin(value)
    # for value in [0, pi]; anything outside that range returns 0 rather than
    # the full periodic sine. fn_cos has no such restriction. Bomber's own
    # eye_bomber mallet-UI Draw() folds `this.p` into [0,1] before multiplying
    # by pi specifically to stay inside this window, confirming scripts are
    # written expecting the clamp.
    if x < 0 or x > math.pi:
        return 0.0
    return math.sin(x)



def _base64encode_impl(s):
    """Encode a string to base64 with standard padding."""
    return base64.b64encode(s.encode('utf-8')).decode('ascii')


def _base64decode_impl(s):
    """Decode a base64 string.
    
    On invalid input, return empty string (strict decode with error handling).
    This is a deliberate choice: GServer-v2's behavior is not fully specified
    for malformed input, so we use the safest fallback.
    """
    if not s:
        return ""
    try:
        return base64.b64decode(s, validate=True).decode('utf-8', errors='replace')
    except Exception:
        return ""


def _base64_sha256(s):
    """Hash a string with SHA256 and return base64-encoded digest.
    
    Used by #E message code and passwordmatches().
    """
    h = hashlib.sha256(s.encode('utf-8')).digest()
    return base64.b64encode(h).decode('ascii')


def _getflagkeys(interp, a):
    """Iterate flag store keys matching a prefix; return numeric array.
    
    The prefix may start with a storage qualifier (this./thiso./client./clientr./server./serverr.).
    Unqualified defaults to the player (client) flag store.
    For each matching key, parse the remainder as a number (non-numeric -> 0).
    
    Returns empty array if the store is not accessible.
    """
    prefix = to_str(a[0]) if a else ""
    
    # Parse the storage qualifier from the prefix
    scope = None
    remainder_prefix = prefix
    
    # Try to match a scope qualifier (check longest first to handle "clientr." before "client.")
    for qual in ("thiso.", "this.", "clientr.", "client.", "serverr.", "server."):
        if prefix.startswith(qual):
            # Extract the namespace part and map it using NAMESPACES
            namespace = qual[:-1]  # Remove the trailing dot
            scope = NAMESPACES.get(namespace, namespace)
            remainder_prefix = prefix[len(qual):]
            break
    
    # If no qualifier found, default to player flags (None scope in var store)
    
    # Get the appropriate scope dict from the context
    ctx = interp.ctx
    if scope is None:
        # Player flags (bare names)
        store_dict = ctx.vars.player_flags
    else:
        # Scoped flags
        store_dict = ctx.vars.scopes.get(scope, {})
    
    # Iterate keys matching the prefix and build the numeric array
    result = []
    for key in sorted(store_dict.keys()):  # Sort for deterministic ordering
        if key.startswith(remainder_prefix):
            # Parse the remainder as a number
            remainder = key[len(remainder_prefix):]
            try:
                val = float(remainder)
            except ValueError:
                val = 0.0
            result.append(val)
    
    return result


_PURE = {
    # math
    "random": _f_random,
    "abs": _f1(abs),
    "int": _f1(lambda x: float(int(x))),       # truncate toward zero
    "sin": _f1(_sin),
    "cos": _f1(math.cos),
    "tan": _f1(math.tan),
    "arctan": _f1(math.atan),
    "exp": _f1(_safe(math.exp)),
    "log": _f1(_safe(math.log)),
    "sqrt": _f1(_safe(math.sqrt)),
    "max": lambda self, a: max((to_num(x) for x in a), default=0.0),
    "min": lambda self, a: min((to_num(x) for x in a), default=0.0),
    "getangle": lambda self, a: _getangle(a),
    "getdir": lambda self, a: _getdir(a),
    "vecx": lambda self, a: _VECX[int(math.floor(to_num(a[0]) if a else 0)) % 4],
    "vecy": lambda self, a: _VECY[int(math.floor(to_num(a[0]) if a else 0)) % 4],
    # strings
    "ascii": lambda self, a: _ascii(a),
    "keycode": lambda self, a: _keycode(a),
    "strlen": lambda self, a: float(len(to_str(a[0]))) if a else 0.0,
    "strtofloat": lambda self, a: to_num(a[0]) if a else 0.0,
    # GS1 string matching is CASE-INSENSITIVE (GServer-v2 uses equalsi/findi),
    # which the Bomber room states rely on ("Open"/"Join"/"Host" vs open/join).
    "strequals": lambda self, a: len(a) > 1 and to_str(a[0]).lower() == to_str(a[1]).lower(),
    "strcontains": lambda self, a: len(a) > 1 and to_str(a[1]).lower() in to_str(a[0]).lower(),
    # startswith(prefix, string) -- checks if STRING starts with PREFIX (note
    # the arg order: the prefix comes first, matching GS1Functions.cpp's own
    # doc comment and its `findi(str, prefix) == 0`, str=arguments[1]).
    "startswith": lambda self, a: len(a) > 1 and to_str(a[1]).lower().startswith(to_str(a[0]).lower()),
    # indexof(substring, str) -> position of substring in str (note arg order).
    # Unlike strequals/strcontains/startswith (equalsi/findi, case-insensitive
    # in GServer-v2), fn_indexof uses plain std::string::find — case-sensitive.
    "indexof": lambda self, a: float(to_str(a[1]).find(to_str(a[0]))) if len(a) > 1 else -1.0,
    "sarraylen": lambda self, a: float(to_str(a[0]).count(",") + 1) if a else 0.0,
    "lindexof": lambda self, a: _lindexof(a),
    # arrays
    "arraylen": lambda self, a: float(len(a[0])) if a and isinstance(a[0], (list, tuple)) else 0.0,
    "aindexof": lambda self, a: _aindexof(a),
    # crypto/password
    "base64encode": lambda self, a: _base64encode_impl(to_str(a[0]) if a else ""),
    "base64decode": lambda self, a: _base64decode_impl(to_str(a[0]) if a else ""),
    "passwordmatches": lambda self, a: (
        len(a) > 1 and isinstance(a[0], str) and isinstance(a[1], str)
        and a[0] == _base64_sha256(to_str(a[1]))
    ),
    "getflagkeys": lambda self, a: _getflagkeys(self, a),
}


# -- convenience entry points ----------------------------------------------
def run(source: str, host: Host = None, ctx: Context = None) -> Context:
    """Parse and execute a GS1 script; return the resulting context."""
    ctx = ctx or Context(host or MemoryHost())
    Interpreter(ctx).run(parse(source))
    return ctx


def run_event(source: str, event: str, host: Host = None, ctx: Context = None) -> Context:
    ctx = ctx or Context(host or MemoryHost())
    Interpreter(ctx).run_event(parse(source), event)
    return ctx


def run_event_resumable(source: str, event: str, host: Host = None,
                         ctx: Context = None) -> ResumableExecution:
    """Opt-in entry point: like run_event, but `sleep` suspends instead of
    breaking its loop; drive the returned ResumableExecution's .resume() to
    continue past each sleep. See ResumableExecution / Interpreter.__init__."""
    ctx = ctx or Context(host or MemoryHost())
    return Interpreter(ctx).run_event_resumable(parse(source), event)
