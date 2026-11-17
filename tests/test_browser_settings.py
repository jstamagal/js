import builtins
from contextlib import contextmanager
from types import SimpleNamespace

from js import turn_settings
from js.toolkit import browser
from js.toolkit.core import ToolContext


def test_browser_launch_uses_configured_fallback(tmp_path, monkeypatch):
    fallback = tmp_path / "chrome"
    fallback.touch()
    calls = []

    class Launched:
        def new_page(self, **_kwargs):
            raise RuntimeError("the fallback browser opened a page")

        def close(self):
            pass

    def launch(**kwargs):
        calls.append(kwargs)
        if "executable_path" not in kwargs:
            raise RuntimeError("bundled browser unavailable")
        return Launched()

    @contextmanager
    def sync_playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=launch))

    real_import = builtins.__import__

    def import_fake_playwright(name, *args, **kwargs):
        if name == "playwright.sync_api":
            return SimpleNamespace(sync_playwright=sync_playwright)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_fake_playwright)
    context = ToolContext(cwd=tmp_path)
    turn_settings.install(context, SimpleNamespace(**turn_settings.project({"BROWSER_FALLBACK_FILE": str(fallback)})))

    report = browser.browser_probe(target="http://127.0.0.1:1/", output_dir=str(tmp_path / "probes"),
                                   context=context)

    assert "the fallback browser opened a page" in report
    assert calls[-1]["executable_path"] == str(fallback)
