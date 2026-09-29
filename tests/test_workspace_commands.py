"""/cd moves the session's working directory. The change reaches the model once as a reminder
on the next user message, and a resumed session is put back where it was."""

from __future__ import annotations

import shutil

import pytest

from js import cli, runtime
from js.toolkit import process_net

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



@pytest.fixture
def places(tmp_path):
    root = tmp_path / "foo"
    (root / "sub").mkdir(parents=True)
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "data.txt").write_text("extra-data\n")
    return root, extra




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





def test_a_resume_restores_the_working_directory(monkeypatch, places, turns):
    root, _extra = places
    repl(monkeypatch, [], [f"/cd {root / 'sub'}", "set up"])
    # A new launch starts wherever it is started.
    monkeypatch.chdir(root.parent)
    runtime.T.STOCK_CONTEXT.cwd = root.parent
    turns.probes = {"pwd": shell_out("pwd")}

    repl(monkeypatch, [], ["resumed"])

    assert f"\n{(root / 'sub').resolve()}\n" in turns.results[-1]["pwd"]
