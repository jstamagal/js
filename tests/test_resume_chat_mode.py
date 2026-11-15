"""Recorded chat mode is restored before any resumed prompt or tools load."""

import io
import json
import os
from pathlib import Path

import ai
import pytest

from js import cli, runtime, session_catalog, session_picker, session_query, session_store
from js.cli_options import RunOptions, current, invocation
from js.model_client import ModelStreamResult
from repl_driver import stub_screen


class Terminal(io.StringIO):
    def isatty(self):
        return True


def _capture(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.sys, "stdin", Terminal())
    (tmp_path / "AGENTS.md").write_text("PROJECT_AGENT_SECRET_MARKER")
    monkeypatch.setattr(runtime.model_client, "stream_model_async", lambda **kw: ModelStreamResult(
        text="answer", tool_calls=[], reasoning="", usage=None, finish_reason="stop",
        assistant_message=ai.assistant_message("answer")))
    requests = []
    original = runtime.run_turn_async
    async def turn(cfg, system, messages, telemetry, **kw):
        requests.append({"tools": kw["tool_registry"].openai_specs(), "system": system,
                         "no_env": os.environ.get("JS_NOENV"), "chat": current().chat})
        return await original(cfg, system, messages, telemetry, **kw)
    monkeypatch.setattr(runtime, "run_turn_async", turn)
    return requests


@pytest.mark.parametrize("resume", ["name", "absolute", "last", "env", "picker", "repl"])
def test_chat_resumes_without_tools_env_or_project_instructions(resume, tmp_path, monkeypatch):
    requests = _capture(monkeypatch, tmp_path)
    assert cli.main(["--chat", "--session", "chatty", "-p", "first"]) == 0
    path = session_store.folder_for(tmp_path) / "chatty.jsonl"
    assert session_catalog.first_metadata(path)["chat"] is True
    assert current().chat is False
    assert "JS_NOENV" not in os.environ
    argv = ["--session", "chatty", "-p", "again"]
    if resume == "absolute":
        argv[1] = str(path)
    elif resume == "last":
        argv = ["--last", "-p", "again"]
    elif resume == "env":
        monkeypatch.setenv("JS_SESSION", "chatty")
        argv = ["-p", "again"]
    elif resume == "picker":
        monkeypatch.setattr(cli.sys, "stdout", Terminal())
        choice = session_picker.Choice(session_picker.RESUME, session_query.Session(
            path=str(path), agent="none", cwd=str(tmp_path)))
        monkeypatch.setattr(session_picker, "pick_session", lambda cwd: choice)
        argv = ["--session", "-p", "again"]
    elif resume == "repl":
        stub_screen(monkeypatch, ["again"], wait=True)
        argv = ["--session", "chatty"]
    assert cli.main(argv) == 0
    assert len(requests) == 2
    assert all(request["tools"] == [] and request["no_env"] == "1" and request["chat"] for request in requests)
    assert all("PROJECT_AGENT_SECRET_MARKER" not in request["system"] for request in requests)
    assert current().chat is False
    assert "JS_NOENV" not in os.environ


def test_direct_prompt_resume_restores_chat_and_named_persona(tmp_path, monkeypatch):
    requests = _capture(monkeypatch, tmp_path)
    agent = tmp_path / ".js" / "agents" / "chat-persona"
    agent.mkdir(parents=True)
    (agent / "01-prompt.md").write_text("NAMED_CHAT_PERSONA")
    with invocation(RunOptions(chat=True, agent="chat-persona")):
        assert cli._run_prompt("first", agent="chat-persona", session="named-chat") == 0
    assert cli._run_prompt("again", session="named-chat") == 0
    assert all(request["tools"] == [] and request["chat"] for request in requests)
    assert all("NAMED_CHAT_PERSONA" in request["system"] for request in requests)
    assert current().chat is False


def test_branch_keeps_chat_metadata(tmp_path, monkeypatch):
    _capture(monkeypatch, tmp_path)
    assert cli.main(["--chat", "--session", "parent", "-p", "first"]) == 0
    parent = session_store.folder_for(Path.cwd()) / "parent.jsonl"
    point = next(record["id"] for record in (json.loads(line) for line in parent.read_text().splitlines()) if record.get("kind") == "message")
    branch = session_catalog.branch_session(parent, point, cwd=tmp_path)
    assert session_catalog.first_metadata(branch)["chat"] is True
