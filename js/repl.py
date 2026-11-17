"""The interactive REPL: startup, the loop that reads lines while a turn
runs, the turns it runs one at a time, steering, the status bar, and the
`/session` switch that replaces the process once the screen has closed.
Slash commands live in `js.repl_commands`, the live state in `js.repl_state`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import contextvars
import copy
import errno
import functools
import json
import os
import shlex
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace

from prompt_toolkit.application.current import get_app_session
from prompt_toolkit.key_binding import merge_key_bindings

from . import (
    agent_messages,
    attach,
    clipimage,
    command_line,
    console,
    conversation,
    debug_trace,
    display as display_mod,
    events,
    exline,
    fset,
    jsrc_line,
    keys as keys_mod,
    login_defaults,
    logins,
    memory as M,
    messages as msgs,
    motd,
    network_display,
    output,
    owning_loop,
    pastes,
    paths as _paths,
    persona as P,
    prompt_commands,
    prompt_history,
    providers,
    repl_commands,
    repl_settings_commands,
    repl_state,
    repl_workspace_commands,
    replcomplete,
    resume_view,
    run_assembly,
    runtime,
    screen,
    session_catalog,
    session_picker,
    session_store,
    setcmd,
    setting_values,
    settings,
    skills,
    snippets,
    status_bar,
    subagent_windows,
    supervisor,
    tool_display,
    transcript as transcript_mod,
    usage as usage_mod,
    usage_display,
)
from .config import Config, InvocationSources, jsrc_paths
from .repl_state import ReplState
from .toolkit import subagents, task_jobs


def print_resume_hint(cfg: Config, state: ReplState) -> None:
    """Print the command that reopens this session.

    Without it the name is only recoverable by hunting through the sessions
    directory, and the session is effectively lost the moment the screen scrolls.
    The model is folded in so the resume comes back on the one in use."""
    if cfg.session_file == Path(os.devnull) or not state.messages:
        return
    # A bare `js --session` runs the `DEFAULT_AGENT` setting, so the hint names the
    # agent unless both this run and that setting are on the js/jsrc default.
    stock = settings.default_value("DEFAULT_AGENT")
    named = cfg.agent_id != stock or settings.knob(cfg.settings, "DEFAULT_AGENT") != stock
    model = state.model
    resume = command_line.argv(("--agent", cfg.agent_id if named else None),
                               ("--model", model if isinstance(model, str) and model else None),
                               ("--home", str(_paths.home()) if _paths.custom_home() else None),
                               ("--session", session_store.session_hint_arg(cfg)))
    msgs.say(msgs.RESUME_HINT, command=shlex.join(["js", *resume]))


def _debug_log_from_live_settings(cfg: Config, live_settings: dict) -> Path | None:
    enabled = settings.live_bool_setting(live_settings, ("DEBUG_LOG",), cfg.debug_log is not None)
    if not enabled:
        return None
    return cfg.debug_log or (_paths.state_root() / cfg.agent_id / "debug.log")


def _sync_telemetry_from_live_settings(cfg: Config, state: ReplState, telemetry: runtime.Telemetry) -> None:
    debug_log = _debug_log_from_live_settings(cfg, state.settings)
    if debug_log is not None:
        try:
            debug_log.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    telemetry.debug_log = debug_log
    _sync_trace_sink(cfg, state.settings, telemetry)
    transcript_mod.sync_transcript_sink(cfg, state.settings, telemetry)


def _sync_trace_sink(
    cfg: Config, live_settings: dict, telemetry: runtime.Telemetry, extra_paths: tuple[Path, ...] = ()
) -> None:
    """Attach/refresh ``telemetry.trace_sink`` for the always-on autolog. Reopens
    only when the target path set changes, so a long REPL session keeps one open
    handle; toggling ``REQUEST_LOG`` off closes it."""
    desired: list[Path] = []
    autolog = debug_trace.debug_autolog_path(cfg, live_settings)
    if autolog is not None:
        desired.append(autolog)
    desired.extend(extra_paths)
    old = telemetry.trace_sink
    if isinstance(old, debug_trace.DebugTraceSink) and old.paths == desired:
        return  # already open on exactly these targets
    telemetry.trace_sink = debug_trace.open_debug_trace_sink(desired) if desired else None
    if isinstance(old, debug_trace.DebugTraceSink) and old is not telemetry.trace_sink:
        old.close()


def _sync_provider_delta_from_live_settings(
    state: ReplState,
    before: tuple[str | None, str | None, str | None],
    after: tuple[str | None, str | None, str | None],
) -> None:
    changed_keys: list[str] = []
    if before[0] != after[0]:
        changed_keys.append("PROVIDER")
    if before[1] != after[1]:
        changed_keys.append("BASE_URL")
    if before[2] != after[2]:
        changed_keys.append("API_KEY")
    if changed_keys:
        repl_state.sync_provider_from_live_settings(state, changed_keys)


# The session `/session` chose, run once the REPL has closed: {"target"}.
pending_switch: dict[str, object] = {}


def _queue_switch(state: ReplState) -> None:
    target = state.switch_session
    if isinstance(target, session_picker.SessionTarget):
        pending_switch.update(target=target)


def exec_session(target: session_picker.SessionTarget) -> None:
    """Replace this process with js resuming `target` in its own directory
    and agent. Resume puts it back on its last-stamped model."""
    session_picker.enter_session_dir(target)
    argv = [sys.executable, "-m", "js", *command_line.argv(
        ("--session", str(target.path)),
        ("-a", target.agent or None),
        ("-C", target.cwd if target.jailed and target.cwd else None),
    )]
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, argv)


def _command_completions(state: ReplState) -> dict[str, str | None]:
    """Verb -> argument completion source, for every command and alias."""
    table: dict[str, str | None] = {name: None for name in prompt_commands.discover(Path.cwd())}
    table.update({verb: command.complete for verb, command in repl_commands.commands(state).items()})
    table.update({name: None for name in state.aliases})
    return table


def _event_dispatcher(state: ReplState, cfg: Config):
    """Run `on` handler text through the command table."""

    def dispatch(hook: events.EventHook, emission: events.EventEmission) -> events.EventHandlerResult:
        handled, error = repl_commands.run_command(setcmd.expand_line(hook.handler, state.settings), state, cfg)
        if not handled:
            parsed = jsrc_line.split_command(hook.handler)
            error = msgs.EVENT_HANDLER_COMMAND_UNSUPPORTED.words(value=parsed[0] if parsed else '<blank>')
        return events.EventHandlerResult(hook=hook, error=error)

    return dispatch


def _session_mcp_host(cfg, telemetry):
    if getattr(cfg, "mcp", None) is None or not getattr(cfg.mcp, "servers", ()):
        return None
    from .mcp.host import MCPHost

    return MCPHost(cfg.mcp, telemetry=telemetry, paranoid_mode=getattr(cfg, "paranoid_mode", "off"))


async def _close_session_mcp_host(state: ReplState) -> None:
    host = state.mcp_host
    if host is not None:
        await host.close()


def _default_startup_banner(cfg: Config, state: ReplState, notices: list[str], errors: list[str]) -> str:
    """What the REPL prints into its scrollback when it is up: the MOTD
    (`js.motd`) raw with its colours reset after, then the startup line,
    notices and errors."""
    text = motd.pick(settings.knob(state.settings, "MOTD_FILE"), settings.knob(state.settings, "MOTD_DIR"))
    return "\n".join([
        *([motd.lines_for_scrollback(text.rstrip("\r\n")) + motd.RESET] if text else []),
        msgs.STARTUP_TITLE.line(),
        msgs.STARTUP_AGENT.line(agent=cfg.agent_id),
        msgs.STARTUP_MODEL.line(model=state.model),
        msgs.STARTUP_PROMPT.line(prompt=", ".join(map(str, cfg.prompts_dir)) if isinstance(cfg.prompts_dir, tuple) else cfg.prompts_dir),
        msgs.STARTUP_MEMORY.line(memory=cfg.session_file),
        "",
        repl_state.repl_hint(msgs.STARTUP_HINT, state).line(),
        "",
        *notices,
        *(msgs.failure_line(error) for error in errors),
    ])


def _startup_banner(cfg: Config, state: ReplState, notices: list[str], errors: list[str]) -> str:
    render = repl_state.repl_assembly(state).banner or _default_startup_banner
    return render(cfg, state, notices, errors)


def _prompt_origin(cfg: Config) -> prompt_history.Origin:
    """Where a prompt typed now is recorded as typed: the working directory,
    the session's `--session` name, the agent."""
    session = "" if cfg.session_file == Path(os.devnull) else session_store.display_name(cfg.session_file)
    return prompt_history.Origin(cwd=os.getcwd(), session=session, agent=cfg.agent_id)


def live_state(cfg, prompt_spec, *, messages: list[dict] | None = None,
                system: str | None = None, model: str | None = None,
                live_settings: dict | None = None, tool_registry=None,
                assembly: repl_state.ReplAssembly | None = None) -> ReplState:
    """The REPL's live state for one session.

    ``model`` is a --model flag value; without one the session runs on
    ``cfg.model``. Unset arguments come from ``cfg`` and ``prompt_spec``: the
    session file's replayed messages, the agent's system prompt, a copy of the
    configured settings, and the configured registry narrowed to the agent's
    tool selectors. The state's event hooks dispatch into this state."""
    if messages is None:
        messages = M.load_replay_messages(cfg.session_file)
    if system is None:
        system = prompt_spec.system
    if live_settings is None:
        live_settings = copy.deepcopy(cfg.settings) if isinstance(cfg.settings, dict) else {}
    fset.bind_settings(live_settings)
    if tool_registry is None:
        tool_registry = run_assembly.registry_for(cfg).select(prompt_spec.tool_selectors)
    event_hooks = events.EventHooks()
    state = ReplState(
        messages=messages,
        system=system,
        model=model if model is not None else cfg.model,
        # A --model flag never lands in the settings store, so /set annotates
        # `MODEL = ... (live: --model flag)` from here; agent.yaml's `model:`
        # is recorded the same way.
        model_source="--model flag" if model is not None else (
            f"agent {cfg.agent_id}" if P.agent_model_applies(cfg, prompt_spec, None) else None),
        provider_id=cfg.provider_id,
        provider_base_url=cfg.provider_base_url,
        provider_api_key=cfg.provider_api_key,
        provider_headers=dict(getattr(cfg, "provider_headers", {}) or {}),
        settings=live_settings,
        prompt_spec=prompt_spec,
        events=event_hooks,
        tool_registry=tool_registry,
        tool_selectors=prompt_spec.tool_selectors,
        repl_assembly=assembly,
    )
    event_hooks.set_dispatcher(_event_dispatcher(state, cfg))
    return state


def _repl_history(cfg: Config) -> prompt_history.PromptHistory:
    """The input history of the REPL."""
    return prompt_history.PromptHistory(
        cfg.history_file,
        lambda: _prompt_origin(cfg),
        cwd_first=settings.knob(cfg.settings, "HISTORY_ORDER") == "cwd",
        limit=settings.knob(cfg.settings, "HISTORY_SIZE"),
        state_root=_paths.state_root(),
    )


def _session_stamp(cfg: Config) -> dict:
    """The stamp for assistant messages a turn on `cfg` writes."""
    return M.stamp_for(getattr(cfg, "model", None), getattr(cfg, "provider_id", None),
                       getattr(cfg, "reasoning_effort", None))


def _persist_turn_messages(cfg: Config, messages: list[dict]) -> None:
    """Persist the turn; `cfg` is the one the turn ran on, so its stamp is the turn's model."""
    M.persist_messages(cfg.session_file, messages, stamp=_session_stamp(cfg))


def _turn_user_index(messages: list[dict], user_message: dict) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        if messages[index] is user_message or messages[index] == user_message:
            return index
    return None


def _turn_has_progress(messages: list[dict], user_message: dict) -> bool:
    index = _turn_user_index(messages, user_message)
    return bool(messages) if index is None else index < len(messages) - 1


def _discard_unstarted_turn(cfg: Config, messages: list[dict], user_message: dict) -> None:
    index = _turn_user_index(messages, user_message)
    if index is not None:
        del messages[index:]
    _persist_turn_messages(cfg, messages)


def _take_pending_notes(state: ReplState) -> list[str]:
    """The reminders queued since the last user message, and one for each
    background task that finished unread since then, removed from the queue."""
    queued, state.pending_notes = state.pending_notes, []
    return [*queued, *task_jobs.completion_notes(runtime.T.STOCK_CONTEXT.task_owner)]


def _with_notes(message: dict, notes: list[str]) -> dict:
    for note in notes:
        message = attach.with_note(message, note)
    return message


def _requeue_notes(state: ReplState, notes) -> None:
    """Put ``notes`` back in front of the queue: the turn that carried them was discarded."""
    if notes:
        state.pending_notes = [*notes, *state.pending_notes]


def _live_cfg_snapshot(cfg: Config, state: ReplState) -> Config:
    """The turn cfg from live state, never from a half-applied command."""
    with repl_state.LIVE_STATE_LOCK:
        return state.config_for_turn(cfg)


async def _do_turn(cfg, state, telemetry, prompt_spec, user_message, turn_cfg, loop,
                   steer=None, notes=()) -> None:
    """One main turn on the async loop. Runs the turn, syncs live-settings
    deltas, persists new messages, then awaits auto-compaction. Owns cancellation
    ENTIRELY: on ^C the turn Task is cancelled, and this handler — never the
    caller — persists partial work and heals orphaned tool_calls, then re-raises
    so the job ends cancelled.

    ``steer`` hands the runtime lines typed during the turn. ``notes`` are
    the queued reminders ``user_message`` carries; a discarded turn puts
    them back in the queue.
    """
    try:
        before_turn_model = repl_state.model_from_live_settings(state.settings)
        before_turn_provider = repl_state.provider_from_live_settings(state.settings)
        before_turn_lock = settings.live_bool_setting(
            state.settings, ("SUBAGENT_MODEL_LOCK",), cfg.lock_subagent_model
        )
        await runtime.run_turn_async(
            turn_cfg,
            state.system,
            state.messages,
            telemetry,
            trace_override=bool(
                settings.get_dotted(state.settings, ("TRACE",), cfg.trace)
            ),
            reasoning_effort_override=turn_cfg.reasoning_effort,
            max_output_override=turn_cfg.max_output_tokens,
            tool_registry=state.tool_registry,
            sampling=login_defaults.sampling(turn_cfg),
            event_hooks=state.events,
            mcp_host=state.mcp_host,
            steer=steer,
            checkpoint=lambda: _persist_turn_messages(turn_cfg, state.messages),
        )
        after_turn_model = repl_state.model_from_live_settings(state.settings)
        if after_turn_model != before_turn_model:
            repl_state.sync_model_from_live_settings(state)
        after_turn_provider = repl_state.provider_from_live_settings(state.settings)
        _sync_provider_delta_from_live_settings(state, before_turn_provider, after_turn_provider)
        after_turn_lock = settings.live_bool_setting(
            state.settings, ("SUBAGENT_MODEL_LOCK",), cfg.lock_subagent_model
        )
        if after_turn_lock != before_turn_lock:
            repl_state.sync_tool_registry_from_live_settings(cfg, state)
        _sync_telemetry_from_live_settings(cfg, state, telemetry)
        _persist_turn_messages(turn_cfg, state.messages)
        await conversation.maybe_auto_compact_async(turn_cfg, state)
    except asyncio.CancelledError:
        repl_state.emit_repl_event(state, telemetry, "cancel", reason="cancelled")
        _sync_telemetry_from_live_settings(cfg, state, telemetry)
        if _turn_has_progress(state.messages, user_message):
            print()
            msgs.say(msgs.TURN_INTERRUPTED_KEPT)
            _persist_turn_messages(turn_cfg, state.messages)
            M.append_mark(cfg.session_file, "turn_interrupted")
            state.messages[:] = M.balance_orphaned_tool_calls(state.messages)
        else:
            print()
            msgs.say(msgs.TURN_ABORTED)
            _discard_unstarted_turn(cfg, state.messages, user_message)
            _requeue_notes(state, notes)
            M.append_mark(cfg.session_file, "turn_aborted")
        raise
    except snippets.SnippetError:
        if _turn_has_progress(state.messages, user_message):
            _persist_turn_messages(turn_cfg, state.messages)
        else:
            _discard_unstarted_turn(cfg, state.messages, user_message)
            _requeue_notes(state, notes)
        raise
    except Exception as e:  # noqa: BLE001
        msgs.say(msgs.FAILED, error=network_display.error_text(e))
        if isinstance(e, OSError) and e.errno == errno.ESTALE:
            state.messages = M.load_replay_messages(turn_cfg.session_file)
            return
        if _turn_has_progress(state.messages, user_message):
            _persist_turn_messages(turn_cfg, state.messages)
        else:
            _discard_unstarted_turn(cfg, state.messages, user_message)
            _requeue_notes(state, notes)
        M.append_mark(cfg.session_file, f"error: {msgs.words_of(network_display.error_text(e))}")


def _steer_mode(state: ReplState) -> str:
    return setting_values.steer_mode(
        settings.knob(state.settings, "STEER_MODE")
    )


def _take_queued(queue: asyncio.Queue) -> list[str]:
    """Every line waiting in ``queue``, in order. Loop-thread only."""
    lines = []
    while True:
        try:
            lines.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return lines
        queue.task_done()


async def _turn_consumer(queue, sup, cfg, state, telemetry, prompt_spec, loop,
                         steer_inbox: list[str] | None = None) -> None:
    """Serialize main turns: one at a time, pulled FIFO from the input queue.
    This is what keeps state.messages single-writer while the input line
    stays live — the producer (`Repl.run`) never blocks on a turn.

    ``steer_inbox`` holds lines typed during a turn at STEER_MODE=now. The
    running turn takes them at its next tool boundary; what it never reached
    runs as one message right after it. At STEER_MODE=batch, the lines
    queued behind a turn run as one message.
    """
    steer_inbox = steer_inbox if steer_inbox is not None else []
    while True:
        line = await queue.get()
        try:
            if callable(line):
                # The agent's startup exchanges, queued ahead of the first typed
                # line: a turn like any other, so ^C cancels it.
                job = sup.spawn(line(), kind="turn", label="startup")
                with contextlib.suppress(asyncio.CancelledError):
                    await job.task
                lines = []
            else:
                lines = [line]
            if lines and _steer_mode(state) == "batch":
                pending = [line, *_take_queued(queue)]
                lines = []
                for item in pending:
                    if isinstance(item, str) and lines and isinstance(lines[-1], str):
                        lines[-1] += "\n" + item
                    else:
                        lines.append(item)
            for item in lines:
                await _run_repl_turn(item, sup, cfg, state, telemetry, prompt_spec, loop, steer_inbox)
            # The startup exchanges take no steered lines: what was typed
            # while they ran is the next message, like a turn's leftovers.
            while steer_inbox:
                leftover = "\n".join(steer_inbox)
                steer_inbox.clear()
                await _run_repl_turn(leftover, sup, cfg, state, telemetry, prompt_spec, loop, steer_inbox)
        finally:
            queue.task_done()


async def _run_repl_turn(line, sup, cfg, state, telemetry, prompt_spec, loop, steer_inbox: list[str]) -> None:
    """Run ``line`` as one supervised turn and wait for it to end. An error
    that escapes the turn is shown and ends it, and the consumer takes the
    next line; a snippet failure ends the REPL."""
    try:
        await _supervised_turn(line, sup, cfg, state, telemetry, prompt_spec, loop, steer_inbox)
    except snippets.SnippetError:
        raise
    except Exception as e:  # noqa: BLE001
        msgs.say(msgs.FAILED, error=network_display.error_text(e))


async def _supervised_turn(line, sup, cfg, state, telemetry, prompt_spec, loop, steer_inbox: list[str]) -> None:
    """Build ``line``'s user message, start its turn as a supervised job and
    wait for the job to end."""
    external_message = line if isinstance(line, dict) else None
    prompt_text, line_attachments = (
        (external_message["content"], []) if external_message is not None
        else attach.split_repl_attachments(line)
    )
    repl_state.emit_repl_event(
        state, telemetry, "input", text=prompt_text, attachments=line_attachments
    )
    _sync_telemetry_from_live_settings(cfg, state, telemetry)
    try:
        if external_message is None:
            prompt_text = _expand_skill_line(prompt_text, state)
        turn_cfg = await loop.run_in_executor(None, _live_cfg_snapshot, cfg, state)
        user_message = external_message or attach.build_user_message(
            prompt_text, line_attachments, turn_cfg, missing_ok=True)
    except snippets.SnippetError:
        raise
    except ValueError as e:
        # AttachmentError / SkillInvocationError (ValueErrors) or a login-gate
        # routing error from re-resolving the live model: degrade to one friendly
        # line, keep the REPL.
        msgs.say(msgs.FAILED, error=e)
        return
    user_message = conversation.note_mode_switch(cfg, user_message, "repl")
    user_message = conversation.note_model_switch(turn_cfg, user_message)
    notes = _take_pending_notes(state)
    user_message = _with_notes(user_message, notes)
    state.messages[:] = M.balance_orphaned_tool_calls(state.messages)
    state.messages.append(user_message)
    conversation.append_turn(cfg, user_message)
    # Logged as the turn starts, not as the line was typed: a line queued
    # behind a running turn joins the transcript where the model sees it.
    if (sink := transcript_mod.transcript_sink(telemetry)) is not None:
        sink.emit(output.event("transcript_user", text=prompt_text if external_message else line))

    async def take_steer() -> dict | None:
        # The inbox is loop-owned: it is read and cleared here, on the loop.
        # Skill expansion, attachment reads and encoding run in the executor.
        # Settings that an input hook changes apply from the next turn, through
        # _do_turn's post-turn delta sync.
        if not steer_inbox:
            return None
        text = "\n".join(steer_inbox)
        steer_inbox.clear()
        steer_text, steer_attachments = attach.split_repl_attachments(text)
        repl_state.emit_repl_event(state, telemetry, "input", text=steer_text, attachments=steer_attachments)
        try:
            built = await loop.run_in_executor(
                None,
                lambda: attach.build_user_message(
                    _expand_skill_line(steer_text, state), steer_attachments, turn_cfg,
                    missing_ok=True,
                ),
            )
        except snippets.SnippetError:
            raise
        except ValueError as e:
            msgs.say(msgs.FAILED, error=e)
            return None
        if (sink := transcript_mod.transcript_sink(telemetry)) is not None:
            sink.emit(output.event("transcript_user", text=text))
        return {**built, "steered": True}

    job = sup.spawn(
        _do_turn(cfg, state, telemetry, prompt_spec, user_message, turn_cfg, loop,
                 steer=take_steer, notes=notes),
        kind="turn",
        label=prompt_text[:40],
    )
    with contextlib.suppress(asyncio.CancelledError):
        await job.task  # _do_turn persists partial work on cancel; keep looping


def _expand_skill_line(prompt_text: str, state: ReplState) -> str:
    """Expand the assembly's skill invocation, or a discovered markdown command."""
    assembly = repl_state.repl_assembly(state)
    match = skills.match_user_invocation(prompt_text, command=assembly.skill_command, pattern=assembly.skill_pattern)
    if match is None:
        words = prompt_text.split(maxsplit=1)
        if not words or not words[0].startswith("/"):
            return prompt_text
        expanded = prompt_commands.expand(prompt_text, prompt_commands.discover(Path.cwd()))
        return prompt_text if expanded is None else expanded
    catalog = skills.discover_skills(Path.cwd(), disabled=repl_state.disabled_skills(state))
    expanded = skills.expand_user_invocation(catalog, prompt_text, command=assembly.skill_command,
                                            pattern=assembly.skill_pattern)
    return prompt_text if expanded is None else expanded


def _repl_input_bindings(settings_store):
    return [clipimage.key_bindings(settings_store), pastes.key_bindings(settings_store)]


def _interrupt_turn_and_queue(sup, drop_pending) -> bool:
    active = sup.turn_active()
    if active:
        n = sup.cancel_kind("turn")
        msgs.say(msgs.CANCELLING, jobs=msgs.plural(n, "turn"))
    flushed = drop_pending()
    if flushed:
        msgs.say(msgs.DROPPED_QUEUED, prompts=msgs.plural(flushed, "queued prompt"))
    return bool(active or flushed)


def _echoes(line: str, state: ReplState) -> bool:
    """Whether a typed line shows in the scrollback: not when dispatch
    (`repl_commands.handle_command`) takes it as a command, a command or alias from the
    table or an invalid one; yes for what goes to the model, `/skill`, a
    markdown command and a slash-led message (`/usr/bin/x is broken`)."""
    if not line.startswith("/") or repl_commands.is_skill_invocation(line, state):
        return True
    verb = line[1:].split(maxsplit=1)[0].lower() if line[1:].strip() else ""
    if verb in repl_commands.commands(state) or verb in state.aliases:
        return False
    return not repl_commands.is_unknown_command(line)


def _is_turn_state_command(line: str, state: ReplState | None = None) -> bool:
    """Commands that clear/rotate/compact the live message list. Running one while
    a turn appends to state.messages races the single-writer discipline the turn
    queue enforces (empty the list mid-turn -> IndexError, dropped output, spurious
    rollback marks) — refuse until the active turn ends."""
    parsed = jsrc_line.split_command(line) if line.startswith("/") else None
    table = repl_commands.commands(state)
    return (repl_state.repl_assembly(state).guard_turn_state and parsed is not None
            and parsed[0] in table and table[parsed[0]].turn_state)


def _drain_queue(queue: asyncio.Queue) -> int:
    """Drop every pending input line, balancing task_done() so the teardown
    queue.join() still completes. Loop-thread only — asyncio.Queue is not
    thread-safe, so this never runs from the repl_commands.handle_command executor hop."""
    return len(_take_queued(queue))


def _live_level(state: ReplState, key: str) -> int:
    """A 0-3 display level from live settings; its js/jsrc value when bad."""
    default = settings.default_value(key)
    value = settings.knob(state.settings, key)
    return value if isinstance(value, int) and value in range(4) else default


def _status_bar_line(cfg: Config, state: ReplState, turn_active: bool, width: int) -> str:
    """The REPL status bar from live state: what is running, on what, how full.
    In a subagent's window it is that subagent's: its model, agent, session
    and turn."""
    context = runtime.T.STOCK_CONTEXT
    provider = state.provider_id or cfg.provider_id
    model = state.model or cfg.model
    agent_id, session_file = cfg.agent_id, cfg.session_file
    windows = state.windows
    window = activity = None
    if windows is not None:
        if len(windows.windows.numbers()) > 1:
            window = windows.windows.current
        activity = windows.windows.activity()
        shown = windows.on_screen()
        if shown is not None:
            launch = shown.launch
            provider, model, agent_id = launch["provider"], launch["model"], launch["agent"]
            session_file = Path(launch["session_file"]) if launch["session_file"] else None
            context = shown.context or context
            turn_active = shown.busy()
    if provider and isinstance(model, str) and model.startswith(f"{provider}/"):
        model = model[len(provider) + 1:]
    prompt_tokens = int(getattr(context, "last_prompt_tokens", 0) or 0)
    cached_tokens = int(getattr(context, "last_cached_tokens", 0) or 0)
    now = time.monotonic()
    phase, output_tokens, throbber = "", None, ""
    if turn_active:
        throbber = status_bar.throbber_frame(now)
        phase, output_tokens = status_bar.turn_centre(context.turn_status, now=now)
    return status_bar.status_line(
        width,
        clock=time.strftime("%H:%M"),
        provider=provider,
        model=model,
        context_tokens=int(getattr(context, "context_tokens", 0) or 0),
        phase=phase,
        output_tokens=output_tokens,
        throbber=throbber,
        agent_id=agent_id,
        session_short=status_bar.session_short(session_file) if session_file is not None else None,
        cache_pct=int(100 * cached_tokens / prompt_tokens) if prompt_tokens else None,
        cost=usage_display.status_text(usage_mod.totals(session_file)) if session_file is not None else None,
        window=window,
        activity=activity,
    )


def _show_resume(state: ReplState, telemetry: runtime.Telemetry, width: int) -> None:
    """Draw the last RESUME_EXCHANGES exchanges of the session the way a turn
    draws them: its text printed, its tool calls through the turn's tool trace,
    so a resumed row folds, carries its whole form and unfolds as a live one
    does. ``telemetry``'s exchange factory is the screen's, when there is one."""
    live = state.settings
    count = settings.knob(live, "RESUME_EXCHANGES")
    replay = runtime.Telemetry(None)
    replay.exchange_factory = getattr(telemetry, "exchange_factory", None)
    context = SimpleNamespace(config=SimpleNamespace(settings=live))
    for kind, part in resume_view.resume_parts(state.messages, count if isinstance(count, int) else 0,
                                               width=width, markdown=display_mod.markdown_enabled(live)):
        if kind == "text":
            console.settle_tool_trace(replay)
            print(msgs.for_stream(part), end="", flush=True)
            continue
        name, args, result = part
        if result is None:
            # A call the session holds no result for is drawn as it was left, running.
            console.settle_tool_trace(replay)
            print(msgs.for_stream(tool_display.render_exchange(
                name, args, None, level=tool_display.tools_level(live), preview=tool_display.preview_lines(live),
                settings=live)), end="")
            continue
        opened = console.trace_call(replay, context, name, args)
        console.trace_result(replay, context, name, result, args=args, exchange=opened)
    console.settle_tool_trace(replay)


async def _initialize_repl_agent(cfg, state, telemetry, prompt_spec) -> None:
    """Run the agent's startup exchanges as the REPL draws a turn: each fired
    user line, then its reply as it streams."""
    if not prompt_spec.exchanges or state.messages:
        return
    turn_cfg = state.config_for_turn(cfg)
    try:
        await agent_messages.initialize(
            turn_cfg, prompt_spec, state.messages, telemetry, system=state.system,
            tool_registry=state.tool_registry,
            sampling=login_defaults.sampling(turn_cfg),
            event_hooks=state.events, mcp_host=state.mcp_host,
            echo_user=display_mod.echo_user,
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        msgs.warn(msgs.TURN_INTERRUPTED)
    except snippets.SnippetError:
        raise
    except Exception as exc:  # noqa: BLE001
        msgs.warn(msgs.FAILED, error=network_display.error_text(exc))


@dataclass(eq=False)
class Repl:
    """One REPL session as `start_repl` settles it: the Config, the live
    state, the agent's prompt spec, the input history and the startup banner.
    It builds the telemetry its turns report to, the completer and the
    status line; `run` runs the session over a screen."""

    cfg: Config
    state: ReplState
    prompt_spec: P.PromptSpec
    history: prompt_history.PromptHistory | None = None
    banner: str = ""
    telemetry: runtime.Telemetry = field(init=False)
    completer: replcomplete.JsCompleter = field(init=False)
    _sup: supervisor.Supervisor | None = field(init=False, default=None)
    _queue: asyncio.Queue | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.telemetry = runtime.Telemetry(debug_log=self.cfg.debug_log)
        # Attach the debug autolog sink before the first turn.
        _sync_telemetry_from_live_settings(self.cfg, self.state, self.telemetry)
        self.completer = replcomplete.JsCompleter(
            commands=lambda: _command_completions(self.state),
            setting_keys=[spec.key for spec in settings.REGISTRY],
            fset_names=fset.names(),
            names=lambda: sorted(set(providers.known_provider_ids()) | set(logins.load_logins())),
            spell=replcomplete.hunspell_suggest,
            bare_words=repl_state.QUIT_WORDS,
        )

    def status(self, width: int) -> str:
        """The status bar, `width` columns wide."""
        turn_active = self._sup is not None and self._sup.turn_active()
        return _status_bar_line(self.cfg, self.state, turn_active, width)

    async def idle(self) -> None:
        """Return once every line the running session has taken has had its
        turn end."""
        if self._queue is not None:
            await self._queue.join()

    async def run(self, build_screen=None) -> ReplState:
        """Run the session on the running event loop until its screen
        closes, and return its state. ``build_screen`` takes the keyword
        arguments of `screen.build_app`, which it defaults to, and returns
        the app that reads the input and the scrollback it shows. A snippet
        failure ends the session and is raised from here.

        Input, the active turn, and subagents all share the one event loop.
        The screen has three regions (scrollback, status, input); turn output
        lands in the scrollback and never touches the input line. ^C cancels
        the active turn instead of killing the process; a line typed during a
        turn steers it or queues behind it, per STEER_MODE."""
        cfg, state, telemetry, prompt_spec = self.cfg, self.state, self.telemetry, self.prompt_spec
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=32, thread_name_prefix="js-dispatch"))
        sup = self._sup = supervisor.Supervisor(loop)
        supervisor.set_current(sup)
        queue: asyncio.Queue = asyncio.Queue()
        self._queue = queue
        steer_inbox: list[str] = []
        mcp_host = state.mcp_host
        # Each subagent a turn launches gets a window; set before the consumer
        # starts, so every turn it runs inherits it.
        windows = screen.Windows()
        child_windows = subagent_windows.SubagentWindows(windows, sup)
        state.windows = child_windows
        opener_token = subagents.window_opener.set(child_windows.open)

        channel_instructed: set[str] = set()

        def channel_message(server: str, params: dict) -> None:
            content = params.get("content")
            if not isinstance(content, str) or not content.strip() or not state.running:
                return
            text = f"External MCP channel {json.dumps(server)}\n"
            if server not in channel_instructed:
                # The server's rules for its channel ride the first push from it.
                channel_instructed.add(server)
                instructions = mcp_host.server_instructions(server=server).get(server)
                if instructions:
                    text += f"Server instructions: {instructions}\n"
            if isinstance(params.get("meta"), dict):
                text += f"Metadata: {json.dumps(params['meta'], ensure_ascii=False)}\n"
            message = {"role": "user", "content": text + "\n" + content}
            queue.put_nowait(message)

        if mcp_host is not None:
            mcp_host.channel_sink = channel_message
        if prompt_spec.exchanges and not state.messages:
            # Runs inside the screen, as the first turn, so its output streams into
            # the scrollback instead of being drawn after the fact.
            queue.put_nowait(functools.partial(_initialize_repl_agent, cfg, state, telemetry, prompt_spec))
        consumer = loop.create_task(
            _turn_consumer(queue, sup, cfg, state, telemetry, prompt_spec, loop, steer_inbox)
        )

        def drop_pending() -> int:
            dropped = _drain_queue(queue) + len(steer_inbox)
            steer_inbox.clear()
            return dropped

        async def _on_line(line: str) -> None:
            if not line:
                return
            shown = child_windows.on_screen()
            if shown is not None:
                # In a subagent's window a command runs as anywhere, its output
                # in that window; any other line is the subagent's.
                if _is_turn_state_command(line, state) and sup.turn_active():
                    with screen.writing(shown.scrollback):
                        msgs.say(repl_state.repl_hint(msgs.TURN_RUNNING, state), command=line.split()[0])
                    return
                command = contextvars.copy_context()
                command.run(screen.writing_to.set, shown.scrollback)
                handled = await loop.run_in_executor(None, command.run, repl_commands.handle_command, line, state, cfg)
                if handled:
                    _sync_telemetry_from_live_settings(cfg, state, telemetry)
                    if not state.running:
                        await close()
                    return
                shown.send(line)
                return
            if line in repl_state.repl_assembly(state).flush_lines:
                # Drop pending input without touching the active turn. Handled
                # here (not in repl_commands.handle_command) because the queue is loop-owned
                # and repl_commands.handle_command runs on an executor thread.
                flushed = drop_pending()
                msgs.say(msgs.DROPPED_QUEUED, prompts=msgs.plural(flushed, "queued prompt"))
                return
            if _is_turn_state_command(line, state) and sup.turn_active():
                msgs.say(repl_state.repl_hint(msgs.TURN_RUNNING, state), command=line.split()[0])
                return
            handled = await loop.run_in_executor(None, repl_commands.handle_command, line, state, cfg)
            if handled:
                _sync_telemetry_from_live_settings(cfg, state, telemetry)
                if not state.running:
                    await close()
                return
            if sup.turn_active() and _steer_mode(state) == "now":
                # The turn writes it to the transcript where the model receives it.
                steer_inbox.append(line)
                return
            queue.put_nowait(line)
            if sup.turn_active() or queue.qsize() > 1:
                msgs.say(msgs.QUEUED, ahead=queue.qsize())

        snippet_failures: list[snippets.SnippetError] = []

        def fail_snippet(error: snippets.SnippetError) -> None:
            snippet_failures.append(error)
            state.running = False
            app.exit()

        async def on_line(line: str) -> None:
            try:
                await _on_line(line)
            except snippets.SnippetError as exc:
                fail_snippet(exc)

        def consumer_done(task: asyncio.Task) -> None:
            if not task.cancelled() and isinstance(error := task.exception(), snippets.SnippetError):
                fail_snippet(error)

        consumer.add_done_callback(consumer_done)

        def on_interrupt() -> bool:
            shown = child_windows.on_screen()
            if shown is not None:
                # In a subagent's window the key stops what runs there.
                return shown.cancel()
            handler = repl_state.repl_assembly(state).interrupt or _interrupt_turn_and_queue
            return handler(sup, drop_pending)

        async def wind_down() -> None:
            # Cancels the running turn, and any turn the consumer starts from a
            # line it took before the queue emptied, and waits for them to end.
            drop_pending()
            joined = loop.create_task(queue.join())
            while not joined.done() and not consumer.done():
                sup.cancel_kind("turn")
                await asyncio.wait({joined}, timeout=0.05)
            joined.cancel()

        closing: list[asyncio.Future] = []

        async def close_screen() -> None:
            on_interrupt()
            await wind_down()
            app.exit()

        def close() -> asyncio.Future:
            # exit and EOF cancel a running turn as ^C does. The screen closes
            # once it has ended, so everything the turn prints lands in the
            # scrollback.
            if not closing:
                closing.append(asyncio.ensure_future(close_screen()))
            return closing[0]

        def on_eof() -> asyncio.Future:
            state.running = False
            return close()

        async def on_ex(line: str, editor: screen.InputEditor) -> None:
            await exline.run_ex(
                line,
                editor,
                is_command=lambda verb: verb in repl_commands.commands(state) or verb in state.aliases,
                dispatch=on_line,
            )

        app, scrollback = (build_screen or screen.build_app)(
            prompt=display_mod.prompt,
            history=self.history,
            completer=self.completer,
            on_line=on_line,
            on_interrupt=on_interrupt,
            on_eof=on_eof,
            turn_active=sup.turn_active,
            status=self.status,
            editing_mode=lambda: settings.knob(state.settings, "EDITING_MODE"),
            echo=lambda line: _echoes(line, state),
            on_ex=on_ex,
            key_bindings=merge_key_bindings([
                *(repl_state.repl_assembly(state).input_bindings or _repl_input_bindings)(lambda: state.settings),
            ]),
            keymap=state.keymap,
            windows=windows,
        )
        windows.set_main(scrollback)
        child_windows.attach(app)
        previous_reasoning_factory = telemetry.reasoning_factory
        previous_display_factory = telemetry.display_factory
        previous_exchange_factory = telemetry.exchange_factory
        telemetry.reasoning_factory = lambda level: screen.ScreenReasoningDisplay(loop, scrollback, app, level)
        telemetry.display_factory = lambda markdown: display_mod.Display(
            getattr(sys.stdout, "write_unlogged", sys.stdout.write),
            live=screen.ScreenLive(loop, scrollback, app) if markdown else None,
            flush=sys.stdout.flush,
        )
        telemetry.exchange_factory = lambda: screen.ScreenExchange(loop, scrollback, app)
        ticker = loop.create_task(status_bar.tick(app, lambda: sup.turn_active() or bool(sup.jobs("subagent"))))
        try:
            with screen.capture_stdio(loop, scrollback, app), transcript_mod.transcript_stdio(telemetry):
                try:
                    network_display.install_sink(network_display.NetSink(
                        level=lambda: _live_level(state, "NET_DISPLAY"),
                        emit=lambda line: print(line, flush=True),
                    ))
                    if self.banner:
                        print(self.banner)
                    repl_state.emit_session_event(state, telemetry, cfg, "session_start")
                    _show_resume(state, telemetry, screen.ScreenLive(loop, scrollback, app).width())
                    await app.run_async()
                finally:
                    # Whatever still runs ends while stdout is the scrollback, so
                    # nothing reaches the terminal after the screen has closed.
                    await wind_down()
                    for task in closing:
                        with contextlib.suppress(Exception):
                            await task
        finally:
            if mcp_host is not None:
                mcp_host.channel_sink = None
            network_display.install_sink(None)
            ticker.cancel()
            telemetry.reasoning_factory = previous_reasoning_factory
            telemetry.display_factory = previous_display_factory
            telemetry.exchange_factory = previous_exchange_factory
            supervisor.set_current(None)
            subagents.window_opener.reset(opener_token)
            state.windows = None
            consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError, snippets.SnippetError):
                await consumer
            for job in sup.jobs():  # backstop: cancel any straggler
                job.task.cancel()
            repl_state.emit_session_event(state, telemetry, cfg, "session_end")
            await _close_session_mcp_host(state)
        if snippet_failures:
            raise snippet_failures[0]
        return state


def start_repl(args: argparse.Namespace, sources: InvocationSources, agent: str | None) -> int:
    """The REPL: resume the session on its stamped model and its recorded
    system prompt, seed the live settings from the flags, run the jsrc, then
    the loop. Its exit code."""
    os.environ["JS_MODE"] = "repl"
    session_catalog.session_leases.start["mode"] = "repl"
    try:
        cfg = run_assembly.from_env(args.session, save_session=not args.no_save, sources=sources,
                        agent_id=agent)
    except ValueError as e:
        msgs.warn(msgs.FAILED, error=e)
        return 2

    # Startup notices go out with the banner, which the screen shows before
    # its prompt.
    notices: list[str] = []
    # Resuming with no --model comes back on the model of the session's last
    # stamp, not the config default; with no --reasoning, on its reasoning
    # level. An explicit flag still wins. A stamp that matches what config
    # already resolved is left alone: applying it as an explicit override would
    # put an otherwise fine launch through the login gate for a model it was
    # going to use anyway.
    resumed = run_assembly.resume(cfg, args.model, args.reasoning)
    if resumed.from_stamp:
        args.model = resumed.model
        notices.append(msgs.line_for(msgs.RESUMED_MODEL, model=resumed.model))
    # A session sends the system prompt it was born with, byte for byte, for its
    # whole life. Rebuilding it per launch puts a fresh clock and load average in
    # front of an append-only history and breaks the shared prefix at byte zero.
    # A change to the prompt files on disk is reported to the model as a message
    # at the end instead. The change is judged on the files before directive
    # expansion, since expanded output such as a clock differs on every launch.
    # The live state resolves the agent and login layers every turn.
    try:
        settled = run_assembly.settle(cfg, resumed=resumed, config_pins=True, layered=False,
                                      expand_recorded=True)
    except run_assembly.RunNotSettled as e:
        msgs.warn(msgs.FAILED, error=P.format_prompt_load_error(cfg, e.cause) if e.loading else e.cause)
        return 2
    cfg, prompt_spec, system = settled.config, settled.prompt_spec, settled.system
    recorded_system = settled.recorded_system
    # Compared with what the resumed model and agent resolve to, not the
    # startup model.
    resumed_reasoning = resumed.layer.get("EFFORT")
    if resumed_reasoning is not None and resumed_reasoning == login_defaults.for_turn(
            login_defaults.with_agent(cfg, prompt_spec)).reasoning_effort:
        resumed_reasoning = None
    if resumed.unreachable is not None:
        msgs.say(msgs.RESUME_MODEL_FALLBACK, stamped=resumed.unreachable,
                 model=conversation.stamp_label(cfg.model, cfg.provider_id))

    keymap, key_errors = keys_mod.load(keys_mod.keys_file(cfg.settings))
    history = _repl_history(cfg)

    messages = M.load_replay_messages(cfg.session_file)
    cut_off = M.turn_cut_off(messages)
    repl_workspace_commands.restore_workspace(cfg)
    if messages:
        notices.append(msgs.line_for(msgs.RESUMED, messages=msgs.plural(len(messages), "prior message")))
    elif args.session is not None:
        # Asked for a specific session and got nothing. Silence here reads as a
        # successful resume, so an empty one has to say so.
        notices.append(msgs.line_for(msgs.EMPTY_SESSION, path=cfg.session_file))
    if args.no_save:
        # -n with --session reads the named session and writes nothing back.
        cfg = replace(cfg, session_file=Path(os.devnull))
    session_catalog.activate_saved_session(cfg, caller_key=args.session_key, model=args.model)
    M.append_mark(cfg.session_file, "session_start")
    prompt_changed = M.record_prompt_seen(cfg.session_file, prompt_spec.source)
    if recorded_system is None:
        M.append_system_prompt(cfg.session_file, system)
    elif prompt_changed:
        # The prompt on disk differs from the one the previous launch saw. The
        # recorded one is still what gets sent; the model is told once, at the
        # launch that first sees the change.
        notice = {"role": "user", "content": snippets.render('prompt-changed')}
        messages.append(notice)
        conversation.append_turn(cfg, notice)
        notices.append(msgs.line_for(msgs.PROMPT_CHANGED))

    live_settings = copy.deepcopy(cfg.settings) if isinstance(cfg.settings, dict) else {}
    if args.reasoning is not None:
        reasoning_seed, effort_error = setting_values.validate_cli_reasoning(args.reasoning)
        if effort_error is not None:
            msgs.warn(msgs.BAD_REASONING, value=args.reasoning, error=effort_error)
            return 2
        settings.set_dotted(live_settings, ("EFFORT",), reasoning_seed)
    elif resumed_reasoning is not None:
        settings.set_dotted(live_settings, ("EFFORT",), resumed_reasoning)
    if args.max_out is not None:
        settings.set_dotted(live_settings, ("OUTPUT_TOKENS_MAX",), args.max_out)
    elif (prompt_spec.max_output_tokens is not None
          and settings.get_dotted(live_settings, ("OUTPUT_TOKENS_MAX",), None) is None):
        # Agent default from agent.yaml — seed the per-turn source of truth so
        # it survives the per-turn rebuild (ReplState.config_for_turn). Config/env/--max-out win.
        settings.set_dotted(live_settings, ("OUTPUT_TOKENS_MAX",), prompt_spec.max_output_tokens)
    state = live_state(
        cfg, prompt_spec,
        messages=messages,
        system=system,
        model=args.model,
        live_settings=live_settings,
        tool_registry=settled.registry,
    )
    state.keymap = keymap
    # A resumed session's reasoning is this session's own choice.
    state.live_resume = resumed.layer
    if cut_off:
        repl_state.queue_note(state, snippets.render('turn-cut-off'))
    rc_errors = repl_commands.run_rc_commands(state, cfg, jsrc_paths(
        Path(getattr(cfg, "project_dir", None) or Path.cwd()), sources))
    repl_settings_commands.mark_saved(state)
    session = Repl(cfg, state, prompt_spec, history,
                   _startup_banner(cfg, state, notices, [*rc_errors, *key_errors]))
    state.mcp_host = _session_mcp_host(cfg, session.telemetry)
    transcript_stack = contextlib.ExitStack()
    # prompt_toolkit creates its output from sys.stdout on first use. The
    # screen and the pickers draw on the terminal, so that first use is here,
    # before stdout becomes the transcript tee.
    get_app_session().output
    transcript_mod.enter_transcript_stdio(transcript_stack, session.telemetry)
    try:
        owning_loop.run_owning_loop(session.run())
    finally:
        transcript_stack.close()
    print_resume_hint(cfg, state)
    _queue_switch(state)
    return 0
