"""The interactive REPL: startup, the loop that reads lines while a turn
runs, the turns it runs one at a time, steering, the status bar, and the
`/session` switch that replaces the process once the screen has closed.
Slash commands live in `js.repl_commands`, the live state in `js.repl_state`."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import errno
import os
import shlex
import sys
from pathlib import Path

from . import (
    attach,
    command_line,
    conversation,
    debug_trace,
    events,
    fset,
    login_defaults,
    memory as M,
    messages as msgs,
    motd,
    network_display,
    output,
    paths as _paths,
    persona as P,
    prompt_commands,
    prompt_history,
    repl_commands,
    repl_state,
    run_assembly,
    runtime,
    session_picker,
    session_store,
    setcmd,
    settings,
    skills,
    snippets,
    transcript as transcript_mod,
)
from .config import Config
from .repl_state import ReplState
from .toolkit import task_jobs


def _print_resume_hint(cfg: Config, state: ReplState) -> None:
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
            parsed = setcmd.split_command(hook.handler)
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


def _repl_state(cfg, prompt_spec, *, messages: list[dict] | None = None,
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
    return settings.steer_mode(
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
    stays live — the producer (`_repl_main`) never blocks on a turn.

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
    """Run ``line`` as one supervised turn and wait for it to end."""
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
