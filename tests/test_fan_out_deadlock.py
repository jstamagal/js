"""Regression for the nested-fan-out pool inversion (SWEEP_REVIEW #1/#2).

The non-blocking REPL runs every turn on ONE loop whose default executor is a
bounded js-dispatch pool. Before the fix, a parent turn dispatching a `task`
call parked a pool thread blocked on its whole subtree; nested children did the
same, so a wide-enough two-level fan-out exhausted the pool and the grandchildren
could never get a thread for their own leaf dispatch -> hard deadlock.

This test wires that exact topology on a deliberately tiny (2-worker) pool: a
parent fans out to N children, each child fans out to one grandchild, and each
grandchild runs a real leaf tool (todo_write) that needs a pool thread. With the
fix, fan-out waits run ON the loop (no thread held) so the grandchildren always
get a worker and the whole tree settles well inside the timeout. Without it, the
`asyncio.wait_for` trips and the test fails (rather than hanging CI forever).
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from js import runtime, supervisor
from js.config import Config
from js.toolkit import ToolContext
from js.toolkit.registry import build_default_registry
from js.model_client import ModelStreamResult, ModelToolCall
import ai


def _stop(text: str) -> ModelStreamResult:
    return ModelStreamResult(
        text=text,
        tool_calls=[],
        reasoning="",
        usage=ai.types.usage.Usage(input_tokens=0, output_tokens=len(text)),
        finish_reason="stop",
        assistant_message=ai.assistant_message(text),
    )


def _tool(name: str, args: str, call_id: str) -> ModelStreamResult:
    return ModelStreamResult(
        text="",
        tool_calls=[ModelToolCall(id=call_id, name=name, arguments=args)],
        reasoning="",
        usage=ai.types.usage.Usage(input_tokens=0, output_tokens=len(args)),
        finish_reason="tool_calls",
        assistant_message=ai.assistant_message(""),
    )


def _make_cfg(tmp_path: Path, agent: str, prompts: Path) -> Config:
    base = tmp_path / ".js" / "sessions" / agent
    return Config(
        agent_id=agent,
        agent_dir=base,
        model="offline-test-model",
        provider_id=None,
        provider_base_url=None,
        provider_api_key=None,
        reasoning_effort=None,
        max_output_tokens=None,
        max_tool_iterations=5,
        max_bash_output_bytes=65536,
        max_tool_result_bytes=65536,
        fetch_timeout_s=5,
        debug_log=None,
        trace=False,
        history_file=base / ".history",
        sessions_dir=base,
        session_file=base / "auto.jsonl",
        prompts_dir=prompts,
    )


def _pending_user_text(messages: list) -> str:
    """The user prompt this call must answer, or '' when the last message is a
    tool result (meaning the model already acted this turn and should stop)."""
    if not messages:
        return ""
    last = messages[-1]
    if getattr(last, "role", None) != "user":
        return ""
    parts = getattr(last, "parts", None) or []
    return getattr(parts[0], "text", "") if parts else ""


def test_nested_fan_out_does_not_deadlock_bounded_pool(monkeypatch, tmp_path):
    # A worker agent that can itself fan out (task) AND run a real leaf tool
    # (todo_write) — so a child can spawn a grandchild whose leaf dispatch needs
    # a pool thread while the parent/child fan-out waits are outstanding.
    worker = tmp_path / "prompts" / "worker"
    worker.mkdir(parents=True)
    (worker / "00-tools.yaml").write_text("tools:\n  - task\n  - todo_write\n", encoding="utf-8")
    (worker / "01-body.md").write_text("WORKER\n", encoding="utf-8")
    prompt_root = worker.parent

    def from_env_stub(*, save_session: bool = True):
        cfg = _make_cfg(tmp_path, "defaultagent", prompt_root / "defaultagent")
        cfg.sessions_dir.mkdir(parents=True, exist_ok=True)
        return cfg

    import js.config as config

    monkeypatch.setattr(config, "from_env", from_env_stub)

    n_children = 3  # parent(1) + children exhaust a 2-worker pool on the old code

    def _first_user_text(messages: list) -> str:
        for m in messages:
            if getattr(m, "role", None) == "user" and getattr(m, "parts", None):
                return getattr(m.parts[0], "text", "")
        return ""

    def stub(**kwargs):
        msgs = kwargs["messages"]
        text = _pending_user_text(msgs)
        if text == "root":
            tasks = [f"child-{i}" for i in range(n_children)]
            return _tool("task", json.dumps({"tasks": tasks, "agent_id": "worker"}), "p")
        if text.startswith("child-"):
            return _tool("task", json.dumps({"tasks": [f"leaf-{text}"], "agent_id": "worker"}), "c" + text)
        if text.startswith("leaf-"):
            # Grandchild: a real leaf tool that needs a pool thread, then stop.
            return _tool("todo_write", json.dumps({"todos": [{"content": text, "status": "pending"}]}), "g" + text)
        # Follow-up after a tool result: stop, echoing this turn's own prompt so
        # completion is observable up the tree.
        return _stop(f"DONE:{_first_user_text(msgs)}")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stub)

    registry = build_default_registry(prompts_root=prompt_root)
    parent_cfg = _make_cfg(tmp_path, "parent", prompt_root / "defaultagent")
    parent_ctx = ToolContext(cwd=tmp_path)
    parent_ctx.task_depth = 0
    messages = [{"role": "user", "content": "root"}]

    executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="js-dispatch")
    loop = asyncio.new_event_loop()
    loop.set_default_executor(executor)
    sup = supervisor.Supervisor(loop)
    supervisor.set_current(sup)

    async def drive():
        job = sup.spawn(
            runtime.run_turn_async(
                parent_cfg,
                "SYS",
                messages,
                runtime.Telemetry(debug_log=None),
                tool_registry=registry,
                tool_context=parent_ctx,
                suppress_output=True,
            ),
            kind="turn",
        )
        await job.task

    try:
        loop.run_until_complete(asyncio.wait_for(drive(), timeout=15))
    finally:
        supervisor.set_current(None)
        executor.shutdown(wait=False, cancel_futures=True)
        loop.close()

    # The parent turn ran to completion: it recorded the fan-out tool result and
    # then a final assistant stop message.
    tool_blob = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "tool")
    assert "TASK_RESULTS agent=worker" in tool_blob
    assert messages[-1].get("role") == "assistant"
    assert messages[-1].get("content") == "DONE:root"
    # Every child reported DONE. A child cannot finish until task_async has
    # gathered its grandchild (whose leaf todo_write needed a pool thread), so
    # all children present == the whole two-level tree cleared the bounded pool.
    for i in range(n_children):
        assert f"DONE:child-{i}" in tool_blob
