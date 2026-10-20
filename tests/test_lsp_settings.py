from types import SimpleNamespace

from js import turn_settings
from js.toolkit import lsp
from js.toolkit.core import ToolContext


def test_registered_setting_filters_language_server_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("JS_LANGUAGE_SERVER_TEST", "private")
    context = ToolContext(cwd=tmp_path)
    turn_settings.install(context, SimpleNamespace(**turn_settings.project({"LSP_ENVIRONMENT_INHERIT": False})))
    assert "JS_LANGUAGE_SERVER_TEST" not in lsp._server_env(context)
