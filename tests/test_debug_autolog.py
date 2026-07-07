"""Debug trace autolog: the full request trace is written only to a file
(logs/<agent>/<session>.log, on by default), never dumped to the terminal, while
`-d` keeps the concise per-turn diagnostics on stdout.

Regression guard for FIXME3 item 10: a plain run leaked the whole request trace
(unclipped system prompt + all tool schemas + messages JSON) to stdout because
the model-boundary dump was wired to the always-on `runtime.trace` flag.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from js import cli, runtime, settings
from js.config import Config
from js import model_client
from js.model_client import ModelStreamResult

REQUEST_MARKER = "━━ REQUEST (model_client) ━━"


def _fake_stream_result(text: str = "ok"):
    import ai.types.usage

    return ModelStreamResult(
        text=text,
        tool_calls=[],
        reasoning="",
        usage=ai.types.usage.Usage(input_tokens=0, output_tokens=len(text)),
        finish_reason="stop",
        assistant_message=ai.assistant_message(text),
    )


def _make_cfg(tmp_path: Path, log_dir: Path, *, trace: bool = False) -> Config:
    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    (prompts / "01.md").write_text("SYSTEM\n", encoding="utf-8")
    sessions_dir = tmp_path / "sessions" / "test-agent"
    return Config(
        agent_id="test-agent",
        agent_dir=sessions_dir,
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
        trace=trace,
        history_file=tmp_path / ".history",
        sessions_dir=sessions_dir,
        session_file=sessions_dir / "sess.jsonl",
        prompts_dir=prompts,
        # Pin the autolog directory so the test does not depend on platformdirs.
        settings={"runtime": {"debug_autolog_dir": str(log_dir)}},
    )


# --- knob default is on ----------------------------------------------------


def test_debug_autolog_knob_defaults_on():
    spec = settings.SPEC_BY_KEY["runtime.debug_autolog"]
    assert spec.type == "bool"
    assert spec.default is True
    assert spec.env == "JS_DEBUG_AUTOLOG"
    # A fresh settings view (no config, no env) resolves the knob to True.
    seeded = settings.seed_defaults()
    assert settings.get_dotted(seeded, ("runtime", "debug_autolog")) is True


def test_from_env_sets_debug_autolog_default_on(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("JS_AGENT", raising=False)
    monkeypatch.delenv("JS_SESSION", raising=False)
    monkeypatch.delenv("JS_DEBUG_AUTOLOG", raising=False)
    from js.config import from_env

    cfg = from_env()
    assert cfg.debug_autolog is True
    # The resolved autolog path lives one directory over from the session JSONL.
    path = cli._debug_autolog_path(cfg, cfg.settings)
    assert path is not None
    assert path.parent.name == cfg.agent_id
    assert path.parent.parent.name == "logs"
    assert path.suffix == ".log"
    assert path.stem == cfg.session_file.stem


def test_debug_autolog_off_disables_path(tmp_path):
    cfg = _make_cfg(tmp_path, tmp_path / "logs")
    off = dict(cfg.settings)
    off.setdefault("runtime", {})
    off["runtime"] = {**off["runtime"], "debug_autolog": False}
    assert cli._debug_autolog_path(cfg, off) is None


# --- the full request trace goes to the sink, never to stdout --------------


def test_emit_request_trace_writes_to_sink_not_stdout(capsys):
    """The oversized request dump must land in the sink and nowhere near stdout."""
    captured: list[str] = []
    sink = SimpleNamespace(write=captured.append)
    sys_msg = SimpleNamespace(role="system", parts=[SimpleNamespace(text="SYSTEM-PROMPT-BODY")])
    user_msg = SimpleNamespace(role="user", parts=[SimpleNamespace(text="hello")])

    model_client._emit_request_trace(
        sink=sink,
        model_id="offline-test-model",
        provider_id=None,
        provider_base_url=None,
        params=None,
        messages=[sys_msg, user_msg],
        tools=None,
        dump_schemas=True,
        dump_from=0,
    )

    out = capsys.readouterr()
    blob = "".join(captured)
    assert REQUEST_MARKER in blob
    assert "SYSTEM PROMPT (unclipped)" in blob
    assert "SYSTEM-PROMPT-BODY" in blob
    # Nothing reached the terminal.
    assert REQUEST_MARKER not in out.out
    assert out.out == ""


def test_emit_request_trace_none_sink_is_noop(capsys):
    model_client._emit_request_trace(
        sink=None,
        model_id="m",
        provider_id=None,
        provider_base_url=None,
        params=None,
        messages=[],
        tools=None,
        dump_schemas=True,
        dump_from=0,
    )
    assert capsys.readouterr().out == ""


# --- plain run: no trace on stdout, autolog file written -------------------


def test_plain_prompt_run_keeps_trace_off_stdout(monkeypatch, tmp_path, capsys):
    log_dir = tmp_path / "autologs"
    cfg = _make_cfg(tmp_path, log_dir)

    seen: list[dict] = []

    def stub(**kwargs):
        seen.append(kwargs)
        # Stream a token so the redirected stdout (the autolog sink) captures it.
        kwargs["on_text"]("streamed-token ")
        return _fake_stream_result("final answer")

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(runtime.model_client, "stream_model_async", stub)
    monkeypatch.setattr(cli, "_append_turn", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "_maybe_auto_compact", lambda *_a, **_k: None)

    rc = cli.main(["-p", "hi"])
    out = capsys.readouterr().out

    assert rc == 0
    # The request trace never reaches the terminal on a plain run.
    assert REQUEST_MARKER not in out
    assert "▸ run" not in out
    assert "streamed-token" not in out  # streaming was suppressed from the terminal
    # The clean final answer is the only thing on stdout.
    assert "final answer" in out
    # The runtime handed the model boundary a live sink and asked for the trace.
    assert seen[0]["trace_request"] is True
    assert seen[0]["trace_sink"] is not None
    # The stubbed turn wrote to the autolog file.
    autolog = log_dir / "sess.log"
    assert autolog.exists()
    assert "streamed-token" in autolog.read_text(encoding="utf-8")


def test_debug_flag_prints_concise_form(monkeypatch, tmp_path, capsys):
    log_dir = tmp_path / "autologs"
    cfg = _make_cfg(tmp_path, log_dir)

    def stub(**kwargs):
        return _fake_stream_result("hello")

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(runtime.model_client, "stream_model_async", stub)
    monkeypatch.setattr(cli, "_append_turn", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "_maybe_auto_compact", lambda *_a, **_k: None)

    rc = cli.main(["-d", "-p", "hi"])
    out = capsys.readouterr().out

    assert rc == 0
    # -d restores the concise per-turn diagnostics on the terminal ...
    assert "▸ run" in out
    # ... but the oversized request trace still never hits stdout.
    assert REQUEST_MARKER not in out
