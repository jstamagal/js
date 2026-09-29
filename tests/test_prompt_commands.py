"""`~/.js/commands/NAME.md` is `/NAME`: the line sends the file's text with
$1 / $@ / ${N:-default} filled from its arguments, and Tab completes the name."""

from __future__ import annotations

import pytest

from js import cli, pastes
from js import prompt_commands as pc
from js.replcomplete import JsCompleter

SESSION = "cmds"


# --- substitution ----------------------------------------------------------------


@pytest.mark.parametrize(("body", "args", "expected"), [
    ("Review $1 for $2.", ["a.py", "bugs"], "Review a.py for bugs."),
    ("All: $@", ["x", "y z"], "All: x y z"),
    ("All: $ARGUMENTS", ["x", "y"], "All: x y"),
    ("Missing: [$3]", ["a"], "Missing: []"),
    ("Mode ${2:-quick}", ["a"], "Mode quick"),
    ("Mode ${2:-quick}", ["a", "deep"], "Mode deep"),
    ("Mode ${1:-quick}", [""], "Mode quick"),
    ("Topic ${@:-anything}", [], "Topic anything"),
    ("Rest ${@:2}", ["a", "b", "c"], "Rest b c"),
    ("One ${@:2:1}", ["a", "b", "c"], "One b"),
    ("Tenth $10", [str(i) for i in range(1, 11)], "Tenth 10"),
])
def test_placeholders(body, args, expected):
    assert pc.substitute(body, args) == expected


def test_an_argument_is_never_substituted_again():
    assert pc.substitute("say $1 then $2", ["$2", "b"]) == "say $2 then b"


def test_quotes_group_words():
    assert pc.split_args("src/app.py \"be strict\" 'and quick' x") == ["src/app.py", "be strict", "and quick", "x"]
    assert pc.split_args('"" b') == ["", "b"]


# --- discovery -----------------------------------------------------------------


@pytest.fixture
def dirs(monkeypatch, tmp_path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    monkeypatch.setenv("HOME", str(home))
    (home / ".js" / "commands").mkdir(parents=True)
    (project / ".js" / "commands").mkdir(parents=True)
    monkeypatch.chdir(project)
    return home / ".js" / "commands", project / ".js" / "commands"


def test_global_and_project_commands_are_found_and_project_wins(dirs, tmp_path):
    global_dir, project_dir = dirs
    (global_dir / "review.md").write_text("global review of $1\n", encoding="utf-8")
    (global_dir / "plan.md").write_text("Plan $@\n", encoding="utf-8")
    (project_dir / "review.md").write_text("project review of $1\n", encoding="utf-8")
    (global_dir / "notes.txt").write_text("not a command", encoding="utf-8")

    found = pc.discover(tmp_path / "project")

    assert sorted(found) == ["plan", "review"]
    assert found["review"].body.strip() == "project review of $1"


def test_description_is_frontmatter_or_first_line(dirs, tmp_path):
    global_dir, _ = dirs
    (global_dir / "a.md").write_text("---\ndescription: Audit a file\n---\nAudit $1\n", encoding="utf-8")
    (global_dir / "b.md").write_text("\n\nExplain $1 simply\nmore\n", encoding="utf-8")

    found = pc.discover(tmp_path / "project")

    assert found["a"].description == "Audit a file"
    assert found["a"].body.strip() == "Audit $1"
    assert found["b"].description == "Explain $1 simply"


def test_expand_names_only_known_commands(dirs, tmp_path):
    global_dir, _ = dirs
    (global_dir / "review.md").write_text("Review $1, focus ${2:-bugs}", encoding="utf-8")
    found = pc.discover(tmp_path / "project")

    assert pc.expand('/review src/app.py "error paths"', found) == "Review src/app.py, focus error paths"
    assert pc.expand("/review", found) == "Review , focus bugs"
    assert pc.expand("/unknown x", found) is None
    assert pc.expand("review x", found) is None


def test_a_file_is_parsed_again_only_when_it_changes(dirs, tmp_path, monkeypatch):
    global_dir, _ = dirs
    command = global_dir / "review.md"
    command.write_text("Review $1\n", encoding="utf-8")
    loads: list[str] = []
    real_load = pc._load
    monkeypatch.setattr(pc, "_load", lambda path: loads.append(path.name) or real_load(path))

    pc.discover(tmp_path / "project")
    pc.discover(tmp_path / "project")
    assert loads == ["review.md"]

    command.write_text("Review $1 closely\n", encoding="utf-8")
    found = pc.discover(tmp_path / "project")
    assert found["review"].body.strip() == "Review $1 closely"
    assert loads == ["review.md", "review.md"]


# --- completion and help -----------------------------------------------------------


def test_tab_completes_a_command_file_name(dirs):
    global_dir, _ = dirs
    (global_dir / "review.md").write_text("Review $1", encoding="utf-8")
    completer = JsCompleter(commands=lambda: cli._command_completions({}))

    assert "/review" in completer.candidates("/rev")[0]
    assert "/review" in completer.candidates("rev")[0]


def test_help_lists_command_files(dirs, capsys):
    global_dir, _ = dirs
    (global_dir / "review.md").write_text("---\ndescription: Review a file\n---\nReview $1", encoding="utf-8")

    cli._cmd_help("", {}, None)

    assert any("/review" in line and "Review a file" in line for line in capsys.readouterr().out.splitlines())


# --- the REPL sends the expanded text -------------------------------------------------


@pytest.fixture
def repl(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("JS_AGENT", "JS_SESSION", "JS_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_maybe_auto_compact", lambda *_a, **_k: None)
    sent: list[str] = []

    def run_turn_stub(cfg, system, messages, *_a, **_k):
        sent.append(str(messages[-1]["content"]))
        messages.append({"role": "assistant", "content": "ok"})

    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)

    def run(lines):
        class PromptSessionStub:
            def __init__(self, history, **kwargs):
                self.lines = iter(lines)

            def prompt(self, *_args, **_kwargs):
                try:
                    return next(self.lines)
                except StopIteration:
                    raise EOFError from None

        monkeypatch.setattr(cli, "PromptSession", PromptSessionStub)
        cli.main(["--blocking", "--session", SESSION])
        return sent

    commands = tmp_path / ".js" / "commands"
    commands.mkdir(parents=True)
    return run, commands


def test_slash_name_sends_the_filled_in_file(repl):
    run, commands = repl
    (commands / "review.md").write_text("Review $1 with focus on ${2:-correctness}.\n", encoding="utf-8")

    sent = run(['/review js/cli.py "error paths"', "/review js/a.py"])

    assert sent == ["Review js/cli.py with focus on error paths.", "Review js/a.py with focus on correctness."]


def test_a_built_in_command_wins_over_a_file_of_the_same_name(repl, capsys):
    run, commands = repl
    (commands / "turns.md").write_text("hijacked", encoding="utf-8")

    sent = run(["/turns"])

    assert sent == []


def test_the_blocking_repl_sends_a_collapsed_paste_in_full(repl):
    run, _commands = repl
    text = "\n".join(f"row {i}" for i in range(30))
    token = pastes.keep(text, 10)

    sent = run([f"summarize {token}"])

    assert sent == [f"summarize {text}"]
