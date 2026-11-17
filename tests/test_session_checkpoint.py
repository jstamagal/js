"""A turn is saved at every tool boundary, so a killed js keeps what it did."""

from __future__ import annotations

import os
import signal
from pathlib import Path

from repl_driver import turn_stub
from test_steering import _call, _cfg, _text
from turn_driver import js

from js import cli, runtime, session_store
from js.memory import load_messages
from js.toolkit import ToolContext
from js.toolkit.core import Tool
from js.toolkit.registry import ToolRegistry

EXCHANGE = [
    {"role": "assistant", "content": "", "tool_calls": [{
        "id": "read-1", "type": "function", "function": {"name": "read", "arguments": "{}"},
    }]},
    {"role": "tool", "tool_call_id": "read-1", "name": "read", "content": "engines/basalt"},
]


def test_checkpoint_runs_after_each_tool_batch_with_its_results(monkeypatch, tmp_path):
    calls = 0

    async def model(**kwargs):
        nonlocal calls
        calls += 1
        return _call("probe", f"call_{calls}") if calls < 3 else _text("done")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", model)
    registry = ToolRegistry(tools=(Tool("probe", "Probe.", lambda: "probed", {}),), aliases={})
    messages = [{"role": "user", "content": "go"}]
    seen: list[list[str]] = []

    runtime.run_turn(
        _cfg(tmp_path), "SYS", messages, runtime.Telemetry(debug_log=None),
        tool_registry=registry, tool_context=ToolContext(cwd=tmp_path), suppress_output=True,
        checkpoint=lambda: seen.append([m["role"] for m in messages]),
    )

    assert seen == [
        ["user", "assistant", "tool"],
        ["user", "assistant", "tool", "assistant", "tool"],
    ]


def test_prompt_mode_turn_is_on_disk_before_the_run_ends(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    on_disk: list[list[dict]] = []

    def turn(cfg, system, messages, telemetry, checkpoint=None, **kwargs):
        messages.extend(EXCHANGE)
        checkpoint()
        # What a SIGKILL here would leave behind.
        on_disk.append(load_messages(cfg.session_file))
        messages.append({"role": "assistant", "content": "done"})

    monkeypatch.setattr(runtime, "run_turn_async", turn_stub(turn))
    assert js(monkeypatch, "--session", "kept", "-p", "look") == 0
    assert on_disk == [[{"role": "user", "content": "look"}, *EXCHANGE]]


def test_prompt_mode_sigterm_keeps_the_turn(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))

    def turn(cfg, system, messages, telemetry, **kwargs):
        messages.extend(EXCHANGE)
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(runtime, "run_turn_async", turn_stub(turn))
    assert js(monkeypatch, "--session", "termed", "-p", "look") == 130
    session = session_store.folder_for(Path.cwd()) / "termed.jsonl"
    assert load_messages(session)[:3] == [{"role": "user", "content": "look"}, *EXCHANGE]
    assert signal.getsignal(signal.SIGTERM) is not cli._raise_keyboard_interrupt


def test_conversation_turn_is_on_disk_before_it_ends(monkeypatch, tmp_path):
    import asyncio

    from js.config import Config
    from js.conversation import Conversation

    cfg = Config(model="offline-test-model", trace=False, session_file=tmp_path / "sessions" / "chat.jsonl",
                 settings={"COMPACTION_AUTO": False})
    on_disk: list[list[str]] = []

    async def turn(cfg, system, messages, telemetry, checkpoint=None, **kwargs):
        messages.extend(EXCHANGE)
        checkpoint()
        on_disk.append([m["role"] for m in load_messages(cfg.session_file)])
        messages.append({"role": "assistant", "content": "done"})

    monkeypatch.setattr(runtime, "run_turn_async", turn)
    chat = Conversation(cfg, "SYS")
    assert asyncio.run(chat.ask("look")) == "done"
    assert on_disk == [["user", "assistant", "tool"]]
