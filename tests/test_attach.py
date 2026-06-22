from __future__ import annotations

import base64
import io
from pathlib import Path

import ai

from js import cli, model_client, runtime
from js.config import Config
from js.memory import load_messages
from js.model_client import ModelStreamResult


_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


def _cfg(tmp_path: Path, *, model: str = "offline-test-model", vision: bool = False) -> Config:
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    (prompts / "01.md").write_text("SYSTEM\n", encoding="utf-8")
    sessions = tmp_path / ".js" / "sessions" / "test-agent"
    return Config(
        agent_id="test-agent",
        agent_dir=sessions,
        model=model,
        provider_id=None,
        provider_base_url=None,
        provider_api_key=None,
        reasoning_effort=None,
        max_output_tokens=None,
        max_tool_iterations=5,
        max_bash_output_bytes=65536,
        max_tool_result_bytes=65536,
        fetch_timeout_s=5,
        debug_log=None,
        trace=False,
        history_file=tmp_path / ".history",
        sessions_dir=sessions,
        session_file=sessions / "session.jsonl",
        prompts_dir=prompts,
        vision_enabled=vision,
    )


def _fake_stream_result(text: str = "ok") -> ModelStreamResult:
    return ModelStreamResult(
        text=text,
        tool_calls=[],
        reasoning="",
        usage=None,
        finish_reason="stop",
        assistant_message=ai.messages.Message(
            role="assistant",
            parts=[ai.types.messages.TextPart(text=text)],
        ),
    )


def test_prompt_file_text_attachment_inlines_content(monkeypatch, tmp_path, capsys):
    note = tmp_path / "notes.md"
    note.write_text("alpha\nbeta\n", encoding="utf-8")
    cfg = _cfg(tmp_path)
    seen: list[str] = []

    def stream_model_stub(**kwargs):
        seen.append(kwargs["messages"][-1].parts[0].text)
        return _fake_stream_result("TEXT_OK")

    monkeypatch.setattr(cli, "_from_env", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(runtime.model_client, "stream_model", stream_model_stub)

    actual = cli.main(["-f", str(note), "-p", "summarize"])

    assert actual == 0
    assert capsys.readouterr().out == "TEXT_OK\nContinue: js --session session\n"
    assert seen and seen[0].startswith("summarize\n\nAttached file:")
    assert str(note) in seen[0]
    assert "alpha\nbeta" in seen[0]
    persisted = load_messages(cfg.session_file)
    assert persisted[0] == {"role": "user", "content": seen[0]}


def test_prompt_dash_file_reads_stdin_bytes_as_attachment(monkeypatch, tmp_path, capsys):
    cfg = _cfg(tmp_path)
    seen: list[str] = []

    class StdinStub:
        buffer = io.BytesIO(b"pasted attachment\n")

        def isatty(self):
            return False

        def read(self):  # pragma: no cover - -f - must consume buffer bytes instead
            raise AssertionError("text stdin reader should not be used for -f -")

    def stream_model_stub(**kwargs):
        seen.append(kwargs["messages"][-1].parts[0].text)
        return _fake_stream_result("STDIN_FILE_OK")

    monkeypatch.setattr(cli, "_from_env", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(runtime.model_client, "stream_model", stream_model_stub)
    monkeypatch.setattr(cli.sys, "stdin", StdinStub())

    actual = cli.main(["-f", "-", "-p", "summarize paste"])

    assert actual == 0
    assert capsys.readouterr().out == "STDIN_FILE_OK\nContinue: js --session session\n"
    assert seen and "Attached file: <stdin>" in seen[0]
    assert "pasted attachment" in seen[0]


def test_history_to_ai_messages_preserves_user_file_part():
    content = [
        ai.types.messages.TextPart(text="VISUAL_FILE image.png mime=image/png size=1 bytes"),
        ai.types.messages.FilePart(data=_TINY_PNG, media_type="image/png"),
    ]

    messages = model_client.history_to_ai_messages("", [{"role": "user", "content": content}])

    assert len(messages) == 1
    assert messages[0].role == "user"
    assert isinstance(messages[0].parts[0], ai.types.messages.TextPart)
    assert isinstance(messages[0].parts[1], ai.types.messages.FilePart)
    assert messages[0].parts[1].data == _TINY_PNG
    assert messages[0].parts[1].media_type == "image/png"


def test_prompt_file_image_attachment_sends_file_part_and_persists_stub(monkeypatch, tmp_path, capsys):
    image = tmp_path / "pixel.png"
    image.write_bytes(_TINY_PNG)
    cfg = _cfg(tmp_path, model="vision-test-gemma4", vision=True)
    seen: list[ai.messages.Message] = []

    def stream_model_stub(**kwargs):
        seen.append(kwargs["messages"][-1])
        return _fake_stream_result("IMAGE_OK")

    monkeypatch.setattr(cli, "_from_env", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(runtime.model_client, "stream_model", stream_model_stub)

    actual = cli.main(["-f", str(image), "-p", "what is this?"])

    assert actual == 0
    assert capsys.readouterr().out == "IMAGE_OK\nContinue: js --session session\n"
    assert seen and seen[0].role == "user"
    text_part = seen[0].parts[0]
    file_part = seen[0].parts[1]
    assert isinstance(text_part, ai.types.messages.TextPart)
    assert "what is this?" in text_part.text
    assert f"VISUAL_FILE {image}" in text_part.text
    assert isinstance(file_part, ai.types.messages.FilePart)
    assert file_part.data == _TINY_PNG
    assert file_part.media_type == "image/png"

    persisted = load_messages(cfg.session_file)
    assert persisted[0]["role"] == "user"
    assert isinstance(persisted[0]["content"], str)
    assert f"VISUAL_FILE {image}" in persisted[0]["content"]
    assert base64.b64encode(_TINY_PNG).decode("ascii") not in persisted[0]["content"]


def test_prompt_file_image_vision_off_falls_back_to_text(monkeypatch, tmp_path, capsys):
    image = tmp_path / "pixel.png"
    image.write_bytes(_TINY_PNG)
    cfg = _cfg(tmp_path, model="offline-test-model", vision=False)
    seen: list[ai.messages.Message] = []

    def stream_model_stub(**kwargs):
        seen.append(kwargs["messages"][-1])
        return _fake_stream_result("NO_VISION_OK")

    monkeypatch.setattr(cli, "_from_env", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(runtime.model_client, "stream_model", stream_model_stub)

    actual = cli.main(["-f", str(image), "-p", "what is this?"])

    assert actual == 0
    assert capsys.readouterr().out == "NO_VISION_OK\nContinue: js --session session\n"
    assert len(seen[0].parts) == 1
    assert isinstance(seen[0].parts[0], ai.types.messages.TextPart)
    assert "vision disabled; image bytes not sent" in seen[0].parts[0].text
    assert not any(isinstance(part, ai.types.messages.FilePart) for part in seen[0].parts)
    assert "vision disabled; image bytes not sent" in load_messages(cfg.session_file)[0]["content"]


def test_repl_at_file_attaches_text_file(monkeypatch, tmp_path, capsys):
    note = tmp_path / "notes.txt"
    note.write_text("repl attachment\n", encoding="utf-8")
    cfg = _cfg(tmp_path)
    seen: list[str] = []

    class SessionStub:
        def __init__(self, history=None):
            self.lines = iter([f"summarize @{note}", "exit"])

        def prompt(self, *args, **kwargs):
            return next(self.lines)

    def run_turn_stub(_cfg, _system, messages, _telemetry, **kwargs):
        seen.append(messages[-1]["content"])

    monkeypatch.setattr(cli, "_from_env", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(cli, "PromptSession", SessionStub)
    monkeypatch.setattr(cli.runtime, "run_turn", run_turn_stub)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)

    actual = cli.main([])

    assert actual == 0
    capsys.readouterr()
    assert seen and seen[0].startswith("summarize\n\nAttached file:")
    assert "repl attachment" in seen[0]
    assert load_messages(cfg.session_file)[0] == {"role": "user", "content": seen[0]}


def test_prompt_file_binary_attachment_uses_descriptor(monkeypatch, tmp_path, capsys):
    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\x00\x01\x02\x03")
    cfg = _cfg(tmp_path)
    seen: list[str] = []

    def stream_model_stub(**kwargs):
        seen.append(kwargs["messages"][-1].parts[0].text)
        return _fake_stream_result("BINARY_OK")

    monkeypatch.setattr(cli, "_from_env", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(runtime.model_client, "stream_model", stream_model_stub)

    actual = cli.main(["-f", str(binary), "-p", "inspect"])

    assert actual == 0
    assert capsys.readouterr().out == "BINARY_OK\nContinue: js --session session\n"
    assert f"ATTACHED_BINARY_FILE {binary}" in seen[0]
    assert "application/octet-stream" in seen[0]
    assert "content not inlined" in seen[0]
    assert load_messages(cfg.session_file)[0]["content"] == seen[0]


def test_missing_attachment_is_clear_error_without_model_call(monkeypatch, tmp_path, capsys):
    cfg = _cfg(tmp_path)

    def stream_model_stub(**kwargs):  # pragma: no cover - must not be called
        raise AssertionError("model should not be called for a missing attachment")

    monkeypatch.setattr(cli, "_from_env", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(runtime.model_client, "stream_model", stream_model_stub)

    actual = cli.main(["-f", str(tmp_path / "missing.txt"), "-p", "summarize"])

    captured = capsys.readouterr()
    assert actual == 2
    assert "attachment not found" in captured.err
    assert load_messages(cfg.session_file) == []
