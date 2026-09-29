"""The tool surface a session loaded is journaled to the session file and
restored on the next turn of the same agent in the same directory."""

from __future__ import annotations

from dataclasses import replace

import pytest

from js import memory, runtime
from js.toolkit import ToolContext
from js.toolkit.registry import build_default_registry
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
