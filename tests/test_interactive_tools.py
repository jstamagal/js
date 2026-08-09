from __future__ import annotations

import importlib.util
import json
import time
import urllib.request
from pathlib import Path

import pytest
from PIL import Image

from js.toolkit import browser as browser_module
from js.toolkit.browser import browser_probe
from js.toolkit.core import ToolContext
from js.toolkit.registry import build_default_registry
from js.toolkit.terminal import close_terminal_sessions, terminal_session, terminal_snapshot


def _payload(result: str) -> dict:
    assert not result.startswith("ERROR:"), result
    return json.loads(result)


def _error_payload(result: str) -> dict:
    assert result.startswith("ERROR:"), result
    return json.loads(result.split("\n", 1)[1])


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


def test_terminal_replacement_keeps_running_session_when_cwd_is_invalid(tmp_path):
    context = ToolContext(cwd=tmp_path)
    try:
        _payload(
            terminal_session(
                action="start",
                session="main",
                command="cat",
                wait_ms=0,
                context=context,
            )
        )

        failed = terminal_session(
            action="start",
            session="main",
            command="printf replacement",
            cwd="missing",
            context=context,
        )
        still_there = _payload(
            terminal_session(action="look", session="main", wait_ms=0, context=context)
        )

        assert failed.startswith("ERROR: no such directory")
        assert still_there["still_running"] is True
        assert context.terminal_sessions["main"]["command"] == "cat"
    finally:
        close_terminal_sessions(context)


def test_terminal_replacement_keeps_running_session_when_spawn_fails(
    tmp_path, monkeypatch
):
    import pexpect

    context = ToolContext(cwd=tmp_path)
    try:
        _payload(
            terminal_session(
                action="start",
                session="main",
                command="cat",
                wait_ms=0,
                context=context,
            )
        )

        def fail_spawn(*_args, **_kwargs):
            raise pexpect.ExceptionPexpect("proved spawn failure")

        monkeypatch.setattr(pexpect, "spawn", fail_spawn)
        failed = terminal_session(
            action="start",
            session="main",
            command="printf replacement",
            context=context,
        )

        assert "proved spawn failure" in failed
        assert context.terminal_sessions["main"]["child"].isalive()
        assert context.terminal_sessions["main"]["command"] == "cat"
    finally:
        close_terminal_sessions(context)


def test_terminal_snapshot_output_is_not_credited_to_later_input(tmp_path):
    context = ToolContext(cwd=tmp_path)
    try:
        _payload(
            terminal_session(
                action="start",
                session="main",
                command=(
                    "stty -echo; sleep 0.15; printf 'BANNER_PRINTED_BY_ITSELF\\n'; "
                    "exec sleep 5"
                ),
                wait_ms=0,
                context=context,
            )
        )
        terminal_snapshot(session="main", wait_ms=300, context=context)

        sent = _payload(
            terminal_session(
                action="send",
                session="main",
                keys="q",
                wait_ms=100,
                context=context,
            )
        )

        assert sent["lines_changed"] == 0
        assert sent["screen_responded"] is False
    finally:
        close_terminal_sessions(context)


def test_terminal_zero_wait_polls_output_that_is_already_ready(tmp_path):
    context = ToolContext(cwd=tmp_path)
    try:
        _payload(
            terminal_session(
                action="start",
                session="main",
                command="sleep 0.15; printf 'HELLO_FROM_CHILD\\n'; sleep 5",
                wait_ms=0,
                context=context,
            )
        )
        time.sleep(0.25)

        looked = _payload(
            terminal_session(action="look", session="main", wait_ms=0, context=context)
        )

        assert any("HELLO_FROM_CHILD" in line for line in looked["lines"])
        assert looked["nonblank_lines"] > 0
    finally:
        close_terminal_sessions(context)


def test_terminal_snapshot_validates_path_before_drain_or_numbering(tmp_path):
    context = ToolContext(cwd=tmp_path)
    try:
        _payload(
            terminal_session(
                action="start",
                session="shot",
                command="sleep 0.15; printf 'PENDING_OUTPUT\\n'; sleep 5",
                wait_ms=0,
                context=context,
            )
        )
        time.sleep(0.25)
        state = context.terminal_sessions["shot"]
        screen_before_errors = list(state["screen"].display)

        first_error = terminal_snapshot(
            session="shot", output_path="bad.txt", wait_ms=500, context=context
        )
        second_error = terminal_snapshot(
            session="shot", output_path="still-bad.jpg", wait_ms=500, context=context
        )

        assert first_error == "ERROR: output_path must end in .png"
        assert second_error == "ERROR: output_path must end in .png"
        assert state["snapshot_n"] == 0
        assert list(state["screen"].display) == screen_before_errors

        terminal_snapshot(session="shot", wait_ms=500, context=context)
        assert (tmp_path / "terminal-snapshots" / "shot-01.png").is_file()
        assert any("PENDING_OUTPUT" in line for line in state["screen"].display)
    finally:
        close_terminal_sessions(context)


def test_terminal_dimensions_are_rejected_outside_start(tmp_path):
    context = ToolContext(cwd=tmp_path)
    try:
        _payload(
            terminal_session(
                action="start",
                session="main",
                command="sleep 5",
                context=context,
            )
        )

        result = terminal_session(
            action="look", session="main", cols=120, rows=50, context=context
        )

        assert result == "ERROR: cols and rows apply only to action=start"
    finally:
        close_terminal_sessions(context)


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


def test_local_probe_server_returns_empty_favicon(tmp_path):
    target = tmp_path / "index.html"
    target.write_text("<p>clean</p>", encoding="utf-8")
    server, url, _ = browser_module._start_local_server(target)
    try:
        favicon_url = url.rsplit("/", 1)[0] + "/favicon.ico"
        with urllib.request.urlopen(favicon_url) as response:
            assert response.status == 204
            assert response.read() == b""
    finally:
        server.shutdown()
        server.server_close()


def test_browser_probe_closes_local_server_when_output_setup_fails(
    tmp_path, monkeypatch
):
    target = tmp_path / "index.html"
    target.write_text("<p>unused</p>", encoding="utf-8")
    events: list[str] = []

    class FakeServer:
        def shutdown(self):
            events.append("shutdown")

        def server_close(self):
            events.append("server_close")

    monkeypatch.setattr(
        browser_module,
        "_start_local_server",
        lambda path: (FakeServer(), "http://127.0.0.1:12345/index.html", path.parent),
    )
    context = ToolContext(cwd=tmp_path)

    def fail_snapshot(_path):
        raise PermissionError("read-only output directory")

    monkeypatch.setattr(context, "snapshot", fail_snapshot)

    report = _error_payload(browser_probe(target=str(target), context=context))

    assert "PermissionError" in report["error"]
    assert events == ["shutdown", "server_close"]


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


@pytest.mark.skipif(
    importlib.util.find_spec("playwright") is None,
    reason="optional Playwright backend is unavailable on this platform",
)
def test_browser_probe_pins_page_region_when_click_creates_canvas(tmp_path):
    target = tmp_path / "creates-canvas.html"
    target.write_text(
        """<!doctype html><html><body style="margin:0">
        <button style="font-size:40px">Play</button>
        <script>
        document.querySelector('button').onclick = () => {
          document.body.innerHTML = '<canvas width="600" height="400"></canvas>';
          const canvas = document.querySelector('canvas');
          canvas.getContext('2d').fillRect(0, 0, 600, 400);
        };
        </script></body></html>""",
        encoding="utf-8",
    )
    context = ToolContext(cwd=tmp_path)

    report = _payload(
        browser_probe(
            target=str(target),
            click="play",
            settle_ms=50,
            viewport_width=800,
            viewport_height=600,
            context=context,
        )
    )

    assert [frame["region"] for frame in report["frames"]] == ["page", "page"]
    assert report["frames"][0]["dimensions"] == report["frames"][1]["dimensions"]
    assert len(report["changed_pixel_percentages"]) == 1
    assert report["changed_pixel_percentages"][0] > 0


@pytest.mark.skipif(
    importlib.util.find_spec("playwright") is None,
    reason="optional Playwright backend is unavailable on this platform",
)
def test_browser_probe_reports_why_pinned_canvas_dimensions_changed(tmp_path):
    target = tmp_path / "resizes-canvas.html"
    target.write_text(
        """<!doctype html><html><body>
        <button>Resize</button><canvas width="400" height="300"></canvas>
        <script>
        document.querySelector('button').onclick = () => {
          const canvas = document.querySelector('canvas');
          canvas.width = 500;
          canvas.getContext('2d').fillRect(0, 0, 500, 300);
        };
        </script></body></html>""",
        encoding="utf-8",
    )
    context = ToolContext(cwd=tmp_path)

    report = _payload(
        browser_probe(
            target=str(target),
            click="resize",
            settle_ms=50,
            context=context,
        )
    )

    assert report["frames"][1]["changed_pct_from_previous"] is None
    assert report["frames"][1]["change_unavailable_reason"] == "dimensions_changed"
    assert report["pixel_change_unavailable"] == [
        {"label": "after click resize", "reason": "dimensions_changed"}
    ]


@pytest.mark.skipif(
    importlib.util.find_spec("playwright") is None,
    reason="optional Playwright backend is unavailable on this platform",
)
def test_browser_probe_total_navigation_failure_is_an_error_result(tmp_path):
    result = browser_probe(
        target="http://127.0.0.1:9/nope",
        settle_ms=0,
        context=ToolContext(cwd=tmp_path),
    )

    report = _error_payload(result)
    assert report["error"]
    assert "failed before it captured a frame" in report["reading"]


@pytest.mark.skipif(
    importlib.util.find_spec("playwright") is None,
    reason="optional Playwright backend is unavailable on this platform",
)
def test_browser_probe_caps_errors_and_returns_parseable_json(tmp_path):
    target = tmp_path / "many-errors.html"
    target.write_text(
        """<!doctype html><html><body><p>visible frame</p><script>
        for (let i = 0; i < 200; i++) console.error(`error-${i}-${'x'.repeat(200)}`);
        </script></body></html>""",
        encoding="utf-8",
    )
    context = ToolContext(cwd=tmp_path, max_tool_result_bytes=5_000)

    result = browser_probe(target=str(target), settle_ms=0, context=context)
    report = _payload(result)

    assert len(result.encode("utf-8")) <= context.max_tool_result_bytes
    assert report["frames"]
    assert report["webgl"]
    assert len(report["console_errors"]) <= 50
    assert len(report["console_errors"]) + report["console_errors_dropped"] == 200
