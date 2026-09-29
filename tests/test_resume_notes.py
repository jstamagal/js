"""Resume picks its model and tells the model what changed (js-1g1.21).

An explicit -m beats the stamp; a stamp whose provider has no login falls back
to the configured model with one line; a model switch puts one note on the
next request; a resumed session whose last turn was cut off gets one note."""

from __future__ import annotations

from pathlib import Path

import pytest

from js import cli, runtime, session_store
from js import memory as M
from js.config import from_env
from js.memory import load_messages
from repl_driver import LineSession


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime.model_metadata, "accepts_image_input", lambda *a, **k: False)
    monkeypatch.setattr(runtime, "_resolve_context_window", lambda *a, **k: 1_000_000)
    monkeypatch.setattr(runtime.model_metadata, "resolve_max_output", lambda *a, **k: 4096)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)


class Turns:
    """Stands in for runtime.run_turn: records the model and the user message
    each turn ran with, and answers."""

    def __init__(self):
        self.models: list[str] = []
        self.users: list = []

    def __call__(self, cfg, system, messages, *a, **k):
        self.models.append(cfg.model)
        self.users.append(messages[-1]["content"])
        messages.append({"role": "assistant", "content": f"on {cfg.model}"})


def _prompt(monkeypatch, argv) -> Turns:
    turns = Turns()
    monkeypatch.setattr(cli.runtime, "run_turn", turns)
    assert cli.main(argv) == 0
    return turns


def _repl(monkeypatch, argv, lines) -> Turns:
    turns = Turns()
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "PromptSession", lambda *a, **k: LineSession(lines))
    monkeypatch.setattr(cli.runtime, "run_turn", turns)
    assert cli.main(["--blocking", *argv]) == 0
    return turns


def _session(name: str, messages: list[dict], stamp: dict | None = None) -> Path:
    path = session_store.folder_for(Path.cwd()) / f"{name}.jsonl"
    for message in messages:
        M.append_message(path, message, stamp=stamp)
    return path


def _switch_note(previous: str, current: str) -> str:
    provider = from_env().provider_id
    return cli._MODEL_SWITCH_NOTICE.format(previous=cli._stamp_label(previous, provider),
                                           current=cli._stamp_label(current, provider))


# --- -m beats the stamp ----------------------------------------------------------


@pytest.mark.parametrize("mode", ["-p", "repl"])
def test_an_explicit_model_beats_the_stamp_on_resume(monkeypatch, mode):
    _prompt(monkeypatch, ["--session", "s", "--model", "model-x", "-p", "one"])

    if mode == "-p":
        turns = _prompt(monkeypatch, ["--session", "s", "--model", "model-z", "-p", "two"])
    else:
        turns = _repl(monkeypatch, ["--session", "s", "--model", "model-z"], ["two"])

    assert turns.models == ["model-z"]


# --- a stamp without a login ----------------------------------------------------------


def _stamped_elsewhere() -> Path:
    return _session("elsewhere", [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
                    stamp=M.stamp_for("gpt-elsewhere", "openai", None))


def test_a_prompt_resume_whose_stamp_has_no_login_runs_on_the_configured_model(monkeypatch, capsys):
    _stamped_elsewhere()
    configured = from_env().model

    turns = _prompt(monkeypatch, ["--session", "elsewhere", "-p", "again"])

    assert turns.models == [configured]
    lines = [line for line in capsys.readouterr().err.splitlines() if "openai/gpt-elsewhere" in line]
    assert len(lines) == 1 and configured in lines[0]


def test_a_repl_resume_whose_stamp_has_no_login_runs_on_the_configured_model(monkeypatch, capsys):
    _stamped_elsewhere()
    configured = from_env().model

    turns = _repl(monkeypatch, ["--session", "elsewhere"], ["again"])

    assert turns.models == [configured]
    out = capsys.readouterr()
    lines = [line for line in (out.out + out.err).splitlines() if "openai/gpt-elsewhere" in line]
    assert len(lines) == 1 and configured in lines[0]


def test_a_stamp_on_a_logged_in_provider_is_resumed(monkeypatch):
    path = _stamped_elsewhere()
    monkeypatch.setattr(cli.routing, "_saved_login", lambda provider: object() if provider == "openai" else None)

    assert cli._stamp_without_login(cli.last_stamp(path), from_env()) is None


# --- the model-switch note ------------------------------------------------------------


def test_a_repl_model_switch_puts_one_note_on_the_next_request(monkeypatch):
    turns = _repl(monkeypatch, ["--model", "model-x"], ["one", "/model model-y", "two", "three"])

    assert turns.models == ["model-x", "model-y", "model-y"]
    assert turns.users == ["one", f"two\n\n{_switch_note('model-x', 'model-y')}", "three"]


def test_a_repl_switch_back_before_the_next_request_adds_no_note(monkeypatch):
    turns = _repl(monkeypatch, ["--model", "model-x"], ["one", "/model model-y", "/model model-x", "two"])

    assert turns.users == ["one", "two"]


def test_an_async_repl_model_switch_puts_one_note_on_the_next_request(monkeypatch):
    import asyncio

    from js import supervisor
    from repl_driver import run_async

    users: list = []
    first_turn_started = asyncio.Event()

    async def turn(cfg, system, messages, telemetry, **kwargs):
        first_turn_started.set()
        users.append(messages[-1]["content"])
        messages.append({"role": "assistant", "content": f"on {cfg.model}"})

    async def script(on_line):
        await on_line("one")
        # A queued line takes the live model when its turn starts, so /model
        # waits until the first turn has ended.
        await first_turn_started.wait()
        for job in supervisor.get_current().jobs("turn"):
            await job.task
        await on_line("/model model-y")
        await on_line("two")
        await on_line("three")

    monkeypatch.setattr(cli.runtime, "run_turn_async", turn)
    run_async(monkeypatch, from_env(), script, model="model-x")

    assert users == ["one", f"two\n\n{_switch_note('model-x', 'model-y')}", "three"]


def test_a_prompt_run_on_another_model_carries_the_note_once(monkeypatch):
    _prompt(monkeypatch, ["--session", "s", "--model", "model-x", "-p", "one"])

    switched = _prompt(monkeypatch, ["--session", "s", "--model", "model-y", "-p", "two"])
    stayed = _prompt(monkeypatch, ["--session", "s", "-p", "three"])

    assert switched.users == [f"two\n\n{_switch_note('model-x', 'model-y')}"]
    assert stayed.models == ["model-y"]
    assert stayed.users == ["three"]


# --- the cut-off note ------------------------------------------------------------------


CUT_OFF_ENDINGS = {
    "unanswered prompt": [{"role": "user", "content": "q"}],
    "tool result": [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read", "content": "x"},
    ],
    "dangling calls": [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
    ],
    "incomplete answer": [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "partial", "incomplete_reason": "cancelled"},
    ],
}


@pytest.mark.parametrize("ending", sorted(CUT_OFF_ENDINGS))
def test_a_turn_that_ended_without_a_finished_reply_is_cut_off(ending):
    assert M.turn_cut_off(CUT_OFF_ENDINGS[ending])


@pytest.mark.parametrize("messages", [
    [],
    [{"role": "user", "content": "q"}, {"role": "assistant", "content": "done"}],
    [{"role": "user", "content": "q"}, {"role": "assistant", "content": "done"},
     {"role": "user", "content": cli._PROMPT_CHANGED_NOTICE}],
    [{"role": "user", "content": "<compaction-summary>\nall of it\n</compaction-summary>"}],
])
def test_a_finished_turn_is_not_cut_off(messages):
    assert not M.turn_cut_off(messages)


def test_a_prompt_resume_after_a_cut_off_turn_carries_the_note_once(monkeypatch):
    _session("cut", CUT_OFF_ENDINGS["incomplete answer"])

    first = _prompt(monkeypatch, ["--session", "cut", "-p", "go on"])
    second = _prompt(monkeypatch, ["--session", "cut", "-p", "and then"])

    assert first.users == [f"go on\n\n{cli._CUT_OFF_NOTICE}"]
    assert second.users == ["and then"]


def test_a_repl_resume_after_a_cut_off_turn_carries_the_note_once(monkeypatch):
    path = _session("cut", CUT_OFF_ENDINGS["tool result"])

    turns = _repl(monkeypatch, ["--session", "cut"], ["go on", "and then"])

    assert turns.users == [f"go on\n\n{cli._CUT_OFF_NOTICE}", "and then"]
    assert load_messages(path)[3]["content"] == f"go on\n\n{cli._CUT_OFF_NOTICE}"


def test_a_resume_after_a_finished_turn_carries_no_note(monkeypatch):
    _session("done", [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}])

    turns = _repl(monkeypatch, ["--session", "done"], ["next"])

    assert turns.users == ["next"]
