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

from js import attach, clipimage, model_client, screen, settings
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
    assert clipimage.clipboard_command({}, {"TERM": "xterm-256color"}) is None
    with pytest.raises(clipimage.ClipboardError) as err:
        clipimage.read_image({}, env={})
    assert err.value.message is msgs.CLIPBOARD_NONE


def test_kitty_over_ssh_reads_through_the_terminal():
    env = {"TERM": "xterm-kitty"}
    assert clipimage.clipboard_command({}, env) == [
        "kitten", "clipboard", "-g", "-m", "image/png", "/dev/stdout"]
    assert clipimage.uses_terminal({}, env)
    assert not clipimage.uses_terminal({}, {"TERM": "xterm-kitty", "WAYLAND_DISPLAY": "wayland-1"})


def test_missing_clipboard_tool_is_one_error(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(clipimage.ClipboardError) as err:
        clipimage.read_image({}, env={"WAYLAND_DISPLAY": "wayland-1"})
    assert err.value.message is msgs.CLIPBOARD_TOOL_MISSING


# --- the key -----------------------------------------------------------------


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
        message = attach.build_user_message(prompt, attachments, cfg, cwd=tmp_path)
        [user] = model_client.history_to_ai_messages("", [message], vision=vision)
        return [(p.data, p.media_type) for p in user.parts if isinstance(p, ai.types.messages.FilePart)]

    pasted = parts(f"describe {token}")
    from_path = parts("describe @shot.png")

    assert pasted == from_path
    assert bool(pasted) is vision
    if vision:
        assert pasted == [(_PNG, "image/png")]


# --- the settings --------------------------------------------------------------


@pytest.mark.parametrize("command", ["wl-paste '", "   "])
def test_paste_command_setting_refuses_unsplittable_or_empty(command):
    spec = settings.SPEC_BY_KEY["PASTE_IMAGE_COMMAND"]

    value, error = settings.coerce_value(spec, command)
    assert value is None and error
    assert settings.coerce_value(spec, "wl-paste --type image/png") == ("wl-paste --type image/png", None)


def test_placeholder_takes_plain_bounded_numbers_only():
    token = clipimage.keep(_PNG)
    assert token == "[image #1]"

    assert clipimage.lookup("[image #01]") is None
    assert clipimage.lookup("[image #\u0661]") is None
    assert clipimage.lookup("[image #" + "1" * 5000 + "]") is None
    assert clipimage.placeholders("[image #" + "1" * 5000 + "] [image #1]") == ["[image #1]"]
    assert clipimage.placeholders("[image #01] and [image #1]") == ["[image #1]"]


def test_registered_clipboard_backends_control_fallback_order():
    command = clipimage.clipboard_command(
        {"PASTE_IMAGE_BACKENDS": ["x11", "wayland"]},
        {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"},
    )
    assert command == list(clipimage.X11_COMMAND)


def test_image_storage_can_belong_to_each_assembly():
    first, second = {}, {}
    with clipimage.using_store(first):
        token = clipimage.keep(b"first")
        with clipimage.using_store(second):
            other = clipimage.keep(b"second")
            assert clipimage.lookup(other) == b"second"
        assert clipimage.lookup(token) == b"first"
    assert first == {1: b"first"} and second == {1: b"second"}
