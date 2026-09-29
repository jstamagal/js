"""/cd moves the session's working directory; under `js -C`, /add changes
what the jail shows. Each change reaches the model once as a reminder
on the next user message, and a resumed session is put back where it was."""

from __future__ import annotations

import shutil

import pytest

from js import cli, jail, runtime
from js import messages as msgs
from js.toolkit import call_tool, process_net
from js.toolkit.registry import build_default_registry

SESSION = "workspace"
needs_bwrap = pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is not installed")


@pytest.fixture
def turns(monkeypatch, tmp_path):
    """Each REPL turn runs the probes in `turns.probes` with the REPL's tool
    context and records the user message and the probe results."""
    for name in ("JS_AGENT", "JS_SESSION", "JS_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runtime.T.STOCK_CONTEXT, "cwd", runtime.T.STOCK_CONTEXT.cwd)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_maybe_auto_compact", lambda *_a, **_k: None)

    class Turns:
        sent: list[str] = []
        results: list[dict[str, str]] = []
        probes: dict[str, object] = {}

    record = Turns()
    record.sent, record.results, record.probes = [], [], {}

    def run_turn_stub(cfg, system, messages, *_a, **_k):
        record.sent.append(str(messages[-1]["content"]))
        context = runtime.T.STOCK_CONTEXT
        record.results.append({name: probe(context) for name, probe in record.probes.items()})
        messages.append({"role": "assistant", "content": "ok"})

    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)
    return record


def repl(monkeypatch, args, lines):
    class PromptSessionStub:
        def __init__(self, history, **kwargs):
            self.lines = iter(lines)

        def prompt(self, *_args, **_kwargs):
            try:
                return next(self.lines)
            except StopIteration:
                raise EOFError from None

    monkeypatch.setattr(cli, "PromptSession", PromptSessionStub)
    cli.main([*args, "--blocking", "--session", SESSION])


def shell_out(command):
    def probe(context):
        return process_net.shell(command, context=context)
    return probe


def read(path):
    def probe(context):
        return call_tool(build_default_registry().resolve("read"), {"path": str(path)}, context)
    return probe


@pytest.fixture
def places(tmp_path):
    root = tmp_path / "foo"
    (root / "sub").mkdir(parents=True)
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "data.txt").write_text("extra-data\n")
    return root, extra



@needs_bwrap
def test_add_is_read_only_unless_rw(monkeypatch, places, turns):
    root, extra = places
    turns.probes = {"touch": shell_out(f"touch {extra}/new")}

    repl(monkeypatch, ["-C", str(root)], [f"/add {extra}", "one", f"/add {extra}:rw", "two"])

    read_only, read_write = turns.results
    assert "exit=0" not in read_only["touch"]
    assert "exit=0" in read_write["touch"]
    assert (extra / "new").exists()


@needs_bwrap
def test_cd_moves_where_commands_run(monkeypatch, places, turns):
    root, _extra = places
    turns.probes = {"pwd": shell_out("pwd")}

    repl(monkeypatch, ["-C", str(root)], ["/cd sub", "where"])

    assert f"\n{(root / 'sub').resolve()}\n" in turns.results[0]["pwd"]
    assert cli._CD_NOTICE.format(path=(root / "sub").resolve()) in turns.sent[0]


@needs_bwrap
def test_cd_outside_the_jail_is_refused(monkeypatch, places, turns):
    root, extra = places
    turns.probes = {"pwd": shell_out("pwd")}

    repl(monkeypatch, ["-C", str(root)], [f"/cd {extra}", "where"])

    assert f"\n{root.resolve()}\n" in turns.results[0]["pwd"]
    assert turns.sent == ["where"]


def test_cd_without_a_jail_moves_the_session(monkeypatch, places, turns):
    _root, extra = places
    turns.probes = {"pwd": shell_out("pwd")}

    repl(monkeypatch, [], [f"/cd {extra}", "where"])

    assert f"\n{extra.resolve()}\n" in turns.results[0]["pwd"]


def test_add_needs_a_jail(places):
    _root, extra = places
    state: dict = {}

    assert cli._cmd_add(str(extra), state, None).message is msgs.NO_JAIL
    assert "pending_notes" not in state



@needs_bwrap
def test_a_resume_restores_the_binds_and_the_working_directory(monkeypatch, places, turns):
    root, extra = places
    repl(monkeypatch, ["-C", str(root)], [f"/add {extra}:rw", f"/cd {extra}", "set up"])
    turns.probes = {"pwd": shell_out("pwd"), "touch": shell_out("touch resumed")}

    repl(monkeypatch, ["-C", str(root)], ["resumed"])

    after = turns.results[-1]
    assert f"\n{extra.resolve()}\n" in after["pwd"]
    assert "exit=0" in after["touch"]
    assert (extra / "resumed").exists()
    assert jail.active().added == [jail.Bind(extra, True)]


def test_a_resume_restores_the_working_directory(monkeypatch, places, turns):
    root, _extra = places
    repl(monkeypatch, [], [f"/cd {root / 'sub'}", "set up"])
    # A new launch starts wherever it is started.
    monkeypatch.chdir(root.parent)
    runtime.T.STOCK_CONTEXT.cwd = root.parent
    turns.probes = {"pwd": shell_out("pwd")}

    repl(monkeypatch, [], ["resumed"])

    assert f"\n{(root / 'sub').resolve()}\n" in turns.results[-1]["pwd"]


@needs_bwrap
def test_add_shows_a_path(monkeypatch, places, turns):
    root, extra = places
    turns.probes = {"shell": shell_out(f"cat {extra}/data.txt"), "read": read(extra / "data.txt")}

    repl(monkeypatch, ["-C", str(root)], ["before", f"/add {extra}", "after"])

    hidden, shown = turns.results
    assert "exit=0" not in hidden["shell"]
    assert hidden["read"].startswith("ERROR:")
    assert "exit=0" in shown["shell"] and "extra-data" in shown["shell"]
    assert "extra-data" in shown["read"]
    assert cli._ADD_NOTICE.format(path=extra, access="read-only") in turns.sent[1]
