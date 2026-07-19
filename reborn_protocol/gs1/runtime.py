"""GS1 runtime: variable store, host interface, execution context.

The interpreter (interp.py) owns control flow, expression evaluation and the
flag/var stores. Everything that touches actual game state — built-in player/NPC
attributes (playerx, hearts, ...), arrays (players[], npcs[]), message codes
that need player context, and side-effecting commands (say, setimg, hide, ...) —
goes through a Host. pygserver supplies a real Host in Phase 5; MemoryHost lets
us execute and test scripts standalone now.

Variable model (from GS1Variables.h): namespaced flags/vars —
  this. / thiso. (npc)   temp. (per-run)   local. (npc-side)
  client. / clientr. (client flags)   server. / serverr. (server flags)
  level. (level)   global. (server, unsaved)   bare name -> player flag/attr
See memory: gs1-python-port.
"""
from __future__ import annotations

import math

from .values import to_num, to_str, to_bool

# recognised namespace prefixes -> internal scope key
NAMESPACES = {
    "this": "this", "thiso": "thiso", "temp": "temp", "local": "local",
    "client": "client", "clientr": "client", "server": "server",
    "serverr": "server", "level": "level", "global": "global",
}

# Reserved constants (GS1Lexer.g4 RESERVEDCONSTANTS / GS1Visitor.cpp
# visitLiteral, commit 66813d8b): a bare, unscoped identifier matching one of
# these names is ALWAYS the constant, never a flag/var lookup — GServer's
# ANTLR grammar lexes 'pi'/'allstats'/'allfeatures' as a dedicated
# RESERVEDCONSTANTS token (case-sensitive, lowercase only) that the parser
# resolves via primaryExpression's `literal_literal` alternative, which is
# tried before `identifier_access`; a *scoped* reference (this.pi) is
# unaffected, since that always requires the multi-token compound_identifier
# path. Assigning to a bare reserved name is rejected upstream
# (visitCompoundIdentifier throws "reserved keyword"); this port just ignores
# the write (see interp.py set_ref) rather than raising, matching this
# module's general lenient-script style.
RESERVED_CONSTANTS = {
    "pi": math.pi,
    "allstats": float(0xFFFF),
    "allfeatures": float(0xFFFF),
}

UNSET = object()  # sentinel: variable does not exist


# -- control-flow signals ---------------------------------------------------
class BreakSignal(Exception):
    pass


class ContinueSignal(Exception):
    pass


class ReturnSignal(Exception):
    pass


class Host:
    """Interface the interpreter calls for game state and side effects.

    All methods have safe no-op defaults so a partial host still runs scripts.
    """

    def get_builtin(self, name, indices, ctx):
        """Return a built-in player/NPC/level attribute or array element.

        Return runtime.UNSET if this host does not know the name (the
        interpreter then falls back to a plain player variable)."""
        return UNSET

    def set_builtin(self, name, value, indices, ctx) -> bool:
        """Try to set a built-in attribute. Return True if handled."""
        return False

    def call_command(self, name, args, ctx) -> None:
        """Perform a side-effecting command (say, setimg, hide, ...)."""

    def call_function(self, name, args, ctx):
        """Evaluate a built-in function not handled by the interpreter core.

        Return runtime.UNSET if unknown (interpreter yields 0)."""
        return UNSET

    def message_code(self, code, args, ctx) -> str:
        """Expand a message code (e.g. #a account name) to a string."""
        return ""

    def weapon_message_code(self, code, index, ctx) -> str:
        """Expand #W/#w; ``index`` is None for the selected weapon."""
        return ""


class MemoryHost(Host):
    """In-memory host for standalone execution and tests.

    Built-ins live in a flat dict; commands are recorded in `log`; a few common
    functions (onwall/getnpc/...) return benign defaults.
    """

    def __init__(self, attrs=None):
        self.attrs = dict(attrs or {})
        self.log = []  # list of (command_name, [arg values])

    def get_builtin(self, name, indices, ctx):
        if name in self.attrs:
            v = self.attrs[name]
            if indices and isinstance(v, (list, tuple)):
                i = int(to_num(indices[0]))
                return v[i] if 0 <= i < len(v) else 0.0
            return v
        return UNSET

    def set_builtin(self, name, value, indices, ctx) -> bool:
        # only handle names declared as built-ins (pre-seeded); everything else
        # falls through so the interpreter stores it as a plain player var
        if name not in self.attrs:
            return False
        if indices:
            i = int(to_num(indices[0]))
            if i < 0:
                return True
            arr = self.attrs[name]
            if isinstance(arr, list):
                while len(arr) <= i:
                    arr.append(0.0)
                arr[i] = value
                return True
        self.attrs[name] = value
        return True

    def call_command(self, name, args, ctx) -> None:
        self.log.append((name, list(args)))

    def call_function(self, name, args, ctx):
        return UNSET

    def message_code(self, code, args, ctx) -> str:
        return ""


_SCOPE_KEYS = ("this", "thiso", "temp", "local", "client", "server",
               "level", "global")


class VarStore:
    """Per-scope flag/var storage for one script execution.

    `scopes` and `player_flags` may be supplied as external backing dicts so
    state persists in the right place: NPC-owned dicts for this/thiso/local,
    the player's flag dict for bare names, server/level dicts for server/level,
    etc. Anything not supplied gets a fresh in-memory dict.
    """

    def __init__(self, scopes=None, player_flags=None):
        self.scopes = scopes if scopes is not None else {}
        for k in _SCOPE_KEYS:
            self.scopes.setdefault(k, {})
        self.player_flags = player_flags if player_flags is not None else {}

    def get(self, scope, key, index=None):
        table = self.scopes.get(scope, self.player_flags) if scope else self.player_flags
        v = table.get(key, UNSET)
        if index is not None:
            # Indexed array access (GameVariable::get<double>(index) / the
            # negative-index special case in GS1Visitor::visitIdentifierValue,
            # GS1Visitor.cpp): a NEGATIVE index never touches storage at all --
            # it returns a fresh detached 0.0, even if the array (or variable)
            # doesn't exist. A non-negative index into a missing/empty/non-array
            # value also yields 0.0 (nothing to clamp into). Otherwise an
            # out-of-bounds positive index CLAMPS to index 0 -- it does NOT
            # grow the array (GS1 arrays are fixed-size: only `setarray`/an
            # array literal resizes them).
            i = int(index)
            if i < 0:
                return 0.0
            if v is UNSET or not isinstance(v, list) or not v:
                return 0.0
            if i >= len(v):
                i = 0
            return v[i]
        if v is UNSET:
            return UNSET
        return v

    def set(self, scope, key, value, index=None):
        table = self.scopes.get(scope, self.player_flags) if scope else self.player_flags
        if index is not None:
            # Mirrors `get` above: negative, or into a missing/empty/non-array
            # value, is a silent no-op; positive-OOB clamps to index 0 rather
            # than growing the array.
            i = int(index)
            if i < 0:
                return
            arr = table.get(key)
            if not isinstance(arr, list) or not arr:
                return
            if i >= len(arr):
                i = 0
            arr[i] = value
        else:
            table[key] = value

    def unset(self, scope, key):
        table = self.scopes.get(scope, self.player_flags) if scope else self.player_flags
        table.pop(key, None)


class Context:
    """Execution context for one NPC/script run."""

    def __init__(self, host: Host, vars: VarStore = None, this_obj=None,
                 player=None, functions=None):
        self.host = host
        self.vars = vars or VarStore()
        self.this_obj = this_obj      # the NPC ("this") handle, host-defined
        self.player = player          # the acting player handle, host-defined
        self.functions = functions or {}  # name -> FuncDef (user functions)
        self.active_event = None      # the firing event name (its flag reads 1)
        self.tokenize_tokens = []     # set by `tokenize`, read by #t(i)
        self.tokens_count = 0         # temporary per-event `tokenscount`
        self.steps = 0                # statement budget guard (infinite loops)
        self.max_steps = 200_000
        # While a setcharprop/setplayerprop VALUE argument is being evaluated
        # this is "npc"/"player" — the command's own target, which is what a
        # bare context-sensitive message code (#C0..#C7) resolves against.
        # Mirrors the pushSource in GServer's processBuiltInCommand
        # (GS1Commands.cpp:430-453). None everywhere else.
        self.charprop_source = None
        # Set by a plain (non-compound) `timeout = x` assignment (see
        # Interpreter._st_Assign); mirrors GS1Visitor's
        # `m_sleepCallStack.clear()` on the same idiom (GS1Visitor.cpp
        # visitStatementAssignment, npcprogramming.doc 5.4) -- reassigning the
        # NPC's timer erases any resumable sleep left pending by a DIFFERENT,
        # already-suspended execution sharing this ctx. A resumable execution
        # checks this flag when the host calls resume() (interp.ResumableExecution);
        # it is *not* checked mid-flight, so a script's own `timeout = x;
        # sleep 1;` doesn't cancel the sleep it's about to register itself
        # (upstream's own clear is a no-op there too -- by the time a script
        # resumes, the stack it might clear was already emptied by the resume
        # itself). Irrelevant/no-op for non-resumable (sync) execution.
        self.sleep_cancelled = False
