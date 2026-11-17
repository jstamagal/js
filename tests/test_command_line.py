"""Every js command line js writes or rewrites, word for word."""
from __future__ import annotations

import argparse
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest

from js import (
    cli,
    command_line,
    home_cli,
    loop_controller,
    paths,
    repl,
    runtime,
    sampling,
    session_picker,
)
from js.cli_options import RunOptions, invocation
from js.repl_state import ReplState


def _parse(argv: list[str]):
    """`argv` as js reads it; a command line js refuses fails the test."""
    parser = command_line.parser()
    cli._refuse_glued_short_options(parser, argv)
    return parser.parse_args(argv)


@pytest.mark.parametrize("argv", [
    ["-p", "hello", "--model", "m"], ["-lp", "x"], ["-lp=x"], ["-lp"], ["--prompt=x y"], ["-p=x"], ["-pfoo"],
    ["-p", "-"], ["-p", "-4.5"], ["-p", "-v is broken"], ["-p", "-a b"], ["-p", "--model", "m"], ["-p", ""],
    ["-s", "--model", "m"], ["-s=name"], ["--session", "-5"], ["--session", "-"],
    ["-C", "-"], ["--max-out", "5"], ["--extra", "a=1", "--extra", "b=2", "-f", "x", "-f", "y"],
    ["-qn", "--last", "-d"], ["--commit", "--", "--last"], ["--commit", "msg"], ["--you-are"],
    ["--you-are", "--chat"], ["--you-are", "voice"], ["--loop", "2h", "-p", "two words"], ["--loop"],
    ["--system-prompt", "--last x"], ["--extra", "x=--loop", "--loop=3"], ["--home", "/h", "--json"],
])
def test_read_takes_the_values_the_parser_takes(argv):
    js = command_line.parser()
    parsed = vars(js.parse_args(argv))
    args = command_line.read(argv)
    assert command_line.words(args) == argv
    actions = {action.dest: action for action in js._actions}
    taken: dict = {}
    for arg in args:
        if arg.dest is None:
            continue
        action = actions[arg.dest]
        if action.nargs == 0:
            value = True
        elif arg.value is None:
            value = action.const
        else:
            value = action.type(arg.value) if action.type else arg.value
        if isinstance(action, argparse._AppendAction):
            taken.setdefault(arg.dest, []).append(value)
        else:
            taken[arg.dest] = value
    assert {dest: parsed[dest] for dest in taken} == taken
    positional = [arg.value for arg in args if arg.option is None]
    if positional[:1] == ["--"]:
        positional = positional[1:]
    assert parsed["target"] == (positional[0] if positional else None)


@pytest.mark.parametrize("prompt", ["-v is broken, fix", "--fix this", "-4", "-4.5", "-.5", "plain opener", "-", ""])
@pytest.mark.parametrize("flag", ["-p", "--prompt", "-lp"])
def test_read_takes_the_prompt_the_parser_takes(flag, prompt):
    argv = [flag, prompt, "--model", "m"]
    dest = "last_prompt" if flag == "-lp" else "prompt"
    assert getattr(command_line.parser().parse_args(argv), dest) == prompt
    assert [(arg.dest, arg.value) for arg in command_line.read(argv)] == [(dest, prompt), ("model", "m")]


def test_argv_gives_each_option_its_value():
    words = command_line.argv(("--chat", True), ("--no-env", False), ("--tools", None), ("--you-are", ""),
                              ("--append-system-prompt", ("a", "b c")), ("-C", "/w"), ("--max-out", 5))
    parsed = command_line.parser().parse_args(words)
    assert (parsed.chat, parsed.no_env, parsed.tools, parsed.you_are) == (True, False, None, "")
    assert (parsed.append_system_prompt, parsed.cd, parsed.max_out) == (["a", "b c"], "/w", 5)
    assert "--no-env" not in words and "--tools" not in words


@pytest.mark.parametrize("options", [
    RunOptions(system_prompt="S", append_system_prompt=("a", "b c"), no_env=True, chat=True, tools="read"),
    RunOptions(you_are="", no_agents_md=True, no_skills=True, no_mcp=True, no_tools=True),
    RunOptions(system_prompt_file="/p f"),
    RunOptions(),
])
def test_options_argv_reproduces_the_options(options):
    parsed = command_line.parser().parse_args(command_line.options_argv(options))
    for field in fields(options):
        if field.name != "agent":
            value = getattr(parsed, field.name)
            assert (tuple(value) if isinstance(value, list) else value) == getattr(options, field.name)


@pytest.mark.parametrize(("argv", "child"), [
    (["-C", "/tmp", "--loop", "10", "-m", "sol", "--extra", "x=y"], ["-C", "/tmp", "-m", "sol", "--extra", "x=y"]),
    (["--loop=45M", "--bench", "a"], ["--bench", "a"]),
    (["--loop"], []),
    (["--loop", "-p", "x"], ["-p", "x"]),
    (["-p", "--loop", "3"], ["-p"]),
    (["-lp", "--loop", "3"], ["-lp"]),
    (["-lp=x", "--loop", "3"], ["-lp=x"]),
    (["--loop", "3", "--loop", "5"], []),
    (["--loop", "3", "-p", "--loop", "3"], ["-p"]),
    (["--loop", "3", "--prompt", "--loop", "3"], ["--prompt"]),
    (["--loop", "3", "-lp", "--loop", "3"], ["-lp"]),
    (["--extra", "x=--loop", "--loop", "2"], ["--extra", "x=--loop"]),
    (["-p", "hi", "--loop=2", "--loop", "2"], ["-p", "hi"]),
    (["--loop", "2h", "-p", "two words"], ["-p", "two words"]),
])
def test_loop_child_command(argv, child):
    _parse(argv)
    assert loop_controller.child_argv(argv) == child
    assert _parse(child).loop is None


@pytest.mark.parametrize(("argv", "home", "rest"), [
    (["--home", "/h", "-p", "x"], "/h", ["-p", "x"]),
    (["--home=/h", "login", "provider"], "/h", ["login", "provider"]),
    (["-p", "text --home is literal", "--", "--home", "literal"], None,
     ["-p", "text --home is literal", "--", "--home", "literal"]),
    (["-p", "x", "--home", "/a", "--home=/b"], "/b", ["-p", "x"]),
    (["-m", "m"], None, ["-m", "m"]),
])
def test_home_is_taken_out_of_the_command(monkeypatch, argv, home, rest):
    chosen = []
    monkeypatch.setattr(home_cli.paths, "configure_home", chosen.append)
    assert home_cli.configure_home(argv) == rest
    assert chosen == [home]


@pytest.fixture
def printed(monkeypatch):
    """The command each hint prints."""
    commands: list[str] = []
    monkeypatch.setattr(cli.msgs, "say", lambda _message, **fields: commands.append(fields["command"]))
    monkeypatch.setattr(cli.msgs, "warn", lambda _message, **fields: commands.append(fields["command"]))
    monkeypatch.setattr(paths, "custom_home", lambda: True)
    monkeypatch.setattr(paths, "home", lambda: Path("/h o"))
    return commands


@pytest.mark.parametrize(("options", "given", "line"), [
    ({}, {"agent": None, "model": None}, "js --home '/h o' --session hint"),
    ({"chat": True, "no_tools": True, "no_env": True, "no_agents_md": True, "no_skills": True, "no_mcp": True},
     {"agent": "x", "model": "p/m"},
     "js --agent x --model p/m --chat --no-tools --no-env --no-agents-md --no-skills --no-mcp --home '/h o' --session hint"),
    ({"tools": "read,fetch:lazy", "you_are": ""}, {"agent": None, "model": None},
     "js --tools read,fetch:lazy --you-are '' --home '/h o' --session hint"),
    ({"system_prompt": "A prompt", "append_system_prompt": ("one", "two words")}, {"agent": None, "model": "m"},
     "js --model m --system-prompt 'A prompt' --append-system-prompt one --append-system-prompt 'two words'"
     " --home '/h o' --session hint"),
    ({"system_prompt_file": "/p f", "you_are": "voice", "tools": "read"}, {"agent": None, "model": None},
     "js --tools read --you-are voice --system-prompt-file '/p f' --home '/h o' --session hint"),
])
def test_continue_command_without_config(printed, options, given, line):
    with invocation(RunOptions(**options)):
        cli._say_continue(runtime.Telemetry(None), "hint", resume_prefix=None, **given)
    assert printed == [line]


_SAMPLING = {"temperature": 0.65, "top_k": 37, "dry_sequence_breakers": ["\n", ":"]}


@pytest.mark.parametrize(("route", "login", "sampled", "reasoning", "options", "line"), [
    ({}, None, {}, None, {}, "js --model prov/mod -r off --home '/h o' --session hint"),
    ({"agent_id": "other", "reasoning_effort": "high"}, None, _SAMPLING, None, {},
     "js --agent other --model prov/mod -r high --temperature 0.65 --top-k 37"
     """ --dry-sequence-breakers '["\\n", ":"]' --home '/h o' --session hint"""),
    ({"provider_base_url": "http://local.test/v1", "provider_id": "openai-responses"}, None, {}, "low", {"chat": True},
     "js -u responses+http://mod@local.test/v1 -r low --chat --home '/h o' --session hint"),
    ({"provider_base_url": "http://local.test/v1", "provider_id": "mine"}, "mine", {"min_p": 0.07}, None, {},
     "js --model mine/mod -r off --min-p 0.07 --home '/h o' --session hint"),
    ({"provider_id": None}, None, {}, "", {"tools": "read"},
     "js --model mod -r off --tools read --home '/h o' --session hint"),
])
def test_continue_command_with_config(monkeypatch, printed, route, login, sampled, reasoning, options, line):
    cfg = SimpleNamespace(**{"agent_id": "defaultagent", "provider_id": "prov", "provider_base_url": None,
                             "model": "mod", "reasoning_effort": None, **route})
    monkeypatch.setattr(cli.login_defaults, "login_for",
                        lambda _provider, _url: None if login is None else SimpleNamespace(provider_id=login))
    monkeypatch.setattr(cli.login_defaults, "sampling", lambda _cfg: sampling.Sampling.from_mapping(sampled))
    with invocation(RunOptions(**options)):
        cli._say_continue(runtime.Telemetry(None), "hint", resume_prefix=None, agent=None, model=None,
                          cfg=cfg, reasoning=reasoning)
    assert printed == [line]


def test_continue_command_after_a_prefix(monkeypatch, printed):
    cfg = SimpleNamespace(agent_id="zz", provider_id="prov", provider_base_url=None, model="mod", reasoning_effort=None)
    monkeypatch.setattr(cli.login_defaults, "login_for", lambda _provider, _url: None)
    monkeypatch.setattr(cli.login_defaults, "sampling", lambda _cfg: sampling.Sampling())
    cli._say_continue(runtime.Telemetry(None), "hint", resume_prefix="js-dev --x 'a b'", agent="zz", model=None, cfg=cfg)
    assert printed == ["js-dev --x 'a b' --model prov/mod -r off --home '/h o' --session hint"]


@pytest.mark.parametrize(("agent", "settings", "model", "custom_home", "line"), [
    ("defaultagent", {}, None, False, "js --session sub/name"),
    ("defaultagent", {}, "p/m x", False, "js --model 'p/m x' --session sub/name"),
    ("other", {}, "m", False, "js --agent other --model m --session sub/name"),
    ("defaultagent", {"DEFAULT_AGENT": "other"}, None, False, "js --agent defaultagent --session sub/name"),
    ("other", {}, "m", True, "js --agent other --model m --home '/h o' --session sub/name"),
])
def test_resume_hint_command(monkeypatch, printed, agent, settings, model, custom_home, line):
    monkeypatch.setattr(paths, "custom_home", lambda: custom_home)
    cfg = SimpleNamespace(session_file=Path("/s/dir/sub/name.jsonl"), sessions_dir=Path("/s/dir"),
                          agent_id=agent, settings=settings)
    repl._print_resume_hint(cfg, ReplState(messages=["one"], model=model))
    assert printed == [line]


@pytest.mark.parametrize(("target", "argv"), [
    (session_picker.SessionTarget(Path("/s/a.jsonl"), "/w", "research"), ["--session", "/s/a.jsonl", "-a", "research"]),
    (session_picker.SessionTarget(Path("/s/a.jsonl"), "/w", None, True), ["--session", "/s/a.jsonl", "-C", "/w"]),
    (session_picker.SessionTarget(Path("/s/a.jsonl"), None, "", True), ["--session", "/s/a.jsonl"]),
    (session_picker.SessionTarget(Path("/s/a.jsonl"), "/w", "r", True), ["--session", "/s/a.jsonl", "-a", "r", "-C", "/w"]),
])
def test_session_switch_command(monkeypatch, target, argv):
    ran = []
    monkeypatch.setattr(session_picker, "enter_session_dir", lambda _target: None)
    monkeypatch.setattr(cli.os, "execv", lambda program, words: ran.append(words))
    repl.exec_session(target)
    assert [words[3:] for words in ran] == [argv]
