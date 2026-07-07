from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import ai

from js import cli, runtime, settings, transcript, tui
from js.config import Config
from js.model_client import ModelStreamResult


def _fake_stream_result(text: str = "ok") -> ModelStreamResult:
    return ModelStreamResult(
        text=text,
        tool_calls=[],
        reasoning="",
        usage=None,
        finish_reason="stop",
        assistant_message=ai.assistant_message(text),
    )


def _cfg(tmp_path: Path, log_dir: Path, *, session_name: str = "sess.jsonl") -> Config:
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    (prompts / "00-tools.md").write_text("---\ntools: []\n---\nSYSTEM\n", encoding="utf-8")
    sessions = tmp_path / ".js" / "sessions" / "test-agent"
    return Config(
        agent_id="test-agent",
        agent_dir=sessions,
        model="offline-test-model",
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
        history_file=sessions / ".history",
        sessions_dir=sessions,
        session_file=sessions / session_name,
        prompts_dir=prompts,
        settings={
            "runtime": {
                "debug_autolog": False,
                "transcript_log_dir": str(log_dir),
            }
        },
        debug_autolog=False,
        transcript_log_dir=str(log_dir),
    )


def test_transcript_log_knob_defaults_on(monkeypatch, tmp_path):
    spec = settings.SPEC_BY_KEY["runtime.transcript_log"]
    assert spec.type == "bool"
    assert spec.default is True
    assert spec.env == "JS_TRANSCRIPT_LOG"
    assert settings.get_dotted(settings.seed_defaults(), ("runtime", "transcript_log")) is True

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.delenv("JS_TRANSCRIPT_LOG", raising=False)

    from js.config import from_env

    cfg = from_env()
    path = cli._transcript_log_path(cfg, cfg.settings)
    assert cfg.transcript_log is True
    assert path is not None
    assert path.parent.parent.name == "transcript"
    assert path.parent.name == cfg.agent_id
    assert path.stem == cfg.session_file.stem


def test_prompt_mode_transcript_logs_final_answer_with_ape(monkeypatch, tmp_path, capsys):
    log_dir = tmp_path / "transcripts"
    cfg = _cfg(tmp_path, log_dir)

    def completion_stub(**_kwargs):
        return _fake_stream_result("FINAL_OK")

    monkeypatch.setattr(cli, "_warn_missing_binaries", lambda: None)
    monkeypatch.setattr(cli, "_from_env", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(runtime.model_client, "stream_model_async", completion_stub)
    monkeypatch.setattr(cli, "_maybe_auto_compact", lambda *_args, **_kwargs: None)

    rc = cli.main(["-p", "hello"])

    assert rc == 0
    assert capsys.readouterr().out == "FINAL_OK\nContinue: js --session sess\n"
    log = (log_dir / "sess.log").read_text(encoding="utf-8")
    assert "<KING> hello" in log
    assert "<APE> FINAL_OK" in log


def test_repl_transcript_logs_user_then_streamed_assistant(monkeypatch, tmp_path):
    log_dir = tmp_path / "transcripts"
    cfg = _cfg(tmp_path, log_dir, session_name="repl.jsonl")

    class SessionStub:
        def __init__(self, history=None, **_kwargs):
            self.lines = iter(["hello", "exit"])

        def prompt(self, *_args, **_kwargs):
            return next(self.lines)

    def completion_stub(**kwargs):
        kwargs["on_text"]("\x1b[32mREPL_OK\x1b[0m")
        return _fake_stream_result("REPL_OK")

    monkeypatch.setattr(cli, "_warn_missing_binaries", lambda: None)
    monkeypatch.setattr(cli, "_from_env", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(cli, "PromptSession", SessionStub)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(runtime.model_client, "stream_model_async", completion_stub)
    monkeypatch.setattr(cli, "_maybe_auto_compact", lambda *_args, **_kwargs: None)

    assert cli.main([]) == 0

    log = (log_dir / "repl.log").read_text(encoding="utf-8")
    assert log.index("<KING> hello") < log.index("<APE> REPL_OK")
    assert "\x1b[" not in log


def test_transcript_sink_strips_ansi(tmp_path):
    path = tmp_path / "screen.log"
    sink = transcript.open_transcript_sink([path])
    assert sink is not None

    sink.write_plain("\x1b[31merror: bad\x1b[0m\n")
    sink.write_assistant("\x1b[32mok\x1b[0m")

    text = path.read_text(encoding="utf-8")
    assert "\x1b[" not in text
    assert "error: bad" in text
    assert "<APE> ok" in text


def test_transcript_logging_failure_never_raises(tmp_path):
    class BadFile:
        def write(self, _text):
            raise OSError("disk is gone")

        def flush(self):
            raise OSError("disk is gone")

        def close(self):
            raise OSError("disk is gone")

    sink = transcript.TranscriptLogSink([BadFile()], [tmp_path / "bad.log"])
    sink.write_user("hello")
    sink.write_plain("still no raise\n")
    sink.close()


def test_tui_write_transcript_reaches_file(monkeypatch, tmp_path):
    log_dir = tmp_path / "transcripts"
    path = log_dir / "tui.log"
    sink = transcript.open_transcript_sink([path])
    assert sink is not None
    cfg = _cfg(tmp_path, log_dir, session_name="tui.jsonl")
    telemetry = runtime.Telemetry(debug_log=None, transcript_log=sink)
    state = {"settings": settings.seed_defaults(), "model": cfg.model}
    app = tui.JsTuiApp(cfg, state, telemetry, None, SimpleNamespace())
    written: list[object] = []

    class Pane:
        def write(self, obj):
            written.append(obj)

    monkeypatch.setattr(tui.JsTuiApp, "query_one", lambda self, *_args, **_kwargs: Pane())

    app._write_transcript("[orange1]error: boom[/]")
    app._write_transcript("answer", speaker="APE", log_text="answer")

    assert written
    text = path.read_text(encoding="utf-8")
    assert "error: boom" in text
    assert "<APE> answer" in text
