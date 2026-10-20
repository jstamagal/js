from __future__ import annotations

from test_output import _Sink

def test_kernel_calls_preserve_their_structured_view(monkeypatch, tmp_path):
    from js import runtime
    from js.toolkit import kernel_bridge
    from js.toolkit.core import ToolContext, call_scope, call_view, view
    from js.toolkit.registry import build_default_registry

    monkeypatch.setattr(kernel_bridge.ToolBridge, "_listen", lambda self: None)
    bridge = kernel_bridge.ToolBridge(tmp_path / "unused.sock")
    sink = _Sink()
    context = ToolContext(cwd=tmp_path)
    bridge.attach(build_default_registry(), context,
                  runtime._cell_call_observer(runtime.Telemetry(None, sink=sink), True, context))
    (tmp_path / "note.txt").write_text("alpha\n")
    with call_scope("outer"):
        view(outer=True)
        result = bridge.handle({"token": bridge.token, "op": "call", "tool": "read",
                                "args": ["note.txt"]})
        assert call_view() == {"outer": True}

    assert "alpha" in result["value"]
    assert sink.named("tool_result")[0]["view"] == {
        "path": str(tmp_path / "note.txt"), "start_line": 1, "end_line": 1,
        "total_lines": 1, "lines": ["alpha"],
    }
