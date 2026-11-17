"""A `-p` turn: what its session file holds, and when."""

from __future__ import annotations

from unittest.mock import AsyncMock

import ai
import ai.types.usage
from repl_driver import turn_stub
from test_agent_messages import _agent, _model
from turn_driver import js

from js import cli, memory, messages as msgs, runtime, session_store
from js.memory import load_messages
from js.model_client import ModelStreamResult


def _answer(kwargs: dict, text: str, prompt_tokens: int = 0) -> ModelStreamResult:
    kwargs["on_text"](text)
    return ModelStreamResult(
        text=text, tool_calls=[], reasoning="",
        usage=ai.types.usage.Usage(input_tokens=prompt_tokens, output_tokens=1),
        finish_reason="stop", assistant_message=ai.assistant_message(text),
    )


def _session(tmp_path, name: str):
    return session_store.folder_for(tmp_path) / f"{name}.jsonl"


def test_the_prompt_is_in_the_session_before_the_model_is_called(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch, {})
    session = _session(tmp_path, "kept")
    on_disk: list[list[dict]] = []

    def stream(**kwargs):
        on_disk.append(load_messages(session))
        return _answer(kwargs, "done")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    assert js(monkeypatch, "--session", "kept", "-p", "look") == 0
    assert on_disk == [[{"role": "user", "content": "look"}]]


def test_no_save_continues_a_session_without_writing_to_it(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch, {})
    session = _session(tmp_path, "kept")
    memory.append_message(session, {"role": "user", "content": "earlier question"})
    memory.append_message(session, {"role": "assistant", "content": "earlier answer"})
    before = session.read_bytes()
    requests = _model(monkeypatch, ["done"])

    assert js(monkeypatch, "-n", "--session", "kept", "-p", "look") == 0
    assert requests[0][-3:] == [("user", "earlier question"), ("assistant", "earlier answer"), ("user", "look")]
    assert session.read_bytes() == before


def test_the_reply_is_stamped_with_the_effort_the_run_names(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch, {})
    session = _session(tmp_path, "kept")
    _model(monkeypatch, ["first", "second"])

    assert js(monkeypatch, "--session", "kept", "-r", "high", "-p", "one") == 0
    assert memory.last_reply_stamp(session)["reasoning"] == "high"
    # A resumed session runs on, and stamps, the effort of its last stamp.
    assert js(monkeypatch, "--session", "kept", "-p", "two") == 0
    assert memory.last_reply_stamp(session)["reasoning"] == "high"


def test_the_check_after_the_turn_reads_the_window_context_size_per_model_sets(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch, {})
    monkeypatch.setattr(runtime.model_client, "stream_model_async",
                        lambda **kwargs: _answer(kwargs, "done", prompt_tokens=180_000))
    compact = AsyncMock(return_value=msgs.COMPACTED.said(keep_from=1, total=2, model="fixture"))
    monkeypatch.setattr(cli.compaction, "compact_now", compact)

    assert js(monkeypatch, "--session", "kept",
              "--extra", 'CONTEXT_SIZE_PER_MODEL={"offline-test-model": 200000}', "-p", "look") == 0
    assert compact.await_count == 1


def test_a_debug_file_run_traces_its_turn(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch, {})
    monkeypatch.setenv("JS_TRACE", "off")
    traced: list[bool] = []

    def turn(cfg, system, messages, telemetry, **kwargs):
        traced.append(kwargs["trace_override"])
        messages.append({"role": "assistant", "content": "done"})

    monkeypatch.setattr(runtime, "run_turn_async", turn_stub(turn))
    assert js(monkeypatch, "-n", "-p", "look") == 0
    assert js(monkeypatch, "-n", "--debug-file", str(tmp_path / "trace.log"), "-p", "look") == 0
    assert traced == [False, True]
