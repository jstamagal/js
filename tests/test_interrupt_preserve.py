"""ctrl+c during a turn must keep the partial work (and survive a reload), not
wipe it — except when the turn produced nothing, where the bare prompt is dropped."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from repl_driver import run_async, turn_stub

from js import cli
from js.config import from_env
from js.memory import append_message, load_messages


def _session_file(tmp_path):
    found = list((tmp_path / ".js" / "sessions").rglob("*.jsonl"))
    assert len(found) == 1, found
    return found[0]


def _drive_repl(monkeypatch, tmp_path, run_turn_stub):
    """Run the REPL for exactly one user line, then EOF to exit."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.runtime, "run_turn_async", turn_stub(run_turn_stub))
    run_async(monkeypatch, from_env(), ["please do the thing"], wait=True)


def test_interrupt_keeps_partial_work_across_reload(monkeypatch, tmp_path):
    def run_turn_stub(cfg, system, messages, telemetry, **kwargs):
        # Simulate a turn that did real work (an assistant tool_call) before ^C cancelled it.
        messages.append({
            "role": "assistant",
            "content": "starting",
            "tool_calls": [{"id": "call_1", "type": "function",
                            "function": {"name": "read", "arguments": "{}"}}],
        })
        raise asyncio.CancelledError

    _drive_repl(monkeypatch, tmp_path, run_turn_stub)

    reloaded = load_messages(_session_file(tmp_path))
    # User prompt + the partial assistant turn survive; the orphaned tool_call is
    # healed with a synthetic result on load (so the next turn is valid).
    assert [m["role"] for m in reloaded] == ["user", "assistant", "tool"]
    assert reloaded[0]["content"] == "please do the thing"
    assert reloaded[1]["tool_calls"][0]["id"] == "call_1"
    assert "interrupted" in reloaded[2]["content"].lower()


def test_turn_persistence_journals_mutated_history_without_overwriting(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    session_file = tmp_path / "session.jsonl"
    append_message(session_file, {"role": "user", "content": "old work"})
    append_message(session_file, {"role": "assistant", "content": "before normalization"})
    seen: dict = {}

    def run_turn_stub(cfg, system, messages, telemetry, **kwargs):
        seen["archived"] = session_file.read_bytes()
        index = next(i for i, m in enumerate(messages) if m.get("content") == "before normalization")
        messages[index] = {"role": "assistant", "content": "after normalization"}
        messages.append({"role": "assistant", "content": "partial work"})
        seen["messages"] = list(messages)

    monkeypatch.setattr(cli.runtime, "run_turn_async", turn_stub(run_turn_stub))
    run_async(monkeypatch, replace(from_env(), session_file=session_file), ["new work"], wait=True)

    assert [m["content"] for m in seen["messages"]][-3:] == ["after normalization", "new work", "partial work"]
    assert load_messages(session_file) == seen["messages"]
    assert session_file.read_bytes().startswith(seen["archived"])


def test_interrupt_with_no_work_drops_the_bare_prompt(monkeypatch, tmp_path):
    def run_turn_stub(cfg, system, messages, telemetry, **kwargs):
        raise asyncio.CancelledError  # stopped before the model produced anything

    _drive_repl(monkeypatch, tmp_path, run_turn_stub)

    # Nothing worth keeping → the user prompt is rolled back on reload too.
    assert load_messages(_session_file(tmp_path)) == []
