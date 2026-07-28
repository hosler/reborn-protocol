"""GS1 parser — recursive-descent port of GServer-v2's GS1Parser.g4.

Consumes the token list from lexer.tokenize() and produces an AST (ast.py).
Precedence and rule shapes follow the ANTLR grammar; statement-level
assignment vs. expression is resolved with backtracking (the grammar relies on
ANTLR trying assignmentStatement before expression, and '=' doubles as the
equality operator inside expressions).

See memory: gs1-python-port.
"""
from __future__ import annotations

from . import ast
from .lexer import tokenize, Token, EOF

# token type groups
ASSIGN_OPS = {"OP_ASSIGN", "OP_ASSIGN_ADD", "OP_ASSIGN_SUB", "OP_ASSIGN_MUL",
              "OP_ASSIGN_DIV", "OP_ASSIGN_MOD", "OP_ASSIGN_POW"}
EQUALITY_OPS = {"OP_EQUAL", "OP_ASSIGN", "OP_NOTEQ"}
RELATIONAL_OPS = {"OP_LESS", "OP_GREAT", "OP_LESS_EQ", "OP_GREAT_EQ"}
ADDITIVE_OPS = {"OP_ADD", "OP_SUB"}
# NB: OP_MOD is NOT in here — in real GS1, % binds TIGHTER than * and /
# (its own level below multiplicative, see parse_mod). Ground truth: Bomber
# Arena's lobby smoke NPC computes its 5x5 texture-grid columns as
# `400/16*this.i%5`, pairing it with `400/16*int(this.i/5)` for rows — which
# only yields a grid if that parses as (400/16)*(this.i%5). With C-style
# same-level parsing it's ((400/16)*i)%5 == 0 for every i, and all 24 tiles
# collapsed onto one off-screen column (verified live 2026-07-22: the smoke
# drew as a single 400px square with hard seams).
MULTIPLICATIVE_OPS = {"OP_MUL", "OP_DIV"}
UNARY_OPS = {"OP_ADD", "OP_SUB", "OP_LOGICALNOT"}
SPECIAL_LITS = {"ITEM", "CARRY", "DIRECTION", "GENDER", "COLOR", "BADDY"}
STMT_TERMINATORS = {"END", "EOF", "TOKEN_BRACE_RIGHT"}
STRING_PARTS = {"STRING", "MESSAGECODE", "RAWMESSAGECODE"}


class ParseError(Exception):
    def __init__(self, msg, tok: Token):
        super().__init__(f"{msg} (got {tok!r} at offset {tok.pos})")
        self.tok = tok


class Parser:
    def __init__(self, tokens: list[Token], recover: bool = True):
        self.toks = tokens
        self.i = 0
        self.recover = recover
        self.errors: list[ParseError] = []
        self._parsing_case_label = False

    def _synchronize(self):
        """Panic-mode recovery: skip to the next ';' (consumed) or '}'/EOF.

        A '{' reached before that terminator belongs to the statement that
        just failed, so the whole balanced block is skipped with it. Stopping
        at the ';' inside instead leaves the '{' dangling: every statement of
        the body is then re-parsed at the ENCLOSING level and runs
        unconditionally, and the block's own '}' surfaces as a second, bogus
        error. Live Bomber (bomblobby.nw NPCs 4 and 50, captured 2026-07-26)
        is the proof - both arrive with a stray '*' where the server's
        comment stripper ate the '/' of a '/*', and the old recovery promoted
        two deliberately disabled effect blocks (seteffect + 55 showimg + a
        self-rearming `timeout = 0.05`) to top level.
        """
        depth = 0
        while not self.at(EOF):
            t = self.next().type
            if t == "TOKEN_BRACE_LEFT":
                depth += 1
            elif t == "TOKEN_BRACE_RIGHT":
                if depth == 0:
                    self.i -= 1  # let the enclosing block consume it
                    return
                depth -= 1
                if depth == 0:
                    return
            elif t == "END" and depth == 0:
                return

    # -- token helpers -----------------------------------------------------
    def peek(self, k=0) -> Token:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else self.toks[-1]

    def at(self, *types) -> bool:
        return self.peek().type in types

    def next(self) -> Token:
        t = self.toks[self.i]
        if t.type != EOF:
            self.i += 1
        return t

    def eat(self, ttype) -> Token:
        if self.peek().type != ttype:
            raise ParseError(f"expected {ttype}", self.peek())
        return self.next()

    def accept(self, ttype):
        if self.peek().type == ttype:
            return self.next()
        return None

    # -- entry -------------------------------------------------------------
    def parse_program(self) -> ast.Program:
        body = []
        while not self.at(EOF):
            if self.accept("END"):
                continue
            if self.accept("TOKEN_BRACE_RIGHT"):
                # A stray unmatched '}' at TOP level closes nothing and shifts
                # nothing — skip it silently like the official client (live
                # GTA's -System3 ends in one extra '}'; erroring here dropped
                # no real statement but cried wolf every session). Mid-script
                # brace damage still surfaces: the '{'-half of a broken block
                # fails inside parse_block, which reports it.
                continue
            before = self.i
            try:
                body.append(self.parse_statement())
            except ParseError as e:
                if not self.recover:
                    raise
                self.errors.append(e)
                if self.i == before:  # ensure forward progress
                    self.next()
                self._synchronize()
        return ast.Program(body)

    # block: '{' statement* '}' | statement
    def parse_block(self):
        if self.at("TOKEN_BRACE_LEFT"):
            self.next()
            body = []
            while not self.at("TOKEN_BRACE_RIGHT", EOF):
                if self.accept("END"):
                    continue
                before = self.i
                try:
                    body.append(self.parse_statement())
                except ParseError as e:
                    if not self.recover:
                        raise
                    self.errors.append(e)
                    if self.i == before:
                        self.next()
                    self._synchronize()
            if not self.accept("TOKEN_BRACE_RIGHT") and not self.at(EOF):
                raise ParseError("expected TOKEN_BRACE_RIGHT", self.peek())
            return body  # unclosed block at EOF is tolerated (truncated source)
        return [self.parse_statement()]

    # -- statements --------------------------------------------------------
    def parse_statement(self):
        t = self.peek().type
        if t == "END":
            self.next()
            return ast.ExprStmt(None)
        if t == "TOKEN_BRACE_LEFT":
            # nested bare block (grammar disallows, but real scripts use it)
            return ast.Block(self.parse_block())
        if t == "KW_IF":
            return self.parse_if()
        if t == "KW_FOR":
            return self.parse_for()
        if t == "KW_WHILE":
            return self.parse_while()
        if t == "KW_WITH":
            return self.parse_with()
        if t == "KW_SWITCH":
            return self.parse_switch()
        if t == "KW_FUNCTION":
            return self.parse_funcdef()
        if t in ("KW_RETURN", "KW_BREAK", "KW_CONTINUE"):
            kind = {"KW_RETURN": "return", "KW_BREAK": "break",
                    "KW_CONTINUE": "continue"}[t]
            self.next()
            # tolerate the call-form `break();` / `continue();` — live GTA
            # (-magic/Effect, 14 statements) writes them GS2-style and the
            # official client accepts it
            if kind != "return" and self.at("TOKEN_PAREN_LEFT"):
                save = self.i
                self.next()
                if not self.accept("TOKEN_PAREN_RIGHT"):
                    self.i = save
            self._end_statement()
            return ast.Flow(kind)
        if t == "COMMAND":
            return self.parse_command()
        return self.parse_assignment_or_expr()

    def _end_statement(self):
        if self.at("END"):
            self.next()
        elif not self.at(EOF, "TOKEN_BRACE_RIGHT"):
            raise ParseError("expected ';' or end of statement", self.peek())

    def parse_if(self):
        self.eat("KW_IF")
        return self._parse_if_tail()

    def _parse_if_tail(self):
        """Condition + branches of an if, after KW_IF (or KW_ELSEIF) was
        consumed. Classic GS1 accepts the single-word `elseif` alongside
        `else if` (live GTA scripts use it); treating it as an unknown
        identifier left its `{` dangling and silently shifted every brace
        after it — whole top-level blocks then attached to the wrong if."""
        self.eat("TOKEN_PAREN_LEFT")
        cond = self.parse_expression()
        self.eat("TOKEN_PAREN_RIGHT")
        then = self.parse_block()
        els = None
        # Tolerate stray ';' between the then-branch and its else: live GTA
        # ships `if (c) stmt;; else stmt;` (weapon *Quiver's scroll arrows,
        # twice) and the official client accepts it — rejecting the else
        # dropped the whole branch. Restore when no else follows so the
        # terminator stays for the enclosing context.
        saved = self.i
        while self.accept("END"):
            pass
        if self.accept("KW_ELSEIF"):
            els = [self._parse_if_tail()]
        elif self.accept("KW_ELSE"):
            els = self.parse_block()
        else:
            self.i = saved
        return ast.If(cond, then, els)

    def parse_for(self):
        self.eat("KW_FOR")
        self.eat("TOKEN_PAREN_LEFT")
        init = None if self.at("END") else self.parse_assignment_or_expr(no_terminator=True)
        self.eat("END")
        cond = None if self.at("END") else self.parse_expression()
        self.eat("END")
        post = None if self.at("TOKEN_PAREN_RIGHT") else self.parse_assignment_or_expr(no_terminator=True)
        while self.accept("END"):  # tolerate stray trailing ';' before ')'
            pass
        self.eat("TOKEN_PAREN_RIGHT")
        body = self.parse_block()
        return ast.For(init, cond, post, body)

    def parse_while(self):
        self.eat("KW_WHILE")
        self.eat("TOKEN_PAREN_LEFT")
        cond = self.parse_expression()
        self.eat("TOKEN_PAREN_RIGHT")
        return ast.While(cond, self.parse_block())

    def parse_with(self):
        self.eat("KW_WITH")
        self.eat("TOKEN_PAREN_LEFT")
        obj = self.parse_expression()
        self.eat("TOKEN_PAREN_RIGHT")
        return ast.With(obj, self.parse_block())

    def parse_switch(self):
        self.eat("KW_SWITCH")
        self.eat("TOKEN_PAREN_LEFT")
        value = self.parse_expression()
        self.eat("TOKEN_PAREN_RIGHT")
        self.eat("TOKEN_BRACE_LEFT")
        cases = []
        default = None
        try:
            while not self.at("TOKEN_BRACE_RIGHT", EOF):
                if self.accept("END"):
                    continue
                if self._accept_word("case"):
                    self._parsing_case_label = True
                    try:
                        match = self.parse_expression()
                    finally:
                        self._parsing_case_label = False
                    self._case_colon()
                    body = self._parse_case_body()
                    cases.append((match, body))
                    continue
                if self._accept_word("default"):
                    self._case_colon()
                    default = self._parse_case_body()
                    continue
                raise ParseError("expected case or default", self.peek())
        except ParseError as error:
            if not self.recover:
                raise
            self.errors.append(error)
            self._discard_switch_body()
            return ast.Switch(value, [], None)
        self.eat("TOKEN_BRACE_RIGHT")
        return ast.Switch(value, cases, default)

    def _discard_switch_body(self):
        """Consume the rest of a malformed switch so its body cannot escape."""
        depth = 1
        while depth and not self.at(EOF):
            token = self.next()
            if token.type == "TOKEN_BRACE_LEFT":
                depth += 1
            elif token.type == "TOKEN_BRACE_RIGHT":
                depth -= 1

    def _case_colon(self):
        if self.at("TOKEN_COLON"):
            self.next()
            return
        if self.at("IDENTIFIER") and self.peek().text == ":":
            self.next()
            return
        raise ParseError("expected ':'", self.peek())

    def _parse_case_body(self):
        body = []
        while (not self.at("TOKEN_BRACE_RIGHT", EOF)
               and not self._at_word("case", "default")):
            if self.accept("END"):
                continue
            body.append(self.parse_statement())
        return body

    def _at_word(self, *words):
        return self.at("IDENTIFIER") and self.peek().text in words

    def _accept_word(self, word):
        if self._at_word(word):
            return self.next()
        return None

    def parse_funcdef(self):
        self.eat("KW_FUNCTION")
        name = self._read_identifier_name()
        self.eat("TOKEN_PAREN_LEFT")
        # Era-2006 new-GS1 parameter list: `function onActionBoomerang(dmg,
        # attackacct, attackguild)` (see ast.FuncDef.params). Rejecting it
        # killed the WHOLE script — era's -System/-MoveSystem/-BulletSystem
        # clientside were 100% dead ("expected ')'" at the first param).
        params = []
        if not self.at("TOKEN_PAREN_RIGHT"):
            params.append(self._read_parameter_name())
            while self.accept("TOKEN_COMMA"):
                params.append(self._read_parameter_name())
        self.eat("TOKEN_PAREN_RIGHT")
        return ast.FuncDef(name, self.parse_block(), params)

    def _read_parameter_name(self):
        ref = self.parse_identifier_access()
        if any(part.index or not part.name for part in ref.parts):
            raise ParseError("expected parameter name", self.peek())
        return ".".join(part.name for part in ref.parts)

    # builtinCommandStatement: COMMAND (arg (',' arg)*)?
    def parse_command(self):
        name = self.next().text
        args = []
        if not self.at(*STMT_TERMINATORS):
            args.append(self._command_arg_or_empty())
            while self.accept("TOKEN_COMMA"):
                args.append(self._command_arg_or_empty())
        self._end_statement()
        return ast.Command(name, args)

    def _command_arg_or_empty(self):
        # An omitted positional arg (leading/consecutive/trailing comma, e.g.
        # `shoot ,,,,,,blank,`) is an empty string in GS1, not a parse error.
        if self.at("TOKEN_COMMA") or self.at(*STMT_TERMINATORS):
            return ast.Str("")
        return self.parse_command_arg()

    def parse_command_arg(self):
        if self.peek().type in SPECIAL_LITS:
            t = self.next()
            return ast.SpecialLit(t.type, t.text)
        return self.parse_expression()

    def parse_assignment_or_expr(self, no_terminator=False):
        # try: identifier_access assignment_operator expression
        mark = self.i
        target = self._try_identifier_access()
        if target is not None and self.peek().type in ASSIGN_OPS:
            op = self.next().text
            # era new-GS1 chained assignment: `atm = grabdisabled = onphone
            # = false;` (-System onCreated). Collect the intermediate
            # targets; everything gets the right-most value.
            extra_targets = []
            while op == "=":
                mark2 = self.i
                t2 = self._try_identifier_access()
                if t2 is not None and self.peek().type == "OP_ASSIGN":
                    self.next()
                    extra_targets.append(t2)
                    continue
                self.i = mark2
                break
            value = self.parse_expression()
            for t2 in reversed(extra_targets):
                value = ast.Assign(t2, "=", value)
            node = ast.Assign(target, op, value)
            if not no_terminator:
                # An array-literal assignment is self-terminating at its '}',
                # like a block — live GTA's -BarrelRide writes
                # `this.x={1,0,...}` with no ';' (six of them) and the
                # official client accepts it.
                if isinstance(value, ast.ArrayLit) and not self.at("END"):
                    return node
                self._end_statement()
            return node
        # userFunctionStatement: name '(' arg-list? ')'
        # (era new-GS1 passes arguments: `CustomScript(dmg, acct);` — the
        # zero-arg-only form dropped every such statement, the
        # "got TOKEN_PAREN_LEFT" parse-recovery signature on era's
        # -ItemSystem/-DayNight/-HPControls.)
        self.i = mark
        if (self.at("IDENTIFIER") and self.peek().text
                and self.peek(1).type == "TOKEN_PAREN_LEFT"):
            name = self.next().text
            self.next()
            args = []
            ok = True
            try:
                if not self.at("TOKEN_PAREN_RIGHT"):
                    args.append(self._paren_arg_or_empty())
                    while self.accept("TOKEN_COMMA"):
                        args.append(self._paren_arg_or_empty())
                self.eat("TOKEN_PAREN_RIGHT")
            except ParseError:
                ok = False
            # Commit only when the call IS the whole statement; anything
            # else (`foo(1)+2;`) falls back to the expression path below.
            if ok and self.at("END", EOF, "TOKEN_BRACE_RIGHT"):
                node = ast.UserCall(name, args)
                if not no_terminator:
                    self._end_statement()
                return node
            self.i = mark
        # otherwise: expression statement
        self.i = mark
        expr = self.parse_expression()
        # tolerate top-level comma lists ('a, b;') by keeping the last expr
        while self.accept("TOKEN_COMMA"):
            expr = self.parse_expression()
        node = ast.ExprStmt(expr)
        if not no_terminator:
            self._end_statement()
        return node

    # =====================================================================
    # Expressions (precedence climbing, mirrors the grammar)
    # =====================================================================
    def parse_expression(self):
        node = self.parse_logic_or()
        while self.accept("TOKEN_QUESTION"):
            a = self.parse_expression()
            self.eat("TOKEN_COLON")
            b = self.parse_expression()
            node = ast.Ternary(node, a, b)
        return node

    def parse_logic_or(self):
        node = self.parse_logic_and()
        while self.at("OP_LOGICALOR"):
            self.next()
            node = ast.BinOp("||", node, self.parse_logic_and())
        return node

    def parse_logic_and(self):
        node = self.parse_equality()
        while self.at("OP_LOGICALAND"):
            self.next()
            node = ast.BinOp("&&", node, self.parse_equality())
        return node

    def parse_equality(self):
        node = self.parse_relational()
        if self.peek().type in EQUALITY_OPS:
            op = self.next().text
            node = ast.BinOp(op, node, self.parse_relational())
        return node

    def parse_relational(self):
        node = self.parse_concat()
        if self.peek().type in RELATIONAL_OPS:
            op = self.next().text
            node = ast.BinOp(op, node, self.parse_concat())
        return node

    def parse_concat(self):
        # era new-GS1 borrows GS2's `@` string concat (`"$" @ player.rupees`,
        # -System). Binds looser than arithmetic, tighter than comparison.
        node = self.parse_additive()
        while self.peek().type == "OP_CONCAT":
            self.next()
            node = ast.BinOp("@", node, self.parse_additive())
        return node

    def parse_additive(self):
        node = self.parse_multiplicative()
        while self.peek().type in ADDITIVE_OPS:
            op = self.next().text
            node = ast.BinOp(op, node, self.parse_multiplicative())
        return node

    def parse_multiplicative(self):
        node = self.parse_mod()
        while self.peek().type in MULTIPLICATIVE_OPS:
            op = self.next().text
            node = ast.BinOp(op, node, self.parse_mod())
        return node

    def parse_mod(self):
        # % sits between multiplicative and 'in': `a*b%c` == a*(b%c) in real
        # GS1 (see the MULTIPLICATIVE_OPS comment at the top of the file).
        node = self.parse_in()
        while self.peek().type == "OP_MOD":
            self.next()
            node = ast.BinOp("%", node, self.parse_in())
        return node

    def parse_in(self):
        # inExpression: exponentiationExpression
        #   ((TOKEN_COMMA exponentiationExpression)* OP_IN (range_literal | primaryExpression))?
        # The comma-separated list ("2,3 in |1,4|" -- ALL values must be in
        # range) only belongs to us if an 'in' eventually follows; otherwise
        # those commas are someone else's (a command/array-literal separator),
        # so back out and let the caller see them.
        node = self.parse_exponent()
        mark = self.i
        values = [node]
        try:
            while self.accept("TOKEN_COMMA"):
                values.append(self.parse_exponent())
        except ParseError:
            # Not an 'in' list: the speculation ran off the end of whatever
            # those commas really belonged to. An array literal with an empty
            # slot (live Bomber room0.nw's furniture catalog writes
            # `this.off={,-1,};`) ends `,}`, so parse_exponent lands on the
            # '}'. Letting that escape put the whole statement into panic
            # recovery, which backs onto the '}' and hands it to parse_block as
            # the ENCLOSING block's brace -- truncating the if-body and
            # re-parsing its remainder at top level, unconditionally. Rewind
            # like any other failed speculation.
            self.i = mark
            return node
        if self.at("OP_IN"):
            self.next()
            if self.at("TOKEN_PIPE", "OP_LESS"):
                return ast.InExpr(values, self.parse_range())
            return ast.InExpr(values, self.parse_primary())
        self.i = mark
        return node

    def parse_range(self):
        open_tok = self.next()  # '|' or '<'
        lo_incl = open_tok.type == "TOKEN_PIPE"
        # Bounds parse BELOW the relational level: an exclusive upper bound's
        # closing '>' (`y in |a,b>` — live GTA's *Clock settings panel) must
        # close the range, not turn `b > )` into a comparison that then fails
        # and silently drops the whole statement (everything after it in the
        # function leaked to top level and ran unconditionally).
        lo = self.parse_additive()
        self.eat("TOKEN_COMMA")
        hi = self.parse_additive()
        if self.accept("TOKEN_PIPE"):
            hi_incl = True
        elif self.accept("OP_GREAT"):
            hi_incl = False
        else:
            raise ParseError("expected '|' or '>' to close range", self.peek())
        return ast.RangeLit(lo, hi, lo_incl, hi_incl)

    def parse_exponent(self):
        node = self.parse_unary()
        while self.at("OP_POW"):
            self.next()
            node = ast.BinOp("^", node, self.parse_unary())
        return node

    def parse_unary(self):
        if self.peek().type in UNARY_OPS:
            op = self.next().text
            return ast.UnaryOp(op, self.parse_unary())
        return self.parse_postfix()

    def parse_postfix(self):
        node = self.parse_primary()
        if self.at("OP_INC", "OP_DEC"):
            op = self.next().text
            node = ast.Postfix(op, node)
        return node

    def parse_primary(self):
        t = self.peek()
        tt = t.type
        if tt == "TOKEN_PAREN_LEFT":
            self.next()
            node = self.parse_expression()
            # tolerate comma-operator lists in parens, '(a, b)' -> keep last
            while self.accept("TOKEN_COMMA"):
                node = self.parse_expression()
            self.eat("TOKEN_PAREN_RIGHT")
            return node
        if tt in STRING_PARTS:
            return self.parse_string_concat()
        if tt == "FUNCTION":
            return self.parse_builtin_function()
        if tt == "TOKEN_BRACE_LEFT":
            return self.parse_array_literal()
        if tt == "LITERAL":
            self.next()
            return self._literal(t.text)
        if tt in ("ALLFEATURES", "ALLSTATS"):
            self.next()
            return ast.SpecialLit(tt, t.text)
        if tt == "IDENTIFIER":
            node = self.parse_identifier_access()
            # user-function call value: name '(' arg-list? ')' (args are
            # era new-GS1 — see parse_funcdef; backtracks on any failure so
            # a bare `name` followed by parenthesized text stays a VarRef).
            # A DOTTED ref followed by '(' is a METHOD call (era GS2-ism:
            # clientr.jail.tokenize(), client.message.add(x)); the last path
            # part is the method, the rest the base ref.
            if (isinstance(node, ast.VarRef)
                    and not node.parts[-1].index
                    and node.parts[-1].name
                    and self.at("TOKEN_PAREN_LEFT")):
                save = self.i
                self.next()
                args = []
                try:
                    if not self.at("TOKEN_PAREN_RIGHT"):
                        args.append(self._paren_arg_or_empty())
                        while self.accept("TOKEN_COMMA"):
                            args.append(self._paren_arg_or_empty())
                    self.eat("TOKEN_PAREN_RIGHT")
                    if len(node.parts) == 1:
                        node = ast.Call(node.parts[0].name, args)
                    else:
                        node = ast.MethodCall(
                            ast.VarRef(node.parts[:-1]),
                            node.parts[-1].name, args)
                    node = self._parse_method_chain(node)
                    return node
                except ParseError:
                    self.i = save
            return node
        raise ParseError("expected expression", t)

    def parse_string_concat(self):
        parts = []
        while self.peek().type in STRING_PARTS:
            t = self.peek()
            if t.type == "STRING":
                self.next()
                parts.append(ast.Str(t.text))
            elif t.type == "RAWMESSAGECODE":
                self.next()  # synthetic marker, no value
            else:  # MESSAGECODE
                parts.append(self.parse_messagecode())
        return ast.StrConcat(parts)

    def _parse_method_chain(self, node):
        """`.method(args)` chain on a call result (era GS2-ism:
        findWeaponNPC("-System").showFloat("*Cured!")). Backtracks if the
        dot isn't followed by a call."""
        while (self.at("TOKEN_PERIOD")
               and self.peek(1).type == "IDENTIFIER"
               and self.peek(2).type == "TOKEN_PAREN_LEFT"):
            save = self.i
            self.next()
            name = self.next().text
            self.next()
            args = []
            try:
                if not self.at("TOKEN_PAREN_RIGHT"):
                    args.append(self._paren_arg_or_empty())
                    while self.accept("TOKEN_COMMA"):
                        args.append(self._paren_arg_or_empty())
                self.eat("TOKEN_PAREN_RIGHT")
            except ParseError:
                self.i = save
                break
            node = ast.MethodCall(node, name, args)
        return node

    def _paren_arg_or_empty(self):
        # An omitted arg (e.g. strequals(a,) or #e(0,x,,)) is an empty string,
        # not a parse error. Needed because the lexer's mode stack doesn't
        # always emit a string token for an empty arg after a nested
        # messagecode-with-parens (#P1(-1)).
        if self.at("TOKEN_COMMA", "TOKEN_PAREN_RIGHT"):
            return ast.Str("")
        return self.parse_expression()

    def parse_messagecode(self):
        t = self.eat("MESSAGECODE")
        args = []
        if self.accept("TOKEN_PAREN_LEFT"):
            if not self.at("TOKEN_PAREN_RIGHT"):
                args.append(self._paren_arg_or_empty())
                while self.accept("TOKEN_COMMA"):
                    args.append(self._paren_arg_or_empty())
            self.eat("TOKEN_PAREN_RIGHT")
        return ast.MessageCode(t.text, args)

    def parse_builtin_function(self):
        name = self.next().text
        self.eat("TOKEN_PAREN_LEFT")
        args = []
        if not self.at("TOKEN_PAREN_RIGHT"):
            args.append(self._paren_arg_or_empty())
            while self.accept("TOKEN_COMMA"):
                args.append(self._paren_arg_or_empty())
        self.eat("TOKEN_PAREN_RIGHT")
        return ast.Call(name, args)

    def parse_array_literal(self):
        self.eat("TOKEN_BRACE_LEFT")
        elements = []
        while not self.at("TOKEN_BRACE_RIGHT", EOF):
            if self.accept("TOKEN_COMMA"):
                continue
            if self.accept("END"):
                continue
            elements.append(self.parse_expression())
            self.accept("TOKEN_COMMA")
        self.eat("TOKEN_BRACE_RIGHT")
        return ast.ArrayLit(elements)

    # identifier_access: name ('.' name)* with optional [index]
    def parse_identifier_access(self):
        parts = [self._read_path_part()]
        while self.at("TOKEN_PERIOD"):
            self.next()
            if self.accept("TOKEN_PAREN_LEFT"):
                expr = self.parse_expression()
                self.eat("TOKEN_PAREN_RIGHT")
                part = ast.PathPart("", [], [expr])
                if self.accept("TOKEN_BRACKET_LEFT"):
                    part.index.append(self.parse_expression())
                    if self.accept("TOKEN_COMMA"):
                        part.index.append(self.parse_expression())
                    self.eat("TOKEN_BRACKET_RIGHT")
                parts.append(part)
            else:
                parts.append(self._read_path_part())
        return ast.VarRef(parts)

    def _try_identifier_access(self):
        if not self.at("IDENTIFIER"):
            return None
        try:
            return self.parse_identifier_access()
        except ParseError:
            return None

    def _read_path_part(self):
        # compound_identifier: (IDENTIFIER | messagecode_string | REAL)+ ; a
        # segment may be dynamic, e.g. this.#v(this.a) -> this.<var>
        atoms = []
        static = ""
        dynamic = False
        while True:
            t = self.peek()
            if t.type == "IDENTIFIER":
                if self._parsing_case_label and t.text == ":":
                    break
                self.next()
                if t.text:
                    atoms.append(ast.Str(t.text))
                    static += t.text
            elif t.type == "LITERAL":
                self.next()
                atoms.append(ast.Str(t.text))
                static += t.text
            elif t.type == "MESSAGECODE":
                atoms.append(self.parse_messagecode())
                dynamic = True
            else:
                break
        index = []
        if self.accept("TOKEN_BRACKET_LEFT"):
            index.append(self.parse_expression())
            if self.accept("TOKEN_COMMA"):
                index.append(self.parse_expression())
            self.eat("TOKEN_BRACKET_RIGHT")
        return ast.PathPart("" if dynamic else static, index, atoms)

    def _read_identifier_name(self):
        """Static identifier name only (used by function definitions)."""
        return self._read_path_part().name

    def _literal(self, text):
        low = text.lower()
        if low == "true":
            return ast.Bool(True)
        if low == "false":
            return ast.Bool(False)
        try:
            if low.startswith("0x"):
                return ast.Num(float(int(text, 16)))
            return ast.Num(float(text))
        except ValueError:
            return ast.Num(0.0)


def parse(text: str) -> ast.Program:
    """Lex + parse a GS1 script into a Program AST."""
    return Parser(tokenize(text)).parse_program()


def parse_tokens(tokens: list[Token]) -> ast.Program:
    return Parser(tokens).parse_program()
