"""The async REPL's status bar: layout, overflow, colours, and the TurnStatus seam.

No test here builds a prompt_toolkit Application.
"""

from __future__ import annotations

import asyncio

import ai
import pytest
from prompt_toolkit.output import ColorDepth

from js import model_client, runtime, screen, setcmd, settings
from js.config import Config
from js.model_client import ModelStreamResult
from js.toolkit.core import ToolContext, TurnStatus

BAR = dict(
    clock="23:59",
    provider="cpa",
    model="claude-fable-5-1",
    context_tokens=121000,
    phase="",
    output_tokens=9642,
    throbber="*",
    agent_id="defaultagent",
    session_short="b61643c8",
    cache_pct=100,
)


def _cfg(tmp_path, *, session_name: str = "s.jsonl", model: str = "test-model") -> Config:
    return Config(
        agent_id="defaultagent",
        agent_dir=tmp_path,
        model=model,
        provider_id="openai",
        provider_base_url=None,
        provider_api_key="k",
        reasoning_effort=None,
        max_output_tokens=None,
        max_tool_iterations=5,
        max_bash_output_bytes=65536,
        max_tool_result_bytes=65536,
        fetch_timeout_s=5,
        debug_log=None,
        trace=False,
        history_file=tmp_path / "history",
        sessions_dir=tmp_path,
        session_file=tmp_path / session_name,
        prompts_dir=tmp_path,
    )


def test_status_line_fills_80_columns_with_three_groups():
    line = screen.status_line(80, **BAR)

    assert len(line) == 80
    assert line.startswith("[23:59] cpa/claude-fable-5-1 121000")
    assert line.endswith("defaultagent/b61643c8 cache 100%")
    centre = line[len("[23:59] cpa/claude-fable-5-1 121000"):-len("defaultagent/b61643c8 cache 100%")]
    assert "*" in centre and "9,600" in centre
    assert centre.startswith("  ") and centre.endswith("  ")


def test_status_line_without_a_turn_has_no_centre_group():
    line = screen.status_line(80, **{**BAR, "throbber": ""})

    assert len(line) == 80
    assert line.startswith("[23:59] cpa/claude-fable-5-1 121000")
    assert line.endswith("defaultagent/b61643c8 cache 100%")
    assert "9,600" not in line


def test_status_line_overflow_drops_cache_before_touching_the_model():
    wide = screen.status_line(70, **BAR)

    assert len(wide) == 70
    assert "cache" not in wide
    assert "cpa/claude-fable-5-1" in wide
    assert "defaultagent/b61643c8" in wide


def test_status_line_overflow_order_at_40_columns():
    line = screen.status_line(40, **BAR)

    assert len(line) == 40
    assert "cache" not in line
    assert "claude-fable-5-1" not in line          # truncated from the left
    assert "fable-5-1" in line
    assert "cpa/" not in line
    assert "9,600" not in line
    assert "defaultagent" not in line
    # never dropped: the clock, the throbber, the session
    assert line.startswith("[23:59]")
    assert "*" in line
    assert "b61643c8" in line


def test_status_line_keeps_the_essentials_at_any_width():
    for width in range(18, 120):
        line = screen.status_line(width, **BAR)
        assert len(line) == width
        assert "[23:59]" in line and "*" in line and "b61643c8" in line


def test_status_line_omits_missing_values():
    line = screen.status_line(80, **{**BAR, "provider": None, "cache_pct": None, "agent_id": None})

    assert len(line) == 80
    assert "cache" not in line
    assert line.startswith("[23:59] claude-fable-5-1 121000")
    assert line.endswith("b61643c8")


def test_format_count_is_a_heartbeat_not_an_invoice():
    assert screen.format_count(9642) == "9,600"
    assert screen.format_count(99) == "0"
    assert screen.format_count(10_049) == "10.0k"
    assert screen.format_count(123_456) == "123.5k"


def test_turn_centre_prefers_tool_then_compaction_then_bytes_then_tokens():
    status = TurnStatus()
    status.call_started()
    status.add_bytes(2345)

    phase, count = screen.turn_centre(status, now=0.0, show_bytes=True)
    assert "2,300" in phase and count is None
    phase, count = screen.turn_centre(status, now=0.0, show_bytes=False)
    assert phase == "" and count is None

    status.stream("x" * 400)
    phase, count = screen.turn_centre(status, now=0.0, show_bytes=True)
    assert phase == "" and count == 100

    status.compacting = True
    phase, count = screen.turn_centre(status, now=0.0, show_bytes=True)
    assert phase and count is None

    status.tool_begin(["shell", "read", "read"])
    status.tool_started = 100.0
    phase, count = screen.turn_centre(status, now=112.4, show_bytes=True)
    assert "shell" in phase and "+2" in phase and "12s" in phase and count is None


def test_turn_status_counts_bytes_only_until_the_first_token():
    status = TurnStatus()
    status.call_started()
    status.add_bytes(100)
    status.add_bytes(200)
    assert status.net_bytes == 300

    status.stream("abcdefgh")
    assert status.net_bytes == 0
    assert status.output_tokens == 2
    status.add_bytes(500)
    assert status.net_bytes == 0

    status.settle(40)
    assert status.output_tokens == 40
    status.call_started()
    status.add_bytes(7)
    status.stream("abcd")
    assert status.net_bytes == 0 and status.output_tokens == 41
    status.settle(0)                      # no usage: the estimate stands
    assert status.output_tokens == 41

    status.reset()
    assert status == TurnStatus()


def test_status_style_uses_the_hex_given_and_falls_back_on_garbage():
    assert "#000000" in screen.status_style("#ffffff", "#000000")
    assert screen.status_style("red", "blue") == screen.STATUS_STYLE


def test_status_colour_settings_accept_only_hex():
    live: dict = {}
    assert setcmd.set_command(live, "ui.status_bg #000000").error is None
    assert settings.get_dotted(live, ("ui", "status_bg"), None) == "#000000"
    assert setcmd.set_command(live, "ui.status_fg white").error
    assert setcmd.set_command(live, "ui.net 4").error
    assert setcmd.set_command(live, "ui.net 3").error is None


def test_status_bar_is_truecolor_and_repaints_on_set(monkeypatch):
    """build_app asks for 24-bit colour even on TERM=linux, and the bar's style
    is read on each repaint. Application is replaced by a recorder."""
    captured: dict = {}

    class Recorder:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setenv("TERM", "linux")
    monkeypatch.setattr(screen, "Application", Recorder)
    live: dict = {}
    screen.build_app(
        prompt="> ", history=None, completer=None,
        on_line=None, on_interrupt=None, on_eof=None,
        status=lambda width: "",
        status_colours=lambda: screen.status_style(
            settings.get_dotted(live, ("ui", "status_fg"), screen.STATUS_FG),
            settings.get_dotted(live, ("ui", "status_bg"), screen.STATUS_BG),
        ),
    )

    assert captured["color_depth"] == ColorDepth.DEPTH_24_BIT
    style = captured["style"]
    assert style.get_attrs_for_style_str("class:status").bgcolor == screen.STATUS_BG.lstrip("#")
    setcmd.set_command(live, "ui.status_bg #000000")
    style.invalidation_hash()
    assert style.get_attrs_for_style_str("class:status").bgcolor == "000000"


def test_session_short_is_the_hex_of_the_session_stem(tmp_path):
    path = tmp_path / "20260918T101010000000Z-b61643c8deadbeef.jsonl"
    assert screen.session_short(path) == "b61643c8"


def test_turn_status_climbs_during_a_turn_and_ends_zeroed(monkeypatch, tmp_path):
    context = ToolContext(cwd=tmp_path)
    seen: list[int] = []

    class _FakeProvider:
        async def aclose(self) -> None:
            pass

    class _FakeModel:
        provider = _FakeProvider()

    async def fake_stream_async(*, on_text, **_kwargs):
        model_client._reasoning_sink.get()("thinking hard " * 10)
        on_text("the answer is here")
        seen.append(context.turn_status.output_tokens)
        return ModelStreamResult(
            text="the answer is here", tool_calls=[], reasoning="thinking hard " * 10,
            usage=ai.types.usage.Usage(input_tokens=10, output_tokens=77),
            finish_reason="stop", assistant_message=ai.assistant_message("the answer is here"),
        )

    monkeypatch.setattr(model_client, "resolve_model", lambda *a, **k: _FakeModel())
    monkeypatch.setattr(model_client, "_stream_async", fake_stream_async)
    cfg = _cfg(tmp_path)
    messages = [{"role": "user", "content": "go"}]
    asyncio.run(runtime.run_turn_async(
        cfg, "sys", messages, runtime.Telemetry(debug_log=None),
        tool_context=context, suppress_output=True,
    ))

    assert seen and seen[0] > 0
    assert context.turn_status == TurnStatus()


@pytest.mark.parametrize("turn_active", [False, True])
def test_status_bar_line_never_raises_on_sparse_state(tmp_path, turn_active):
    from js import cli

    cfg = _cfg(tmp_path, session_name="20260918T101010000000Z-0123456789abcdef.jsonl")
    line = cli._status_bar_line(cfg, {"settings": {}}, turn_active, 60)
    assert len(line) == 60
    assert "01234567" in line


def test_status_colour_survives_save_and_load(tmp_path):
    live = settings.seed_defaults()
    assert setcmd.set_command(live, "ui.status_bg #000000").error is None
    path = tmp_path / "jsrc"
    settings.save_settings_to_jsrc(path, live)

    reloaded = settings.seed_defaults()
    assert settings.load_jsrc_files([path], reloaded) == []
    assert settings.get_dotted(reloaded, ("ui", "status_bg"), None) == "#000000"
