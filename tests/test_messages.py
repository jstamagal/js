"""js/messages.py: every operator-facing string is a named entry, banner lines
go through the slot, and severity is colour, never a word."""

from __future__ import annotations

import ast
import io
import re
import subprocess
from pathlib import Path

import pytest

from js import cli, commit_helper, home, settings, tool_binaries
from js import tooldiag, toolstats
from js import messages as msgs
from js.promptexpand import expand_prompt
from js.toolkit import policy
from js.toolkit.registry import build_default_registry

JS_ROOT = Path(msgs.__file__).parent
ANSI = re.compile(r"\x1b\[[0-9;]*m")
DEAD = ("error:", "warning:", "js:", "note:", "(no ", "knob", "!!")


def _plain(text: str) -> str:
    return ANSI.sub("", text)


def test_format_specs_and_conversions_survive_painting():
    entry = msgs.Message("{fullness:.0%} {name!r}", msgs.WARN)
    assert _plain(entry.line(fullness=0.5, name="x")) == msgs.banner(entry.text(fullness=0.5, name="x"))


def _string_parts(node: ast.AST) -> list[str]:
    parts: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            parts.append(child.value)
    return parts


def _modules() -> list[Path]:
    return sorted(JS_ROOT.rglob("*.py"))


def _command_line_text(tree: ast.AST) -> list[ast.AST]:
    """What argparse shows: help=, description= and parser.error(...), plus a
    COMMANDS doc, the third argument of Command(...)."""
    found: list[ast.AST] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in ("add_argument", "ArgumentParser"):
            found.extend(kw.value for kw in node.keywords if kw.arg in ("help", "description"))
        elif name == "error" and isinstance(func, ast.Attribute) and "parser" in ast.unparse(func.value):
            found.extend(node.args)
        elif name == "Command" and len(node.args) >= 3:
            found.append(node.args[2])
    return found


def test_command_line_help_and_command_docs_name_an_entry():
    """No literal: the wording is in js/messages.py."""
    literal = []
    for path in _modules():
        for value in _command_line_text(ast.parse(path.read_text(encoding="utf-8"))):
            if _string_parts(value):
                literal.append(f"{path.relative_to(JS_ROOT)}:{value.lineno}")
    assert literal == []


def _clean(lines: list[str]) -> list[str]:
    """Lines that carry a dead word or a paren aside."""
    return [line for line in lines
            if "(" in line or ")" in line or any(word in line.lower() for word in DEAD)]


def test_the_tools_table_names_an_undecided_tool_without_a_paren_aside():
    decisions = policy.resolve(build_default_registry().tools, ())
    rows = policy.render_table(decisions, {})
    assert len(rows) == len(decisions) + 1
    assert _clean(rows) == []


def test_a_failed_directive_without_stderr_warns_without_a_paren_aside(capsys):
    assert expand_prompt("x !{sh exit 3} y", allow_code=True) == "x !{sh exit 3} y"
    err = _plain(capsys.readouterr().err).strip()
    reason = msgs.DIRECTIVE_EXITED_SILENT.text(label="!{sh}", code=3)
    assert err == msgs.banner(msgs.DIRECTIVE_NOT_EXPANDED.text(error=reason))
    assert _clean([err]) == []


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_the_commit_helper_speaks_in_entries(tmp_path, capsys):
    """A clean tree with no history, then a failed stage: every line is an
    entry, with no dead word and no paren aside."""
    _git(tmp_path, "init", "-q", "-b", "main")
    assert commit_helper.main(["-C", str(tmp_path), "survey"]) == 0
    out = capsys.readouterr().out
    assert msgs.SURVEY_CLEAN.text() in out
    assert msgs.SURVEY_NO_HISTORY.text() in out
    assert _clean(out.splitlines()[1:]) == []   # the heading line carries the path

    assert commit_helper.main(["-C", str(tmp_path), "stage", "nope.txt", "1"]) == 2
    err = capsys.readouterr().err.strip()
    assert err == msgs.STAGE_NO_CHANGES.text(path="nope.txt")
    assert "\x1b" not in out + err


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize(("main", "flag"), [
    (cli.main, "--help"), (cli.main, "--help-full"),
    (home.main, "--help"), (toolstats.main, "--help"), (tooldiag.main, "--help"),
])
def test_the_help_screens_carry_no_paren_aside_or_dead_word(main, flag, capsys):
    with pytest.raises(SystemExit) as exc:
        main([flag])
    assert exc.value.code == 0
    out = capsys.readouterr().out.splitlines()
    assert out and _clean(out) == []


def test_a_bad_command_line_is_refused_through_the_slot(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--no-such-flag"])
    assert exc.value.code == 2
    err = capsys.readouterr().err.splitlines()
    assert err[-1].startswith(msgs.banner("")) and "--no-such-flag" in err[-1]
    assert _clean(err) == []


def test_no_setting_doc_carries_a_paren_aside():
    """A call like fetch() or min(a, b) is code; an aside opens after a space."""
    assert [spec.key for spec in settings.REGISTRY if re.search(r"(^|\s)\(", spec.doc)] == []


def test_say_paints_a_terminal_and_nothing_else():
    terminal, pipe = _Terminal(), io.StringIO()
    msgs.say(msgs.NOT_SAVED_NO_RESUME, file=terminal)
    msgs.say(msgs.NOT_SAVED_NO_RESUME, file=pipe)
    assert terminal.getvalue() == msgs.NOT_SAVED_NO_RESUME.line() + "\n"
    assert pipe.getvalue() == msgs.banner(msgs.NOT_SAVED_NO_RESUME.text()) + "\n"


def test_the_urllib_fallback_is_an_entry_on_stderr_once_per_purpose(monkeypatch, capsys):
    monkeypatch.setattr(tool_binaries, "_URLLIB_FALLBACK_REPORTED", set())
    tool_binaries.warn_urllib_fallback("x")
    tool_binaries.warn_urllib_fallback("x")
    assert capsys.readouterr().err.splitlines() == [msgs.banner(msgs.URLLIB_FALLBACK.text(purpose="x"))]
