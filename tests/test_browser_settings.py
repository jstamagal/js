from types import SimpleNamespace

from js import turn_settings
from js.toolkit import browser
from js.toolkit.core import ToolContext


def test_browser_launch_uses_configured_fallback(tmp_path):
    fallback = tmp_path / "chrome"
    fallback.touch()
    calls = []
    launched = object()

    def launch(**kwargs):
        calls.append(kwargs)
        if "executable_path" not in kwargs:
            raise RuntimeError("bundled browser unavailable")
        return launched

    context = ToolContext(cwd=tmp_path)
    turn_settings.install(context, SimpleNamespace(**turn_settings.project({"BROWSER_FALLBACK_FILE": str(fallback)})))
    assert browser._launch_browser(SimpleNamespace(chromium=SimpleNamespace(launch=launch)), context) is launched
    assert calls[-1]["executable_path"] == str(fallback)
