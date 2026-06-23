#!/usr/bin/env python3
"""
ircii-dsl v0.1 — ircii-shaped scripting language in Python.
Strings only. TCL+Rexx had a baby. Turing complete.

Usage:
    python ircii_dsl.py              # interactive REPL
    python ircii_dsl.py script.irc   # run script file

Commands:  ASSIGN SET ECHO EVAL EXEC IF ELIF ELSE WHILE FE FEC FOREACH
           ALIAS ON TRIGGER EXPR TIMER SOURCE BREAK CONTINUE RETURN
           LEN SUBSTR CAT WORD NW STRIP TOUPPER TOLOWER MID
           EQ NEQ GT LT LE GE AND OR NOT IIF
           SLEEP DUMP DUMPA DUMPE NOP TYPE TRUE FALSE

Syntax:    $var ${var}   variable interpolation
           {block}       deferred code (raw, no interp until eval)
           [expr]        inline subexpression
           # comment     line comment
           ;             statement separator (also newline)
"""

import sys
import re
import time
import subprocess
from collections import OrderedDict


# ═══════════════════════════════════════════════════════════
#  TOKEN
# ═══════════════════════════════════════════════════════════

class Token:
    """Single lexer token."""
    __slots__ = ('type', 'value', 'line')

    def __init__(self, type_, value, line=0):
        self.type = type_
        self.value = value
        self.line = line

    def __repr__(self):
        return f'Token({self.type}, {self.value!r})'


# ═══════════════════════════════════════════════════════════
#  LEXER
# ═══════════════════════════════════════════════════════════

class Lexer:
    """
    Tokenize ircii-shaped text.

    Token types:
        WORD      bare word (command name, argument, operator)
        STRING    "quoted string" with $var interpolation
        VAR       $var or ${var} reference
        BLOCK     {raw block} — no interpolation, deferred eval
        SUBEXPR   [inline expression] — evaluated at resolve
        NL        newline (statement separator)
        SC        semicolon (statement separator)
        EOF       end of input
    """

    def __init__(self, text):
        self.text = text
        self.pos = 0
        self.line = 1

    def peek(self):
        return self.text[self.pos] if self.pos < len(self.text) else None

    def advance(self):
        ch = self.text[self.pos]
        self.pos += 1
        if ch == '\n':
            self.line += 1
        return ch

    def tokenize(self):
        tokens = []
        while self.pos < len(self.text):
            ch = self.peek()
            if ch in ' \t\r':
                self.advance()
            elif ch == '\n':
                tokens.append(Token('NL', '\n', self.line))
                self.advance()
            elif ch == ';':
                tokens.append(Token('SC', ';', self.line))
                self.advance()
            elif ch == '#':
                self._skip_comment()
            elif ch == '{':
                tokens.append(self._read_block())
            elif ch == '"':
                tokens.append(self._read_string())
            elif ch == '$':
                tokens.append(self._read_variable())
            elif ch == '[':
                tokens.append(self._read_subexpr())
            elif ch == '\\':
                self.advance()
                escaped = self.advance() if self.pos < len(self.text) else ''
                tokens.append(Token('WORD', escaped, self.line))
            else:
                tokens.append(self._read_word())
        tokens.append(Token('EOF', '', self.line))
        return tokens

    def _skip_comment(self):
        while self.pos < len(self.text) and self.text[self.pos] != '\n':
            self.advance()

    def _read_block(self):
        """Read {block} — raw text, no interpolation, handles nesting."""
        ln = self.line
        self.advance()  # skip {
        depth = 1
        buf = []
        while self.pos < len(self.text) and depth > 0:
            ch = self.advance()
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    break
            buf.append(ch)
        return Token('BLOCK', ''.join(buf), ln)

    def _read_string(self):
        """Read "string" — handles \\escapes, raw $var kept for interp."""
        ln = self.line
        self.advance()  # skip opening "
        buf = []
        escapes = {'n': '\n', 't': '\t', '\\': '\\', '"': '"'}
        while self.pos < len(self.text):
            ch = self.advance()
            if ch == '"':
                break
            if ch == '\\' and self.pos < len(self.text):
                nxt = self.advance()
                buf.append(escapes.get(nxt, nxt))
            else:
                buf.append(ch)
        return Token('STRING', ''.join(buf), ln)

    def _read_variable(self):
        """Read $var or ${var}."""
        ln = self.line
        self.advance()  # skip $
        if self.peek() == '{':
            self.advance()  # skip {
            buf = []
            while self.pos < len(self.text) and self.text[self.pos] != '}':
                buf.append(self.advance())
            if self.peek() == '}':
                self.advance()
            return Token('VAR', ''.join(buf), ln)
        buf = []
        while self.pos < len(self.text) and (self.text[self.pos].isalnum() or self.text[self.pos] == '_'):
            buf.append(self.advance())
        return Token('VAR', ''.join(buf), ln)

    def _read_subexpr(self):
        """Read [expr] — handles nesting."""
        ln = self.line
        self.advance()  # skip [
        depth = 1
        buf = []
        while self.pos < len(self.text) and depth > 0:
            ch = self.advance()
            if ch == '[':
                depth += 1
            elif ch == ']':
                depth -= 1
                if depth == 0:
                    break
            buf.append(ch)
        return Token('SUBEXPR', ''.join(buf), ln)

    def _read_word(self):
        """Read bare word — stops at whitespace and special chars."""
        ln = self.line
        stop_chars = ' \t\n\r{}"$;[]\\#'
        buf = []
        while self.pos < len(self.text) and self.text[self.pos] not in stop_chars:
            buf.append(self.advance())
        return Token('WORD', ''.join(buf), ln)


# ═══════════════════════════════════════════════════════════
#  AST NODES
# ═══════════════════════════════════════════════════════════

class Lit:
    """Literal string value (from WORD or STRING token)."""
    __slots__ = ('value',)
    def __init__(self, value):
        self.value = value
    def __repr__(self):
        return f'Lit({self.value!r})'


class Var:
    """Variable reference ($name)."""
    __slots__ = ('name',)
    def __init__(self, name):
        self.name = name
    def __repr__(self):
        return f'${self.name}'


class Blk:
    """Raw code block {text} — deferred evaluation."""
    __slots__ = ('text',)
    def __init__(self, text):
        self.text = text
    def __repr__(self):
        return f'{{{self.text}}}'


class Sub:
    """Inline subexpression [text] — evaluated at resolve."""
    __slots__ = ('text',)
    def __init__(self, text):
        self.text = text
    def __repr__(self):
        return f'[{self.text}]'


class Cmd:
    """Command: name + arg list."""
    __slots__ = ('name', 'args', 'line')
    def __init__(self, name, args, line=0):
        self.name = name
        self.args = args
        self.line = line
    def __repr__(self):
        return f'{self.name}({", ".join(repr(a) for a in self.args)})'


# ═══════════════════════════════════════════════════════════
#  PARSER
# ═══════════════════════════════════════════════════════════

class Parser:
    """Parse token stream into list of Cmd nodes.

    Grammar (per line):
        command := WORD arg*
        arg     := WORD | STRING | VAR | BLOCK | SUBEXPR

    Lines separated by NL or SC. Commands are first-word-of-line.
    """

    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos]

    def advance(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def skip_sep(self):
        while self.pos < len(self.tokens) and self.peek().type in ('NL', 'SC'):
            self.advance()

    def parse(self):
        commands = []
        self.skip_sep()
        while self.pos < len(self.tokens) and self.peek().type != 'EOF':
            cmd = self._parse_command()
            if cmd:
                commands.append(cmd)
            self.skip_sep()
        return commands

    def _parse_command(self):
        self.skip_sep()
        if self.peek().type == 'EOF':
            return None
        if self.peek().type != 'WORD':
            self.advance()  # skip unexpected token
            return None

        tok = self.advance()
        name = tok.value
        line = tok.line
        args = []

        while self.pos < len(self.tokens) and self.peek().type not in ('NL', 'SC', 'EOF'):
            tok = self.peek()
            if tok.type == 'WORD':
                args.append(Lit(self.advance().value))
            elif tok.type == 'STRING':
                args.append(Lit(self.advance().value))
            elif tok.type == 'VAR':
                args.append(Var(self.advance().value))
            elif tok.type == 'BLOCK':
                args.append(Blk(self.advance().value))
            elif tok.type == 'SUBEXPR':
                args.append(Sub(self.advance().value))
            else:
                break

        return Cmd(name.upper(), args, line)


# ═══════════════════════════════════════════════════════════
#  ENVIRONMENT
# ═══════════════════════════════════════════════════════════

class Env:
    """Variable scope with parent chain."""

    def __init__(self, parent=None):
        self.data = OrderedDict()
        self.parent = parent

    def get(self, key):
        if key in self.data:
            return self.data[key]
        if self.parent:
            return self.parent.get(key)
        return ''

    def put(self, key, value):
        self.data[key] = str(value)

    def has(self, key):
        if key in self.data:
            return True
        return self.parent.has(key) if self.parent else False


# ═══════════════════════════════════════════════════════════
#  CONTROL FLOW SIGNALS
# ═══════════════════════════════════════════════════════════

class Signal(Exception):
    pass

class Brk(Signal):
    """BREAK — exit innermost loop."""
    pass

class Cont(Signal):
    """CONTINUE — next iteration of innermost loop."""
    pass

class Ret(Signal):
    """RETURN — exit current alias/script with value."""
    def __init__(self, value=''):
        self.value = value


# ═══════════════════════════════════════════════════════════
#  INTERPRETER
# ═══════════════════════════════════════════════════════════

class Ircii:
    """ircii-DSL interpreter. Strings only. Everything is text."""

    def __init__(self):
        self.env = Env()
        self.aliases = {}       # name -> body text
        self.events = {}        # event_name -> [handler_text, ...]
        self._if_matched = False  # IF/ELIF/ELSE chain state
        self._output = False    # tracks if output was printed (for REPL)
        self.cmd_count = 0

    # ── resolve helpers ──────────────────────────────────

    def ri(self, node):
        """Resolve node to string + interpolate (blocks stay raw).

        Lit  → interpolated (handles "hello $name")
        Var  → env value (no double-interpolation)
        Blk  → raw text (deferred)
        Sub  → evaluate expression, catch RETURN
        """
        if isinstance(node, Blk):
            return node.text
        if isinstance(node, Var):
            return self.env.get(node.name)
        if isinstance(node, Sub):
            # Bare variable ref: resolve directly (no ECHO side-effect)
            text = node.text.strip()
            m = re.match(r'^\$\{(\w+)\}$|^\$([A-Za-z_]\w*|\d+)$', text)
            if m:
                return self.env.get(m.group(1) or m.group(2))
            try:
                return self.run_text(text)
            except Ret as r:
                return r.value
        # Lit
        return self.interpolate(node.value)

    def interpolate(self, text):
        """Substitute $var and ${var} references in text.

        Handles both named ($foo) and positional ($0, $1) variables.
        """
        def replacer(m):
            name = m.group(1) or m.group(2) or ''
            return self.env.get(name)
        return re.sub(r'\$\{([^}]+)\}|\$([A-Za-z_]\w*|\d+)', replacer, text)

    def resolve_cond(self, node):
        """Resolve a condition: Block → eval it, else → ri."""
        if isinstance(node, Blk):
            return self.run_text(node.text)
        return self.ri(node)

    def run_block(self, node):
        """Execute a block or resolve a non-block."""
        if isinstance(node, Blk):
            return self.run_text(node.text)
        return self.ri(node)

    def truthy(self, value):
        """ircii truthiness: non-empty, non-'0'."""
        return bool(value) and value != '0'

    def num(self, value, default=0):
        """Convert to int, fallback to default."""
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    # ── execution ────────────────────────────────────────

    def run_text(self, text):
        """Parse and execute text as ircii script. Return last result."""
        tokens = Lexer(text).tokenize()
        commands = Parser(tokens).parse()
        result = ''
        for cmd in commands:
            result = self.execute(cmd)
        return result

    def execute(self, cmd):
        """Execute a single Cmd node. Return result string."""
        self.cmd_count += 1

        # Check aliases first
        if cmd.name in self.aliases:
            return self._call_alias(cmd.name, cmd.args)

        # Find command handler
        handler = getattr(self, f'c_{cmd.name.lower()}', None)
        if handler:
            return handler(cmd.args)

        return self.emit(f'?{cmd.name}')

    def emit(self, text):
        """Print output."""
        self._output = True
        print(text)
        return text

    def _call_alias(self, name, args):
        """Call a user-defined alias.

        Positional params ($0, $1, ...) set as env vars — avoids re-lexing
        issues (like #channel becoming a comment after text substitution).
        """
        body = self.aliases[name]
        resolved_args = [self.ri(a) for a in args]

        # Set positional params in environment
        for i, arg in enumerate(resolved_args):
            self.env.put(str(i), arg)
        self.env.put('*', ' '.join(resolved_args))

        try:
            return self.run_text(body)
        except Ret as r:
            return r.value

    # ═══════════════════════════════════════════════════════
    #  COMMANDS
    # ═══════════════════════════════════════════════════════

    # ── I/O ───────────────────────────────────────────────

    def c_echo(self, args):
        """ECHO text — print line."""
        parts = [self.ri(a) for a in args]
        return self.emit(' '.join(parts))

    def c_type(self, args):
        """TYPE text — print (alias for ECHO)."""
        return self.c_echo(args)

    # ── variables ─────────────────────────────────────────

    def c_assign(self, args):
        """ASSIGN var value — set variable."""
        if len(args) < 2:
            return ''
        name = self.ri(args[0])
        value = self.ri(args[1])
        self.env.put(name, value)
        return value

    # SET is an alias for ASSIGN
    c_set = c_assign

    # ── evaluation ────────────────────────────────────────

    def c_eval(self, args):
        """EVAL text — parse and execute text as script."""
        text = ' '.join(self.ri(a) for a in args)
        return self.run_text(text)

    def c_expr(self, args):
        """EXPR arithmetic — evaluate math expression. Returns string."""
        parts = [self.ri(a) for a in args]
        expr_str = ' '.join(parts)
        try:
            result = eval(expr_str, {"__builtins__": {}}, {})
            return str(result)
        except Exception as e:
            return self.emit(f'EXPR: {e}')

    def c_exec(self, args):
        """EXEC command — run shell command."""
        cmd = ' '.join(self.ri(a) for a in args)
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True,
                               text=True, timeout=10)
            output = r.stdout.strip()
            if output:
                self.emit(output)
            return output
        except Exception as e:
            return self.emit(f'EXEC: {e}')

    # ── conditionals ──────────────────────────────────────

    def c_if(self, args):
        """IF {cond} {body} [ELIF {cond} {body}] [ELSE {body}]

        Handles both single-line and multi-line IF/ELIF/ELSE chains.
        Multi-line: each is a separate command, state tracked via _if_matched.
        Single-line: ELIF/ELSE embedded as trailing args.
        """
        if len(args) < 2:
            return ''
        self._if_matched = False

        cond = self.resolve_cond(args[0])
        body = args[1]
        if self.truthy(cond):
            self._if_matched = True
            return self.run_block(body)

        # Check for inline ELIF/ELSE in same command
        i = 2
        while i < len(args):
            tag = self.ri(args[i]).upper()
            if tag == 'ELIF' and i + 2 < len(args):
                if not self._if_matched:
                    elif_cond = self.resolve_cond(args[i + 1])
                    if self.truthy(elif_cond):
                        self._if_matched = True
                        return self.run_block(args[i + 2])
                i += 3
                continue
            if tag == 'ELSE' and i + 1 < len(args):
                if not self._if_matched:
                    self._if_matched = True
                    return self.run_block(args[i + 1])
                return ''
            i += 1
        return ''

    def c_elif(self, args):
        """ELIF {cond} {body} — standalone (multi-line) elif."""
        if len(args) < 2:
            return ''
        if self._if_matched:
            return ''
        cond = self.resolve_cond(args[0])
        if self.truthy(cond):
            self._if_matched = True
            return self.run_block(args[1])
        return ''

    def c_else(self, args):
        """ELSE {body} — standalone (multi-line) else."""
        if not args:
            return ''
        if self._if_matched:
            return ''
        self._if_matched = True
        return self.run_block(args[0])

    # ── loops ─────────────────────────────────────────────

    def c_while(self, args):
        """WHILE {cond} {body} — loop while condition is truthy.

        Condition is re-evaluated each iteration.
        If condition is a Block, it's evaluated each time.
        """
        if len(args) < 2:
            return ''
        cond_node = args[0]
        body_node = args[1]
        result = ''
        for _ in range(100000):  # safety limit
            if not self.truthy(self.resolve_cond(cond_node)):
                break
            try:
                result = self.run_block(body_node)
            except Brk:
                break
            except Cont:
                pass
        return result

    def c_fe(self, args):
        """FE list var {body} — foreach word in list.

        Splits list on whitespace, iterates with var set to each word.
        """
        if len(args) < 3:
            return ''
        items = self.ri(args[0]).split()
        var_name = self.ri(args[1])
        body = args[2]
        result = ''
        for item in items:
            self.env.put(var_name, item)
            try:
                result = self.run_block(body)
            except Brk:
                break
            except Cont:
                pass
        return result

    def c_foreach(self, args):
        """FOREACH var list {body} — foreach, var-first syntax."""
        if len(args) < 3:
            return ''
        var_name = self.ri(args[0])
        items = self.ri(args[1]).split()
        body = args[2]
        result = ''
        for item in items:
            self.env.put(var_name, item)
            try:
                result = self.run_block(body)
            except Brk:
                break
            except Cont:
                pass
        return result

    def c_fec(self, args):
        """FEC var string {body} — foreach character in string."""
        if len(args) < 3:
            return ''
        var_name = self.ri(args[0])
        chars = self.ri(args[1])
        body = args[2]
        result = ''
        for ch in chars:
            self.env.put(var_name, ch)
            try:
                result = self.run_block(body)
            except Brk:
                break
            except Cont:
                pass
        return result

    def c_break(self, args):
        """BREAK — exit innermost loop."""
        raise Brk()

    def c_continue(self, args):
        """CONTINUE — skip to next iteration."""
        raise Cont()

    def c_return(self, args):
        """RETURN [value] — exit current alias with value."""
        value = ' '.join(self.ri(a) for a in args) if args else ''
        raise Ret(value)

    # ── aliases / events ──────────────────────────────────

    def c_alias(self, args):
        """ALIAS name {body} — define command alias.

        Body uses $0, $1, ... for positional args, $* for all args.
        """
        if len(args) < 2:
            return ''
        name = self.ri(args[0]).upper()
        body = self.ri(args[1])
        self.aliases[name] = body
        return ''

    def c_on(self, args):
        """ON event {handler} — register event handler.

        Handler uses $0, $1, ... for event args.
        """
        if len(args) < 2:
            return ''
        event = self.ri(args[0]).upper()
        handler = self.ri(args[1])
        self.events.setdefault(event, []).append(handler)
        return ''

    def c_trigger(self, args):
        """TRIGGER event [args] — fire event, call handlers."""
        if not args:
            return ''
        event = self.ri(args[0]).upper()
        event_args = [self.ri(a) for a in args[1:]]
        result = ''
        for handler in self.events.get(event, []):
            # Set positional params as env vars
            for i, arg in enumerate(event_args):
                self.env.put(str(i), arg)
            self.env.put('*', ' '.join(event_args))
            try:
                result = self.run_text(handler)
            except Ret as r:
                return r.value
        return result

    # ── string operations ─────────────────────────────────

    def c_len(self, args):
        """LEN string — return length as string."""
        return str(len(self.ri(args[0]))) if args else '0'

    def c_substr(self, args):
        """SUBSTR string start [length] — substring."""
        if len(args) < 2:
            return ''
        s = self.ri(args[0])
        start = self.num(self.ri(args[1]))
        if len(args) >= 3:
            length = self.num(self.ri(args[2]))
            return s[start:start + length]
        return s[start:]

    c_mid = c_substr  # MID is alias for SUBSTR

    def c_cat(self, args):
        """CAT args... — concatenate strings."""
        return ''.join(self.ri(a) for a in args)

    def c_word(self, args):
        """WORD n string — return nth word (0-indexed)."""
        if len(args) < 2:
            return ''
        n = self.num(self.ri(args[0]))
        words = self.ri(args[1]).split()
        return words[n] if 0 <= n < len(words) else ''

    def c_nw(self, args):
        """NW string — number of words."""
        return str(len(self.ri(args[0]).split())) if args else '0'

    def c_strip(self, args):
        """STRIP string — strip leading/trailing whitespace."""
        return self.ri(args[0]).strip() if args else ''

    def c_toupper(self, args):
        """TOUPPER string — uppercase."""
        return self.ri(args[0]).upper() if args else ''

    def c_tolower(self, args):
        """TOLOWER string — lowercase."""
        return self.ri(args[0]).lower() if args else ''

    # ── comparison (return "1" or "0") ────────────────────

    def c_eq(self, args):
        """EQ a b — string equality."""
        if len(args) < 2:
            return '0'
        return '1' if self.ri(args[0]) == self.ri(args[1]) else '0'

    def c_neq(self, args):
        """NEQ a b — string inequality."""
        return '0' if self.c_eq(args) == '1' else '1'

    def c_gt(self, args):
        """GT a b — numeric greater-than."""
        if len(args) < 2:
            return '0'
        return '1' if self.num(self.ri(args[0])) > self.num(self.ri(args[1])) else '0'

    def c_lt(self, args):
        """LT a b — numeric less-than."""
        if len(args) < 2:
            return '0'
        return '1' if self.num(self.ri(args[0])) < self.num(self.ri(args[1])) else '0'

    def c_le(self, args):
        """LE a b — numeric less-or-equal."""
        if len(args) < 2:
            return '0'
        return '1' if self.num(self.ri(args[0])) <= self.num(self.ri(args[1])) else '0'

    def c_ge(self, args):
        """GE a b — numeric greater-or-equal."""
        if len(args) < 2:
            return '0'
        return '1' if self.num(self.ri(args[0])) >= self.num(self.ri(args[1])) else '0'

    def c_and(self, args):
        """AND args... — logical AND (all truthy → 1)."""
        for a in args:
            if not self.truthy(self.ri(a)):
                return '0'
        return '1'

    def c_or(self, args):
        """OR args... — logical OR (any truthy → 1)."""
        for a in args:
            if self.truthy(self.ri(a)):
                return '1'
        return '0'

    def c_not(self, args):
        """NOT val — logical NOT."""
        return '0' if args and self.truthy(self.ri(args[0])) else '1'

    def c_iif(self, args):
        """IIF cond then_val else_val — inline if expression."""
        if len(args) < 2:
            return ''
        if self.truthy(self.ri(args[0])):
            return self.ri(args[1])
        return self.ri(args[2]) if len(args) >= 3 else ''

    # ── timer / sleep ─────────────────────────────────────

    def c_timer(self, args):
        """TIMER mode — TIMER ON {block} / TIMER OFF / TIMER n {block}."""
        if not args:
            return ''
        mode = self.ri(args[0]).upper()
        if mode == 'ON' and len(args) >= 2:
            return self.run_block(args[1])
        if mode == 'OFF':
            return ''
        # TIMER n {body} — repeat n times with 1s delay
        n = self.num(self.ri(args[0]), 1)
        if len(args) >= 2:
            for _ in range(n):
                time.sleep(1)
                self.run_block(args[1])
        return ''

    def c_sleep(self, args):
        """SLEEP n — pause n seconds (max 10)."""
        if args:
            time.sleep(min(self.num(self.ri(args[0])), 10))
        return ''

    # ── file I/O ──────────────────────────────────────────

    def c_source(self, args):
        """SOURCE file — execute script file."""
        if not args:
            return ''
        path = self.ri(args[0])
        try:
            with open(path) as f:
                text = f.read()
            return self.run_text(text)
        except FileNotFoundError:
            return self.emit(f'SOURCE: {path}: not found')
        except Exception as e:
            return self.emit(f'SOURCE: {e}')

    # ── debug ─────────────────────────────────────────────

    def c_dump(self, args):
        """DUMP — show all variables."""
        if not self.env.data:
            return self.emit('(no variables)')
        for k, v in self.env.data.items():
            self.emit(f'  {k} = {v!r}')
        return ''

    def c_dumpa(self, args):
        """DUMPA — show all aliases."""
        if not self.aliases:
            return self.emit('(no aliases)')
        for k, v in self.aliases.items():
            self.emit(f'  {k} -> {v!r}')
        return ''

    def c_dumpe(self, args):
        """DUMPE — show all event handlers."""
        if not self.events:
            return self.emit('(no events)')
        for k, v in self.events.items():
            self.emit(f'  {k} -> {len(v)} handler(s)')
        return ''

    # ── misc ──────────────────────────────────────────────

    def c_nop(self, args):
        """NOP — no operation."""
        return ''

    def c_true(self, args):
        """TRUE — return '1'."""
        return '1'

    def c_false(self, args):
        """FALSE — return '0'."""
        return '0'


# ═══════════════════════════════════════════════════════════
#  REPL
# ═══════════════════════════════════════════════════════════

HELP_TEXT = """\
═══════════════════════════════════════════════════════════
  ircii-dsl command reference
═══════════════════════════════════════════════════════════

VARIABLES                  I/O
  ASSIGN var value           ECHO text
  SET var value              EXEC shell_cmd
                             SOURCE file
EVALUATION                 TYPE text (alias for ECHO)
  EVAL expr
  EXPR arithmetic           FLOW CONTROL
                             BREAK  CONTINUE  RETURN val
CONDITIONALS
  IF {cond} {body}          LOOPS
  ELIF {cond} {body}         WHILE {cond} {body}
  ELSE {body}                FE list var {body}
                             FOREACH var list {body}
STRING OPS                  FEC var string {body}
  LEN s    SUBSTR s n [m]
  CAT a..  WORD n s          ALIASES / EVENTS
  NW s     STRIP s           ALIAS name {body}
  TOUPPER s  TOLOWER s       ON event {handler}
  MID s n [m]                TRIGGER event [args]
COMPARISON (→ 1 or 0)      TIMER ON {block} / OFF / n {b}
  EQ a b    NEQ a b          SLEEP n
  GT a b    LT a b
  LE a b    GE a b          DEBUG
  AND a..   OR a..            DUMP  DUMPA  DUMPE
  NOT v     IIF c t f        NOP  TRUE  FALSE

SYNTAX
  $var / ${var}    variable reference
  {block}          deferred code (raw until eval)
  [expr]           inline subexpression
  # comment        line comment
  ;                statement separator (also newline)
  Multi-line: open { auto-continues prompt
═══════════════════════════════════════════════════════════"""


def repl():
    """Interactive REPL with multiline brace support."""
    print('ircii-dsl v0.1 — strings-only, TCL+Rexx had a baby')
    print('HELP for reference, Ctrl-C/D to quit')
    print()

    interp = Ircii()
    buf = []
    brace_depth = 0

    while True:
        try:
            if brace_depth > 0:
                dots = '.' * brace_depth
                prompt = f'  {dots}> '
            else:
                prompt = 'ircii> '
            line = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print('\nquit')
            break

        # HELP shortcut
        if not buf and line.strip().upper() == 'HELP':
            print(HELP_TEXT)
            continue

        # Track brace depth for multiline input
        buf.append(line)
        brace_depth += line.count('{') - line.count('}')
        if brace_depth > 0:
            continue  # wait for closing brace

        text = '\n'.join(buf)
        buf.clear()
        brace_depth = 0

        if not text.strip():
            continue

        # REPL convenience: bare $var prints its value
        stripped = text.strip()
        if stripped.startswith('$') and ' ' not in stripped and '{' not in stripped:
            text = 'ECHO ' + text

        try:
            tokens = Lexer(text).tokenize()
            commands = Parser(tokens).parse()
            interp._output = False
            result = ''
            for cmd in commands:
                try:
                    result = interp.execute(cmd)
                except Ret:
                    pass
            # Auto-print result if no explicit output was emitted
            if not interp._output and result:
                print(result)
        except Exception as e:
            print(f'! {e}')


# ═══════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════

if __name__ == '__main__':
    if len(sys.argv) > 1:
        interp = Ircii()
        interp.cmd_count = 0
        # Source each file argument
        for path in sys.argv[1:]:
            interp.c_source([Lit(path)])
    else:
        repl()
