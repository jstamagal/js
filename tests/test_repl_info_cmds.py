from __future__ import annotations

from pathlib import Path

from js import cli
from js.config import Config


def make_cfg(tmp_path: Path) -> Config:
    return Config(
        agent_id="repl-agent",
        agent_dir=tmp_path / ".js" / "sessions" / "repl-agent",
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
        history_file=tmp_path / ".js" / "sessions" / "repl-agent" / ".history",
        sessions_dir=tmp_path / ".js" / "sessions" / "repl-agent",
        session_file=tmp_path / ".js" / "sessions" / "repl-agent" / "repl.jsonl",
        prompts_dir=tmp_path / "prompts" / "repl-agent",
    )


def test_turns_command_prints_message_count(tmp_path, capsys):
    # /turns -> cli.py:588-590, prints len(state["messages"]).
    cfg = make_cfg(tmp_path)
    state = {
        "messages": [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "user", "content": "three"},
        ],
    }

    handled = cli._handle_command("/turns", state, cfg)

    assert handled is True
    out = capsys.readouterr().out
    assert "3 messages in context" in out


def test_turns_command_reports_zero_for_empty_context(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    state = {"messages": []}

    handled = cli._handle_command("/turns", state, cfg)

    assert handled is True
    assert "0 messages in context" in capsys.readouterr().out


def test_persona_command_prints_system_prompt(tmp_path, capsys):
    # /persona -> cli.py:582-587, prints state["system"][:2048].
    cfg = make_cfg(tmp_path)
    state = {"messages": [], "system": "YOU ARE A HELPFUL APE"}

    handled = cli._handle_command("/persona", state, cfg)

    assert handled is True
    out = capsys.readouterr().out
    assert "YOU ARE A HELPFUL APE" in out
    # short prompt -> no truncation note.
    assert "truncated" not in out


def test_persona_command_truncates_at_2048_bytes_with_note(tmp_path, capsys):
    # state["system"] longer than 2048 -> only first 2048 printed + a note that
    # carries the FULL byte length (cli.py:584-586).
    cfg = make_cfg(tmp_path)
    full = "A" * 2048 + "TAIL_THAT_MUST_NOT_PRINT"
    state = {"messages": [], "system": full}

    handled = cli._handle_command("/persona", state, cfg)

    assert handled is True
    out = capsys.readouterr().out
    assert "A" * 2048 in out
    assert "TAIL_THAT_MUST_NOT_PRINT" not in out
    assert "truncated" in out
    assert str(len(full)) in out


def test_persona_command_keeps_exactly_2048_without_truncation_note(tmp_path, capsys):
    # boundary: len == 2048 is not > 2048, so no note is printed.
    cfg = make_cfg(tmp_path)
    full = "B" * 2048
    state = {"messages": [], "system": full}

    handled = cli._handle_command("/persona", state, cfg)

    assert handled is True
    out = capsys.readouterr().out
    assert "B" * 2048 in out
    assert "truncated" not in out


def test_session_command_prints_session_file_path(tmp_path, capsys):
    # /session -> cli.py:591-593, prints cfg.session_file.
    cfg = make_cfg(tmp_path)
    state = {"messages": []}

    handled = cli._handle_command("/session", state, cfg)

    assert handled is True
    assert str(cfg.session_file) in capsys.readouterr().out
