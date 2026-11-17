from types import SimpleNamespace

import pytest
from test_lsp import fake, run

from js import turn_settings
from js.toolkit import lsp
from js.toolkit.core import ToolContext


@pytest.mark.parametrize("inherit", [False, True])
def test_registered_setting_filters_language_server_environment(tmp_path, monkeypatch, inherit):
    monkeypatch.setenv("JS_LANGUAGE_SERVER_TEST", "private")
    (tmp_path / "fake.toml").write_text("")
    source = tmp_path / "a.fake"
    source.write_text("environ\n")
    context = ToolContext(cwd=tmp_path)
    context.lsp_timeout_s = 20
    turn_settings.install(context, SimpleNamespace(**turn_settings.project({"LSP_ENVIRONMENT_INHERIT": inherit})))
    fake(context, tmp_path / "lsp.log")
    try:
        shown = run(context, operation="hover", file_path=str(source), line=1, character=1)
    finally:
        lsp.shutdown_servers()
    assert not shown.startswith("ERROR"), shown
    assert ("JS_LANGUAGE_SERVER_TEST" in shown) is inherit
