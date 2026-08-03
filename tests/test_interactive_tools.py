from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from PIL import Image

from js.toolkit.browser import browser_probe
from js.toolkit.core import ToolContext
from js.toolkit.registry import build_default_registry
from js.toolkit.terminal import close_terminal_sessions, terminal_session, terminal_snapshot


def _payload(result: str) -> dict:
    assert not result.startswith("ERROR:"), result
    return json.loads(result)


def test_browser_probe_reports_optional_backend_when_playwright_is_missing(
    tmp_path, monkeypatch
):
    real_import = __import__

    def import_without_playwright(name, *args, **kwargs):
        if name == "playwright.sync_api":
            raise ImportError
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", import_without_playwright)

    result = browser_probe(target="unused", context=ToolContext(cwd=tmp_path))

    assert "requires the optional Playwright backend" in result
    assert "musllinux" in result


def test_registry_exposes_only_canonical_interactive_tool_names():
    registry = build_default_registry()

    assert registry.resolve("terminal_session").name == "terminal_session"
    assert registry.resolve("terminal_snapshot").name == "terminal_snapshot"
    assert registry.resolve("browser_probe").name == "browser_probe"
    assert registry.resolve("terminal_probe") is None
    assert registry.resolve("first_contact") is None
    assert registry.resolve("heavy_browser") is None

    terminal = registry.resolve("terminal_session")
    snapshot = registry.resolve("terminal_snapshot")
    browser = registry.resolve("browser_probe")
    assert terminal.required == ("action",)
    assert set(terminal.params) == {
        "action", "session", "command", "keys", "cwd", "wait_ms", "cols", "rows"
    }
    assert snapshot.required == ()
    assert set(snapshot.params) == {"session", "output_path", "wait_ms"}
    assert browser.required == ("target",)
    assert set(browser.params) == {
        "target", "click", "press", "output_dir", "settle_ms", "hold_ms",
        "viewport_width", "viewport_height",
    }


def test_terminal_sessions_are_real_persistent_ptys_and_accept_keys(tmp_path):
    context = ToolContext(cwd=tmp_path)
    try:
        started = _payload(
            terminal_session(
                action="start",
                session="demo",
                command="printf 'ready\\n'; read line; printf 'got:%s\\n' \"$line\"; sleep 10",
                wait_ms=300,
                context=context,
            )
        )
        assert started["still_running"] is True
        assert any("ready" in line for line in started["lines"])

        sent = _payload(
            terminal_session(
                action="send",
                session="demo",
                keys="hello,enter",
                wait_ms=300,
                context=context,
            )
        )
        assert any("got:hello" in line for line in sent["lines"])
        assert sent["screen_responded"] is True

        listed = _payload(terminal_session(action="list", context=context))
        assert listed["sessions"][0]["session"] == "demo"

        stopped = _payload(
            terminal_session(action="stop", session="demo", context=context)
        )
        assert stopped == {"session": "demo", "stopped": True}
    finally:
        close_terminal_sessions(context)


def test_terminal_session_state_is_isolated_by_tool_context(tmp_path):
    first = ToolContext(cwd=tmp_path / "first")
    second = ToolContext(cwd=tmp_path / "second")
    first.cwd.mkdir()
    second.cwd.mkdir()
    try:
        _payload(
            terminal_session(
                action="start",
                session="same-name",
                command="printf first; sleep 5",
                wait_ms=200,
                context=first,
            )
        )
        assert terminal_session(
            action="look", session="same-name", context=second
        ).startswith("ERROR: no terminal session")
        assert _payload(terminal_session(action="list", context=second))["sessions"] == []
    finally:
        close_terminal_sessions(first)
        close_terminal_sessions(second)


def test_terminal_snapshot_writes_png_and_uses_image_result(tmp_path):
    context = ToolContext(cwd=tmp_path, vision_enabled=True)
    try:
        _payload(
            terminal_session(
                action="start",
                session="picture",
                command="printf '\\033[31mRED\\033[0m\\n'; sleep 5",
                wait_ms=250,
                context=context,
            )
        )
        result = terminal_snapshot(
            session="picture",
            output_path="shot.png",
            context=context,
        )
        assert result.startswith("IMAGE_RESULT\t")
        target = tmp_path / "shot.png"
        assert target.is_file()
        with Image.open(target) as image:
            assert image.format == "PNG"
            assert image.width > 100
            assert image.height > 100
    finally:
        close_terminal_sessions(context)


@pytest.mark.skipif(
    importlib.util.find_spec("playwright") is None,
    reason="optional Playwright backend is unavailable on this platform",
)
def test_browser_probe_opens_local_html_clicks_and_reports_visual_state(tmp_path):
    target = tmp_path / "index.html"
    target.write_text(
        """<!doctype html>
        <html><body style="margin:0;background:rgb(180,0,0)">
        <button id="go" style="font-size:40px">Go Blue</button>
        <button id="noop" style="font-size:40px">No Op</button>
        <script>
        console.error('probe-console-error');
        document.getElementById('go').onclick = () => {
          document.body.style.background = 'rgb(0,0,180)';
          document.getElementById('go').textContent = 'Changed';
        };
        document.getElementById('noop').onclick = () => {};
        document.addEventListener('keydown', event => {
          if (event.key === 'x') document.body.style.background = 'rgb(0,180,0)';
        });
        setTimeout(() => { throw new Error('probe-page-error'); }, 10);
        </script></body></html>""",
        encoding="utf-8",
    )
    context = ToolContext(cwd=tmp_path)

    report = _payload(
        browser_probe(
            target=str(target),
            click="go blue>no op",
            press="x",
            hold_ms=50,
            settle_ms=150,
            viewport_width=640,
            viewport_height=480,
            context=context,
        )
    )

    assert "error" not in report, report.get("error")
    assert len(report["frames"]) == 4
    assert report["clicked"] == [
        {"pattern": "go blue", "text": "Go Blue"},
        {"pattern": "no op", "text": "No Op"},
    ]
    assert report["frames"][1]["changed_pct_from_previous"] > 50
    assert report["frames"][2]["changed_pct_from_previous"] < 5
    assert report["frames"][3]["changed_pct_from_previous"] > 50
    assert report["changed_pixel_percentages"][0] > 50
    assert report["changed_pixel_percentages"][1] < 5
    assert report["changed_pixel_percentages"][2] > 50
    assert report["pressed"] == "x"
    assert report["webgl"]["webgl"] or report["webgl"]["webgl2"]
    assert any("probe-console-error" in error for error in report["console_errors"])
    assert any("probe-page-error" in error for error in report["page_errors"])
    for frame in report["frames"]:
        path = Path(frame["path"])
        assert path.is_file()
        with Image.open(path) as image:
            assert list(image.size) == frame["dimensions"]
