"""Answer Display: finished Markdown blocks commit once, the last block is live,
and output that is not a terminal is plain text."""

from __future__ import annotations

import io
import re
from pathlib import Path

from js import cli, display, runtime, screen
from js.config import Config
from js.model_client import ModelStreamResult
from test_debug_autolog import _fake_stream_result

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

ANSWER = """Here are three snippets.

```python
alpha_one = 1
```

Then the second.

```python
beta_two = 2
```

```python
gamma_three = 3
```

Done.
"""


class RecordingLive:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def width(self) -> int:
        return 60

    def update(self, rendered: str) -> None:
        self.events.append(("update", rendered))

    def commit(self, rendered: str) -> None:
        self.events.append(("commit", rendered))


def _stream(text: str, sink: display.Display, size: int = 5) -> None:
    for start in range(0, len(text), size):
        sink.chunk("text", text[start:start + size])
    sink.finish()


def test_three_fenced_blocks_each_render_highlighted_once_and_are_never_redrawn():
    live = RecordingLive()
    _stream(ANSWER, display.Display(lambda _text: None, live=live, refresh_s=0))

    commits = [text for kind, text in live.events if kind == "commit"]
    for name in ("alpha_one", "beta_two", "gamma_three"):
        holding = [text for text in commits if name in ANSI.sub("", text)]
        assert len(holding) == 1, name
        # Highlighted: the committed block carries colour sequences.
        assert ANSI.search(holding[0])
        committed_at = live.events.index(("commit", holding[0]))
        later_updates = [text for kind, text in live.events[committed_at + 1:] if kind == "update"]
        assert not any(name in ANSI.sub("", text) for text in later_updates), name
    # Everything the stream carried ends up committed.
    plain = ANSI.sub("", "".join(commits))
    for words in ("Here are three snippets.", "Then the second.", "Done."):
        assert words in plain


def test_not_a_terminal_writes_plain_bytes_as_they_arrive():
    out = io.StringIO()
    sink = display.Display.for_stream(out)
    assert not sink.pretty

    sink.chunk("text", "**bold** and `code`\x1b[31m")
    assert out.getvalue() == "**bold** and `code`"
    sink.chunk("text", " end")
    sink.finish()

    assert out.getvalue() == "**bold** and `code` end\n"


def test_markdown_off_on_a_terminal_writes_plain_bytes():
    class Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    out = Tty()
    display.print_answer("# Title\n\ntext", out, markdown=False)

    assert out.getvalue() == "# Title\n\ntext\n"


def test_scrollback_live_answer_is_replaced_until_committed():
    scrollback = screen.Scrollback()
    scrollback.append("before\n")
    scrollback.answer_update("a\n")
    scrollback.answer_update("ab\n")
    scrollback.answer_commit("AB\n")
    scrollback.answer_update("next\n")
    scrollback.append("after\n")
    scrollback.answer_commit("NEXT\n")
    scrollback.flush()

    assert scrollback.buffer.text == "before\nAB\nNEXT\nafter\n"


def test_scrollback_live_answer_follows_a_collapsing_reasoning_block():
    scrollback = screen.Scrollback()
    block = scrollback.reasoning(1)
    block.append("thinking hard\nstill thinking\n")
    scrollback.answer_update("x\n")
    block.answer_started()
    scrollback.answer_update("xy\n")
    scrollback.answer_commit("XY\n")

    text = scrollback.buffer.text
    assert text.endswith("XY\n")
    assert "still thinking" not in text
    assert text.count("XY") == 1
    assert "xy" not in text and "\nx\n" not in text


def test_tty_live_redraws_only_the_rows_it_wrote():
    written: list[str] = []
    live = display.TtyLive(written.append)

    live.update("one\ntwo\n")
    live.commit("final\n")

    # The commit moves up over exactly the two live rows and erases below them.
    assert written[-2] == "\x1b[2F\x1b[J"
    assert written[-1] == "final\n"


def _cfg(tmp_path: Path) -> Config:
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "01.md").write_text("SYSTEM\n", encoding="utf-8")
    sessions = tmp_path / "sessions"
    return Config(
        agent_id="test-agent", agent_dir=sessions, model="offline-test-model",
        provider_id=None, provider_base_url=None, provider_api_key=None,
        reasoning_effort=None, max_output_tokens=None, max_tool_iterations=5,
        max_bash_output_bytes=65536, max_tool_result_bytes=65536, fetch_timeout_s=5,
        debug_log=None, trace=False, history_file=tmp_path / ".history",
        sessions_dir=sessions, session_file=sessions / "sess.jsonl", prompts_dir=prompts,
        settings={"runtime": {"debug_autolog": False, "transcript_log": False}},
    )


def test_prompt_mode_to_a_pipe_prints_the_answer_as_plain_bytes(monkeypatch, tmp_path, capsys):
    cfg = _cfg(tmp_path)
    answer = "# Heading\n\n```python\nx = 1\n```\n\x1b]0;title\x07tail"

    def stub(**kwargs):
        kwargs["on_text"](answer)
        result: ModelStreamResult = _fake_stream_result(answer)
        return result

    monkeypatch.setattr(cli, "_from_env", lambda session=None, save_session=True, extras=None: cfg)
    monkeypatch.setattr(runtime.model_client, "stream_model_async", stub)
    monkeypatch.setattr(cli, "_append_turn", lambda *_a, **_k: None)
    monkeypatch.setattr(cli, "_maybe_auto_compact", lambda *_a, **_k: None)

    assert cli.main(["-p", "hi", "--no-save"]) == 0
    out = capsys.readouterr().out

    assert out.startswith("# Heading\n\n```python\nx = 1\n```\ntail\n")
    assert "\x1b" not in out
