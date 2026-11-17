from __future__ import annotations

import asyncio
import re
from pathlib import Path

from repl_driver import record_turns, run_async

from js.config import Config
from js.memory import append_message


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


def _printed(monkeypatch, tmp_path, capsys, line: str, *, system: str = "SYSTEM") -> str:
    """What `line` printed, typed into the REPL on make_cfg with the system
    prompt `system`; it is not a turn."""
    monkeypatch.chdir(tmp_path)
    cfg = make_cfg(tmp_path)
    cfg.prompts_dir.mkdir(parents=True)
    (cfg.prompts_dir / "01-prompt.md").write_text("SYSTEM\n", encoding="utf-8")
    turns = record_turns(monkeypatch)
    printed: list[str] = []

    async def typing(enter):
        # The resumed exchanges, drawn as the screen came up, land first.
        await asyncio.sleep(0)
        capsys.readouterr()
        await enter(line)
        printed.append(capsys.readouterr().out)

    run_async(monkeypatch, cfg, typing, system=system)
    assert turns == []
    return printed[0]


def test_turns_command_prints_message_count(tmp_path, monkeypatch, capsys):
    # /turns prints len(state.messages).
    cfg = make_cfg(tmp_path)
    for message in (
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ):
        append_message(cfg.session_file, message)

    out = _printed(monkeypatch, tmp_path, capsys, "/turns")

    assert re.search(r"(?<!\d)3(?!\d)", out)


def test_persona_command_prints_system_prompt(tmp_path, monkeypatch, capsys):
    # /persona prints state.system[:2048].
    out = _printed(monkeypatch, tmp_path, capsys, "/persona", system="YOU ARE A HELPFUL APE")

    # short prompt -> the prompt and nothing after it.
    assert out == "YOU ARE A HELPFUL APE\n"


def test_persona_command_truncates_at_2048_bytes_with_note(tmp_path, monkeypatch, capsys):
    # state.system longer than 2048 -> only first 2048 printed + a note that
    # carries the FULL byte length.
    full = "A" * 2048 + "TAIL_THAT_MUST_NOT_PRINT"

    out = _printed(monkeypatch, tmp_path, capsys, "/persona", system=full)

    assert "A" * 2048 in out
    assert "TAIL_THAT_MUST_NOT_PRINT" not in out
    assert str(len(full)) in out  # the note names the full length


def test_persona_command_keeps_exactly_2048_without_truncation_note(tmp_path, monkeypatch, capsys):
    # boundary: len == 2048 is not > 2048, so no note is printed.
    full = "B" * 2048

    out = _printed(monkeypatch, tmp_path, capsys, "/persona", system=full)

    assert out == "B" * 2048 + "\n"
