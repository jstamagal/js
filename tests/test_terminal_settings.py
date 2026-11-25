import json
from types import SimpleNamespace

from js import turn_settings
from js.toolkit.core import ToolContext
from js.toolkit.terminal import close_terminal_sessions, terminal_session


def test_terminal_setting_can_filter_inherited_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("JS_TERMINAL_TEST", "inherited")
    context = ToolContext(cwd=tmp_path)
    turn_settings.install(context, SimpleNamespace(**turn_settings.project({"TERMINAL_ENVIRONMENT_INHERIT": False})))
    try:
        result = json.loads(terminal_session(
            action="start", command='printf "%s\\n" "${JS_TERMINAL_TEST-filtered}"',
            context=context,
        ))
        assert "filtered" in result["lines"]
    finally:
        close_terminal_sessions(context)
