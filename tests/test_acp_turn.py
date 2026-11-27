"""The ACP worker uses the headless entry point and its runtime approval gate."""

import asyncio
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from test_acp_stdio import acp_provider as acp_provider

from js import acp_turn, headless


@pytest.mark.parametrize("limited", [False, True])
def test_saved_literal_prompt_and_runtime_denial(tmp_path, monkeypatch, acp_provider, limited):
    base_url, requests = acp_provider
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "profile"
    agent = home / "agents" / "defaultagent"
    agent.mkdir(parents=True)
    (agent / "agent.yaml").write_text("tools:\n  - read:eager\n")
    (agent / "prompt.md").write_text("Read the requested file.")
    marker = "private-file-content-not-in-model-request"
    (workspace / "workspace-note.txt").write_text(marker)
    for key in tuple(os.environ):
        if key.startswith("JS_"):
            monkeypatch.delenv(key)
    for key, value in {
        "JS_HOME": str(home), "JS_PROVIDER": "llama.cpp", "JS_BASE_URL": base_url,
        "JS_API_KEY": "fixture", "JS_MODEL": "acp-fixture", "NO_PROXY": "127.0.0.1",
    }.items():
        monkeypatch.setenv(key, value)
    session_id = uuid4().hex
    output, approvals = [], []
    text = "--no-save @workspace-note.txt\n!{sh false}\r\n" + "word " * 100

    async def emit(event):
        output.append(event)

    async def deny(call):
        approvals.append(call)
        return "Permission denied by test"

    async def run():
        prompts = (text,) if limited else (text, "Recall the previous request.")
        options = ["--extra", "TURN_ROUNDS_MAX=1"] if limited else []
        for prompt in prompts:
            async with asyncio.timeout(20):
                await acp_turn.run_turn(cwd=workspace, session_id=session_id, text=prompt,
                                       options=options, mcp_servers={}, emit=emit, approve=deny)

    asyncio.run(run())
    assert len(approvals) == 1
    assert approvals[0]["name"] == "read"
    assert json.loads(approvals[0]["arguments"]) == {"file_path": "workspace-note.txt"}
    if limited:
        assert output[-1]["stop_reason"] == "max_turn_requests"
        assert output[-1]["answer"] == ""
        assert any(event["type"] == "tool_result" for event in output)
        return
    assert any("Permission denied by test" in str(message) for message in requests[1][1]["messages"])
    for _, request in requests:
        assert marker not in str(request)
        assert any(message.get("content") == text for message in request["messages"])
    starts = [event for event in output if event["type"] == "start"]
    assert [event["resumed"] for event in starts] == [False, True]
    assert starts[0]["file"] == starts[1]["file"]
    assert Path(starts[0]["file"]).stem == session_id


def test_a_steer_joins_the_conversation_at_the_tool_boundary(tmp_path, monkeypatch, acp_provider):
    base_url, requests = acp_provider
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "profile"
    agent = home / "agents" / "defaultagent"
    agent.mkdir(parents=True)
    (agent / "agent.yaml").write_text("tools:\n  - read:eager\n")
    (agent / "prompt.md").write_text("Read the requested file.")
    (workspace / "workspace-note.txt").write_text("note")
    for key in tuple(os.environ):
        if key.startswith("JS_"):
            monkeypatch.delenv(key)
    for key, value in {
        "JS_HOME": str(home), "JS_PROVIDER": "llama.cpp", "JS_BASE_URL": base_url,
        "JS_API_KEY": "fixture", "JS_MODEL": "acp-fixture", "NO_PROXY": "127.0.0.1",
    }.items():
        monkeypatch.setenv(key, value)
    steers = acp_turn.TurnInput()
    output = []

    async def emit(event):
        output.append(event)

    async def allow_and_steer(call):
        # Sent while the tool waits on its approval: ahead of the tool boundary.
        await steers.steer("and say it twice")
        return None

    async def run():
        async with asyncio.timeout(20):
            await acp_turn.run_turn(cwd=workspace, session_id=uuid4().hex, text="read the note", options=[],
                                   mcp_servers={}, emit=emit, approve=allow_and_steer, steers=steers)

    asyncio.run(run())
    assert steers.waiting == []
    messages = requests[1][1]["messages"]
    assert [message["role"] for message in messages[-2:]] == ["tool", "user"]
    assert messages[-1]["content"] == "and say it twice"
    assert not any(event["type"] == "steered" for event in output)


@pytest.mark.parametrize("word", ["HALT", "LOOP"])
def test_a_startup_control_word_ends_the_turn_normally(tmp_path, monkeypatch, acp_provider, word):
    base_url, requests = acp_provider
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "profile"
    agent = home / "agents" / "defaultagent"
    agent.mkdir(parents=True)
    (agent / "prompt.md").write_text("Answer.")
    runs = tmp_path / "runs"
    (agent / "1-user.md").write_text(f"!{{sh echo x >> '{runs}'}}\n%%{word}%%\n")
    for key in tuple(os.environ):
        if key.startswith("JS_"):
            monkeypatch.delenv(key)
    for key, value in {
        "JS_HOME": str(home), "JS_PROVIDER": "llama.cpp", "JS_BASE_URL": base_url,
        "JS_API_KEY": "fixture", "JS_MODEL": "acp-fixture", "NO_PROXY": "127.0.0.1",
    }.items():
        monkeypatch.setenv(key, value)
    output = []

    async def emit(event):
        output.append(event)

    async def run():
        async with asyncio.timeout(20):
            await acp_turn.run_turn(cwd=workspace, session_id=uuid4().hex, text="question",
                                   options=[], mcp_servers={}, emit=emit)

    asyncio.run(run())
    assert output[-1]["type"] == "end"
    assert output[-1]["ok"] is True
    # The control word ended the run inside the worker: no repeat of it ran.
    assert len(runs.read_text().splitlines()) == 1
    assert requests == []


@pytest.mark.parametrize("stdout, diagnostic", [
    ("garbage", "Invalid js worker JSON stream"),
    ('{"type":"error","message":"model failed"}', "model failed"),
    ("", "worker stderr"),
])
def test_worker_errors(tmp_path, monkeypatch, stdout, diagnostic):
    _replace_worker(monkeypatch, f"""
import sys
sys.stdin.readline()
sys.stdout.write({stdout + chr(10) if stdout else ""!r})
print("worker stderr", file=sys.stderr)
sys.exit(2)
""")

    async def emit(event):
        pass

    with pytest.raises(acp_turn.TurnError, match=diagnostic):
        asyncio.run(acp_turn.run_turn(cwd=tmp_path, session_id="session", text="question",
                                     options=[], mcp_servers={}, emit=emit))


def test_cancellation_stops_worker_and_detached_child(tmp_path, monkeypatch):
    _replace_worker(monkeypatch, """
import json, os, subprocess, sys, time
sys.stdin.readline()
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                         start_new_session=True)
print(json.dumps({"type": "start", "worker": os.getpid(), "child": child.pid}), flush=True)
time.sleep(60)
""")

    async def run():
        ready = asyncio.Future()

        async def emit(event):
            ready.set_result(event)

        async with asyncio.timeout(5):
            turn = asyncio.create_task(acp_turn.run_turn(
                cwd=tmp_path, session_id="session", text="question",
                options=[], mcp_servers={}, emit=emit,
            ))
            pids = await ready
            turn.cancel()
            with pytest.raises(asyncio.CancelledError):
                await turn
            assert not Path(f"/proc/{pids['worker']}").exists()
            while (stat := Path(f"/proc/{pids['child']}/stat")).exists():
                try:
                    if stat.read_text().rsplit(")", 1)[1].split()[0] == "Z":
                        break
                except (FileNotFoundError, ProcessLookupError):
                    break
                await asyncio.sleep(0.01)

    asyncio.run(run())


def _replace_worker(monkeypatch, script):
    launch = asyncio.create_subprocess_exec

    async def start(*args, **kwargs):
        return await launch(sys.executable, "-c", script, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start)


def test_cancellation_closes_pending_approval_pipe(tmp_path, monkeypatch):
    _replace_worker(monkeypatch, """
import json, signal, sys
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
assert json.loads(sys.stdin.readline())["approval"]
print(json.dumps({"type": "approval", "call": {"name": "read"}}), flush=True)
assert sys.stdin.readline() == ""
Path("approval-pipe-closed").touch()
""")

    async def run():
        ready = asyncio.Event()

        async def emit(event):
            pytest.fail(f"Private worker event escaped: {event}")

        async def approve(call):
            ready.set()
            await asyncio.Future()

        async with asyncio.timeout(5):
            turn = asyncio.create_task(acp_turn.run_turn(
                cwd=tmp_path, session_id="session", text="question",
                options=[], mcp_servers={}, emit=emit, approve=approve,
            ))
            await ready.wait()
            turn.cancel()
            with pytest.raises(asyncio.CancelledError):
                await turn

    asyncio.run(run())
    assert (tmp_path / "approval-pipe-closed").is_file()


@pytest.mark.parametrize(("incomplete", "expected"), [
    ("max_output_tokens", "max_tokens"),
    ("content_filter", "refusal"),
    ("server_error", None),
])
def test_headless_stop_reason(capsys, incomplete, expected):
    def run():
        headless.active.runtime_event("turn_end", {
            "reason": "incomplete", "incomplete_reason": incomplete,
        })
        return 0

    assert headless.json_run(run) == 0
    end = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert end.get("stop_reason") == expected
