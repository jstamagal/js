"""js's console: the sink that draws a turn's output events on stdout, or on
the screen when the telemetry's factories are set (`Telemetry.display_factory`,
`reasoning_factory`, `exchange_factory`). It also writes the tool records and
the streamed answer to the transcript log, and mutes the transcript's stdout
tee while it draws them, so each is recorded once."""

from __future__ import annotations

import contextlib
import sys
import threading
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import display
from . import fset
from . import messages as msgs
from . import output
from . import toolkit as T
from .reasoning_display import ReasoningDisplay, StderrReasoning
from .toolkit.core import ToolContext


def _tool_settings(tool_context: ToolContext | None) -> Any:
    """Live settings for the display dials; `/fset TOOLS_DISPLAY 3` reaches the next exchange."""
    context = tool_context or T.STOCK_CONTEXT
    return getattr(getattr(context, "config", None), "settings", None)


def _write_transcript(telemetry: Any, text: str) -> None:
    write_plain = getattr(getattr(telemetry, "transcript_log", None), "write_plain", None)
    if callable(write_plain):
        write_plain(display.for_transcript(text))


def _print_trace(telemetry: Any, text: str) -> None:
    """Write `text` to stdout with the transcript tee muted. Caller holds `output.LOCK`."""
    if not text:
        return
    sink = getattr(telemetry, "transcript_log", None)
    mute = getattr(sink, "mute_tee", None)
    with mute() if callable(mute) else contextlib.nullcontext():
        sys.stdout.write(msgs.for_stream(text))
        sys.stdout.flush()


# Seconds between redraws of a running command's output, and how long a
# command runs before its output is shown at all.
_LIVE_TICK_S = 0.5
_LIVE_AFTER_S = 1.0


def _elapsed_text(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole}s" if whole < 60 else f"{whole // 60}m {whole % 60:02d}s"


@dataclass
class _Fold:
    """The quiet calls of one tool in the current run, shown as one row."""

    name: str
    # Each call as (key, args); the key is the call's own object, so two calls
    # with equal arguments stay apart.
    calls: list[tuple[object, dict | None]] = field(default_factory=list)
    # id(key) -> the whole form of a call that returned, rendered once when it returned.
    wholes: dict[int, str] = field(default_factory=dict)
    running: int = 0
    exchange: Any = None

    def drop(self, key: object) -> None:
        """Forget the call ``key``."""
        self.calls = [call for call in self.calls if call[0] is not key]
        self.wholes.pop(id(key), None)


@dataclass
class _OpenCall:
    """A call `_trace_call` has shown and `_trace_result` will finish."""

    name: str
    args: dict | None
    malformed: bool = False
    fold: _Fold | None = None
    exchange: Any = None
    done: threading.Event = field(default_factory=threading.Event)


class ToolTrace:
    """What one turn shows of its tool calls.

    At TOOLS_DISPLAY 1 the quiet calls between two things that are not quiet are
    a run: each tool's calls in it share one row. On a screen that can redraw
    (`Telemetry.exchange_factory`) a row is drawn when its first call starts
    and redrawn as calls join it; on plain stdout the run is written when it
    ends. Every finished call is written to the transcript on its own."""

    def __init__(self, telemetry: Any) -> None:
        self._telemetry = telemetry
        self._folds: dict[str, _Fold] = {}
        self._held: list[str] = []  # failed quiet calls of a run not yet written

    def _factory(self) -> Callable[[], Any] | None:
        return getattr(self._telemetry, "exchange_factory", None)

    def _fold_text(self, fold: _Fold, settings: Any) -> str:
        if not fold.calls:
            return ""
        return display.render_fold(fold.name, [args for _key, args in fold.calls], running=fold.running,
                                   settings=settings)

    def _draw(self, fold: _Fold, settings: Any) -> None:
        factory = self._factory()
        if factory is None:
            return
        text = self._fold_text(fold, settings)
        if fold.exchange is None:
            fold.exchange = factory()
            fold.exchange.open(text)
            if (whole := self._fold_whole(fold)) is not None:
                # A call that reaches the trace with its result unfolds at once too.
                fold.exchange.update(text, whole)
        else:
            # The calls that have returned unfold at once, before the run ends.
            fold.exchange.update(text, self._fold_whole(fold))

    def _fold_whole(self, fold: _Fold) -> str | None:
        """Every call of a fold row whole, as TOOLS_DISPLAY 3 draws it; None
        for a row left with no call that returned."""
        if not any(id(key) in fold.wholes for key, _args in fold.calls):
            return None
        return "".join(fold.wholes[id(key)] for key, _args in fold.calls if id(key) in fold.wholes)

    def settle(self, settings: Any = None) -> None:
        """End the run: the next quiet call starts a new row. Caller holds `output.LOCK`."""
        folds, held = self._folds, self._held
        self._folds, self._held = {}, []
        if self._factory() is not None:
            for fold in folds.values():
                if fold.exchange is not None and not fold.running:
                    fold.exchange.close(self._fold_text(fold, settings), self._fold_whole(fold))
            return
        _print_trace(self._telemetry, "".join(
            [self._fold_text(fold, settings) for fold in folds.values()] + held))

    def call(self, tool_context: ToolContext | None, name: str, args: dict | None,
             malformed: bool) -> _OpenCall | None:
        settings = _tool_settings(tool_context)
        level = display.tools_level(settings)
        if level <= 0:
            return None
        opened = _OpenCall(name, args, malformed)
        with output.LOCK:
            # Quiet calls fold only on the screen, where the reasoning_toggle key unfolds them.
            if (level == 1 and not malformed and self._factory() is not None
                    and display.call_kind(name, args, settings) == "quiet"):
                fold = self._folds.get(name)
                if fold is None:
                    fold = self._folds[name] = _Fold(name)
                fold.calls.append((opened, args))
                fold.running += 1
                opened.fold = fold
                self._draw(fold, settings)
                return opened
            self.settle(settings)
            factory = self._factory()
            if factory is not None:
                opened.exchange = factory()
                opened.exchange.open(display.render_exchange(
                    name, args, None, level=level, preview=display.preview_lines(settings),
                    malformed=malformed, settings=settings))
        if opened.exchange is not None and name == "shell":
            threading.Thread(target=self._follow, args=(opened, tool_context, level),
                             name="js-live-output", daemon=True).start()
        return opened

    def _follow(self, opened: _OpenCall, tool_context: ToolContext | None, level: int) -> None:
        """Redraw a running command with the tail of its output until it ends."""
        from .toolkit import process_net

        command = (opened.args or {}).get("command")
        if opened.done.wait(_LIVE_AFTER_S):
            return
        while not opened.done.wait(_LIVE_TICK_S):
            tail = process_net.live_tail(tool_context, command, display.LIVE_LINES)
            if tail is None:
                continue
            lines, seconds = tail
            settings = _tool_settings(tool_context)
            with output.LOCK:
                if opened.done.is_set():
                    return
                opened.exchange.update(display.render_exchange(
                    opened.name, opened.args, None, level=level,
                    preview=display.preview_lines(settings), settings=settings,
                    live=lines, elapsed=_elapsed_text(seconds)))

    def result(self, tool_context: ToolContext | None, name: str, result: Any, *,
               args: dict | None, malformed: bool, opened: _OpenCall | None) -> None:
        settings = _tool_settings(tool_context)
        level = display.tools_level(settings)
        if level <= 0:
            return
        finished = display.render_exchange(
            name, args, result, level=level, preview=display.preview_lines(settings),
            malformed=malformed, settings=settings)
        # A folded row's or a preview's whole form, for the reasoning_toggle key to unfold.
        whole = (display.render_exchange(name, args, result, level=3, malformed=malformed, settings=settings)
                 if level < 3 else None)
        if whole == finished:
            whole = None  # shown whole already: nothing to unfold
        with output.LOCK:
            if opened is not None:
                opened.done.set()
            # The transcript is the record: every call whole, at every level.
            _write_transcript(self._telemetry, whole if whole is not None else finished)
            fold = opened.fold if opened is not None else None
            quiet = (level == 1 and not malformed and self._factory() is not None
                     and display.call_kind(name, args, settings) == "quiet")
            failed = display.tool_output(name, result).failed
            if fold is None and quiet and not failed:
                fold = self._folds.get(name)
                if fold is None:
                    fold = self._folds[name] = _Fold(name)
                key = opened if opened is not None else object()
                fold.calls.append((key, args))
                fold.wholes[id(key)] = whole if whole is not None else finished
                self._draw(fold, settings)
                return
            if fold is not None:
                fold.running -= 1
                if failed:
                    fold.drop(opened)
                else:
                    fold.wholes[id(opened)] = whole if whole is not None else finished
                self._draw(fold, settings)
                if self._folds.get(fold.name) is not fold and not fold.running and fold.exchange is not None:
                    # Its run ended while this call ran: the row closes now.
                    fold.exchange.close(self._fold_text(fold, settings), self._fold_whole(fold))
                if not failed:
                    return
            if fold is not None or (quiet and failed):
                # A failed quiet call stands on its own row, inside the run.
                tight = display.render_exchange(
                    name, args, result, level=level, preview=display.preview_lines(settings),
                    malformed=malformed, settings=settings)
                factory = self._factory()
                if factory is None:
                    self._held.append(tight)
                else:
                    row = factory()
                    row.open(tight)
                    row.close(tight, whole)
                return
            if opened is not None and opened.exchange is not None:
                opened.exchange.close(finished, whole)
                return
            self.settle(settings)
            # Plain stdout has no key to unfold a row: every call is shown whole.
            _print_trace(self._telemetry, whole if whole is not None else finished)


def _print_run_line(fields: list[str]) -> None:
    """The run line through its format, wrapped between two fields."""
    parts = msgs.RUN_LINE.runs(fields="".join(display.FIELD_BREAK + display.clean(field) for field in fields))
    for row in display.field_rows(parts, display.block_width()):
        print(msgs.for_stream(fset.to_ansi(row)), flush=True)


@dataclass
class _Stream:
    """The Displays one turn's streamed reply is drawn in, and the transcript
    log it is written to: the one the telemetry had when the turn began."""

    transcript: Any = None
    answer: display.Display | None = None
    thinking: ReasoningDisplay | None = None


class ConsoleSink:
    """Draws the loop's output events as js shows them (see `js/output.py`
    for the events and their fields)."""

    def __init__(self, telemetry: Any) -> None:
        self._telemetry = telemetry
        self._trace = ToolTrace(telemetry)
        self._open: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self._streams: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

    def emit(self, event: output.OutputEvent) -> None:
        handler = getattr(self, f"_on_{event.name}", None)
        if handler is not None:
            handler(**dict(event.fields))

    # Tool exchanges

    def _on_tool_call(self, *, call: Any, name: str, args: dict | None, malformed: bool,
                      context: ToolContext | None, traced: bool = True, deferred: bool = False) -> None:
        if not traced or deferred:
            return
        opened = self._trace.call(context, name, args, malformed)
        if opened is not None:
            self._open[call] = opened

    def _on_tool_result(self, *, call: Any, name: str, args: dict | None, result: Any, malformed: bool,
                        context: ToolContext | None, traced: bool = True, **_more: Any) -> None:
        if not traced:
            return
        opened = self._open.pop(call, None) if call is not None else None
        self._trace.result(context, name, result, args=args, malformed=malformed, opened=opened)

    def _on_tools_settle(self) -> None:
        with output.LOCK:
            self._trace.settle()

    # Lines

    def _on_run_line(self, *, fields: list[str]) -> None:
        _print_run_line(fields)

    def _on_call_stats(self, *, label: str, elapsed_s: float, finish: Any, tool_calls: int, tokens: int,
                       first_token_s: float | None, prompt_tokens: int, cached_tokens: int) -> None:
        tps = (tokens / elapsed_s) if elapsed_s > 0 else 0.0
        cache = ""
        if prompt_tokens > 0 and cached_tokens > 0:
            cache = f"  cache {100.0 * cached_tokens / prompt_tokens:.0f}%"
        ttft = f"  ttft {int(first_token_s * 1000)}ms" if first_token_s is not None else ""
        msgs.say(msgs.CALL_STATS, flush=True, label=f"{label}: " if label else "", ms=int(elapsed_s * 1000),
                 finish=finish, tool_calls=tool_calls, tokens=tokens, tps=tps, ttft=ttft, cache=cache)

    def _on_notice(self, *, message: msgs.Message, fields: dict, stderr: bool, flush: bool) -> None:
        msgs.say(message, file=sys.stderr if stderr else None, flush=flush, **fields)

    def _on_network(self, *, level: int, line: Any, fallback: bool) -> None:
        from . import stream_transport

        stream_transport.draw(level, line, fallback=fallback)

    # The streamed reply

    def _on_stream_open(self, *, stream: output.Key) -> None:
        self._streams[stream] = _Stream(getattr(self._telemetry, "transcript_log", None))

    def _shown(self, stream: output.Key) -> _Stream:
        shown = self._streams.get(stream)
        if shown is None:
            self._on_stream_open(stream=stream)
            shown = self._streams[stream]
        return shown

    def _on_answer_started(self, *, stream: output.Key, suppressed: bool) -> None:
        shown = self._streams.get(stream)
        if shown is not None and shown.thinking is not None:
            shown.thinking.answer_started()

    def _on_answer(self, *, stream: output.Key, text: str, markdown: bool, suppressed: bool) -> None:
        if suppressed:
            return
        shown = self._shown(stream)
        if shown.transcript is not None:
            write_chunk = getattr(shown.transcript, "write_assistant_chunk", None)
            if callable(write_chunk):
                write_chunk(text)
        with _muted_tee(shown.transcript):
            if shown.answer is None:
                self._on_tools_settle()
                factory = self._telemetry.display_factory
                shown.answer = (
                    factory(markdown) if factory is not None
                    else display.Display.for_stream(sys.stdout, markdown=markdown)
                )
                shown.answer.speaker(*display.agent_speaker())
            shown.answer.chunk("text", text)

    def _on_answer_end(self, *, stream: output.Key, suppressed: bool) -> None:
        shown = self._streams.get(stream)
        if shown is None:
            return
        if not suppressed and shown.answer is not None:
            if shown.transcript is not None:
                end_stream = getattr(shown.transcript, "end_assistant_stream", None)
                if callable(end_stream):
                    end_stream()
            with _muted_tee(shown.transcript):
                shown.answer.finish()
        shown.answer = None

    def _on_reasoning(self, *, stream: output.Key, text: str, level: int, suppressed: bool) -> None:
        if suppressed or level == 0:
            return
        shown = self._shown(stream)
        if shown.thinking is None:
            self._on_tools_settle()
            factory = self._telemetry.reasoning_factory
            shown.thinking = (
                factory(level) if factory is not None
                else StderrReasoning(level, sys.stderr)
            )
        shown.thinking.append(text)

    def _on_reasoning_end(self, *, stream: output.Key, tokens: int | None) -> None:
        shown = self._streams.get(stream)
        if shown is not None and shown.thinking is not None:
            shown.thinking.finish(tokens)
            shown.thinking = None


def _muted_tee(transcript: Any):
    mute = getattr(transcript, "mute_tee", None)
    if callable(mute):
        return mute()
    return contextlib.nullcontext()
