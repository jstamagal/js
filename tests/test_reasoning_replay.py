"""Reasoning survives live turns and session resume through the real SDK serializer."""

from __future__ import annotations

import json
from dataclasses import replace

import ai
import pytest
from ai.providers.openai.protocol import _messages_to_openai

from js import cli, memory, model_client, runtime
from js.config import from_env
from js.toolkit import ToolContext


_THOUGHT = " first thought\n  keep spacing and Unicode: λ\n"


@pytest.fixture
def replay(monkeypatch, tmp_path):
    cfg = from_env(
        agent_id="replay",
        session="replay",
        extras=[
            "model.id=qwen3.8-27b",
            "provider.id=openai",
            "provider.base_url=http://llamacpp.test/v1",
            "provider.api_key=fixture",
            "model.vision=off",
            "model.context_window=262144",
            "model.max_output_tokens=64",
            "runtime.debug_autolog=off",
            "runtime.transcript_log=off",
        ],
    )
    prompts = tmp_path / ".config" / "js" / "agents" / "replay"
    prompts.mkdir(parents=True)
    (prompts / "01-prompt.md").write_text("SYSTEM\n")
    cfg = replace(cfg, prompts_dir=prompts, project_dir=tmp_path)
    context = ToolContext(cwd=tmp_path)
    monkeypatch.setattr(runtime.T, "DEFAULT_CONTEXT", context)
    monkeypatch.setattr(cli, "_from_env", lambda *args, **kwargs: cfg)
    wire = []

    async def capture(**kwargs):
        wire.append(await _messages_to_openai(kwargs["messages"]))
        assistant = ai.assistant_message(ai.thinking(_THOUGHT), "answer")
        return model_client.ModelStreamResult(
            text="answer", tool_calls=[], reasoning=_THOUGHT, usage=None,
            finish_reason="stop", assistant_message=assistant,
        )

    # Only the network/response boundary is replaced. Production runtime,
    # message reconstruction and the SDK's OpenAI serializer all execute.
    monkeypatch.setattr(model_client, "_stream_async", capture)
    return cfg, wire, context


def test_live_next_turn_retains_tool_free_reasoning(replay):
    cfg, wire, context = replay
    messages = [{"role": "user", "content": "first"}]
    for prompt in (None, "next"):
        if prompt is not None:
            messages.append({"role": "user", "content": prompt})
        runtime.run_turn(
            cfg, "SYSTEM", messages, runtime.Telemetry(None),
            tool_context=context, trace_override=False, suppress_output=True,
        )

    prior = next(m for m in wire[1] if m["role"] == "assistant")
    assert prior["reasoning"] == _THOUGHT
    assert prior["content"] == "answer"
    assert wire[1][:len(wire[0])] == wire[0]


@pytest.mark.parametrize("mode", ["prompt", "repl"])
def test_resume_retains_reasoning_without_rewriting_the_journal(replay, monkeypatch, mode):
    cfg, wire, _context = replay
    memory.append_message(cfg.session_file, {"role": "user", "content": "first"})
    memory.append_message(cfg.session_file, {
        "role": "assistant", "content": "answer", "reasoning_content": _THOUGHT,
    })
    original = cfg.session_file.read_bytes()
    if mode == "prompt":
        argv = ["-p", "next"]
    else:
        class PromptSession:
            def __init__(self, *args, **kwargs):
                self.lines = iter(["next", "exit"])

            def prompt(self, *args, **kwargs):
                return next(self.lines)

        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(cli, "PromptSession", PromptSession)
        argv = ["--blocking"]
    assert cli.main(argv) == 0
    prior = next(m for m in wire[0] if m["role"] == "assistant")
    assert prior["reasoning"] == _THOUGHT
    assert prior["content"] == "answer"
    assert cfg.session_file.read_bytes().startswith(original)
    records = [json.loads(line) for line in cfg.session_file.read_text().splitlines()]
    assert not any(r.get("marker", "").startswith("rollback_to:") for r in records)
    reloaded = memory.load_messages(cfg.session_file, preserve_reasoning=True)
    assert [m["reasoning_content"] for m in reloaded if m["role"] == "assistant"] == [
        _THOUGHT, _THOUGHT,
    ]
    saved = cfg.session_file.read_bytes()
    memory.persist_messages(cfg.session_file, reloaded)
    assert cfg.session_file.read_bytes() == saved


@pytest.mark.parametrize(("provider_id", "full_replay"), [
    ("openai", True), ("openai-completions", True), ("ollama", True), ("mimo", True),
    ("deepseek", False), ("anthropic", False), ("openai-responses", False),
])
def test_reasoning_replay_policy_is_transport_specific(provider_id, full_replay):
    messages = model_client.history_to_ai_messages("SYSTEM", [
        {"role": "assistant", "content": "answer", "reasoning_content": _THOUGHT},
        {"role": "assistant", "content": "", "reasoning_content": _THOUGHT,
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "name": "read", "content": "ok"},
    ], provider_id=provider_id)
    assert bool(messages[1].reasoning) is full_replay
    assert messages[2].reasoning == _THOUGHT
