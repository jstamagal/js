"""The tool surface a session loaded is journaled to the session file and
restored on the next turn of the same agent in the same directory."""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from pathlib import Path

import pytest

from js import memory, runtime
from js.toolkit import ToolContext
from js.toolkit.registry import build_default_registry
from js.turn_surface import SurfaceJournal
from test_lazy_tool_discovery import _cfg, _result


@pytest.mark.parametrize("field", ["agent_id", "cwd"])
def test_a_surface_saved_under_another_scope_is_not_restored(tmp_path, monkeypatch, field):
    cfg = _cfg(tmp_path)
    scope = {"version": 1, "agent_id": cfg.agent_id, "cwd": str(tmp_path.resolve())}
    scope[field] = "elsewhere"
    memory.append_tool_surface(cfg.session_file, {**scope, "ids": ["native:shell"], "mcp_sources": []})
    seen = []

    def stream(**kwargs):
        seen.append([tool.name for tool in kwargs["tools"]])
        return _result(text="ready")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    runtime.run_turn(cfg, "system", [{"role": "user", "content": "hi"}], runtime.Telemetry(None),
                     tool_registry=build_default_registry().select(["shell:lazy"]),
                     tool_context=ToolContext(cwd=tmp_path))
    assert seen == [["tool_discovery"]]


def test_a_surface_saved_under_this_scope_is_restored(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    memory.append_tool_surface(cfg.session_file, {
        "version": 1, "agent_id": cfg.agent_id, "cwd": str(tmp_path.resolve()),
        "ids": ["native:shell"], "mcp_sources": []})
    seen = []

    def stream(**kwargs):
        seen.append(sorted(tool.name for tool in kwargs["tools"]))
        return _result(text="ready")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    runtime.run_turn(replace(cfg), "system", [{"role": "user", "content": "hi"}], runtime.Telemetry(None),
                     tool_registry=build_default_registry().select(["shell:lazy"]),
                     tool_context=ToolContext(cwd=tmp_path))
    assert seen == [["shell", "tool_discovery"]]


# --------------------------------------------------------------------------
# SurfaceJournal
# --------------------------------------------------------------------------

class _Registry:
    def __init__(self, state):
        self.state = state
        self.restored = []
        self.on_change = None

    async def restore(self, saved):
        self.restored.append(saved)
        self.state = {"ids": saved["ids"], "mcp_sources": saved["mcp_sources"]}

    def snapshot(self):
        return self.state


def _journal(path, registry, tmp_path):
    return SurfaceJournal(path, agent_id="agent", cwd=tmp_path, registry=registry)


def _scope(tmp_path):
    return {"version": 1, "agent_id": "agent", "cwd": str(tmp_path.resolve())}


def test_restore_takes_the_surface_saved_under_the_same_scope(tmp_path):
    path = tmp_path / "session.jsonl"
    saved = {**_scope(tmp_path), "ids": ["native:shell"], "mcp_sources": []}
    memory.append_tool_surface(path, saved)
    registry = _Registry({"ids": [], "mcp_sources": []})
    journal = _journal(path, registry, tmp_path)
    asyncio.run(journal.restore())
    assert registry.restored == [saved]
    assert registry.on_change == journal.on_change


def test_restore_skips_a_surface_saved_under_another_scope(tmp_path):
    path = tmp_path / "session.jsonl"
    memory.append_tool_surface(path, {**_scope(tmp_path), "agent_id": "other",
                                      "ids": ["native:shell"], "mcp_sources": []})
    registry = _Registry({"ids": [], "mcp_sources": []})
    asyncio.run(_journal(path, registry, tmp_path).restore())
    assert registry.restored == []


def test_a_change_is_appended_once_under_the_scope(tmp_path):
    path = tmp_path / "session.jsonl"
    registry = _Registry({"ids": [], "mcp_sources": []})
    journal = _journal(path, registry, tmp_path)
    asyncio.run(journal.restore())
    journal.on_change({"ids": [], "mcp_sources": []})
    assert memory.load_tool_surface(path) is None
    loaded = {"ids": ["native:shell"], "mcp_sources": []}
    journal.on_change(loaded)
    journal.on_change(dict(loaded))
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    assert memory.load_tool_surface(path) == {**_scope(tmp_path), **loaded}


def test_a_devnull_session_is_never_read_or_written(tmp_path, monkeypatch):
    touched = []
    monkeypatch.setattr(memory, "load_tool_surface", lambda path: touched.append(("load", path)))
    monkeypatch.setattr(memory, "append_tool_surface", lambda path, state: touched.append(("append", path)))
    registry = _Registry({"ids": [], "mcp_sources": []})
    journal = _journal(Path(os.devnull), registry, tmp_path)
    asyncio.run(journal.restore())
    journal.on_change({"ids": ["native:shell"], "mcp_sources": []})
    assert touched == []
    assert registry.restored == []
