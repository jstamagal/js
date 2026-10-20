"""Every line js draws comes from one render step (`js.fset.runs`) through
one converter per output target. This guard fails on a new way around it:
an entry's fixed `.text()` handed straight to a printer, `colour=False`, or
an escape sequence built by hand. The pickers and the migration notes are
the known gap, listed here."""

from __future__ import annotations

import ast
from pathlib import Path

JS = Path(__file__).resolve().parent.parent / "js"

# Files that may hand an entry's fixed text to a printer: the curses and
# prompt_toolkit pickers and the migration notes.
TEXT_ALLOWED = {"login_cli.py", "picker.py", "session_picker.py", "session_query.py", "agent_migration.py",
                "home.py", "rename.py"}
# Files that may write an escape sequence by hand: the converters themselves,
# cursor movement, key sequences sent to a terminal, and MOTD art passed through.
ESCAPE_ALLOWED = {"fset.py", "display.py", "toolkit/terminal.py", "motd.py"}
PRINTERS = {"print", "write", "_write", "say", "say_or_print", "say_for_caller", "warn", "emit", "Text",
            "from_ansi", "CommandResult", "append", "draw", "addstr"}


def _modules() -> list[tuple[str, ast.AST]]:
    return [(str(path.relative_to(JS)), ast.parse(path.read_text(encoding="utf-8")))
            for path in sorted(JS.rglob("*.py"))]


def _name(func: ast.AST) -> str:
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _fixed_text(node: ast.AST) -> bool:
    """`msgs.NAME.text(...)`: an entry's wording without its format."""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "text"
            and isinstance(node.func.value, ast.Attribute) and node.func.value.attr.isupper()
            and isinstance(node.func.value.value, ast.Name) and node.func.value.value.id in ("msgs", "_msgs"))


def test_no_printer_is_handed_an_entry_s_fixed_text():
    found = []
    for name, tree in _modules():
        if name in TEXT_ALLOWED:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _name(node.func) in PRINTERS:
                for arg in [*node.args, *(keyword.value for keyword in node.keywords)]:
                    found += [f"{name}:{inner.lineno}" for inner in ast.walk(arg) if _fixed_text(inner)]
    assert found == []


# The calls that fill an entry's holes to draw it.
HOLE_FILLERS = {"line_for", "line", "body", "body_runs", "runs", "plain"}


def test_no_hole_is_filled_with_an_entry_s_fixed_text():
    found = []
    for name, tree in _modules():
        if name in TEXT_ALLOWED:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _name(node.func) in HOLE_FILLERS:
                for arg in [*node.args, *(keyword.value for keyword in node.keywords)]:
                    found += [f"{name}:{inner.lineno}" for inner in ast.walk(arg) if _fixed_text(inner)]
    assert found == []


def test_no_error_is_raised_with_an_entry_s_fixed_text():
    """A refusal raised as an error is shown later; raised as `said()` it
    keeps its entry, and its format words it where it is shown."""
    found = []
    for name, tree in _modules():
        if name in TEXT_ALLOWED:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and node.exc is not None:
                found += [f"{name}:{inner.lineno}" for inner in ast.walk(node.exc) if _fixed_text(inner)]
    assert found == []


def test_nothing_draws_without_colour_by_asking_for_it():
    found = [f"{name}:{node.lineno}" for name, tree in _modules() for node in ast.walk(tree)
             if isinstance(node, ast.keyword) and node.arg == "colour"
             and isinstance(node.value, ast.Constant) and node.value.value is False]
    assert found == []


def test_a_console_that_writes_to_a_stream_takes_the_stream_s_colour():
    """Only the string renderer, whose text a converter then fits to its
    target, names its colour system itself."""
    found = [f"{name}:{node.lineno}" for name, tree in _modules() if name != "display.py" for node in ast.walk(tree)
             if isinstance(node, ast.keyword) and node.arg == "color_system" and isinstance(node.value, ast.Constant)]
    assert found == []


def test_no_escape_sequence_is_built_by_hand():
    found = []
    for name, tree in _modules():
        if name in ESCAPE_ALLOWED:
            continue
        compiled = {id(arg) for node in ast.walk(tree) if isinstance(node, ast.Call) and _name(node.func) == "compile"
                    for arg in node.args}
        found += [f"{name}:{node.lineno}" for node in ast.walk(tree)
                  if isinstance(node, ast.Constant) and isinstance(node.value, str) and "\x1b[" in node.value
                  and id(node) not in compiled]
    assert found == []


def _functions(tree: ast.AST) -> list[ast.AST]:
    return [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _own_nodes(function: ast.AST) -> list[ast.AST]:
    """The nodes of ``function`` outside the functions defined inside it."""
    nodes: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        nodes.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return nodes


def test_no_printer_is_handed_an_entry_s_fixed_text_through_a_name():
    """`line = msgs.X.text(...)` and later `print(line)`: the same bypass, one step removed."""
    found = []
    for name, tree in _modules():
        if name in TEXT_ALLOWED:
            continue
        for function in _functions(tree):
            own = _own_nodes(function)
            fixed = {target.id for node in own if isinstance(node, ast.Assign) and _fixed_text(node.value)
                     for target in node.targets if isinstance(target, ast.Name)}
            for node in own:
                if isinstance(node, ast.Call) and _name(node.func) in PRINTERS:
                    for arg in [*node.args, *(keyword.value for keyword in node.keywords)]:
                        found += [f"{name}:{inner.lineno}" for inner in ast.walk(arg)
                                  if isinstance(inner, ast.Name) and inner.id in fixed]
    assert found == []


def test_nothing_hands_on_an_entry_s_fixed_text_by_returning_it():
    """A helper that returns `msgs.X.text(...)`, or a name holding it, hands
    its caller wording without its format; it returns `said()` instead, and
    whoever shows it draws it through the format."""
    found = []
    for name, tree in _modules():
        if name in TEXT_ALLOWED:
            continue
        for function in _functions(tree):
            own = _own_nodes(function)
            fixed = {target.id for node in own if isinstance(node, ast.Assign)
                     and any(_fixed_text(inner) for inner in ast.walk(node.value))
                     for target in node.targets if isinstance(target, ast.Name)}
            for node in own:
                handed = []
                if isinstance(node, ast.Return) and node.value is not None:
                    handed = [node.value]
                elif isinstance(node, ast.Call) and _name(node.func) in HOLE_FILLERS | {"said"}:
                    handed = [*node.args, *(keyword.value for keyword in node.keywords)]
                for value in handed:
                    found += [f"{name}:{inner.lineno}" for inner in ast.walk(value)
                              if _fixed_text(inner) or (isinstance(inner, ast.Name) and inner.id in fixed)]
    assert found == []


# Calls named `.text(` that are not an entry's fixed wording: a tool result,
# a drawn image, a kernel's output, a session's parsed text, a sink's text.
NOT_AN_ENTRY = {"ToolResult", "draw", "output", "sink", "transcript", "parse"}
# Where an entry's fixed wording is the point: the entry's own methods, and the
# commit helper, whose printed lines the commit agent reads as they are.
WORDING_ALLOWED = TEXT_ALLOWED | {"commit_helper.py"}


def _receiver(node: ast.AST) -> str:
    value = node.func.value
    while isinstance(value, ast.Call):
        value = value.func
    return value.attr if isinstance(value, ast.Attribute) else getattr(value, "id", "")


def test_no_entry_s_fixed_wording_is_used_at_all():
    """`.text()` is an entry's wording without its format; anything that
    shows an entry asks for `said()`, `.line()`, `.body()` or `.words()`
    instead, so no path, however indirect, reaches a screen without the
    format."""
    found = []
    for name, tree in _modules():
        if name in WORDING_ALLOWED:
            continue
        inside_message = set()
        if name == "messages.py":
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name in ("Message", "Said"):
                    inside_message |= {id(inner) for inner in ast.walk(node)}
        found += [f"{name}:{node.lineno}" for node in ast.walk(tree)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "text"
                  and _receiver(node) not in NOT_AN_ENTRY and id(node) not in inside_message]
    assert found == []


# Helpers that turn an error into text on purpose: `of_error` itself, once
# it has found no entry; an MCP server's error, redacted of its secrets; and
# an httpx transport error, which is never raised with an entry.
FLATTENS_ON_PURPOSE = {("messages.py", "of_error"), ("mcp/client.py", "_redact_exception_message"),
                       ("model_client.py", "_transport_error")}


def test_no_helper_flattens_an_error_that_may_carry_an_entry():
    """A helper given an error returns `msgs.of_error(error)`, which keeps an
    entry the error was raised with, not `str(error)` or an f-string of it."""
    found = []
    for name, tree in _modules():
        if name in TEXT_ALLOWED:
            continue
        for function in _functions(tree):
            if (name, function.name) in FLATTENS_ON_PURPOSE:
                continue
            errors = {arg.arg for arg in function.args.args
                      if arg.annotation is not None and "Exception" in ast.unparse(arg.annotation)}
            for node in _own_nodes(function):
                if not (isinstance(node, ast.Return) and node.value is not None):
                    continue
                for inner in ast.walk(node.value):
                    flat = ((isinstance(inner, ast.Call) and _name(inner.func) == "str" and inner.args)
                            and isinstance(inner.args[0], ast.Name) and inner.args[0].id in errors)
                    formatted = (isinstance(inner, ast.FormattedValue) and isinstance(inner.value, ast.Name)
                                 and inner.value.id in errors)
                    if flat or formatted:
                        found.append(f"{name}:{function.name}:{inner.lineno}")
    assert found == []


def _returns_said(function: ast.AST) -> bool:
    return any(isinstance(node, ast.Return) and node.value is not None
               and any(isinstance(inner, ast.Call) and _name(inner.func) == "said" for inner in ast.walk(node.value))
               for node in _own_nodes(function))


def test_no_entry_a_helper_returns_is_flattened_by_a_join_or_an_f_string():
    """`text = helper()` where the helper returns `said()`, then
    `"\\n".join(...)` or `f"...{text}"`: the entry's wording lands without its
    format. Word it first (`msgs.words_of`), or hand it to a hole."""
    found = []
    for name, tree in _modules():
        if name in TEXT_ALLOWED:
            continue
        said_helpers = {function.name for function in _functions(tree) if _returns_said(function)}
        for function in _functions(tree):
            own = _own_nodes(function)
            # A dict of helpers called by key, `builders[key]()`.
            tables = {target.id for node in own if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                      and any(isinstance(value, ast.Name) and value.id in said_helpers for value in node.value.values)
                      for target in node.targets if isinstance(target, ast.Name)}

            def said_call(value: ast.AST) -> bool:
                if not isinstance(value, ast.Call):
                    return False
                func = value.func
                return ((isinstance(func, ast.Name) and func.id in said_helpers)
                        or (isinstance(func, ast.Subscript) and isinstance(func.value, ast.Name)
                            and func.value.id in tables))

            held = {target.id for node in own if isinstance(node, ast.Assign) and said_call(node.value)
                    for target in node.targets if isinstance(target, ast.Name)}
            for node in own:
                flattened = []
                if isinstance(node, ast.Call) and _name(node.func) == "join":
                    flattened = node.args
                elif isinstance(node, ast.FormattedValue):
                    flattened = [node.value]
                for value in flattened:
                    if isinstance(value, ast.Call) and _name(value.func) == "words_of":
                        continue
                    found += [f"{name}:{inner.lineno}" for inner in ast.walk(value)
                              if (isinstance(inner, ast.Name) and inner.id in held) or said_call(inner)]
    assert found == []


def _formatted(node: ast.AST) -> bool:
    """A call that draws an entry through its format: `.line(`, `.body(`, `.runs(`, `.body_runs(`."""
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("line", "body", "runs", "body_runs")
            and isinstance(node.func.value, ast.Attribute) and node.func.value.attr.isupper())


def test_no_format_output_has_its_colour_cleaned_away():
    """`clean()` strips escapes: fine for model and tool text, wrong for a format's own colours,
    whether the format reaches `clean` directly or through a function that cleans its argument."""
    found = []
    trees = _modules()
    # function name -> the positions and names of the parameters it cleans
    cleaning: dict[str, set[int | str]] = {"clean": {0}}
    for _name_, tree in trees:
        for function in _functions(tree):
            params = [arg.arg for arg in function.args.args]
            for node in _own_nodes(function):
                if isinstance(node, ast.Call) and _name(node.func) == "clean" and node.args:
                    for inner in ast.walk(node.args[0]):
                        if isinstance(inner, ast.Name) and inner.id in params:
                            cleaning.setdefault(function.name, set()).update({params.index(inner.id), inner.id})
    for name, tree in trees:
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _name(node.func) in cleaning):
                continue
            cleaned = cleaning[_name(node.func)]
            method = isinstance(node.func, ast.Attribute)
            args = [arg for index, arg in enumerate(node.args) if (index + method) in cleaned or index in cleaned]
            args += [keyword.value for keyword in node.keywords if keyword.arg in cleaned]
            for arg in args:
                found += [f"{name}:{inner.lineno}" for inner in ast.walk(arg) if _formatted(inner)]
    assert found == []


# Where js may cut text short with an ellipsis: the folded TOOLS_DISPLAY 1 and
# 2 rows, which Ctrl-O unfolds, and the status bar, which has one row.
ELLIPSIS_ALLOWED = {("display.py", "key_argument"), ("display.py", "_single_line"), ("display.py", "fold_label"),
                    ("screen.py", "status_line"), ("screen.py", "truncate_model"), ("screen.py", "_elide"),
                    ("display.py", ""),  # "" is module level: the running mark, MARK = "…"
                    # The notice for a media block TOOL_MEDIA_MAX drops: the model's copy only.
                    ("runtime.py", "_clip_name")}


def test_nothing_is_cut_short_outside_the_folded_rows():
    found = []
    for name, tree in _modules():
        if name not in ("display.py", "screen.py", "runtime.py"):
            continue
        docs = {id(node.body[0].value) for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module))
                and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
        owner: dict[int, str] = {}  # each node's innermost function
        for function in sorted(_functions(tree), key=lambda f: f.lineno, reverse=True):
            for node in ast.walk(function):
                owner.setdefault(id(node), function.name)
        for node in ast.walk(tree):
            cut = (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs
                   and ("…" in node.value or "..." in node.value))
            cut = cut or (isinstance(node, ast.keyword) and node.arg == "overflow"
                          and isinstance(node.value, ast.Constant) and node.value.value == "ellipsis")
            cut = cut or (isinstance(node, ast.Call) and _name(node.func) == "truncate")
            if cut and (name, owner.get(id(node), "")) not in ELLIPSIS_ALLOWED:
                found.append(f"{name}:{node.lineno} in {owner.get(id(node), '<module>')}")
    assert found == []


# Validators: what refuses a value says why through an entry, and the refusal
# is shown through that entry's format, so none of them may flatten it.
VALIDATORS = {("fset.py", None), ("setcmd.py", None), ("settings.py", "coerce_value"),
              ("settings.py", "_validate_alias_profiles")}


def test_no_validator_flattens_its_refusal_to_fixed_text():
    found = []
    for name, tree in _modules():
        for module, function_name in VALIDATORS:
            if name != module:
                continue
            scopes = [tree] if function_name is None else [f for f in _functions(tree) if f.name == function_name]
            found += [f"{name}:{node.lineno}" for scope in scopes for node in ast.walk(scope) if _fixed_text(node)]
    assert found == []
