"""Clipboard image paste (js.clipimage): the paste key puts `[image #N]` in the
input line, and the submitted line sends that image the way an @path image is
sent. The clipboard command is a fake script; no test reads a real clipboard."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import ai
import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from js import attach, clipimage, screen, settings
from js import messages as msgs
from js.config import Config

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)
CTRL_V = "\x16"


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch):
    monkeypatch.setattr(clipimage, "_images", {})


@pytest.fixture
def no_display(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)


def _fake_command(tmp_path: Path, body: str) -> str:
    script = tmp_path / "fake-clip"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script)


def _png_command(tmp_path: Path, data: bytes = _PNG) -> str:
    image = tmp_path / "clip.bin"
    image.write_bytes(data)
    return _fake_command(tmp_path, f"cat '{image}'")


def _cfg(tmp_path: Path, *, vision: bool) -> Config:
    sessions = tmp_path / "sessions"
    return Config(
        agent_id="test-agent", agent_dir=sessions, model="offline-test-model",
        provider_id=None, provider_base_url=None, provider_api_key=None,
        reasoning_effort=None, max_output_tokens=None, max_tool_iterations=5,
        max_bash_output_bytes=65536, max_tool_result_bytes=65536, fetch_timeout_s=5,
        debug_log=None, trace=False, history_file=tmp_path / ".history",
        sessions_dir=sessions, session_file=sessions / "session.jsonl",
        prompts_dir=tmp_path / "prompts", vision_enabled=vision,
    )


def _drive(chunks: list[str], live_settings: dict) -> tuple[list[str], str]:
    """Type ``chunks`` into a real screen app with the paste key bound; return
    the submitted lines and the input buffer left at the end."""
    lines: list[str] = []

    async def on_line(line: str) -> None:
        lines.append(line)

    async def main() -> str:
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            app, _scrollback = screen.build_app(
                prompt="> ", history=InMemoryHistory(), completer=None,
                on_line=on_line, on_interrupt=lambda: None, on_eof=lambda: None,
                key_bindings=clipimage.key_bindings(lambda: live_settings),
            )
            task = asyncio.ensure_future(app.run_async())
            for chunk in chunks:
                await asyncio.sleep(0.1)
                pipe.send_text(chunk)
            await asyncio.sleep(0.3)
            left = app.layout.current_buffer.text
            app.exit()
            await task
            return left

    left = asyncio.run(main())
    return lines, left


# --- the command -------------------------------------------------------------

def test_wayland_reads_with_wl_paste_and_x11_with_xclip():
    assert clipimage.clipboard_command({}, {"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":0"}) == [
        "wl-paste", "--type", "image/png"]
    assert clipimage.clipboard_command({}, {"DISPLAY": ":0"}) == [
        "xclip", "-selection", "clipboard", "-t", "image/png", "-o"]


def test_no_display_server_means_no_clipboard():
    assert clipimage.clipboard_command({}, {}) is None
    with pytest.raises(clipimage.ClipboardError) as err:
        clipimage.read_image({}, env={})
    assert err.value.message is msgs.CLIPBOARD_NONE


def test_configured_command_wins_over_the_display(tmp_path):
    command = _png_command(tmp_path)
    live = {"ui": {"paste_image_command": command}}

    assert clipimage.clipboard_command(live, {"WAYLAND_DISPLAY": "wayland-1"}) == [command]
    assert clipimage.read_image(live, env={}) == _PNG


def test_missing_clipboard_tool_is_one_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(clipimage.ClipboardError) as err:
        clipimage.read_image({}, env={"WAYLAND_DISPLAY": "wayland-1"})
    assert err.value.message is msgs.CLIPBOARD_TOOL_MISSING


@pytest.mark.parametrize("body", ["exit 1", "echo just some text"])
def test_clipboard_without_an_image_is_one_error(tmp_path, body):
    live = {"ui": {"paste_image_command": _fake_command(tmp_path, body)}}
    with pytest.raises(clipimage.ClipboardError) as err:
        clipimage.read_image(live, env={})
    assert err.value.message is msgs.CLIPBOARD_NO_IMAGE


def test_image_over_max_file_bytes_is_refused(tmp_path):
    live = {"ui": {"paste_image_command": _png_command(tmp_path)},
            "limits": {"max_file_bytes": len(_PNG) - 1}}
    with pytest.raises(clipimage.ClipboardError) as err:
        clipimage.read_image(live, env={})
    assert err.value.message is msgs.CLIPBOARD_IMAGE_TOO_LARGE


# --- the key -----------------------------------------------------------------

def test_ctrl_v_puts_a_placeholder_in_the_line_and_submit_carries_it(tmp_path):
    live = {"ui": {"paste_image_command": _png_command(tmp_path)}}

    lines, _left = _drive(["what is ", CTRL_V, " and ", CTRL_V, "\r"], live)

    assert lines == ["what is [image #1] and [image #2]"]
    assert clipimage.lookup("[image #1]") == _PNG
    assert clipimage.lookup("[image #2]") == _PNG


def test_paste_key_follows_the_setting(tmp_path):
    live = {"ui": {"paste_image_command": _png_command(tmp_path), "paste_image_key": "escape v"}}

    _lines, left = _drive(["x", "\x1bv"], live)

    assert left == "x[image #1]"


def test_no_clipboard_prints_one_line_and_leaves_the_line_alone(no_display, capsys):
    capsys.readouterr()
    lines, left = _drive(["hello", CTRL_V], {})

    assert left == "hello"
    assert lines == []
    assert clipimage._images == {}
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


# --- submit ------------------------------------------------------------------

def test_placeholders_become_attachments_and_stay_in_the_text():
    token = clipimage.keep(_PNG)

    prompt, attachments = attach.split_repl_attachments(f"compare {token} with @a.png")

    assert attachments == ["a.png", token]
    assert token in prompt
    assert attach.split_repl_attachments("text [image #9]") == ("text [image #9]", [])


@pytest.mark.parametrize("vision", [True, False])
def test_pasted_image_reaches_the_model_as_an_at_path_image_does(tmp_path, vision):
    cfg = _cfg(tmp_path, vision=vision)
    (tmp_path / "shot.png").write_bytes(_PNG)
    token = clipimage.keep(_PNG)

    def parts(line: str) -> list:
        prompt, attachments = attach.split_repl_attachments(line)
        content = attach.build_user_message(prompt, attachments, cfg, cwd=tmp_path).runtime_message["content"]
        if not isinstance(content, list):
            return []
        return [(p.data, p.media_type) for p in content if isinstance(p, ai.types.messages.FilePart)]

    pasted = parts(f"describe {token}")
    from_path = parts("describe @shot.png")

    assert pasted == from_path
    assert bool(pasted) is vision
    if vision:
        assert pasted == [(_PNG, "image/png")]


# --- the settings --------------------------------------------------------------

def test_paste_key_setting_takes_key_names_and_refuses_others():
    spec = settings.SPEC_BY_KEY["ui.paste_image_key"]

    assert settings.coerce_value(spec, "escape  v") == ("escape v", None)
    value, error = settings.coerce_value(spec, "c-notakey")
    assert value is None and error
    assert settings.default_value("ui.paste_image_key") == "c-v"
    assert settings.default_value("ui.paste_image_command") is None
