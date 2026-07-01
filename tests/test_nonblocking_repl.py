"""The --nonblocking REPL drives one real turn end-to-end on the async loop:
prompt_async → queue → supervised _do_turn → run_turn_async → persist → clean
EOF shutdown. Headless: a stub session feeds lines, run_turn_async is stubbed,
and patch_stdout is neutralized (prompt_toolkit's terminal machinery is its own
concern, not ours)."""

from __future__ import annotations

import contextlib

from js import cli
from js.memory import load_messages


def _session_file(tmp_path):
    found = list((tmp_path / ".local" / "share" / "js" / "sessions").rglob("*.jsonl"))
    assert len(found) == 1, found
    return found[0]


def _drive_async_repl(monkeypatch, tmp_path, lines, run_turn_async_stub):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    class PromptSessionStub:
        def __init__(self, history, **kwargs):
            self._lines = iter(lines)

        async def prompt_async(self, *_args, **_kwargs):
            try:
                return next(self._lines)
            except StopIteration:
                raise EOFError  # ends the REPL loop cleanly

    monkeypatch.setattr(cli, "PromptSession", PromptSessionStub)
    monkeypatch.setattr(cli, "patch_stdout", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(cli.runtime, "run_turn_async", run_turn_async_stub)
    return cli.main(["--nonblocking"])


def test_nonblocking_repl_runs_a_turn_and_persists(monkeypatch, tmp_path):
    async def run_turn_async_stub(cfg, system, messages, telemetry, **kwargs):
        messages.append({"role": "assistant", "content": "did the thing"})

    rc = _drive_async_repl(monkeypatch, tmp_path, ["please do the thing"], run_turn_async_stub)
    assert rc == 0

    reloaded = load_messages(_session_file(tmp_path))
    assert [m["role"] for m in reloaded] == ["user", "assistant"]
    assert reloaded[0]["content"] == "please do the thing"
    assert reloaded[1]["content"] == "did the thing"


def test_nonblocking_repl_empty_line_then_eof_is_clean(monkeypatch, tmp_path):
    async def run_turn_async_stub(cfg, system, messages, telemetry, **kwargs):
        raise AssertionError("no turn should run for a blank line")

    rc = _drive_async_repl(monkeypatch, tmp_path, ["   "], run_turn_async_stub)
    assert rc == 0
    assert load_messages(_session_file(tmp_path)) == []
