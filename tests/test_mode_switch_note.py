"""A session that continues in a different mode from its last turn (-p versus
the REPL) tells the model once, on the next user message."""

from __future__ import annotations

import pytest

from js import cli, memory
from js.memory import load_messages
from repl_driver import run_async
from test_repl_harness import make_cfg

SESSION = "modeswitch"


@pytest.fixture
def home(monkeypatch, tmp_path):
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
    return sent


def _one_shot(text):
    assert cli.main(["--session", SESSION, "-p", text]) == 0


def _repl(monkeypatch, lines):
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


def _session_file(tmp_path):
    found = list((tmp_path / ".js" / "sessions").rglob(f"{SESSION}.jsonl"))
    assert len(found) == 1, found
    return found[0]


def _reminders(tmp_path, notice):
    return [m for m in load_messages(_session_file(tmp_path)) if notice in str(m.get("content", ""))]


def test_one_shot_then_repl_carries_the_interactive_reminder_once(monkeypatch, tmp_path, home):
    _one_shot("first")
    _repl(monkeypatch, ["second", "third"])

    assert home[0] == "first"
    assert home[1] == f"second\n\n{cli._MODE_SWITCH_NOTICES['repl']}"
    assert home[2] == "third"
    assert len(_reminders(tmp_path, cli._MODE_SWITCH_NOTICES["repl"])) == 1


def test_repl_then_one_shot_carries_the_one_shot_reminder(monkeypatch, tmp_path, home):
    _repl(monkeypatch, ["first"])
    _one_shot("second")

    assert home == ["first", f"second\n\n{cli._MODE_SWITCH_NOTICES['-p']}"]


def test_unchanged_mode_carries_no_reminder(monkeypatch, tmp_path, home):
    _one_shot("first")
    _one_shot("second")
    _repl(monkeypatch, ["third"])
    _repl(monkeypatch, ["fourth"])

    assert home[1] == "second"
    assert home[3] == "fourth"
    assert _reminders(tmp_path, cli._MODE_SWITCH_NOTICES["-p"]) == []


def test_a_repl_launch_with_no_turn_does_not_change_the_mode(monkeypatch, tmp_path, home):
    _one_shot("first")
    _repl(monkeypatch, [])
    _one_shot("second")

    assert home == ["first", "second"]


def test_async_repl_after_one_shot_carries_the_reminder(monkeypatch, tmp_path):
    cfg = make_cfg(tmp_path)
    cfg.prompts_dir.mkdir(parents=True)
    (cfg.prompts_dir / "01-prompt.md").write_text("SYSTEM\n", encoding="utf-8")
    memory.record_turn_mode(cfg.session_file, "-p")
    sent: list[str] = []

    async def run_turn_async_stub(cfg, system, messages, *_a, **_k):
        sent.append(str(messages[-1]["content"]))
        messages.append({"role": "assistant", "content": "ok"})

    monkeypatch.setattr(cli.runtime, "run_turn_async", run_turn_async_stub)

    run_async(monkeypatch, cfg, ["hello", "again"])

    assert sent == [f"hello\n\n{cli._MODE_SWITCH_NOTICES['repl']}", "again"]


def test_record_turn_mode_reports_only_a_change(tmp_path):
    session = tmp_path / "s.jsonl"

    assert memory.record_turn_mode(session, "-p") is None
    assert memory.record_turn_mode(session, "-p") is None
    assert memory.record_turn_mode(session, "repl") == "-p"
    assert memory.record_turn_mode(session, "repl") is None
    assert memory.record_turn_mode(session, "-p") == "repl"
