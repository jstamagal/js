from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import threading
import time

from js import runtime
from js.config import Config
from js.memory import load_messages
from js.toolkit import ToolContext, call_tool
from js.toolkit import fs
from js.toolkit.meta import task, todo_read, todo_write
from js.toolkit.registry import build_default_registry, select
from js.model_client import ModelStreamResult, ModelToolCall
import ai


def _fake_stream_result(text: str = "ok"):
    return ModelStreamResult(
        text=text,
        tool_calls=[],
        reasoning="",
        usage=ai.types.usage.Usage(input_tokens=0, output_tokens=len(text)),
        finish_reason="stop",
        assistant_message=ai.assistant_message(text),
    )


def _fake_tool_result(name: str, args: str, call_id: str = "call_test"):
    tool_calls = [ModelToolCall(id=call_id, name=name, arguments=args)]
    return ModelStreamResult(
        text="",
        tool_calls=tool_calls,
        reasoning="",
        usage=ai.types.usage.Usage(input_tokens=0, output_tokens=len(args)),
        finish_reason="tool_calls",
        assistant_message=ai.assistant_message(""),
    )


def make_cfg(tmp_path: Path, agent: str, prompts: Path) -> Config:
    return Config(
        agent_id=agent,
        agent_dir=tmp_path / ".js" / "sessions" / agent,
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
        history_file=tmp_path / ".js" / "sessions" / agent / ".history",
        sessions_dir=tmp_path / ".js" / "sessions" / agent,
        session_file=tmp_path / ".js" / "sessions" / agent / "auto.jsonl",
        prompts_dir=prompts,
    )


def prompt_dir(tmp_path: Path, agent: str, manifest: str = "tools: []\n", body: str = "WORKER\n") -> Path:
    prompts = tmp_path / "prompts" / agent
    prompts.mkdir(parents=True)
    (prompts / "00-tools.yaml").write_text(manifest, encoding="utf-8")
    (prompts / "01-body.md").write_text(body, encoding="utf-8")
    return prompts


def patch_from_env(monkeypatch, tmp_path: Path, prompt_root: Path):
    def from_env_stub(*, save_session: bool = True):
        agent = "defaultagent"
        prompts = prompt_root / agent
        cfg = make_cfg(tmp_path, agent, prompts)
        cfg.sessions_dir.mkdir(parents=True, exist_ok=True)
        if save_session and not cfg.session_file.exists():
            cfg.session_file.write_text("", encoding="utf-8")
        return cfg

    import js.config as config

    # meta.py does `from ..config import from_env` inside the task function
    # (a local import), so it can't be monkeypatched through the meta module
    # namespace. Patching js.config.from_env is enough because by the time
    # the local import runs, it re-reads from the same module object —
    # which monkeypatch has already updated.
    monkeypatch.setattr(config, "from_env", from_env_stub)


def test_subagent_prompt_roots_use_project_global_repo_precedence(monkeypatch, tmp_path):
    repo = tmp_path / "repo" / "prompts"
    glob = tmp_path / "home" / ".js" / "agents"
    proj = tmp_path / "project" / ".js" / "agents"
    for root, body, tool in (
        (repo, "REPO SYSTEM\n", "shell"),
        (glob, "GLOBAL SYSTEM\n", "todo_write"),
        (proj, "PROJECT SYSTEM\n", "todo_read"),
    ):
        worker = root / "worker"
        worker.mkdir(parents=True)
        (worker / "00-tools.yaml").write_text(f"tools:\n  - {tool}\n", encoding="utf-8")
        (worker / "01-body.md").write_text(body, encoding="utf-8")

    def from_env_stub(*, save_session: bool = True):
        agent = "defaultagent"
        cfg = make_cfg(tmp_path, agent, repo / agent)
        cfg = replace(cfg, prompt_roots=(repo, glob, proj))
        cfg.sessions_dir.mkdir(parents=True, exist_ok=True)
        if save_session and not cfg.session_file.exists():
            cfg.session_file.write_text("", encoding="utf-8")
        return cfg

    import js.config as config

    monkeypatch.setattr(config, "from_env", from_env_stub)
    seen: dict[str, object] = {}

    def completion_stub(**kwargs):
        seen["system"] = kwargs["messages"][0].parts[0].text
        seen["tools"] = [spec.name for spec in kwargs.get("tools", [])]
        return _fake_stream_result("SHADOW_OK")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", completion_stub)

    actual = task(["use the most specific prompt"], agent_id="worker", context=ToolContext(cwd=tmp_path))

    assert "SHADOW_OK" in actual
    assert "PROJECT SYSTEM" in str(seen["system"])
    assert "GLOBAL SYSTEM" not in str(seen["system"])
    assert "REPO SYSTEM" not in str(seen["system"])
    assert seen["tools"] == ["todo_read"]


def test_task_requires_named_agent_id(tmp_path):
    actual = task(["work"], context=ToolContext(cwd=tmp_path))

    assert actual == "ERROR: task requires agent_id"


def test_task_rejects_non_string_task_items(tmp_path):
    actual = task([{"task": "old compatibility shape"}], agent_id="worker", context=ToolContext(cwd=tmp_path))

    assert actual == "ERROR: task tasks must be strings"


def test_subagent_boolean_task_max_depth_falls_back_to_default(monkeypatch, tmp_path):
    prompts = prompt_dir(tmp_path, "worker", "tools: []\n", "WORKER\n")
    patch_from_env(monkeypatch, tmp_path, prompts.parent)
    parent = ToolContext(cwd=tmp_path)
    parent.task_depth = 1
    parent.task_max_depth = True

    monkeypatch.setattr(runtime.model_client, "stream_model_async", lambda **kwargs: _fake_stream_result("BOOL_DEPTH_DONE"))

    actual = task(["work"], agent_id="worker", context=parent)

    assert "BOOL_DEPTH_DONE" in actual
    assert "recursion depth limit reached (1)" not in actual

def test_subagent_cannot_undo_parent_snapshot(monkeypatch, tmp_path):
    prompts = prompt_dir(tmp_path, "worker", "tools:\n  - undo\n")
    patch_from_env(monkeypatch, tmp_path, prompts.parent)
    target = tmp_path / "owned.txt"
    target.write_text("old\n", encoding="utf-8")
    parent = ToolContext(cwd=tmp_path)
    fs.read("owned.txt", context=parent)
    fs.patch("owned.txt", old_string="old", new_string="new", context=parent)
    tool_results: list[str] = []
    calls = 0

    def completion_stub(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _fake_tool_result("undo", '{"path":"owned.txt"}')
        tool_results.append(kwargs["messages"][-1].parts[0].get_model_input())
        return _fake_stream_result("UNDO_TEST_DONE")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", completion_stub)

    actual = task(["try undo"], agent_id="worker", context=parent)

    assert "UNDO_TEST_DONE" in actual
    assert "no snapshot available" in tool_results[0]
    assert target.read_text(encoding="utf-8") == "new\n"


def test_subagent_does_not_inherit_parent_read_set(monkeypatch, tmp_path):
    prompts = prompt_dir(tmp_path, "worker", "tools:\n  - write\n")
    patch_from_env(monkeypatch, tmp_path, prompts.parent)
    target = tmp_path / "guard.txt"
    target.write_text("old\n", encoding="utf-8")
    parent = ToolContext(cwd=tmp_path)
    fs.read("guard.txt", context=parent)
    tool_results: list[str] = []
    calls = 0

    def completion_stub(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _fake_tool_result("write", '{"file_path":"guard.txt","content":"new\\n","overwrite":true}')
        tool_results.append(kwargs["messages"][-1].parts[0].get_model_input())
        return _fake_stream_result("WRITE_TEST_DONE")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", completion_stub)

    actual = task(["try overwrite"], agent_id="worker", context=parent)

    assert "WRITE_TEST_DONE" in actual
    assert "must read the file" in tool_results[0]
    assert target.read_text(encoding="utf-8") == "old\n"


def test_subagent_todos_and_search_cache_are_fresh(monkeypatch, tmp_path):
    prompts = prompt_dir(tmp_path, "worker", "tools:\n  - todo_write\n  - fs_search\n")
    patch_from_env(monkeypatch, tmp_path, prompts.parent)
    (tmp_path / "needle.txt").write_text("needle\n", encoding="utf-8")
    parent = ToolContext(cwd=tmp_path)
    todo_write([{"content": "parent", "status": "pending"}], context=parent)
    fs.fs_search("needle", path=".", context=parent)
    tool_results: list[str] = []
    calls = 0

    def completion_stub(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _fake_tool_result("todo_write", '{"todos":[{"content":"child","status":"pending"}]}')
        if calls == 2:
            return _fake_tool_result("fs_search", '{"pattern":"needle","path":"."}')
        tool_results.append(kwargs["messages"][-1].parts[0].get_model_input())
        return _fake_stream_result("STATE_TEST_DONE")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", completion_stub)

    actual = task(["mutate child state"], agent_id="worker", context=parent)

    assert "STATE_TEST_DONE" in actual
    assert todo_read(context=parent) == "- [pending] parent"
    assert "deduplicated repeated search" not in tool_results[0]


def test_agent_id_loads_real_persona_tools_and_creates_session(monkeypatch, tmp_path):
    prompts = prompt_dir(tmp_path, "workerx", "tools:\n  - todo_read\n", "WORKERX SYSTEM\n")
    patch_from_env(monkeypatch, tmp_path, prompts.parent)
    seen: dict[str, object] = {}

    def completion_stub(**kwargs):
        seen["system"] = kwargs["messages"][0].parts[0].text
        seen["tools"] = [spec.name for spec in kwargs.get("tools", [])]
        return _fake_stream_result("AGENTX_DONE")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", completion_stub)

    actual = task(["hello"], agent_id="workerx", session_id="child-session", context=ToolContext(cwd=tmp_path))

    session_file = tmp_path / ".js" / "sessions" / "workerx" / "child-session.jsonl"
    assert "AGENTX_DONE" in actual
    assert seen["tools"] == ["todo_read"]
    assert "WORKERX SYSTEM" in str(seen["system"])
    assert "---" not in str(seen["system"])
    assert session_file.exists()
    assert [m["role"] for m in load_messages(session_file)] == ["user", "assistant"]


def test_named_agent_tool_runs_agent_with_only_tasks_input(monkeypatch, tmp_path):
    prompts = prompt_dir(tmp_path, "worker", "tools:\n  - todo_read\n", "WORKER SYSTEM\n")
    patch_from_env(monkeypatch, tmp_path, prompts.parent)
    registry = build_default_registry(prompts_root=prompts.parent)
    tool = registry.resolve("worker")
    assert tool is not None
    seen: dict[str, object] = {}

    def completion_stub(**kwargs):
        seen["system"] = kwargs["messages"][0].parts[0].text
        seen["tools"] = [spec.name for spec in kwargs.get("tools", [])]
        return _fake_stream_result("NAMED_AGENT_DONE")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", completion_stub)

    actual = call_tool(tool, {"tasks": ["hello from named tool"]}, ToolContext(cwd=tmp_path))

    assert tool.required == ("tasks",)
    assert set(tool.params) == {"tasks"}
    assert "TASK_RESULTS agent=worker" in actual
    assert "NAMED_AGENT_DONE" in actual
    assert "WORKER SYSTEM" in str(seen["system"])
    assert seen["tools"] == ["todo_read"]


def test_task_session_id_resumes_named_agent_conversation(monkeypatch, tmp_path):
    prompts = prompt_dir(tmp_path, "worker", "tools:\n  - todo_read\n")
    patch_from_env(monkeypatch, tmp_path, prompts.parent)
    seen_tools: list[list[str]] = []
    seen_message_counts: list[int] = []

    def completion_stub(**kwargs):
        seen_tools.append([spec.name for spec in kwargs.get("tools", [])])
        seen_message_counts.append(len(kwargs["messages"]))
        return _fake_stream_result(f"TURN_{len(seen_tools)}")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", completion_stub)

    first = task(["first"], agent_id="worker", session_id="resume-me", context=ToolContext(cwd=tmp_path))
    second = task(["second"], agent_id="worker", session_id="resume-me", context=ToolContext(cwd=tmp_path))

    session_file = tmp_path / ".js" / "sessions" / "worker" / "resume-me.jsonl"
    assert "TURN_1" in first
    assert "TURN_2" in second
    assert seen_tools == [["todo_read"], ["todo_read"]]
    assert seen_message_counts[1] > seen_message_counts[0]
    assert [m["role"] for m in load_messages(session_file)] == ["user", "assistant", "user", "assistant"]


def test_task_workers_run_in_parallel_not_serially(monkeypatch, tmp_path):
    prompts = prompt_dir(tmp_path, "worker")
    patch_from_env(monkeypatch, tmp_path, prompts.parent)

    def completion_stub(**kwargs):
        time.sleep(0.25)
        return _fake_stream_result(kwargs["messages"][-1].parts[0].text.upper())

    monkeypatch.setattr(runtime.model_client, "stream_model_async", completion_stub)

    started = time.perf_counter()
    actual = task(["a", "b", "c"], agent_id="worker", context=ToolContext(cwd=tmp_path))
    elapsed = time.perf_counter() - started

    assert "1. A" in actual and "2. B" in actual and "3. C" in actual
    assert "max_workers" not in actual
    assert elapsed < 0.65


def test_two_concurrent_workers_have_no_todo_state_bleed(monkeypatch, tmp_path):
    prompts = prompt_dir(tmp_path, "worker", "tools:\n  - todo_write\n")
    patch_from_env(monkeypatch, tmp_path, prompts.parent)
    tool_results: list[str] = []
    lock = threading.Lock()

    def completion_stub(**kwargs):
        last = kwargs["messages"][-1]
        if last.role == "user":
            content = last.parts[0].text
            return _fake_tool_result("todo_write", f'{{"todos":[{{"content":"{content}","status":"pending"}}]}}')
        with lock:
            tool_results.append(last.parts[0].get_model_input())
        return _fake_stream_result("TODO_DONE")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", completion_stub)

    actual = task(["left", "right"], agent_id="worker", context=ToolContext(cwd=tmp_path))

    assert "1. TODO_DONE" in actual and "2. TODO_DONE" in actual
    assert len(tool_results) == 2
    assert all("before=[]" in result for result in tool_results)
    assert any("after=[('left', 'pending')]" in result for result in tool_results)
    assert any("after=[('right', 'pending')]" in result for result in tool_results)


def test_one_failing_parallel_worker_does_not_sink_siblings(monkeypatch, tmp_path):
    prompts = prompt_dir(tmp_path, "worker")
    patch_from_env(monkeypatch, tmp_path, prompts.parent)

    def completion_stub(**kwargs):
        prompt = kwargs["messages"][-1].parts[0].text
        if prompt == "fail":
            raise RuntimeError("boom")
        return _fake_stream_result(prompt.upper())

    monkeypatch.setattr(runtime.model_client, "stream_model_async", completion_stub)

    actual = task(["ok", "fail", "also"], agent_id="worker", context=ToolContext(cwd=tmp_path))

    assert "1. OK" in actual
    assert "2. ERROR RuntimeError: boom" in actual
    assert "3. ALSO" in actual


def test_subagent_does_not_inherit_parent_selected_tool_surface(monkeypatch, tmp_path):
    prompts = prompt_dir(tmp_path, "worker", "tools:\n  - todo_read\n")
    patch_from_env(monkeypatch, tmp_path, prompts.parent)
    parent = ToolContext(cwd=tmp_path)
    parent.tool_registry = select(["shell", "write"])
    seen: dict[str, list[str]] = {}

    def completion_stub(**kwargs):
        seen["tools"] = [spec.name for spec in kwargs.get("tools", [])]
        return _fake_stream_result("SURFACE_OK")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", completion_stub)

    actual = task(["check tools"], agent_id="worker", context=parent)

    assert "SURFACE_OK" in actual
    assert seen["tools"] == ["todo_read"]
