"""js/messages.py: every operator-facing string is a named entry, banner lines
go through the slot, and severity is colour, never a word."""

from __future__ import annotations

import ast
import io
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from js import cli, colors as C, commit_helper, home, paths, settings, stream_transport, tool_binaries
from js import tooldiag, toolstats
from js import messages as msgs
from js.promptexpand import expand_prompt
from js.toolkit import policy
from js.toolkit.registry import build_default_registry

JS_ROOT = Path(msgs.__file__).parent
ANSI = re.compile(r"\x1b\[[0-9;]*m")
DEAD = ("error:", "warning:", "js:", "note:", "(no ", "knob", "!!")


def _entries() -> dict[str, msgs.Message]:
    return {name: value for name, value in vars(msgs).items() if isinstance(value, msgs.Message)}


def _plain(text: str) -> str:
    return ANSI.sub("", text)


def test_a_banner_line_starts_with_the_slot_and_a_plain_line_does_not(monkeypatch):
    monkeypatch.setattr(msgs, "BANNER", "@@")
    assert _plain(msgs.Message("Model: {model}").line(model="m")).startswith("@@ ")
    assert not _plain(msgs.Message("{model}", banner=False).line(model="m")).startswith("@@")


def test_command_output_reaches_the_screen_through_the_slot(monkeypatch, capsys):
    monkeypatch.setattr(msgs, "BANNER", "@@")
    state = {"provider_id": None, "provider_base_url": None, "provider_api_key": None}
    cli._cmd_baseurl("https://example.test/v1", state, object())
    cli._handle_command("/alias nope", {"aliases": {}}, object())
    out = [_plain(line) for line in capsys.readouterr().out.splitlines()]
    assert out == [f"@@ {msgs.BASE_URL_SET.text()}", f"@@ {msgs.NO_ALIAS.text(name='nope')}"]
    assert state["provider_base_url"] == "https://example.test/v1"


def test_network_lines_go_through_the_slot(monkeypatch):
    monkeypatch.setattr(msgs, "BANNER", "@@")
    lines: list[str] = []
    stream_transport.install_sink(stream_transport.NetSink(level=lambda: 2, emit=lines.append))
    try:
        stream_transport.begin_call("https://api.example.test/v1", "m")
    finally:
        stream_transport.install_sink(None)
    assert lines and all(_plain(line).startswith("@@ ") for line in lines)


@pytest.mark.parametrize(("severity", "colour"), [(msgs.GRAVE, C.BR_RED), (msgs.WARN, C.BR_YELLOW)])
def test_severity_paints_the_holes_not_the_line(severity, colour):
    line = msgs.Message("Model {model} not saved: {error}", severity).line(model="m1", error="disk full")
    assert f"{colour}m1{C.RESET}" in line
    assert f"{colour}disk full{C.RESET}" in line
    assert f"{colour}Model" not in line


def test_a_message_without_holes_is_painted_whole():
    line = msgs.Message("Prompt is empty.", msgs.GRAVE).line()
    assert line == msgs.banner(f"{C.BR_RED}Prompt is empty.{C.RESET}")


def test_info_is_not_coloured():
    assert "\x1b" not in msgs.Message("Model: {model}").line(model="m")


def test_format_specs_and_conversions_survive_painting():
    entry = msgs.Message("{fullness:.0%} {name!r}", msgs.WARN)
    assert _plain(entry.line(fullness=0.5, name="x")) == msgs.banner(entry.text(fullness=0.5, name="x"))


def test_text_is_the_message_without_slot_or_colour(monkeypatch):
    monkeypatch.setattr(msgs, "BANNER", "@@")
    text = msgs.Message("Model {model} not saved", msgs.GRAVE).text(model="m1")
    assert "@@" not in text and "\x1b" not in text and "m1" in text


@pytest.mark.parametrize("name", sorted(_entries()))
def test_no_entry_carries_the_slot_a_severity_word_or_a_paren(name):
    template = _entries()[name].template
    assert msgs.BANNER not in template
    assert "(" not in template and ")" not in template
    assert not any(word in template.lower() for word in DEAD)


def _string_parts(node: ast.AST) -> list[str]:
    parts: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            parts.append(child.value)
    return parts


def _screen_calls(tree: ast.AST) -> list[ast.AST]:
    """print(...) and console.print(...) calls, writes to sys.stdout /
    sys.stderr, and argparse help strings."""
    calls: list[ast.AST] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "print":
            calls.append(node)
        elif isinstance(func, ast.Attribute) and func.attr == "print":
            calls.append(node)
        elif (isinstance(func, ast.Attribute) and func.attr == "write"
              and isinstance(func.value, ast.Attribute) and func.value.attr in ("stdout", "stderr")):
            calls.append(node)
        elif isinstance(func, ast.Attribute) and func.attr == "add_argument":
            calls.extend(kw.value for kw in node.keywords if kw.arg == "help")
    return calls


def _modules() -> list[Path]:
    return sorted(JS_ROOT.rglob("*.py"))


def test_no_print_in_js_carries_a_dead_word_or_the_slot():
    found = []
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _screen_calls(tree):
            for text in _string_parts(call):
                lowered = text.lower()
                if msgs.BANNER in text or any(word in lowered for word in DEAD):
                    found.append(f"{path.relative_to(JS_ROOT)}:{getattr(call, 'lineno', 0)}: {text!r}")
    assert found == []


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


def test_the_slot_is_spelled_only_in_messages():
    spelled = [str(path.relative_to(JS_ROOT)) for path in _modules()
               if path.name != "messages.py" and f'"{msgs.BANNER}' in path.read_text(encoding="utf-8")]
    assert spelled == []


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


def test_help_lists_every_command_without_a_paren_aside_or_dead_word(capsys):
    cli._cmd_help("", {"aliases": {"x": "set model.id y"}}, object())
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) > len(set(cli.COMMANDS.values()))
    assert _clean(lines) == []


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


def test_a_command_refusal_shows_in_its_entry_severity(monkeypatch):
    """A usage slip is not painted; a failure is painted in its entry's colour."""
    terminal = _Terminal()
    monkeypatch.setattr(sys, "stdout", terminal)
    monkeypatch.setitem(cli.COMMANDS, "boom", cli.Command(
        lambda arg, state, cfg: msgs.SAVE_FAILED.said(error="disk full"), "boom", msgs.CMD_HELP))

    cli._handle_command("/compact-auto maybe", {}, object())
    cli._handle_command("/boom", {}, object())

    usage, failure = terminal.getvalue().splitlines()
    assert usage == msgs.USAGE.line(usage="/compact-auto on|off")
    assert "\x1b" not in usage
    assert failure == msgs.SAVE_FAILED.line(error="disk full")
    assert msgs.SEVERITY_COLOR[msgs.SAVE_FAILED.severity] in failure


def test_a_refused_home_move_shows_in_its_entry_severity(tmp_path):
    legacy_jsrc = paths.legacy_homes()["config"] / "jsrc"
    legacy_jsrc.parent.mkdir(parents=True, exist_ok=True)
    legacy_jsrc.write_text("old\n", encoding="utf-8")
    paths.global_config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.global_config_file().write_text("new\n", encoding="utf-8")
    terminal = _Terminal()

    steps = home.migrate_once(terminal)

    assert [step.kind for step in steps] == ["refuse"]
    said = home.describe(steps[0], apply=True)
    assert said.message is msgs.HOME_REFUSED
    assert terminal.getvalue() == said.message.line(**said.fields) + "\n"
    assert msgs.SEVERITY_COLOR[msgs.HOME_REFUSED.severity] in terminal.getvalue()


def test_offline_compaction_reports_through_the_slot(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_from_env", lambda *a, **kw: SimpleNamespace(session_file="s.jsonl"))
    monkeypatch.setattr(cli.P, "load_configured_prompt_spec", lambda cfg: SimpleNamespace(system="S"))
    monkeypatch.setattr(cli.M, "load_replay_messages", lambda path: [])
    monkeypatch.setattr(cli.compaction, "compact_now_sync",
                        lambda *a, **kw: msgs.COMPACT_SKIPPED_NO_PREFIX.said())
    assert cli._run_compact_offline("s") == 0
    assert capsys.readouterr().out.strip() == msgs.banner(msgs.COMPACT_SKIPPED_NO_PREFIX.text())


def test_the_urllib_fallback_is_an_entry_on_stderr_once_per_purpose(monkeypatch, capsys):
    monkeypatch.setattr(tool_binaries, "_URLLIB_FALLBACK_REPORTED", set())
    tool_binaries.warn_urllib_fallback("x")
    tool_binaries.warn_urllib_fallback("x")
    assert capsys.readouterr().err.splitlines() == [msgs.banner(msgs.URLLIB_FALLBACK.text(purpose="x"))]
