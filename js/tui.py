"""Textual nonblocking REPL cockpit for js."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import io
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.markdown import Markdown
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import Input, RichLog

from . import attach, events, logins, memory as M, providers, replcomplete, runtime, setcmd, settings, supervisor
from . import transcript as transcript_mod
from .config import Config
from .sampling import Sampling


@dataclass(frozen=True)
class TuiDeps:
    """CLI callbacks the TUI uses without importing cli.py back into this module."""

    handle_command: Callable[[str, dict, Config], bool]
    is_turn_state_command: Callable[[str], bool]
    cfg_for_live_state: Callable[[Config, dict], Config]
    append_turn: Callable[[Config, dict], None]
    maybe_auto_compact: Callable[[Config, dict], None]
    sync_telemetry_from_live_settings: Callable[[Config, dict, runtime.Telemetry], None]
    sync_sampling_from_live_settings: Callable[[dict], Sampling]
    sync_model_from_live_settings: Callable[[dict], None]
    sync_provider_from_live_settings: Callable[[dict, list[str]], None]
    sync_tool_registry_from_live_settings: Callable[[Config, dict], None]
    event_results_changed_sampling: Callable[[list[events.EventHandlerResult]], bool]
    event_results_changed_model: Callable[[list[events.EventHandlerResult]], bool]
    event_result_changed_keys: Callable[[list[events.EventHandlerResult]], list[str]]
    changed_provider_key: Callable[[list[str]], bool]
    changed_lock_subagent_model_key: Callable[[list[str]], bool]


class TuiEventHooks(events.EventHooks):
    """EventHooks subclass that also posts every emission to the Textual app."""

    def __init__(self, app: JsTuiApp, dispatcher: events.EventHandlerDispatcher | None = None) -> None:
        super().__init__(dispatcher)
        self.app = app

    def emit(self, event: str, **payload) -> events.EventEmission:
        emission = super().emit(event, **payload)
        try:
            self.app.call_from_thread(self.app.on_runtime_event, emission.event, emission.payload)
        except RuntimeError:
            # Textual forbids call_from_thread from the app loop. REPL input events
            # originate on that loop; runtime/tool events may originate elsewhere.
            self.app.on_runtime_event(emission.event, emission.payload)
        return emission


class TuiInput(Input):
    """Input with REPL history and existing js completion semantics."""

    BINDINGS = [
        Binding("up", "history_prev", "history previous", show=False, priority=True),
        Binding("down", "history_next", "history next", show=False, priority=True),
        Binding("tab", "complete", "complete", show=False, priority=True),
        *Input.BINDINGS,
    ]

    def action_history_prev(self) -> None:
        self.app.action_history_prev()

    def action_history_next(self) -> None:
        self.app.action_history_next()

    def action_complete(self) -> None:
        self.app.action_complete()


class JsTuiApp(App[int]):
    """Nonblocking Textual REPL: input stays hot while turns/tools run."""

    CSS = """
    Screen {
        background: #111318;
        color: #d8dee9;
    }
    #body {
        height: 1fr;
        border: solid #5e81ac;
        padding: 0 1;
    }
    #transcript {
        height: 1fr;
    }
    Input {
        dock: bottom;
        border: solid #ebcb8b;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel_turn", "cancel/drain", show=False, priority=True),
        Binding("ctrl+d", "quit_now", "quit", show=False, priority=True),
        Binding("ctrl+l", "clear_logs", "clear", show=False),
    ]

    def __init__(self, cfg: Config, state: dict, telemetry: runtime.Telemetry, prompt_spec: Any, deps: TuiDeps) -> None:
        super().__init__()
        self.cfg = cfg
        self.state = state
        self.telemetry = telemetry
        self.prompt_spec = prompt_spec
        self.deps = deps
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.sup: supervisor.Supervisor | None = None
        self.consumer: asyncio.Task | None = None
        self._turn_count = 0
        self._history: list[str] = []
        self._history_index: int | None = None
        self._completer = replcomplete.JsCompleter(
            setting_keys=[spec.key for spec in settings.REGISTRY],
            names=lambda: sorted(set(providers.known_provider_ids()) | set(logins.load_logins())),
            spell=None,
        )
        self._response_rendered = False

    def compose(self) -> ComposeResult:
        with Container(id="body"):
            yield RichLog(id="transcript", markup=True, wrap=True, highlight=True)
        yield TuiInput(placeholder="type prompt or /command", id="prompt")

    async def on_mount(self) -> None:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=32, thread_name_prefix="js-tui-dispatch"))
        self.sup = supervisor.Supervisor(loop)
        supervisor.set_current(self.sup)
        hookset = TuiEventHooks(self)
        dispatcher = setcmd.EventCommandDispatcher(
            settings=self.state["settings"],
            cwd=getattr(self.cfg, "project_dir", Path.cwd()),
            events=hookset,
        )
        hookset.set_dispatcher(dispatcher)
        self.state["events"] = hookset
        self.consumer = asyncio.create_task(self._turn_consumer(loop))
        self.query_one(TuiInput).focus()
        self._write_transcript(f"[bold green]js[/] [dim]{self.cfg.agent_id}[/] model=[cyan]{self.state['model']}[/]")
        self._refresh_status()

    async def on_unmount(self) -> None:
        supervisor.set_current(None)
        self._drain_queue()
        if self.sup:
            for job in self.sup.jobs():
                job.task.cancel()
        if self.consumer and not self.consumer.done():
            self.consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.consumer

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        event.input.value = ""
        self._history_index = None
        if not line:
            return
        self._history.append(line)
        if line in {"/exit", "/quit", "/q"}:
            self.exit(0)
            return
        if line in ("/flush", "/cancel queued"):
            flushed = self._drain_queue()
            self._write_transcript(f"[orange1](dropped {flushed} queued prompt{'s' if flushed != 1 else ''})[/]")
            return
        assert self.sup is not None
        if self.deps.is_turn_state_command(line) and self.sup.turn_active():
            self._write_transcript(f"[orange1](a turn is running — {line.split()[0]} would clobber context; ctrl+c to cancel it, or wait)[/]")
            return
        if await self._handle_command(line):
            self.deps.sync_telemetry_from_live_settings(self.cfg, self.state, self.telemetry)
            self._refresh_status()
            return
        self._write_transcript(Text(f"KING 👑 {line}", style="bold #ebcb8b"), speaker="KING", log_text=line)
        self.queue.put_nowait(line)
        if self.sup.turn_active() or self.queue.qsize() > 1:
            self._write_transcript(f"[dim](queued — {self.queue.qsize()} ahead)[/]")
        self._refresh_status()

    async def _handle_command(self, line: str) -> bool:
        if line in {"/model", "/pick-model"}:
            self._write_transcript("[orange1]/model picker is disabled inside --tui for now. Use /model PROVIDER/MODEL or /model MODEL.[/]")
            return True
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            handled = await asyncio.get_running_loop().run_in_executor(
                None, self.deps.handle_command, line, self.state, self.cfg
            )
        out = buf.getvalue().strip()
        if out:
            self._write_transcript(out)
        if line in {"exit", "quit", ":q"}:
            self.exit(0)
        return handled

    async def _turn_consumer(self, loop: asyncio.AbstractEventLoop) -> None:
        while True:
            line = await self.queue.get()
            try:
                await self._start_turn(line, loop)
            finally:
                self.queue.task_done()
                self._refresh_status()

    async def _start_turn(self, line: str, loop: asyncio.AbstractEventLoop) -> None:
        assert self.sup is not None
        prompt_text, line_attachments = attach.split_repl_attachments(line)
        input_event = self._emit_repl_event("input", text=prompt_text, attachments=line_attachments)
        self._sync_from_event(input_event)
        self.deps.sync_telemetry_from_live_settings(self.cfg, self.state, self.telemetry)
        try:
            turn_cfg = self.deps.cfg_for_live_state(self.cfg, self.state)
            user_bundle = attach.build_user_message(prompt_text, line_attachments, turn_cfg)
        except attach.AttachmentError as e:
            self._write_transcript(f"[orange1]error: {e}[/]")
            return
        before_len = len(self.state["messages"])
        self.state["messages"].append(user_bundle.runtime_message)
        self.deps.append_turn(self.cfg, user_bundle.history_message)
        self._write_transcript("[dim]assistant running...[/]")
        job = self.sup.spawn(
            self._do_turn(user_bundle, turn_cfg, before_len, loop),
            kind="turn",
            label=prompt_text[:40],
        )
        self._refresh_status()
        with contextlib.suppress(asyncio.CancelledError):
            await job.task

    async def _do_turn(self, user_bundle, turn_cfg: Config, before_len: int, loop: asyncio.AbstractEventLoop) -> None:
        try:
            self._response_rendered = False
            before_sampling = self.deps.sync_sampling_from_live_settings(self.state["settings"])
            await runtime.run_turn_async(
                turn_cfg,
                self.state["system"],
                self.state["messages"],
                self.telemetry,
                trace_override=bool(settings.get_dotted(self.state["settings"], ("runtime", "trace"), self.cfg.trace)),
                reasoning_effort_override=turn_cfg.reasoning_effort,
                max_output_override=turn_cfg.max_output_tokens,
                tool_registry=self.state["tool_registry"],
                sampling=self._sampling_for_turn(turn_cfg, before_sampling),
                event_hooks=self.state.get("events"),
                suppress_output=True,
            )
            after_sampling = self.deps.sync_sampling_from_live_settings(self.state["settings"])
            if after_sampling != before_sampling:
                self.state["sampling_cli"] = after_sampling
            self.state["messages"][before_len] = user_bundle.history_message
            for m in self.state["messages"][before_len + 1:]:
                self.deps.append_turn(self.cfg, m)
            await loop.run_in_executor(None, functools.partial(self.deps.maybe_auto_compact, turn_cfg, self.state))
            if not self._response_rendered:
                self._render_latest_assistant(before_len)
            self._turn_count += 1
        except asyncio.CancelledError:
            cancel_event = self._emit_repl_event("cancel", reason="cancelled")
            self._sync_from_event(cancel_event)
            self.deps.sync_telemetry_from_live_settings(self.cfg, self.state, self.telemetry)
            if len(self.state["messages"]) > before_len + 1:
                self._write_transcript("[orange1](turn interrupted — partial work kept)[/]")
                self.state["messages"][before_len] = user_bundle.history_message
                for m in self.state["messages"][before_len + 1:]:
                    self.deps.append_turn(self.cfg, m)
                M.append_mark(self.cfg.session_file, "turn_interrupted")
                self.state["messages"][:] = M.balance_orphaned_tool_calls(self.state["messages"])
            else:
                self._write_transcript("[orange1](turn aborted)[/]")
                self.state["messages"][:] = self.state["messages"][:before_len]
                M.append_mark(self.cfg.session_file, f"rollback_to:{before_len}")
                M.append_mark(self.cfg.session_file, "turn_aborted")
            raise
        except Exception as e:  # noqa: BLE001
            self._write_transcript(f"[orange1]error: {type(e).__name__}: {e}[/]")
            self.state["messages"][:] = self.state["messages"][:before_len]
            M.append_mark(self.cfg.session_file, f"rollback_to:{before_len}")
            M.append_mark(self.cfg.session_file, f"error: {type(e).__name__}: {e}")

    def _sampling_for_turn(self, turn_cfg: Config, sampling_cli: Sampling):
        # Import lazily to avoid a cli.py import cycle at module load.
        from .cli import _sampling_for_turn

        return _sampling_for_turn(turn_cfg, self.prompt_spec, sampling_cli)

    def _sync_from_event(self, emission: events.EventEmission) -> None:
        if self.deps.event_results_changed_sampling(emission.results):
            self.state["sampling_cli"] = self.deps.sync_sampling_from_live_settings(self.state["settings"])
        if self.deps.event_results_changed_model(emission.results):
            self.deps.sync_model_from_live_settings(self.state)
        changed = self.deps.event_result_changed_keys(emission.results)
        if self.deps.changed_provider_key(changed):
            self.deps.sync_provider_from_live_settings(self.state, changed)
        if self.deps.changed_lock_subagent_model_key(changed):
            self.deps.sync_tool_registry_from_live_settings(self.cfg, self.state)

    def _emit_repl_event(self, event: str, **payload: Any) -> events.EventEmission:
        hookset = self.state.get("events")
        if isinstance(hookset, events.EventHooks):
            return hookset.emit(event, **payload)
        return events.EventEmission(event=event, payload=payload, hooks=[])

    def on_runtime_event(self, event: str, payload: dict) -> None:
        if event == "stream":
            return
        if event == "response":
            text = str(payload.get("text", "")).strip()
            if text:
                self._write_transcript(Markdown(text), speaker="APE")
                self._response_rendered = True
            return
        if event == "tool_call":
            self._write_transcript(f"[magenta]▸ tool[/] {payload.get('name')} [dim]{payload.get('arguments', '')[:160]}[/]")
        elif event == "tool_result":
            result = str(payload.get("result", ""))
            first = result.splitlines()[0] if result else ""
            self._write_transcript(f"[cyan]▸ result[/] {payload.get('name')} [dim]{first[:160]}[/]")
        elif event == "turn_end":
            self._write_transcript(f"[green]▸ turn[/] {payload.get('reason', 'done')}")
        elif event == "error":
            self._write_transcript(f"[orange1]▸ error[/] {payload.get('error')}")
        elif event in {"prompt", "input", "cancel"}:
            return
        self._refresh_status()

    def _render_latest_assistant(self, before_len: int) -> None:
        for message in reversed(self.state["messages"][before_len + 1:]):
            if message.get("role") == "assistant" and message.get("content"):
                text = str(message["content"]).strip()
                self._write_transcript(Markdown(text), speaker="APE")
                return
        self._write_transcript("[orange1](turn ended with no assistant text)[/]")

    def action_cancel_turn(self) -> None:
        flushed = self._drain_queue()
        n = self.sup.cancel_kind("turn") if self.sup is not None else 0
        prompt = self.query_one(TuiInput)
        if prompt.value:
            prompt.value = ""
        self._write_transcript(f"[orange1](cancelled {n} active turn{'s' if n != 1 else ''}; dropped {flushed} queued)[/]")

    def action_quit_now(self) -> None:
        self._drain_queue()
        if self.sup:
            for job in self.sup.jobs():
                job.task.cancel()
        self.exit(0)

    def action_clear_logs(self) -> None:
        self.query_one("#transcript", RichLog).clear()

    def _drain_queue(self) -> int:
        dropped = 0
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self.queue.task_done()
            dropped += 1
        return dropped

    def _refresh_status(self) -> None:
        return

    def _write_transcript(self, obj: object, *, speaker: str | None = None, log_text: str | None = None) -> None:
        self.query_one("#transcript", RichLog).write(obj)
        sink = getattr(self.telemetry, "transcript_log", None)
        if sink is None:
            return
        text = log_text if log_text is not None else transcript_mod.render_plain(obj)
        if speaker == "KING":
            write_user = getattr(sink, "write_user", None)
            if callable(write_user):
                write_user(text)
            return
        if speaker == "APE":
            write_assistant = getattr(sink, "write_assistant", None)
            if callable(write_assistant):
                write_assistant(text)
            return
        write_plain = getattr(sink, "write_plain", None)
        if callable(write_plain):
            write_plain(text.rstrip("\n") + "\n")

    def action_history_prev(self) -> None:
        if not self._history:
            return
        if self._history_index is None:
            self._history_index = len(self._history) - 1
        else:
            self._history_index = max(0, self._history_index - 1)
        prompt = self.query_one(TuiInput)
        prompt.value = self._history[self._history_index]
        prompt.cursor_position = len(prompt.value)

    def action_history_next(self) -> None:
        if self._history_index is None:
            return
        prompt = self.query_one(TuiInput)
        self._history_index += 1
        if self._history_index >= len(self._history):
            self._history_index = None
            prompt.value = ""
        else:
            prompt.value = self._history[self._history_index]
        prompt.cursor_position = len(prompt.value)

    def action_complete(self) -> None:
        prompt = self.query_one(TuiInput)
        candidates, token_len = self._completer.candidates(prompt.value[: prompt.cursor_position])
        if not candidates:
            return
        if len(candidates) == 1:
            replacement = candidates[0]
            start = prompt.cursor_position - token_len
            prompt.value = prompt.value[:start] + replacement + prompt.value[prompt.cursor_position:]
            prompt.cursor_position = start + len(replacement)
            return
        self._write_transcript("[dim]" + "  ".join(candidates[:40]) + "[/]")


def run_tui_repl(cfg: Config, state: dict, telemetry: runtime.Telemetry, prompt_spec: Any, deps: TuiDeps) -> int:
    app = JsTuiApp(cfg, state, telemetry, prompt_spec, deps)
    result = app.run()
    return int(result or 0)
