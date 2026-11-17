"""Every js command line js writes or rewrites, word for word."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from js import cli, home_cli, loop_controller, paths, runtime, sampling, swarm
from js.cli_options import RunOptions, invocation


def _parse(argv: list[str]):
    """`argv` as js reads it; a command line js refuses fails the test."""
    parser = cli._build_parser()
    cli._refuse_glued_short_options(parser, argv)
    return parser.parse_args(argv)


@pytest.mark.parametrize(("argv", "sibling"), [
    (["-p", "hello"], ["-p", "-", "--swarm", "/r/b"]),
    (["--prompt", "hello", "--model", "m"], ["--prompt", "-", "--model", "m", "--swarm", "/r/b"]),
    (["-lp", "hello"], ["-p", "-", "--swarm", "/r/b"]),
    (["-lp=hello"], ["--prompt=-", "--swarm", "/r/b"]),
    (["--prompt=hello"], ["--prompt=-", "--swarm", "/r/b"]),
    (["-p"], ["-p", "-", "--swarm", "/r/b"]),
    (["-p", "-", "-m", "m"], ["-p", "-", "-m", "m", "--swarm", "/r/b"]),
    (["-p", "-4.5"], ["-p", "-", "--swarm", "/r/b"]),
    (["-p", "-v is broken"], ["-p", "-", "--swarm", "/r/b"]),
    (["-p", "--model", "m"], ["-p", "-", "--model", "m", "--swarm", "/r/b"]),
    (["-a", "a", "--swarm", "/r/a", "-s", "name", "-p", "x"],
     ["-a", "a", "--swarm", "/r/b", "-s", "name-b", "-p", "-"]),
    (["--swarm=/r/a", "--session=name"], ["--swarm=/r/b", "--session=name-b", "-p", "-"]),
    (["-s", "--model", "m"], ["-s", "--model", "m", "--swarm", "/r/b", "-p", "-"]),
    (["--session", "-", "--last"], ["--session", "--b", "--swarm", "/r/b", "-p", "-"]),
    (["--last", "-a", "agent", "--extra", "k=v", "--extra", "x=y"],
     ["-a", "agent", "--extra", "k=v", "--extra", "x=y", "--swarm", "/r/b", "-p", "-"]),
    (["-qn", "-p", "hi"], ["-qn", "-p", "-", "--swarm", "/r/b"]),
    (["--commit", "--", "--last"], ["--commit", "--", "--swarm", "/r/b", "-p", "-"]),
    (["-s=name", "-p", "x"], ["-s=name", "-p", "-", "--swarm", "/r/b"]),
    (["-p=hello"], ["-p=hello", "--swarm", "/r/b", "-p", "-"]),
    (["--session", "-5"], ["--session", "-5", "--swarm", "/r/b", "-p", "-"]),
    (["--swarm", "-x y"], ["--swarm", "-x y", "-p", "-"]),
    (["-p", "-a b"], ["-p", "-", "--swarm", "/r/b"]),
    (["--home", "/h", "-p", "x", "--you-are"], ["--home", "/h", "-p", "-", "--you-are", "--swarm", "/r/b"]),
    (["--you-are", "--chat", "--loop", "3"], ["--you-are", "--chat", "--loop", "3", "--swarm", "/r/b", "-p", "-"]),
    (["--system-prompt", "--last x", "--prompt", "--last"],
     ["--system-prompt", "--last x", "--prompt", "-", "--swarm", "/r/b"]),
])
def test_sibling_command(argv, sibling):
    _parse(argv)
    assert swarm.sibling_argv(["js", *argv], "/r/b", "b")[3:] == sibling


@pytest.mark.parametrize(("argv", "child"), [
    (["-C", "/tmp", "--loop", "10", "-m", "sol", "--extra", "x=y"], ["-C", "/tmp", "-m", "sol", "--extra", "x=y"]),
    (["--loop=45M", "--bench", "a"], ["--bench", "a"]),
    (["--loop"], []),
    (["--loop", "-p", "x"], ["-p", "x"]),
    (["-p", "--loop", "3"], ["-p"]),
    (["-lp", "--loop", "3"], ["-lp"]),
    (["-lp=x", "--loop", "3"], ["-lp=x"]),
    (["--loop", "3", "--loop", "5"], ["--loop", "3"]),
    (["--loop", "3", "-p", "--loop", "3"], ["-p", "--loop", "3"]),
    (["--loop", "3", "--prompt", "--loop", "3"], ["--prompt", "--loop", "3"]),
    (["--loop", "3", "-lp", "--loop", "3"], ["--loop", "3", "-lp"]),
    (["--extra", "x=--loop", "--loop", "2"], ["--extra", "x=--loop"]),
    (["-p", "hi", "--loop=2", "--loop", "2"], ["-p", "hi", "--loop=2"]),
    (["--loop", "2h", "-p", "two words"], ["-p", "two words"]),
])
def test_loop_child_command(argv, child):
    assert loop_controller.child_argv(argv, _parse(argv).loop) == child


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
    ({"provider_base_url": "http://local.test/v1", "provider_id": "openai"}, None, {}, "low", {"chat": True},
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
    monkeypatch.setattr(cli.model_client, "wire_name", lambda _provider: "responses")
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


@pytest.mark.parametrize(("agent", "settings", "model", "line"), [
    ("defaultagent", {}, None, "js --session sub/name"),
    ("defaultagent", {}, "p/m x", "js --model 'p/m x' --session sub/name"),
    ("other", {}, "m", "js --agent other --model m --session sub/name"),
    ("defaultagent", {"DEFAULT_AGENT": "other"}, None, "js --agent defaultagent --session sub/name"),
])
def test_resume_hint_command(printed, agent, settings, model, line):
    cfg = SimpleNamespace(session_file=Path("/s/dir/sub/name.jsonl"), sessions_dir=Path("/s/dir"),
                          agent_id=agent, settings=settings)
    cli._print_resume_hint(cfg, {"messages": ["one"], "model": model})
    assert printed == [line]


@pytest.mark.parametrize(("target", "argv"), [
    (cli._SessionTarget(Path("/s/a.jsonl"), "/w", "research"), ["--session", "/s/a.jsonl", "-a", "research"]),
    (cli._SessionTarget(Path("/s/a.jsonl"), "/w", None, True), ["--session", "/s/a.jsonl", "-C", "/w"]),
    (cli._SessionTarget(Path("/s/a.jsonl"), None, "", True), ["--session", "/s/a.jsonl"]),
    (cli._SessionTarget(Path("/s/a.jsonl"), "/w", "r", True), ["--session", "/s/a.jsonl", "-a", "r", "-C", "/w"]),
])
def test_session_switch_command(monkeypatch, target, argv):
    ran = []
    monkeypatch.setattr(cli, "_enter_session_dir", lambda _target: None)
    monkeypatch.setattr(cli.os, "execv", lambda program, words: ran.append(words))
    cli._exec_session(target)
    assert [words[3:] for words in ran] == [argv]
