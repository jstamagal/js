"""A `-p` run: one prompt, or the agent's startup exchanges, run to an
answer with no REPL, printed on stdout or written as JSON events
(`js.headless`), and the Continue line that resumes its session."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import shlex
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import (
    agent_messages,
    attach,
    command_line,
    context_window,
    conversation,
    debug_trace,
    display as display_mod,
    events,
    headless,
    login_defaults,
    memory as M,
    messages as msgs,
    network_display,
    output,
    owning_loop,
    paths as _paths,
    persona as P,
    repl_state,
    repl_workspace_commands,
    routing,
    run_assembly,
    runtime,
    session_catalog,
    session_store,
    setting_values,
    settings,
    snippets,
    standalone_hooks,
    stats,
    transcript as transcript_mod,
    usage as usage_mod,
)
from .cli_options import current
from .config import Config, InvocationSources, jsrc_paths, vision_enabled_for_model
from .conversation import Conversation
from .repl_state import ReplState
from .toolkit import ToolContext, task_jobs


def _mute_transcript_tee(sink):
    mute = getattr(sink, "mute_tee", None)
    return mute() if callable(mute) else contextlib.nullcontext()


def _run_state(cfg: Config, system: str, messages: list[dict],
               hooks: events.EventHooks | None = None) -> ReplState:
    """The live state a run outside the REPL compacts between turns with: its
    own system prompt, messages, route, settings and `on` handlers."""
    return ReplState(
        system=system,
        messages=messages,
        model=cfg.model,
        provider_id=cfg.provider_id,
        provider_base_url=cfg.provider_base_url,
        provider_api_key=cfg.provider_api_key,
        provider_headers=dict(getattr(cfg, "provider_headers", {}) or {}),
        settings=cfg.settings,
        events=hooks,
    )


def _raise_keyboard_interrupt(*_args) -> None:
    raise KeyboardInterrupt


@dataclass(frozen=True)
class PromptInputs:
    """What one `-p` run is asked to do: the keywords of `run`. A
    ``prompt`` of None runs the agent's startup exchanges as the whole run:
    the last one is the turn whose answer is printed."""

    prompt: str | None
    model: str | None = None
    debug: bool = False
    debug_file: str | None = None
    agent: str | None = None
    session: str | None = None
    save: bool = True
    # The words the Continue line starts with in place of `js`.
    resume_prefix: str | None = None
    reasoning: str | None = None
    maxout: int | None = None
    show_continue: bool = True
    sources: InvocationSources = InvocationSources()
    tool_context: ToolContext | None = None
    files: list[str] | None = None
    stdin_attachment: bytes | None = None
    stats_json: str | None = None
    stats_csv: str | None = None
    caller_key: str | None = None
    events: headless.JsonEvents | None = None
    config_pins: bool = False


@dataclass
class _PreparedRun:
    """A `-p` run as its steps prepare it. ``cfg`` and ``effort`` are the ones
    the command line names until `_settle` replaces them with the settled
    ones; under -n `_load_history` points ``cfg`` at os.devnull."""

    inputs: PromptInputs
    # The prompt with its @path words taken out, and what it attaches.
    prompt: str | None
    attachments: list[str]
    effort: str | None
    resumed: run_assembly.Resume
    cfg: Config
    telemetry: runtime.Telemetry
    settled: run_assembly.SettledRun | None = None
    messages: list[dict] = field(default_factory=list)
    user_message: dict | None = None
    state: ReplState | None = None
    call_stats: list[dict] = field(default_factory=list)
    started_at: float = 0.0


@session_catalog.session_scope
def run(prompt: str | None, **kwargs) -> int:
    """One `-p` run (`_prompt_run`). Once its session is open, the Continue
    line prints on every exit: an answer, no answer, an error, ^C; once its
    turns start, session_end fires before it."""
    inputs = PromptInputs(prompt, **kwargs)
    opened: dict = {}
    try:
        code = _prompt_run(inputs, opened)
        events = inputs.events
        if events is not None and code != 0 and not events.errored:
            headless.emit_failure(events, getattr(sys.stderr, "last_line", ""), code)
        return code
    finally:
        if (end := opened.get("end")) is not None:
            end()
        if (say_continue := opened.get("say")) is not None:
            say_continue()


def _prompt_run(inputs: PromptInputs, opened: dict) -> int:
    """The steps of a `-p` run, in order; the first to give an exit code ends
    it. ``opened`` gets the session_end and Continue line calls."""
    asked = _asked(inputs)
    if isinstance(asked, int):
        return asked
    prepared = _open_session(inputs, *asked, opened)
    if isinstance(prepared, int):
        return prepared
    if (code := _settle(prepared)) is not None:
        return code
    cut_off = _load_history(prepared)
    _emit_start(prepared)
    if (code := _build_user_message(prepared, cut_off)) is not None:
        return code
    _open_live_state(prepared)
    if (code := _run_turns(prepared, opened)) is not None:
        return code
    return _finish(prepared)


def _asked(inputs: PromptInputs) -> tuple[str | None, list[str], str | None] | int:
    """The prompt with its @path words taken out, the files it attaches, and
    the --reasoning effort checked. An exit code when there is nothing to
    send or the effort is not one."""
    # @path in the prompt attaches like it does in the REPL, read here before
    # any jail, so `-p "@/tmp/prompt.txt"` needs no JAIL_BIND.
    prompt, attachments = inputs.prompt, []
    if prompt is not None:
        prompt, named = attach.split_repl_attachments(prompt)
        attachments = [*(inputs.files or []), *named]
        if not prompt.strip() and not attachments:
            msgs.warn(msgs.PROMPT_EMPTY)
            return 2
    effort = None
    if inputs.reasoning is not None:
        effort, effort_error = setting_values.validate_cli_reasoning(inputs.reasoning)
        if effort_error is not None:
            msgs.warn(msgs.BAD_REASONING, value=inputs.reasoning, error=effort_error)
            return 2
    return prompt, attachments, effort


def _open_session(inputs: PromptInputs, prompt: str | None, attachments: list[str], effort: str | None,
                  opened: dict) -> _PreparedRun | int:
    """The run's config and what its session's last stamp resumes, the saved
    session made the run's own, the telemetry and transcript, and the
    Continue line the run prints when it ends. An exit code when the config
    does not load."""
    try:
        cfg = run_assembly.from_env(inputs.session, save_session=inputs.save, sources=inputs.sources,
                                    agent_id=inputs.agent)
    except ValueError as e:
        msgs.warn(msgs.FAILED, error=e)
        return 2
    # A resumed session continues on the model, provider and reasoning of its
    # last stamp unless this run names them. Read before this start records its own.
    resumed = run_assembly.resume(cfg, inputs.model, effort)
    if inputs.save:
        session_catalog.activate_saved_session(
            cfg,
            caller_key=inputs.caller_key,
            announce_generated=inputs.session is None and os.environ.get("JS_SESSION") is None,
            model=resumed.model,
        )
    telemetry = runtime.Telemetry(debug_log=cfg.debug_log)
    transcript_mod.sync_transcript_sink(cfg, getattr(cfg, "settings", {}) or {}, telemetry)
    prepared = _PreparedRun(inputs, prompt, attachments, effort, resumed, cfg, telemetry)
    if inputs.save and inputs.show_continue:
        hint = session_store.session_hint_arg(cfg)
        # The config and effort are read when the line prints: the settled ones by then.
        opened["say"] = lambda: say_continue(telemetry, hint, resume_prefix=inputs.resume_prefix,
                                             agent=inputs.agent, model=resumed.model, cfg=prepared.cfg,
                                             reasoning=prepared.effort)
    return prepared


def _settle(prepared: _PreparedRun) -> int | None:
    """The run settled: its config, prompt, tools and effort. A resumed
    session stays on its stamp; agent.yaml sets a fresh run. An exit code
    when it does not settle."""
    inputs, resumed = prepared.inputs, prepared.resumed
    try:
        settled = run_assembly.settle(
            prepared.cfg, reasoning=prepared.effort, resumed=resumed, config_pins=inputs.config_pins,
            agent_reasoning=not inputs.config_pins and resumed.stamp is None and prepared.effort is None)
    except run_assembly.RunNotSettled as e:
        with transcript_mod.transcript_stdio(prepared.telemetry):
            msgs.warn(msgs.FAILED, error=P.format_prompt_load_error(prepared.cfg, e.cause) if e.loading else e.cause)
        return 2
    # A resumed session's reasoning is this session's own choice, like /set,
    # even when it is what the model would pick: its turns and the children
    # they start run on it.
    prepared.settled, prepared.cfg, prepared.effort = settled, settled.config, settled.effort
    if resumed.unreachable is not None:
        with transcript_mod.transcript_stdio(prepared.telemetry):
            msgs.warn(msgs.RESUME_MODEL_FALLBACK, stamped=resumed.unreachable,
                      model=conversation.stamp_label(prepared.cfg.model, prepared.cfg.provider_id))
    return None


def _load_history(prepared: _PreparedRun) -> bool:
    """The session's messages; a saved session records its system prompt when
    it has none, and a note when the agent's prompt changed since it did. The
    workspace the session last ran in is restored. Whether its last turn was
    cut off."""
    cfg, settled = prepared.cfg, prepared.settled
    prepared.messages = M.load_replay_messages(cfg.session_file)
    cut_off = M.turn_cut_off(prepared.messages)
    if prepared.inputs.save:
        prompt_changed = M.record_prompt_seen(cfg.session_file, settled.prompt_spec.source)
        if settled.recorded_system is None:
            M.append_system_prompt(cfg.session_file, settled.system)
        elif prompt_changed:
            notice = {"role": "user", "content": snippets.render('prompt-changed')}
            prepared.messages.append(notice)
            conversation.append_turn(cfg, notice)
    repl_workspace_commands.restore_workspace(cfg)
    if not prepared.inputs.save:
        prepared.cfg = replace(cfg, session_file=Path(os.devnull))
    return cut_off


def _emit_start(prepared: _PreparedRun) -> None:
    """The `start` record of a --json run."""
    events, cfg = prepared.inputs.events, prepared.cfg
    if events is None:
        return
    saved = cfg.session_file != Path(os.devnull)
    events.emit(
        "start", version=headless.SCHEMA_VERSION,
        id=session_store.session_hint_arg(cfg) if saved else None,
        file=str(cfg.session_file) if saved else None,
        resumed=len(prepared.messages) > 0, agent=cfg.agent_id, model=cfg.model, provider=cfg.provider_id,
        effort=prepared.effort if prepared.effort is not None else cfg.reasoning_effort,
        cwd=str(Path.cwd()), usage=usage_mod.totals(cfg.session_file).as_dict(),
    )


def _build_user_message(prepared: _PreparedRun, cut_off: bool) -> int | None:
    """The message the prompt sends: its attachments, and the notes on a
    switch of mode or model and on a turn cut off. With no prompt, a saved
    session records -p as its mode. An exit code when an attachment cannot be
    read."""
    inputs = prepared.inputs
    if prepared.prompt is None:
        if inputs.save and prepared.cfg.session_file != Path(os.devnull):
            M.record_turn_mode(prepared.cfg.session_file, "-p")
        return None
    settled_cfg, model = prepared.settled.config, prepared.resumed.model
    attachment_cfg = (
        replace(settled_cfg, model=model,
                vision_enabled=vision_enabled_for_model(model, getattr(settled_cfg, "settings", None)))
        if model is not None
        else settled_cfg
    )
    try:
        user_message = attach.build_user_message(
            prepared.prompt,
            prepared.attachments,
            attachment_cfg,
            stdin_attachment=inputs.stdin_attachment,
            room_bytes=context_window.attachment_room_bytes(
                attachment_cfg, prepared.settled.system, prepared.messages),
        )
    except attach.AttachmentError as e:
        with transcript_mod.transcript_stdio(prepared.telemetry):
            msgs.warn(msgs.FAILED, error=e)
        return 2
    if inputs.save:
        user_message = conversation.note_mode_switch(prepared.cfg, user_message, "-p")
    user_message = conversation.note_model_switch(settled_cfg, user_message)
    if cut_off:
        user_message = attach.with_note(user_message, snippets.render('turn-cut-off'))
    prepared.user_message = user_message
    return None


def _open_live_state(prepared: _PreparedRun) -> None:
    """The run's `on` handlers from its jsrc files, and the live state its
    events and auto-compaction read."""
    cfg = prepared.cfg
    with transcript_mod.transcript_stdio(prepared.telemetry):
        hooks = standalone_hooks.table(cfg, jsrc_paths(Path(getattr(cfg, "project_dir", None) or Path.cwd()),
                                              prepared.inputs.sources))
    prepared.state = _run_state(cfg, prepared.settled.system, prepared.messages, hooks)


def _run_turns(prepared: _PreparedRun, opened: dict) -> int | None:
    """The turns, with the debug trace open and session_start fired first;
    SIGTERM (a runner, `timeout`) ends them like ^C. The session is saved and
    the handler SIGTERM had put back however they end. An exit code when they
    fail or are interrupted."""
    inputs, cfg, telemetry, state = prepared.inputs, prepared.cfg, prepared.telemetry, prepared.state
    turn_kwargs = prepared.settled.turn_kwargs(call_stats=prepared.call_stats, event_hooks=state.events)
    if inputs.events is not None:
        turn_kwargs["event_sink"] = inputs.events.runtime_event
        telemetry.sink = output.Fanout(output.sink_of(telemetry), headless.OutputEvents(inputs.events))
    if inputs.maxout is not None:
        turn_kwargs["max_output_override"] = inputs.maxout
    prepared.started_at = time.time()
    trace_sink = _open_trace_sink(prepared)
    telemetry.trace_sink = trace_sink
    transcript = transcript_mod.transcript_sink(telemetry)
    chat = Conversation(cfg, prepared.settled.system, registry=prepared.settled.registry,
                        context=inputs.tool_context or runtime.T.STOCK_CONTEXT,
                        telemetry=telemetry, messages=prepared.messages)

    def finished_tasks() -> dict | None:
        # A background task that finished since the last tool boundary hands
        # the model its result there.
        notes = task_jobs.completion_notes(chat.context.task_owner, int(chat.context.max_tool_result_bytes or 0))
        return {"role": "user", "content": "\n\n".join(notes), "steered": True} if notes else None

    turn_kwargs.setdefault("steer", finished_tasks)
    # The run hands back what its tasks return before it ends (`_background_turns`).
    delivered_before = chat.context.delivers_task_results
    chat.context.delivers_task_results = settings.live_bool_setting(
        getattr(cfg, "settings", None) or {}, ("SUBAGENT_WAKE",), True)

    def turns() -> None:
        _turns(prepared, chat, transcript, turn_kwargs)

    previous_sigterm = None
    with contextlib.suppress(ValueError):  # not the main thread: no signal hook
        previous_sigterm = signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    repl_state.emit_session_event(state, telemetry, cfg, "session_start")
    opened["end"] = lambda: repl_state.emit_session_event(state, telemetry, cfg, "session_end")
    try:
        try:
            if inputs.debug:
                _turns_on_the_terminal(prepared, trace_sink, turns)
            else:
                _turns_captured(prepared, trace_sink, turns)
        except (KeyboardInterrupt, asyncio.CancelledError):
            repl_state.emit_repl_event(state, telemetry, "cancel", reason="cancelled")
            with transcript_mod.transcript_stdio(telemetry):
                msgs.warn(msgs.TURN_INTERRUPTED)
            return 130
        except Exception as e:  # noqa: BLE001
            with transcript_mod.transcript_stdio(telemetry):
                msgs.warn(msgs.FAILED, error=network_display.error_text(e))
            code = 2 if isinstance(e, snippets.SnippetError) else 1
            events = inputs.events
            if events is not None and not events.errored:
                headless.emit_failure(events, network_display.error_text(e), code)
            return code
    finally:
        chat.context.delivers_task_results = delivered_before
        try:
            if inputs.save:
                chat.save()
        finally:
            if trace_sink is not None:
                trace_sink.close()
            telemetry.trace_sink = None
            if previous_sigterm is not None:
                signal.signal(signal.SIGTERM, previous_sigterm)
    return None


def _open_trace_sink(prepared: _PreparedRun) -> debug_trace.DebugTraceSink | None:
    """The debug trace: the run's autolog and its --debug-file."""
    cfg = prepared.cfg
    autolog = debug_trace.debug_autolog_path(cfg, getattr(cfg, "settings", {}) or {})
    sink_paths: list[Path] = [p for p in (autolog,) if p is not None]
    if prepared.inputs.debug_file:
        sink_paths.append(Path(prepared.inputs.debug_file))
    return debug_trace.open_debug_trace_sink(sink_paths)


def _turns(prepared: _PreparedRun, chat: Conversation, transcript, turn_kwargs: dict) -> None:
    """The agent's startup exchanges when the session has no messages, then
    the prompt's turn."""
    inputs, cfg, telemetry = prepared.inputs, prepared.cfg, prepared.telemetry
    # -d shows the conversation as the REPL draws it: each user line, typed or
    # fired from the agent's files, ahead of the answer it got.
    echo_user = display_mod.echo_user if inputs.debug else None
    trace_override = inputs.debug or bool(inputs.debug_file) or cfg.trace
    prompt_spec = prepared.settled.prompt_spec
    if prompt_spec.exchanges and not prepared.messages:
        owning_loop.run_owning_loop(agent_messages.initialize(
            cfg, prompt_spec, prepared.messages, replace(telemetry, transcript_log=transcript),
            system=prepared.settled.system, save=inputs.save, trace_override=trace_override,
            tool_context=inputs.tool_context, echo_user=echo_user, **turn_kwargs,
        ))
    if prepared.user_message is None:
        return

    def started() -> None:
        """The prompt is on disk, in the transcript and echoed before the
        model is called."""
        if inputs.save:
            conversation.append_turn(cfg, prepared.user_message)
        if transcript is not None:
            transcript.emit(output.event("transcript_user", text=prepared.prompt))
        if echo_user is not None:
            echo_user(prepared.prompt)

    repl_state.emit_repl_event(prepared.state, telemetry, "input", text=prepared.prompt, attachments=prepared.attachments)
    # In this context as well as the turn's, so the check after the
    # turn reads the same window.
    context_window.install_context_window_overrides(cfg)
    owning_loop.run_owning_loop(chat.turn(
        prepared.user_message, started=started, trace_override=trace_override, **turn_kwargs))
    _background_turns(prepared, chat, transcript, echo_user, trace_override, turn_kwargs)


def _background_turns(prepared: _PreparedRun, chat: Conversation, transcript, echo_user,
                      trace_override: bool, turn_kwargs: dict) -> None:
    """With SUBAGENT_WAKE on, the run waits for each background task the
    agent left running and gives it the result in a turn of its own, until
    none is left; the last of those turns' answers is the run's."""
    cfg = prepared.cfg
    if not settings.live_bool_setting(getattr(cfg, "settings", None) or {}, ("SUBAGENT_WAKE",), True):
        return
    owner = chat.context.task_owner
    result_max = int(chat.context.max_tool_result_bytes or 0)
    while task_jobs.outstanding(owner):
        task_jobs.wait_any(owner)
        notes = task_jobs.completion_notes(owner, result_max)
        if not notes:
            continue
        message = {"role": "user", "content": snippets.render("background-task-wake").strip()}
        for note in notes:
            message = attach.with_note(message, note)

        def started(message: dict = message) -> None:
            if prepared.inputs.save:
                conversation.append_turn(cfg, message)
            if transcript is not None:
                transcript.emit(output.event("transcript_user", text=message["content"]))
            if echo_user is not None:
                echo_user(message["content"])

        owning_loop.run_owning_loop(chat.turn(
            message, started=started, trace_override=trace_override, **turn_kwargs))


def _turns_on_the_terminal(prepared: _PreparedRun, trace_sink, turns: Callable[[], None]) -> None:
    """-d: the concise trace and the streamed answer live on the terminal;
    the giant request trace goes to the log only. stdout is teed so the log
    also keeps the concise lines and the answer."""
    with transcript_mod.transcript_stdio(prepared.telemetry):
        stdout = sys.stdout
        stdout_ctx = (
            contextlib.redirect_stdout(debug_trace.StdoutTee(stdout, trace_sink))
            if trace_sink is not None
            else contextlib.nullcontext()
        )
        with stdout_ctx:
            turns()


def _turns_captured(prepared: _PreparedRun, trace_sink, turns: Callable[[], None]) -> None:
    """Plain and --debug-file: stdout carries only the final answer, which
    `_finish` prints. The log captures the streamed answer plus, for
    --debug-file, the concise trace lines; the full request trace lands there
    via telemetry.trace_sink. With TRACE on, the concise trace also shows on
    stderr as the model runs."""
    telemetry = prepared.telemetry
    capture = trace_sink if trace_sink is not None else io.StringIO()
    trace = bool(getattr(prepared.cfg, "trace", False))
    visible_transcript = telemetry.transcript_log
    visible_display = telemetry.display_factory
    # The answer is recorded once it is printed; the tool calls are recorded
    # as they finish.
    telemetry.transcript_log = (transcript_mod.ToolRecordsOnly(visible_transcript)
                                if visible_transcript is not None else None)
    if trace:
        answer_sink = capture
        telemetry.display_factory = (
            lambda markdown: display_mod.Display.for_stream(answer_sink, markdown=markdown))
        capture = debug_trace.StdoutTee(sys.stderr, trace_sink) if trace_sink is not None else sys.stderr
    try:
        with contextlib.redirect_stdout(capture):
            turns()
    finally:
        telemetry.transcript_log = visible_transcript
        telemetry.display_factory = visible_display


def _finish(prepared: _PreparedRun) -> int:
    """The stats files and the JSON result, then the answer printed and the
    saved session compacted when it is due. 1 when there is no answer or a
    stats file was not written."""
    inputs, cfg, telemetry = prepared.inputs, prepared.cfg, prepared.telemetry
    answer = _turn_answer(prepared.messages)
    stats_ok = _record_stats(prepared, answer)
    if not answer:
        with transcript_mod.transcript_stdio(telemetry):
            msgs.warn(msgs.NO_ANSWER)
        return 1
    # In debug, run_turn already streamed the answer live to stdout — a
    # second print here is the double-print bug. Only print when the
    # stream was suppressed (non-debug one-shot/pipe).
    with transcript_mod.transcript_stdio(telemetry):
        if not inputs.debug:
            if (sink := transcript_mod.transcript_sink(telemetry)) is not None:
                sink.emit(output.event("transcript_assistant", text=answer))
            if inputs.events is None:
                display_mod.blocking_stdio()
                with _mute_transcript_tee(transcript_mod.transcript_sink(telemetry)):
                    display_mod.print_answer(
                        answer, sys.stdout,
                        markdown=display_mod.markdown_enabled(getattr(cfg, "settings", None)),
                    )
        if inputs.save:
            owning_loop.run_owning_loop(conversation.maybe_auto_compact_async(cfg, prepared.state))
    return 0 if stats_ok else 1


def _record_stats(prepared: _PreparedRun, answer: str) -> bool:
    """The --stats-json and --stats-csv files and the --json result of the
    run's turn; False when a stats file was not written."""
    inputs, cfg, events = prepared.inputs, prepared.cfg, prepared.inputs.events
    if not (inputs.stats_json or inputs.stats_csv or events is not None):
        return True
    error = None if answer else msgs.NO_ANSWER.words()
    row = {"name": "prompt", "prompt": prepared.prompt or "",
           "max_tokens": cfg.max_output_tokens if inputs.maxout is None else inputs.maxout,
           "ok": bool(answer), "error": error,
           **stats.summarize_calls(prepared.call_stats, wall_s=time.time() - prepared.started_at)}
    payload = {"agent": cfg.agent_id, "model": cfg.model, "provider": cfg.provider_id, "turns": [row]}
    stats_ok = stats.write_requested(inputs.stats_json, inputs.stats_csv, payload, [row])
    if events is not None:
        events.result = {**payload, "answer": answer,
                         "session": session_store.session_hint_arg(cfg) if inputs.save else None,
                         "effort": prepared.effort if prepared.effort is not None else cfg.reasoning_effort,
                         "usage": (usage_mod.totals(cfg.session_file).as_dict() if inputs.save
                                   else events.usage.as_dict())}
    return stats_ok


def _turn_answer(messages: list[dict]) -> str:
    """The answer of the turn that ended ``messages``: the text of its last
    assistant message, "" when that message has none."""
    for message in reversed(messages):
        role = message.get("role")
        if role == "user":
            break
        if role == "assistant":
            content = message.get("content")
            return content.strip() if isinstance(content, str) else ""
    return ""


def say_continue(telemetry: runtime.Telemetry, hint: str, *, resume_prefix: str | None,
                  agent: str | None, model: str | None, cfg: Config | None = None,
                  reasoning: str | None = None) -> None:
    words = shlex.split(resume_prefix) if resume_prefix else ["js"]
    selected_agent = cfg.agent_id if cfg is not None else agent
    if resume_prefix is None and selected_agent and selected_agent != settings.default_value("DEFAULT_AGENT"):
        words += ["--agent", selected_agent]
    if cfg is not None:
        saved_login = login_defaults.login_for(cfg.provider_id, cfg.provider_base_url)
        if cfg.provider_base_url and (saved_login is None or saved_login.provider_id != cfg.provider_id):
            wire = routing.resolve_wire(cfg.provider_id).name
            scheme, _, host = cfg.provider_base_url.partition("://")
            words += ["-u", f"{wire}+{scheme}://{cfg.model}@{host}"]
        else:
            route = f"{cfg.provider_id}/{cfg.model}" if cfg.provider_id else cfg.model
            words += ["--model", route]
        effort = reasoning if reasoning is not None else cfg.reasoning_effort
        words += ["-r", effort or "off"]
        for field, value in login_defaults.sampling(cfg).as_dict_nonnull().items():
            words += [command_line.flag(field),
                      json.dumps(list(value)) if isinstance(value, tuple) else str(value)]
    elif model:
        words += ["--model", model]
    words += command_line.options_argv(current())
    if _paths.custom_home():
        words += ["--home", str(_paths.home())]
    words += command_line.argv(*command_line.isolate_argv())
    words += ["--session", hint]
    with transcript_mod.transcript_stdio(telemetry):
        msgs.warn(msgs.CONTINUE_HINT, command=shlex.join(words))
