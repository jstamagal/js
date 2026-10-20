from __future__ import annotations

from pathlib import Path

import ai

from js import cli, runtime, transcript
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
    (prompts / "01-prompt.md").write_text("SYSTEM\n", encoding="utf-8")
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
            "REQUEST_LOG": False, "TRANSCRIPT_LOG_DIR": str(log_dir)
        },
        debug_autolog=False,
        transcript_log_dir=str(log_dir),
    )


def test_transcript_log_knob_defaults_on(monkeypatch, tmp_path):
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

    monkeypatch.setattr(cli, "_from_env", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(runtime.model_client, "stream_model_async", completion_stub)
    monkeypatch.setattr(cli, "_maybe_auto_compact", lambda *_args, **_kwargs: None)

    rc = cli._run_prompt("hello")

    assert rc == 0
    assert capsys.readouterr().out.splitlines()[0] == "FINAL_OK"
    log = (log_dir / "sess.log").read_text(encoding="utf-8")
    assert "<USER> hello" in log
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

    assert cli.main(["--blocking"]) == 0

    log = (log_dir / "repl.log").read_text(encoding="utf-8")
    assert log.index("<USER> hello") < log.index("<APE> REPL_OK")
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


def test_strip_ansi_removes_string_sequences_and_two_byte_escapes():
    """OSC (title, clipboard 52, hyperlink 8), DCS, APC and two-byte escapes
    must not reach the transcript file, where `cat`/`tail -f` would run them."""
    for raw in (
        "a\x1b]0;PWNED\x07b",
        "a\x1b]52;c;ZGF0YQ==\x07b",
        "a\x1b]8;;http://x.test\x1b\\b",
        "a\x1bPq#0;2;0;0;0\x1b\\b",
        "a\x1b_payload\x1b\\b",
        "a\x1b(Bb",
        "a\x1b=b",
        "a\x1b[31mb",
    ):
        assert transcript.strip_ansi(raw) == "ab", repr(raw)


def test_strip_ansi_keeps_plain_control_characters():
    assert transcript.strip_ansi("a\tb\r\nc") == "a\tb\r\nc"


def test_strip_ansi_drops_lone_esc_and_c1_openers():
    for raw in ("a\x1b", "a\x9d0;PWNED", "a\x90qpayload", "a\x9b2J"):
        assert "\x1b" not in transcript.strip_ansi(raw)
        assert not any("\x80" <= ch <= "\x9f" for ch in transcript.strip_ansi(raw)), repr(raw)


def test_escape_split_across_writes_does_not_rejoin_in_the_file(tmp_path):
    path = tmp_path / "t.log"
    sink = transcript.open_transcript_sink([path])
    sink.write_plain("a\x1b")
    sink.write_plain("[2Jb\n")
    sink.close()
    assert "\x1b" not in path.read_text(encoding="utf-8")


def test_transcript_file_gets_no_osc(tmp_path):
    path = tmp_path / "t.log"
    sink = transcript.open_transcript_sink([path])
    sink.write_assistant("hi\x1b]0;PWNED\x07 there")
    sink.write_plain("tool \x1b]52;c;eA==\x07out\n")
    sink.close()
    text = path.read_text(encoding="utf-8")
    assert "\x1b" not in text
    assert "hi there" in text and "tool out" in text


def test_an_escape_open_on_one_stream_hides_nothing_another_stream_writes():
    import io

    log = io.StringIO()
    sink = transcript.TranscriptLogSink([log], [])
    out = transcript.TranscriptTee(io.StringIO(), lambda: sink)
    err = transcript.TranscriptTee(io.StringIO(), lambda: sink)
    out.write("before \x1b]0;title")
    err.write("STDERR_DIAGNOSTIC\n")
    out.write("\x07 after\n")
    assert "STDERR_DIAGNOSTIC" in log.getvalue()
    assert "title" not in log.getvalue()


def test_prompt_mode_transcript_records_each_tool_call_whole(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace

    log_dir = tmp_path / "transcripts"
    cfg = _cfg(tmp_path, log_dir)
    result = "shell=bash\nexit=0\n--- stdout ---\n" + "\n".join(f"TOOL_LINE_{n}" for n in range(30)) + "\n"

    def run_turn(cfg, system, messages, telemetry, **_kwargs):
        context = SimpleNamespace(config=SimpleNamespace(settings={"TOOLS_DISPLAY": 1}))
        opened = runtime._trace_call(telemetry, context, "shell", {"command": "make"})
        runtime._trace_result(telemetry, context, "shell", result, args={"command": "make"}, exchange=opened)
        messages.append({"role": "assistant", "content": "FINAL_OK"})

    monkeypatch.setattr(cli, "_from_env", lambda *args, **kwargs: cfg)
    monkeypatch.setattr(runtime, "run_turn", run_turn)
    monkeypatch.setattr(cli, "_maybe_auto_compact", lambda *_args, **_kwargs: None)

    cli._run_prompt("hello")

    capsys.readouterr()
    log = (log_dir / "sess.log").read_text(encoding="utf-8")
    assert all(f"TOOL_LINE_{n}" in log for n in range(30)), log


def test_the_transcript_records_the_same_bytes_whatever_console_draws_the_turn(monkeypatch, tmp_path, capsys):
    import asyncio
    import re

    from tool_loading import after_loading

    from js import output
    from js.model_client import ModelToolCall
    from js.toolkit import ToolContext
    from test_output import _offline_config, _reply

    (tmp_path / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")

    def respond(**kwargs):
        if not any(part.kind == "tool_result" and part.tool_call_id == "r1"
                   for message in kwargs["messages"] for part in message.parts):
            return _reply(tool_calls=[ModelToolCall(id="r1", name="read", arguments='{"file_path":"notes.txt"}')])
        kwargs["on_text"]("the answer")
        return _reply("the answer")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", after_loading(respond, "read"))

    class Silent:
        def emit(self, event):
            pass

    def record(console) -> str:
        path = tmp_path / f"{console}.log"
        log = transcript.open_transcript_sink([path])
        telemetry = runtime.Telemetry(None, transcript_log=log)
        if console == "silent":
            telemetry.sink = output.Fanout(transcript.TranscriptSink(telemetry), Silent())
        asyncio.run(runtime.run_turn_async(
            _offline_config(tmp_path, trace=True, settings={}), "system",
            [{"role": "user", "content": "read notes.txt"}], telemetry, tool_context=ToolContext(cwd=tmp_path)))
        log.close()
        return re.sub(r"\[\d\d:\d\d\] ", "", path.read_text(encoding="utf-8"))

    drawn = record("js")
    assert capsys.readouterr().out
    assert record("silent") == drawn
    assert capsys.readouterr().out == ""
    assert "alpha" in drawn and "<APE> the answer" in drawn


def test_transcript_notices_and_statistics_do_not_depend_on_console_rendering(capsys):
    import io

    from js import messages, output

    class Silent:
        def emit(self, event):
            pass

    def record(silent):
        contents = io.StringIO()
        log = transcript.TranscriptLogSink([contents], [])
        log._stamp = lambda: "[00:00] "
        telemetry = runtime.Telemetry(None, transcript_log=log)
        if silent:
            telemetry.sink = output.Fanout(transcript.TranscriptSink(telemetry), Silent())
        with cli._transcript_stdio(telemetry):
            runtime._output(telemetry, "run_line", fields=["model=offline", "tools=3"])
            runtime._output(telemetry, "call_stats", label="", elapsed_s=1, finish="stop",
                            tool_calls=0, tokens=10, first_token_s=0.1, prompt_tokens=20,
                            cached_tokens=10)
            runtime._notice(telemetry, messages.STEERED, flush=True)
            runtime._notice(telemetry, messages.RESPONSE_INCOMPLETE, stderr=True, reason="length")
        return contents.getvalue()

    drawn = record(False)
    assert drawn
    assert capsys.readouterr().out
    assert record(True) == drawn
    assert capsys.readouterr() == ("", "")
