"""Every renderer, fed text mixing wide characters, tabs, emoji and long
unbroken runs, keeps each row inside the window in cells; the ones that wrap
rather than fold lose no visible character."""

from __future__ import annotations

import io
import re
from collections import Counter

import pytest
from prompt_toolkit.utils import get_cwidth

from js import display, fset, reasoning_display, runtime, screen, transcript
from js import messages as msgs

SGR = re.compile(r"\x1b\[[0-9;]*m")
SAMPLES = [
    "汉字汉字 tab\there and\tthere 😀😀 " + "x" * 90 + " end",
    "漢" * 70,
    "short",
    "a\tb\tc " + "😀" * 30 + " " + "y" * 45,
    "🇺🇸🇯🇵 flags 👨‍👩‍👧 family 🏳️‍🌈 " + "🇺🇸" * 15,
    "\t\t漢a",
    "    indented by spaces, then words that run on past the edge of the row",
    "a" * 17 + "\x1b[31mhello\x1b[0m raw \x1b]0;title\x07escapes",
    "| a | b |\n|---|---|\n| short | " + "x" * 26 + "TAIL |",
    "hello\n\n\x1b]0;TITLE\n\n\x07world \x1b[1mbold\x1b[0m",
    "an unended string \x1b]0;keeps its words",
    "| A | B | C | D | E | F | G | H |\n|---|---|---|---|---|---|---|---|\n| a1 | b2 | c3 | d4 | e5 | f6 | g7 | h8 |",
    "| Alpha | Bravo | Charlie | Delta | Echo | Foxtrot | Golf | Hotel |\n|---|---|---|---|---|---|---|---|",
]


def _chunks(text: str, size: int) -> list[str]:
    """``text`` in pieces of ``size``, so an escape sequence is cut between two."""
    return [text[start:start + size] for start in range(0, len(text), size)]


@pytest.fixture(params=["", "🇺🇸"])
def marks(request):
    """The stock speaker marks, or a flag for both."""
    if request.param:
        fset.set_entry("MARK_USER", request.param)
        fset.set_entry("MARK_AGENT", request.param)
    return request.param


def _rows(text: str) -> list[str]:
    return SGR.sub("", text).split("\n")


def _visible(text: str) -> Counter:
    """The characters that show: not spaces, not the variation selectors the
    scrollback drops on purpose (js.screen._drawable)."""
    return Counter(char for char in SGR.sub("", text) if not char.isspace() and char not in "\ufe0e\ufe0f")


def _fits(text: str, width: int) -> None:
    widest = max(_rows(text), key=get_cwidth)
    assert get_cwidth(widest) <= width, (width, widest)


def _keeps(source: str, shown: str) -> None:
    """Every visible character of ``source``, cleaned of escape sequences as
    js cleans model and tool text, is in ``shown``, and no piece of an escape
    sequence shows."""
    missing = _visible(display.clean(source)) - _visible(shown)
    assert not missing, missing
    assert "31m" not in SGR.sub("", shown) and "]0;" not in SGR.sub("", shown), repr(shown)


def _adds_only(source: str, shown: str, *, allowed: str) -> None:
    """Every visible character ``shown`` has beyond ``source``'s is one js
    draws around the text (in ``allowed``) or a table's rule: no piece of an
    escape sequence's payload."""
    extra = _visible(shown) - _visible(display.clean(source))
    assert not [char for char in extra if char not in allowed and not "\u2500" <= char <= "\u257f"], extra


@pytest.fixture(params=[20, 40, 80])
def width(request, monkeypatch):
    """The terminal's width; js draws one column short of it."""
    monkeypatch.setattr(display, "terminal_width", lambda: request.param)
    return request.param


@pytest.mark.parametrize("sample", SAMPLES)
def test_conversation_text_fits_and_keeps_every_character(width, sample, marks):
    drawn = "".join(fset.plain(part) for part in (display.agent_speaker()[0], display.user_speaker()[0],
                                                  msgs.REASONING_HEADING.body()))
    line = display.user_line(sample)
    _fits(line, width - 1)
    _keeps(sample, line)

    for size in (1, 7, len(sample)):
        out = io.StringIO()
        sink = display.Display(out.write)
        sink.speaker(*display.agent_speaker())
        for piece in _chunks(sample, size):
            sink.chunk("text", piece)
        sink.finish()
        _fits(out.getvalue(), width - 1)
        _keeps(sample, out.getvalue())
        _adds_only(sample, out.getvalue(), allowed=drawn)

        class Live:
            def __init__(self) -> None:
                self.shown = ""

            def width(self) -> int:
                return width - 1

            def update(self, rendered: str) -> None:
                pass

            def commit(self, rendered: str) -> None:
                self.shown += rendered

        live = Live()
        rendered = display.Display(lambda _text: None, live=live, refresh_s=0)
        rendered.speaker(*display.agent_speaker())
        for piece in _chunks(sample, size):
            rendered.chunk("text", piece)
        rendered.finish()
        _fits(live.shown, width - 1)
        _keeps(sample.replace("|", "").replace("-", ""), live.shown)  # Markdown turns a table's syntax into a grid
        _adds_only(sample, live.shown, allowed=drawn)

        scroll = screen.Scrollback()
        block = scroll.reasoning(2)
        for piece in _chunks(sample + "\n", size):
            block.append(piece)
        block.finish()
        _fits(scroll.buffer.text, width - 1)
        _keeps(sample, scroll.buffer.text)
        _adds_only(sample, scroll.buffer.text, allowed=drawn)

        log = io.StringIO()
        sink = transcript.TranscriptLogSink([log], [])
        for piece in _chunks(sample, size):
            sink.write_assistant_chunk(piece)
        sink.end_assistant_stream()
        logged = re.sub(r"(?m)^\[\d\d:\d\d\] <APE> ", "", log.getvalue())
        _keeps(sample, logged)
        _adds_only(sample, logged, allowed=drawn)

        err = io.StringIO()
        stderr_block = reasoning_display.StderrReasoning(2, err)
        for piece in _chunks(sample, size):
            stderr_block.append(piece)
        stderr_block.finish()
        _keeps(sample, err.getvalue())
        _adds_only(sample, err.getvalue(), allowed=drawn)


@pytest.mark.parametrize("sample", SAMPLES)
@pytest.mark.parametrize("level", [1, 2, 3])
def test_tool_rows_fit_and_the_whole_form_keeps_every_character(width, sample, level):
    result = f"shell=bash\nexit=0\n--- stdout ---\n{sample}\n{sample}\n"
    args = {"command": f"echo {sample}", "cwd": sample}
    shown = display.render_exchange("shell", args, result, level=level)
    _fits(shown, width - 1)
    if level == 3:
        _keeps(f"echo {sample}{sample}{sample}{sample}", shown)
    fset.set_entry("TOOL_NO_OUTPUT", sample)
    _fits(display.render_exchange("web_thing", {"q": sample}, "", level=level), width - 1)


@pytest.mark.parametrize("sample", SAMPLES)
def test_the_run_line_and_the_status_bar_fit(width, sample, capsys):
    runtime._print_run_line([f"model={sample}", "ctx=1000000", sample])
    printed = capsys.readouterr().out
    _fits(printed, width - 1)
    _keeps(f"model={sample}ctx=1000000{sample}", printed)
    bar = screen.status_line(width, clock="23:59", provider="cpa", model=sample, context_tokens=1000,
                             phase=sample, output_tokens=100, throbber="*", agent_id=sample,
                             session_short="s1", cache_pct=50, cost="$1")
    assert get_cwidth(fset.plain(bar)) == width, (width, bar)
    assert msgs.STATUS_CLOCK.line(clock="23:59")  # the bar draws through its formats


TOOL_CALLS = [
    ("shell", {"command": "echo ARGA", "cwd": "/srv/ARGB"}, "shell=bash\nexit=0\n--- stdout ---\nRESULTA\n"),
    ("terminal_session", {"command": "make ARGA"}, "RESULTA"),
    ("kernel", {"code": "print('ARGA')"}, "RESULTA"),
    ("toolbox", {"source": "def ARGA(): pass"}, "RESULTA"),
    ("write", {"file_path": "/w/ARGA.py", "content": "ARGB = 1"}, "wrote RESULTA"),
    ("plan", {"plan_name": "ARGA", "content": "ARGB one line"}, "RESULTA"),
    ("read", {"file_path": "/w/ARGA.py"}, "1|RESULTA"),
    ("fs_search", {"pattern": "ARGA\nARGB", "path": "/w"}, "RESULTA"),
    ("fetch", {"url": "https://ARGA.test/x"}, "RESULTA"),
    ("send", {"to": "ARGA", "text": "ARGB"}, "RESULTA"),
    ("post_task", {"title": "ARGA", "body": "ARGB"}, "RESULTA"),
    ("finish_task", {"key": "ARGA", "result": "ARGB"}, "RESULTA"),
    ("web_thing", {"q": "ARGA", "extra": {"deep": "ARGB"}}, "RESULTA"),
]


@pytest.mark.parametrize(("name", "args", "result"), TOOL_CALLS, ids=[call[0] for call in TOOL_CALLS])
def test_the_whole_form_of_every_tool_shows_every_argument_and_its_result(width, name, args, result):
    shown = display.render_exchange(name, args, result, level=3)
    _fits(shown, width - 1)
    plain = "".join(SGR.sub("", shown).split())
    missing = [token for token in ("ARGA", "ARGB", "RESULTA")
               if (token in "".join(map(str, args.values())) + result) and token not in plain]
    assert not missing, (name, missing, shown)


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize("literal", ["%C00" + "x" * 19, "%C00" + "x" * 60, "%C04" + "漢" * 40 + "%C tail",
                                     "CUSTOM_PREFIX_LONG_ENOUGH %s", "%C04" + "LONG PREFIX " * 12 + "%C%s tail"])
def test_a_coloured_literal_run_line_wraps_without_breaking_its_escapes(width, literal, monkeypatch):
    import sys

    monkeypatch.setattr(sys, "stdout", _Terminal())
    fset.set_entry("RUN_LINE", literal)
    runtime._print_run_line(["model=m", "ctx=1"])
    printed = sys.stdout.getvalue()
    if "%s" in literal:
        _keeps("model=mctx=1", printed)
    _fits(printed, width - 1)
    assert not re.search(r"\x1b(?!\[[0-9;]*m)", printed), repr(printed)
    assert all(row.strip() not in ("m", "0m") for row in _rows(printed))
    _keeps(fset.plain(fset.render(literal, [])), printed)


@pytest.mark.parametrize("hint", ["", "  press %s to see the whole of it, " + "漢字" * 12 + " every word",
                                  " " + "x" * 90])
@pytest.mark.parametrize("tokens", [None, 1_000, 123_456_789])
def test_folded_headings_and_their_hints_fit(width, hint, tokens):
    if hint:
        fset.set_entry("TOOL_EXPAND", hint)
        fset.set_entry("REASONING_EXPAND", hint)
    scroll = screen.Scrollback()
    scroll.toggle_key = "c-o"
    block = scroll.reasoning(1)
    block.append("thinking it over\n")
    block.finish(tokens)
    _fits(scroll.buffer.text.rstrip("\n"), width - 1)
    _keeps(fset.plain(msgs.REASONING_EXPAND.body(key="c-o")), scroll.buffer.text)

    folded = display.render_exchange("read", {"file_path": "/w/" + "a" * 30 + ".py"}, "1|x", level=1)
    whole = display.render_exchange("read", {"file_path": "/w/" + "a" * 30 + ".py"}, "1|x", level=3)
    span = scroll.exchange_open(folded)
    scroll.exchange_close(span, folded, whole)
    scroll.flush()
    _fits(scroll.buffer.text.rstrip("\n"), width - 1)
    _keeps(fset.plain(msgs.TOOL_EXPAND.body(key="c-o")), scroll.buffer.text)


def _every_argument(tool) -> tuple[dict, list[str]]:
    """Arguments setting every parameter of ``tool``, each to a value that
    shows a token of its own; and those tokens."""
    args, tokens = {}, []
    for index, (key, schema) in enumerate(tool.params.items()):
        kind = (schema or {}).get("type")
        token = f"ARG{index}TOKEN"
        if kind == "boolean":
            args[key], token = True, key
        elif kind in ("integer", "number"):
            args[key] = 7000 + index
            token = str(7000 + index)
        elif kind == "array":
            args[key] = [token]
        elif kind == "string":
            args[key] = token
        else:
            args[key] = {"inner": token}
        tokens.append(token)
    return args, tokens


def _tools():
    from js.toolkit.registry import registry_for_roots

    return list(registry_for_roots(None).tools)


@pytest.mark.parametrize("tool", _tools(), ids=lambda tool: tool.name)
def test_the_whole_form_of_every_tool_shows_every_one_of_its_arguments(tool):
    args, tokens = _every_argument(tool)
    for result in ("RESULTA", "ERROR: it failed"):
        shown = "".join(SGR.sub("", display.render_exchange(tool.name, args, result, level=3, width=200)).split())
        missing = [token for token in tokens if token not in shown]
        assert not missing, (tool.name, result, missing)


@pytest.mark.parametrize("columns", [3, 7, 9])
def test_a_table_whose_cells_are_rule_characters_loses_none_of_them(width, columns):
    """Cells made of the characters rich draws its rules with: only the
    count outside the rule rows tells whether a column was left out."""
    source = ("| " + " | ".join("\u2500" for _ in range(columns)) + " |\n|" + "---|" * columns
              + "\n| " + " | ".join("\u2500\u2500" for _ in range(columns)) + " |\n")
    shown = SGR.sub("", display.render_markdown(source, width - 1))
    _fits(shown, width - 1)
    rule = re.compile("\u2500{4,}")
    drawn = sum(row.count("\u2500") for row in shown.split("\n") if not rule.fullmatch(row.strip()))
    assert drawn >= 3 * columns, shown
