"""The `js` command line: parse it, settle the run's setup, and run the mode
it names: a listing, --bench, --compact, --commit, a -p run, or the REPL
(`js.repl`)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import re
import shlex
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from . import (
    agent_lock,
    agent_messages,
    attach,
    bench,
    command_line,
    commit_cli,
    compaction,
    conversation,
    debug_trace,
    display as display_mod,
    dotenv,
    endpoint_uri,
    events,
    headless,
    home as _home,
    jail as _jail,
    listings,
    login_defaults,
    memory as M,
    messages as msgs,
    migrate_config,
    model_client,
    model_metadata,
    network_display,
    output,
    paths as _paths,
    persona as P,
    repl,
    repl_commands,
    repl_state,
    routing,
    run_assembly,
    runtime,
    sampling as sampling_mod,
    session_catalog,
    session_picker,
    session_store,
    setcmd,
    settings,
    snippets,
    stats,
    transcript as transcript_mod,
    usage as usage_mod,
)
from .cli_options import Setup
from .config import (
    Config,
    InvocationSources,
    derive_session_name,
    jsrc_paths,
    resolve_agent_id,
    resolve_session_file,
    run_settings,
    set_invocation,
    validate_agent_id,
    vision_enabled_for_model,
)
from .conversation import Conversation
from .repl_state import ReplState
from .tool_binaries import resolve_binary


def _blocking_stdio() -> None:
    """Clear O_NONBLOCK on stdin, stdout and stderr. Every program in a
    terminal shares one open file description, and one that set it
    non-blocking (nvim and node do, through libuv) can leave it so; a write
    bigger than the terminal's buffer then stops part way with EAGAIN."""
    for fd in (0, 1, 2):
        with contextlib.suppress(OSError):
            if not os.get_blocking(fd):
                os.set_blocking(fd, True)


def _latest_session_name(agent_id: str) -> str | None:
    """The most recently started session file for *agent_id*, or None when there is none.

    A recorded session that no longer exists is treated as absent rather
    than resurrected, so --last never creates an empty session."""
    latest = session_store.read_latest(_paths.state_root() / agent_id)
    return str(latest) if latest is not None else None


def _exchanges_are_the_prompt(args: argparse.Namespace, agent: str | None,
                              sources: InvocationSources) -> bool:
    """Whether a run with no -p runs the agent's NN-user.md / NN-agent.md
    exchanges as a one-shot: the agent has them and the session is fresh,
    so they are what the run sends."""
    try:
        cfg = run_assembly.from_env(None, save_session=False, sources=sources, agent_id=agent)
        spec = P.load_configured_prompt_spec(cfg, expand=False)
    except (FileNotFoundError, ValueError):
        # The REPL path reports the error.
        return False
    if not spec.exchanges:
        return False
    name = args.session if args.session is not None else os.environ.get("JS_SESSION")
    if name is None:
        return True
    try:
        session_file = resolve_session_file(cfg.sessions_dir, name)
    except ValueError:
        # No such session yet: the run starts it.
        return True
    return not M.load_replay_messages(session_file)


def _invocation_agent(args: argparse.Namespace, sources: InvocationSources) -> str:
    """The agent this invocation runs: --agent, --commit, then the `DEFAULT_AGENT`
    setting, resolved as `from_env` resolves it."""
    return resolve_agent_id(
        args.agent or ("commit" if args.commit else None), sources=sources)


def _invocation_sources(args: argparse.Namespace) -> InvocationSources:
    """What the parsed command line adds to settings: its --extra values, its
    --preset names split at commas, and its --no-*-config switches."""
    return InvocationSources(
        extras=tuple(args.extras),
        presets=tuple(name for spec in args.presets for name in spec.split(",") if name.strip()),
        ignore_local_config=args.no_local_config,
        ignore_global_config=args.no_global_config,
    )


def _read_stdin_if_piped() -> str:
    if sys.stdin.isatty():
        return ""
    try:
        return sys.stdin.read()
    except (OSError, ValueError):
        return ""

def _read_stdin_attachment_if_piped() -> bytes:
    if sys.stdin.isatty():
        return b""
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        return buffer.read()
    return sys.stdin.read().encode("utf-8")


def _mute_transcript_tee(sink):
    mute = getattr(sink, "mute_tee", None)
    return mute() if callable(mute) else contextlib.nullcontext()


def _maybe_auto_compact(cfg: Config, state: ReplState) -> None:
    model_client.run_owning_loop(conversation.maybe_auto_compact_async(cfg, state))


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


def _standalone_dispatcher(cfg: Config):
    """Run `on` handler text whose command stands alone, with no REPL state.
    What the command prints goes to stderr: stdout carries the run's answer."""

    def dispatch(hook: events.EventHook, emission: events.EventEmission) -> events.EventHandlerResult:
        line = setcmd.expand_line(hook.handler, cfg.settings)
        verb, arg = setcmd.split_command(line) or ("<blank>", "")
        command = repl_commands.standalone(line)
        if command is None:
            return events.EventHandlerResult(hook=hook, error=msgs.EVENT_HANDLER_COMMAND_UNSUPPORTED.words(value=verb))
        with contextlib.redirect_stdout(sys.stderr):
            return events.EventHandlerResult(hook=hook, error=command.run(arg, None, cfg))

    return dispatch


def _admit_standalone(hook: events.EventHook) -> str | None:
    if repl_commands.standalone(hook.handler) is not None:
        return None
    verb = (setcmd.split_command(hook.handler) or ("",))[0]
    return msgs.ON_HANDLER_NEEDS_REPL.said(event=hook.event, command=verb)


def _prompt_hooks(cfg: Config, paths: list[Path]) -> events.EventHooks:
    """The `on` handlers the jsrc files ``paths`` register for a run outside
    the REPL: those whose command stands alone (`Command.stands_alone`). An
    `on` line that registers no handler, one whose command needs the REPL
    among them, is reported on stderr."""
    hooks = events.EventHooks(_standalone_dispatcher(cfg), admit=_admit_standalone)
    for path, lineno, line in settings.jsrc_commands(paths):
        parsed = setcmd.split_command(line)
        if parsed is not None and parsed[0] == "on" and (error := setcmd.on_command(hooks, parsed[1]).error):
            msgs.warn(msgs.SCRIPT_LINE_FAILED, path=path, lineno=lineno, error=error)
    return hooks


# The command-line flags that set a setting: (args attribute, setting). Each
# reaches every run path as an --extra, which also records it as a flag value
# that a saved login's defaults never replace (js.login_defaults).
SETTING_FLAGS: tuple[tuple[str, str], ...] = (
    ("reasoning", "EFFORT"),
    ("max_out", "OUTPUT_TOKENS_MAX"),
    ("max_turns", "TURN_ROUNDS_MAX"),
    ("template_kwargs", "TEMPLATE_KWARGS"),
    *((field, field.upper()) for field in sampling_mod.SAMPLING_FIELDS),
)


def _setting_flag_extras(args: argparse.Namespace) -> tuple[list[str], int | None]:
    """The --extra entries for the `SETTING_FLAGS` ``args`` sets, and an exit
    code when one of them is not a value its setting takes."""
    extras: list[str] = []
    for attr, key in SETTING_FLAGS:
        value = getattr(args, attr, None)
        if value is None:
            continue
        if key == "EFFORT":
            _seed, effort_error = settings.validate_cli_reasoning(str(value))
            if effort_error is not None:
                msgs.warn(msgs.BAD_REASONING, value=value, error=effort_error)
                return [], 2
        else:
            _value, error = settings.coerce_value(settings.SPEC_BY_KEY[key], str(value))
            if error is not None:
                msgs.warn(msgs.BAD_FLAG_VALUE, flag="--" + attr.replace("_", "-"), value=value, error=error)
                return [], 2
        extras.append(f"{key}={value}")
    return extras, None


def _raise_keyboard_interrupt(*_args) -> None:
    raise KeyboardInterrupt


@session_catalog.session_scope
def _run_prompt(prompt: str | None, **kwargs) -> int:
    """One `-p` run (`_prompt_run`). Once its session is open, the Continue
    line prints on every exit: an answer, no answer, an error, ^C; once its
    turns start, session_end fires before it."""
    opened: dict = {}
    try:
        code = _prompt_run(prompt, opened=opened, **kwargs)
        events = kwargs.get("events")
        if events is not None and code != 0 and not events.errored:
            _emit_failure(events, getattr(sys.stderr, "last_line", ""), code)
        return code
    finally:
        if (end := opened.get("end")) is not None:
            end()
        if (say_continue := opened.get("say")) is not None:
            say_continue()


def _write_requested_stats(json_path, csv_path, payload, rows, *, announce: bool = False) -> bool:
    ok = True
    for path, writer, data in ((json_path, stats.write_json, payload), (csv_path, stats.write_csv, rows)):
        if not path:
            continue
        try:
            writer(path, data)
        except OSError as exc:
            msgs.warn(msgs.FAILED, error=f"stats output {path}: {exc}")
            ok = False
        else:
            if announce:
                msgs.warn(msgs.STATS_WRITTEN, path=path)
    return ok


def _prompt_run(prompt: str | None, model: str | None = None, debug: bool = False,
                debug_file: str | None = None,
                agent: str | None = None, session: str | None = None, save: bool = True,
                resume_prefix: str | None = None,
                reasoning: str | None = None, maxout: int | None = None,
                show_continue: bool = True,
                sources: InvocationSources = InvocationSources(), tool_context=None,
                files: list[str] | None = None, stdin_attachment: bytes | None = None,
                stats_json: str | None = None, stats_csv: str | None = None,
                caller_key: str | None = None,
                events: headless.JsonEvents | None = None,
                config_pins: bool = False, opened: dict | None = None) -> int:
    """A ``prompt`` of None runs the agent's startup exchanges as the whole
    run: the last one is the turn whose answer is printed."""
    # @path in the prompt attaches like it does in the REPL, read here before
    # any jail, so `-p "@/tmp/prompt.txt"` needs no JAIL_BIND.
    attachments: list[str] = []
    if prompt is not None:
        prompt, named = attach.split_repl_attachments(prompt)
        attachments = [*(files or []), *named]
        if not prompt.strip() and not attachments:
            msgs.warn(msgs.PROMPT_EMPTY)
            return 2
    reasoning_override = None
    if reasoning is not None:
        reasoning_override, effort_error = settings.validate_cli_reasoning(reasoning)
        if effort_error is not None:
            msgs.warn(msgs.BAD_REASONING, value=reasoning, error=effort_error)
            return 2
    try:
        cfg = run_assembly.from_env(session, save_session=save, sources=sources, agent_id=agent)
    except ValueError as e:
        msgs.warn(msgs.FAILED, error=e)
        return 2
    # A resumed session continues on the model, provider and reasoning of its
    # last stamp unless this run names them. Read before this start records its own.
    resumed = run_assembly.resume(cfg, model, reasoning_override)
    model = resumed.model
    if save:
        session_catalog.activate_saved_session(
            cfg,
            caller_key=caller_key,
            announce_generated=session is None and os.environ.get("JS_SESSION") is None,
            model=model,
        )
    telemetry = runtime.Telemetry(debug_log=cfg.debug_log)
    transcript_mod.sync_transcript_sink(cfg, getattr(cfg, "settings", {}) or {}, telemetry)
    if save and show_continue and opened is not None:
        hint = session_store.session_hint_arg(cfg)
        # `model` is read when the line prints: a resumed session's spec by then.
        opened["say"] = lambda: _say_continue(telemetry, hint, resume_prefix=resume_prefix,
                                              agent=agent, model=model, cfg=cfg, reasoning=reasoning_override)

    try:
        # A resumed session stays on its stamp; agent.yaml sets a fresh run.
        settled = run_assembly.settle(
            cfg, reasoning=reasoning_override, resumed=resumed, config_pins=config_pins,
            agent_reasoning=not config_pins and resumed.stamp is None and reasoning_override is None)
    except run_assembly.RunNotSettled as e:
        with transcript_mod.transcript_stdio(telemetry):
            msgs.warn(msgs.FAILED, error=P.format_prompt_load_error(cfg, e.cause) if e.loading else e.cause)
        return 2
    # A resumed session's reasoning is this session's own choice, like /set,
    # even when it is what the model would pick: its turns and the children
    # they start run on it.
    cfg, reasoning_override = settled.config, settled.effort
    prompt_spec, system, recorded_system = settled.prompt_spec, settled.system, settled.recorded_system
    if resumed.unreachable is not None:
        with transcript_mod.transcript_stdio(telemetry):
            msgs.warn(msgs.RESUME_MODEL_FALLBACK, stamped=resumed.unreachable,
                      model=conversation.stamp_label(cfg.model, cfg.provider_id))

    attachment_cfg = (
        replace(cfg, model=model, vision_enabled=vision_enabled_for_model(model, getattr(cfg, "settings", None)))
        if model is not None
        else cfg
    )
    messages = M.load_replay_messages(cfg.session_file)
    cut_off = M.turn_cut_off(messages)
    if save:
        prompt_changed = M.record_prompt_seen(cfg.session_file, prompt_spec.source)
        if recorded_system is None:
            M.append_system_prompt(cfg.session_file, system)
        elif prompt_changed:
            notice = {"role": "user", "content": snippets.render('prompt-changed')}
            messages.append(notice)
            conversation.append_turn(cfg, notice)
    repl_commands.restore_workspace(cfg)
    resume_cfg = cfg
    if not save:
        cfg = replace(cfg, session_file=Path(os.devnull))
    before_len = len(messages)
    if events is not None:
        saved = cfg.session_file != Path(os.devnull)
        events.emit(
            "start", version=headless.SCHEMA_VERSION,
            id=session_store.session_hint_arg(cfg) if saved else None,
            file=str(cfg.session_file) if saved else None,
            resumed=before_len > 0, agent=cfg.agent_id, model=cfg.model, provider=cfg.provider_id,
            effort=reasoning_override if reasoning_override is not None else cfg.reasoning_effort,
            cwd=str(Path.cwd()), usage=usage_mod.totals(cfg.session_file).as_dict(),
        )
    user_message = None
    if prompt is not None:
        try:
            user_message = attach.build_user_message(
                prompt,
                attachments,
                attachment_cfg,
                stdin_attachment=stdin_attachment,
            )
        except attach.AttachmentError as e:
            with transcript_mod.transcript_stdio(telemetry):
                msgs.warn(msgs.FAILED, error=e)
            return 2
        if save:
            user_message = conversation.note_mode_switch(cfg, user_message, "-p")
        user_message = conversation.note_model_switch(resume_cfg, user_message)
        if cut_off:
            user_message = attach.with_note(user_message, snippets.render('turn-cut-off'))
    elif save and cfg.session_file != Path(os.devnull):
        M.record_turn_mode(cfg.session_file, "-p")
    with transcript_mod.transcript_stdio(telemetry):
        hooks = _prompt_hooks(cfg, jsrc_paths(Path(getattr(cfg, "project_dir", None) or Path.cwd()), sources))
    run_state = _run_state(cfg, system, messages, hooks)
    call_stats: list[dict] = []
    turn_kwargs = settled.turn_kwargs(call_stats=call_stats, event_hooks=hooks)
    if events is not None:
        turn_kwargs["event_sink"] = events.runtime_event
        telemetry.sink = output.Fanout(output.sink_of(telemetry), headless.OutputEvents(events))
    if maxout is not None:
        turn_kwargs["max_output_override"] = maxout
    _t_wall = time.time()
    autolog = debug_trace.debug_autolog_path(cfg, getattr(cfg, "settings", {}) or {})
    sink_paths: list[Path] = [p for p in (autolog,) if p is not None]
    if debug_file:
        sink_paths.append(Path(debug_file))
    trace_sink = debug_trace.open_debug_trace_sink(sink_paths)
    telemetry.trace_sink = trace_sink
    turn_transcript = transcript_mod.transcript_sink(telemetry)

    # -d shows the conversation as the REPL draws it: each user line, typed or
    # fired from the agent's files, ahead of the answer it got.
    echo_user = display_mod.echo_user if debug else None
    trace_override = debug or bool(debug_file) or cfg.trace
    chat = Conversation(cfg, system, registry=settled.registry, context=tool_context or runtime.T.STOCK_CONTEXT,
                        telemetry=telemetry, messages=messages)

    def started() -> None:
        """The prompt is on disk, in the transcript and echoed before the
        model is called."""
        if save:
            conversation.append_turn(cfg, user_message)
        if turn_transcript is not None:
            turn_transcript.emit(output.event("transcript_user", text=prompt))
        if echo_user is not None:
            echo_user(prompt)

    def turns() -> None:
        if prompt_spec.exchanges and not messages:
            model_client.run_owning_loop(agent_messages.initialize(
                cfg, prompt_spec, messages, replace(telemetry, transcript_log=turn_transcript),
                system=system, save=save, trace_override=trace_override,
                tool_context=tool_context, echo_user=echo_user, **turn_kwargs,
            ))
        if user_message is not None:
            repl_state.emit_repl_event(run_state, telemetry, "input", text=prompt, attachments=attachments)
            # In this context as well as the turn's, so the check after the
            # turn reads the same window.
            runtime.install_context_window_overrides(cfg)
            model_client.run_owning_loop(chat.turn(
                user_message, started=started, trace_override=trace_override, **turn_kwargs))

    # SIGTERM (a runner, `timeout`) ends the run like ^C:
    # the session is persisted and the exit code says interrupted. The
    # handler this run replaces is put back when it ends.
    previous_sigterm = None
    with contextlib.suppress(ValueError):  # not the main thread: no signal hook
        previous_sigterm = signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
    repl_state.emit_session_event(run_state, telemetry, cfg, "session_start")
    if opened is not None:
        opened["end"] = lambda: repl_state.emit_session_event(run_state, telemetry, cfg, "session_end")
    try:
        try:
            if debug:
                # Concise trace + streamed answer live on the terminal; the giant
                # request trace goes to the log only. Tee stdout so the log also
                # keeps the concise lines and the answer.
                with transcript_mod.transcript_stdio(telemetry):
                    _real_stdout = sys.stdout
                    stdout_ctx = (
                        contextlib.redirect_stdout(debug_trace.StdoutTee(_real_stdout, trace_sink))
                        if trace_sink is not None
                        else contextlib.nullcontext()
                    )
                    with stdout_ctx:
                        turns()
            else:
                # Plain and --debug-file: stdout carries only the final answer,
                # reprinted below. The log captures the streamed answer plus, for
                # --debug-file, the concise trace lines; the full request trace
                # lands there via telemetry.trace_sink. With TRACE on, the
                # concise trace also shows on stderr as the model runs.
                capture = trace_sink if trace_sink is not None else io.StringIO()
                trace = bool(getattr(cfg, "trace", False))
                visible_transcript = telemetry.transcript_log
                visible_display = telemetry.display_factory
                # The answer is recorded once it is reprinted below; the tool
                # calls are recorded as they finish.
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
        except (KeyboardInterrupt, asyncio.CancelledError):
            repl_state.emit_repl_event(run_state, telemetry, "cancel", reason="cancelled")
            with transcript_mod.transcript_stdio(telemetry):
                msgs.warn(msgs.TURN_INTERRUPTED)
            return 130
        except Exception as e:  # noqa: BLE001
            with transcript_mod.transcript_stdio(telemetry):
                msgs.warn(msgs.FAILED, error=network_display.error_text(e))
            code = 2 if isinstance(e, snippets.SnippetError) else 1
            if events is not None and not events.errored:
                _emit_failure(events, network_display.error_text(e), code)
            return code
    finally:
        try:
            if save:
                chat.save()
        finally:
            if trace_sink is not None:
                trace_sink.close()
            telemetry.trace_sink = None
            if previous_sigterm is not None:
                signal.signal(signal.SIGTERM, previous_sigterm)

    answer = _turn_answer(messages)

    stats_ok = True
    if stats_json or stats_csv or events is not None:
        error = None if answer else msgs.NO_ANSWER.words()
        row = {"name": "prompt", "prompt": prompt or "", "max_tokens": cfg.max_output_tokens if maxout is None else maxout,
               "ok": bool(answer), "error": error, **stats.summarize_calls(call_stats, wall_s=time.time() - _t_wall)}
        payload = {"agent": cfg.agent_id, "model": cfg.model, "provider": cfg.provider_id, "turns": [row]}
        stats_ok = _write_requested_stats(stats_json, stats_csv, payload, [row])
        if events is not None:
            events.result = {**payload, "answer": answer,
                             "session": session_store.session_hint_arg(cfg) if save else None,
                             "effort": reasoning_override if reasoning_override is not None else cfg.reasoning_effort,
                             "usage": (usage_mod.totals(cfg.session_file).as_dict() if save else events.usage.as_dict())}

    if not answer:
        with transcript_mod.transcript_stdio(telemetry):
            msgs.warn(msgs.NO_ANSWER)
        return 1
    # In debug, run_turn already streamed the answer live to stdout — a
    # second print here is the double-print bug. Only print when the
    # stream was suppressed (non-debug one-shot/pipe).
    with transcript_mod.transcript_stdio(telemetry):
        if not debug:
            if (sink := transcript_mod.transcript_sink(telemetry)) is not None:
                sink.emit(output.event("transcript_assistant", text=answer))
            if events is None:
                _blocking_stdio()
                with _mute_transcript_tee(transcript_mod.transcript_sink(telemetry)):
                    display_mod.print_answer(
                        answer, sys.stdout,
                        markdown=display_mod.markdown_enabled(getattr(cfg, "settings", None)),
                    )
        if save:
            _maybe_auto_compact(cfg, run_state)
    return 0 if stats_ok else 1


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


def _say_continue(telemetry: runtime.Telemetry, hint: str, *, resume_prefix: str | None,
                  agent: str | None, model: str | None, cfg: Config | None = None,
                  reasoning: str | None = None) -> None:
    from .cli_options import current

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
    words += ["--session", hint]
    with transcript_mod.transcript_stdio(telemetry):
        msgs.warn(msgs.CONTINUE_HINT, command=shlex.join(words))


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


_json_events: headless.JsonEvents | None = None


def _json_run(run: Callable[[], int], *, stream: bool = True) -> int:
    """Keep stdout machine-readable, ending with the completed run object."""
    global _json_events
    stdout = sys.stdout
    events = headless.JsonEvents(stdout if stream else io.StringIO())
    err = headless.LastLine(sys.stderr)
    _json_events = events
    try:
        with contextlib.redirect_stdout(err), contextlib.redirect_stderr(err):
            try:
                code = run()
            except Exception as exc:  # noqa: BLE001 - the stream must still end with a result
                msgs.warn(msgs.FAILED, error=network_display.error_text(exc))
                code = 2 if isinstance(exc, snippets.SnippetError) else 1
    finally:
        _json_events = None
    opened = events.session
    if opened is None:
        events.emit("start", version=headless.SCHEMA_VERSION)
    if code != 0 and not events.errored:
        _emit_failure(events, err.last_line, code)
    payload = events.result or {
        "answer": events.final_text if code == 0 else "",
        "session": None if opened is None else opened.get("id"),
        "provider": None if opened is None else opened.get("provider"),
        "model": None if opened is None else opened.get("model"),
        "effort": None if opened is None else opened.get("effort"),
        "usage": None if opened is None else opened.get("usage"),
    }
    if "benchmarks" not in payload:
        if opened is not None and opened.get("file"):
            payload["usage"] = usage_mod.totals(Path(opened["file"])).as_dict()
        payload.update(ok=code == 0, exit_code=code)
    if stream:
        events.emit("end", **payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, default=str), file=stdout)
    return code


def _emit_failure(events: headless.JsonEvents, line: str, code: int) -> None:
    if events.session is None:
        events.emit("start", version=headless.SCHEMA_VERSION)
    message = _WARN_MARK.sub("", _ANSI.sub("", line))
    events.emit("error", message=message or f"exit {code}", retryable=False)


_WARN_MARK = re.compile(r"^\*+\s*")


def _run_prompt_json(prompt: str | None, **kwargs) -> int:
    """`_run_prompt` with its run written to stdout as JSON events
    (`js.headless`). A failure without an `error` event gets one carrying the
    last line printed to stderr before `_run_prompt` returned."""
    events = _json_events
    if events is None:
        return _json_run(lambda: _run_prompt_json(prompt, **kwargs))
    code = _run_prompt(prompt, events=events, **kwargs)
    if code != 0 and not events.errored:
        _emit_failure(events, getattr(sys.stderr, "last_line", ""), code)
    return code


def _run_compact_offline(session: str, *, agent: str | None = None, focus: str = "",
                         sources: InvocationSources = InvocationSources(),
                         model: str | None = None) -> int:
    try:
        cfg = run_assembly.from_env(session, save_session=False, sources=sources, agent_id=agent)
        if not cfg.session_file.is_file():
            raise ValueError(msgs.CONFIG_SESSION_MUST_IDENTIFY_AN_EXISTING_JSONL_FILE.format(session=session))
        prompt_spec = P.load_configured_prompt_spec(cfg)
        messages = M.load_replay_messages(cfg.session_file)
        emit, skipped = compaction.track_compact_skips(compaction.printing())
        result = compaction.compact_now_sync(cfg, prompt_spec.system, messages, focus=focus, forced=True,
                                             model=model, emit=emit,
                                             log_dir=compaction.flight_log_dir(cfg))
    except snippets.SnippetError:
        raise
    except Exception as e:  # noqa: BLE001
        msgs.warn(msgs.FAILED, error=network_display.error_text(e))
        return 1
    if result not in skipped:
        msgs.say(msgs.COMPACTION_DONE, result=result)
    return 0

def _compact(args: argparse.Namespace, setup: Setup) -> int:
    """`js --compact SESSION`: -p TEXT is the focus, `-p -` reads it from stdin."""
    focus = _read_stdin_if_piped() if args.prompt == "-" else args.prompt or ""
    return _run_compact_offline(args.compact, agent=setup.agent, focus=focus, sources=setup.sources,
                                model=args.model)


def _commit(args: argparse.Namespace, setup: Setup) -> int:
    """`js --commit [TARGET]`. It runs the commit agent, so --agent beside it
    exits 2. -p TEXT and piped stdin are extra context for the agent."""
    if args.agent:
        msgs.warn(msgs.COMMIT_WITH_AGENT)
        return 2
    extra_context = None
    if args.prompt is not None:
        if args.prompt == "-":
            extra_context = _read_stdin_if_piped()
        else:
            stdin_text = _read_stdin_if_piped()
            extra_context = args.prompt if not stdin_text.strip() else f"{args.prompt.rstrip()}\n\n{stdin_text.strip()}"
    session_catalog.session_leases.start["mode"] = "commit"
    return commit_cli.run_commit(args.target, model=args.model, debug=args.debug, debug_file=args.debug_file,
                       session=args.session, save=not args.no_save,
                       reasoning=args.reasoning, maxout=args.max_out,
                       extra_context=extra_context, sources=setup.sources)


def _agent_bench(args: argparse.Namespace, setup: Setup) -> int | None:
    """The run's agent runs its NN-benchmark.md cases when it has some and no
    system prompt flag or chat mode replaces its prompt; None otherwise."""
    try:
        agent = _invocation_agent(args, setup.sources)
        agent_dir = _paths.agent_dirs(agent)
    except (ValueError, OSError) as exc:
        msgs.warn(msgs.FAILED, error=exc)
        return 2
    from .cli_options import current
    if not current().replaces_prompt and not current().chat and P.has_benchmarks(agent_dir):
        return bench.run_agent_bench(agent, args, setup.sources)
    return None


def _warn_unmatched_presets(sources: InvocationSources) -> None:
    from .config import _preset_config_paths

    project_dir = Path.cwd()
    for name in sources.presets:
        candidates = _preset_config_paths(replace(sources, presets=(name,)), project_dir)
        if not any(path.exists() for path in candidates):
            if candidates:
                msgs.warn(msgs.PRESET_NOT_FOUND, name=name, looked=" or ".join(str(p) for p in candidates))
            else:
                msgs.warn(msgs.PRESET_NO_CONFIG_DIRS, name=name)


# External binaries js leans on. Each missing one is warned about once at
# startup, so a run without it has an obvious cause, and startup goes on
# without it. `just install` provisions them.
_EXPECTED_BINARIES: tuple[tuple[str, str], ...] = (
    ("rg", "fs_search"),
    ("fd", "file finding"),
    ("bat", "highlighted previews"),
    ("fzf", "the interactive pickers"),
)
_warned_binaries: set[str] = set()


def _warn_missing_binaries() -> None:
    for name, why in _EXPECTED_BINARIES:
        if name in _warned_binaries:
            continue
        binary = resolve_binary(name)
        if binary is None:
            _warned_binaries.add(name)
            msgs.warn(msgs.BINARY_MISSING, name=name, user=why)


def main(argv: list[str] | None = None) -> int:
    try:
        code = _main(argv)
    except snippets.SnippetError as exc:
        msgs.warn(msgs.FAILED, error=exc)
        code = 2
    pending = dict(repl.pending_switch)
    repl.pending_switch.clear()
    if pending:
        repl.exec_session(pending["target"])
    return code


@session_catalog.session_scope
def _main(argv: list[str] | None = None) -> int:
    from .home_cli import configure_home
    try:
        dispatch_argv = configure_home(list(argv if argv is not None else sys.argv[1:]))
    except ValueError as exc:
        msgs.warn(msgs.FAILED, error=exc)
        return 2
    _blocking_stdio()
    # Before anything reads or writes ~/.js: move the old locations in, once,
    # then make sure every directory of the layout is there.
    _home.migrate_once()
    with contextlib.suppress(OSError):
        _paths.ensure_home()
    try:
        _home.sweep_tmp()
    except (OSError, ValueError) as exc:
        msgs.warn(msgs.FAILED, error=exc)
        return 2
    # Handle login/logout before argparse so they don't require a valid agent/config.
    # None of them take -C, so the cwd is already final and .env can load here;
    # otherwise `js --login x` and `js --login=x` (argparse path) would disagree.
    if dispatch_argv and dispatch_argv[0] in (
        "--login", "login", "--logout", "logout", "--models-edit", "models-edit"
    ):
        dotenv.load()
    if dispatch_argv and dispatch_argv[0] in ("--login", "login") and not any(
        flag in dispatch_argv for flag in ("--json", "--json-stream")
    ):
        from . import login_cli
        return login_cli.main(dispatch_argv[1:])
    if dispatch_argv and dispatch_argv[0] in ("--logout", "logout") and not any(
        flag in dispatch_argv for flag in ("--json", "--json-stream")
    ):
        from . import login_cli
        return login_cli.main(["logout"] + dispatch_argv[1:])
    if dispatch_argv and dispatch_argv[0] in ("--models-edit", "models-edit"):
        from . import login_cli
        return login_cli.main(["models-edit"] + dispatch_argv[1:])

    parser = command_line.parser()
    _refuse_glued_short_options(parser, dispatch_argv)
    _validate_output_argv(parser, dispatch_argv)
    args = parser.parse_args(dispatch_argv)
    _validate_output_flags(parser, args)
    if args.last_prompt is not None:
        if args.prompt is not None:
            parser.error(msgs.LAST_PROMPT_WITH_PROMPT.text())
        args.last, args.prompt = True, args.last_prompt
    if args.target is not None and not args.commit:
        parser.error(msgs.UNEXPECTED_ARGUMENT.text(word=args.target))
    if args.bare and args.agent is not None:
        parser.error(msgs.BARE_WITH_AGENT.text())
    if args.loop is not None:
        from . import loop_controller
        try:
            loop_settings = run_settings(Path(args.cd).expanduser() if args.cd else None,
                                         sources=_invocation_sources(args))
            return loop_controller.run(dispatch_argv, args.loop, bench=args.bench, prompt=args.prompt,
                                       settings=loop_settings)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
    from .cli_options import RunOptions, invocation
    if args.bare:
        args.agent = 'slim'
    options = RunOptions(
        system_prompt_file=args.system_prompt_file, system_prompt=args.system_prompt,
        append_system_prompt=tuple(args.append_system_prompt), you_are=args.you_are,
        no_env=args.no_env or args.bare, chat=args.chat, agent=args.agent,
        no_agents_md=args.no_agents_md, no_skills=args.no_skills or args.bare,
        no_mcp=args.no_mcp or args.bare, no_tools=args.no_tools, tools=args.tools,
    )
    try:
        options.selectors(())
        with invocation(options):
            if _json_run_requested(args):
                return _json_run(lambda: _run_args(args, dispatch_argv), stream=args.json_stream)
            return _run_args(args, dispatch_argv)
    except (ValueError, OSError) as exc:
        msgs.warn(msgs.FAILED, error=exc)
        return 2


_NEGATIVE_NUMBER = re.compile(r"-\d+|-\d*\.\d+")


def _refuse_glued_short_options(parser: argparse.ArgumentParser, argv: list[str]) -> None:
    """Exit 2 on a single-dash word that is not a defined option. argparse reads
    one as the short option its first letters name, with the rest glued on as
    the value: `-sysf FILE` is `-s ysf`, a session. Boolean short flags may
    cluster (`-qn`); a word with a value-taking letter in it is refused."""
    known = parser._option_string_actions
    for token in argv:
        if token == "--":
            break
        name = token.partition("=")[0]
        if (not token.startswith("-") or token.startswith("--") or len(token) < 2
                or " " in token or name in known or _NEGATIVE_NUMBER.fullmatch(token)):
            continue
        letters = [known.get("-" + letter) for letter in name[1:]]
        if letters and all(action is not None and action.nargs == 0 for action in letters):
            continue
        parser.error(msgs.UNKNOWN_OPTION.text(option=token))


def _validate_output_argv(parser: argparse.ArgumentParser, argv: list[str]) -> None:
    flags = {word.partition("=")[0] for word in argv[:argv.index("--") if "--" in argv else len(argv)]}
    if {"--json", "--json-stream"} <= flags:
        parser.error(msgs.JSON_FLAGS_EXCLUSIVE.words())
    if flags & {"--json", "--json-stream"}:
        for command in ("--help", "-h", "--help-full"):
            if command in flags:
                error = msgs.JSON_UNSUPPORTED.words(mode="--json/--json-stream", command=command)
                parser.error(error)


def _validate_output_flags(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.json and args.json_stream:
        parser.error(msgs.JSON_FLAGS_EXCLUSIVE.words())
    for name in ("list_sessions", "list_providers", "list_logins"):
        if getattr(args, name) and not args.json:
            error = msgs.LIST_REQUIRES_JSON.words(command="--" + name.replace("_", "-"))
            parser.error(error)
    listings = ("list_sessions", "list_providers", "list_logins", "list_agents", "list_models", "verify")
    for name in listings:
        value = getattr(args, name)
        if value is not None and value is not False and args.json_stream:
            error = msgs.JSON_UNSUPPORTED.words(mode="--json-stream", command="--" + name.replace("_", "-"))
            parser.error(error)
    if args.json or args.json_stream:
        for name in ("commit", "compact", "login", "logout", "migrate_config", "refresh_model_catalog"):
            value = getattr(args, name)
            if value is not None and value is not False:
                error = msgs.JSON_UNSUPPORTED.words(mode="--json-stream" if args.json_stream else "--json", command="--" + name.replace("_", "-"))
                parser.error(error)


def _json_run_requested(args: argparse.Namespace) -> bool:
    return (args.json or args.json_stream) and not any(
        getattr(args, name) is not None and getattr(args, name) is not False
        for name in ("list_sessions", "list_providers", "list_logins", "list_agents", "list_models", "verify")
    )


def _setup(args: argparse.Namespace, dispatch_argv: list[str]) -> Setup | int:
    """Before any mode: the setting sources with -u and the setting flags in
    them, the session a bare --session picks, .env, and the -C jail. An exit
    code when one of them stops the run."""
    session_catalog.session_leases.start = {"mode": None, "command": ["js", *dispatch_argv]}
    sources = _invocation_sources(args)
    if args.url:
        # Desugar before anything reads the extras. Prepended, not appended, so
        # an explicit --extra on the same command line still overrides a field
        # the spec filled in.
        try:
            endpoint_settings = run_settings(Path(args.cd).expanduser() if args.cd else None,
                                             sources=sources)
            endpoint = endpoint_uri.parse(args.url, settings=endpoint_settings, env=os.environ)
            sources = replace(sources, extras=(*endpoint.as_settings(), *sources.extras))
        except endpoint_uri.EndpointSpecError as exc:
            msgs.warn(msgs.BAD_URL_SPEC, error=exc)
            return 2
    else:
        # The env layer reads a JS_BASE_URL in the -u form; one it cannot
        # read, or one another JS_ variable contradicts, stops the run here.
        try:
            endpoint_uri.from_env(os.environ)
        except endpoint_uri.EndpointSpecError as exc:
            msgs.warn(msgs.BAD_BASE_URL, error=exc)
            return 2
    flag_extras, flag_error = _setting_flag_extras(args)
    if flag_error is not None:
        return flag_error
    # After --extra, so a flag beats an --extra naming the same setting.
    sources = replace(sources, extras=(*sources.extras, *flag_extras))
    set_invocation(sources)
    if args.session == "":
        # A bare --session opens the picker; the chosen session is resumed in
        # its own directory and agent, jailed there when it started under -C.
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            msgs.warn(msgs.SESSIONS_NEEDS_TERMINAL)
            return 2
        try:
            choice = session_picker.pick_session(Path.cwd())
            target = session_picker.session_target(choice) if choice is not None else None
        except Exception as exc:  # noqa: BLE001
            msgs.warn(msgs.FAILED, error=f"{type(exc).__name__}: {exc}")
            return 1
        if target is None:
            return 0
        session_picker.enter_session_dir(target)
        args.session = str(target.path)
        if target.agent:
            args.agent = target.agent
        if target.jailed and target.cwd and not args.cd:
            args.cd = target.cwd
    # Fill unset env names from .env, the run's directory (DIR under -C)
    # upward, then ~/.js/.env. The real environment always wins. `just run`
    # already did this via the justfile's dotenv-load; this gives a bare `js`
    # on PATH the same keys. Every setting source is loaded before the jail
    # below is checked and built.
    dotenv.load(Path(args.cd).expanduser() if args.cd else None)
    if args.cd:
        # -C is the jail: every tool that starts a process runs under
        # bubblewrap, and the file tools stay inside DIR and the bound paths.
        # The self-test builds the jail this run uses: its JAIL_BIND and JAIL_HIDE.
        try:
            jail_settings = run_settings(Path(args.cd).expanduser())
        except ValueError as exc:
            msgs.warn(msgs.FAILED, error=exc)
            return 2
        try:
            jailed = _jail.enter(Path(args.cd).expanduser(),
                                 setting=settings.knob(jail_settings, "JAIL_BIND") or (),
                                 hide=settings.knob(jail_settings, "JAIL_HIDE"))
        except _jail.JailError as exc:
            msgs.warn(msgs.JAIL_REFUSED, error=exc)
            return 2
        os.chdir(jailed.root)
        # STOCK_CONTEXT is built at import (before this chdir), so its cwd is
        # stale; rebind it so -p/REPL turns (which fall back to it) run in DIR.
        runtime.T.STOCK_CONTEXT.cwd = Path.cwd()
    return Setup(sources)


def _settle_session(args: argparse.Namespace, setup: Setup) -> int | None:
    """--last and --session-key name the session; the run takes the chat mode
    a session was saved with and warns about a --preset nothing matches. An
    exit code when the session cannot be named."""
    sources = setup.sources
    if args.last:
        if args.session is not None:
            msgs.warn(msgs.LAST_WITH_SESSION)
            return 2
        try:
            last_agent = _invocation_agent(args, sources)
        except ValueError as e:
            msgs.warn(msgs.FAILED, error=e)
            return 2
        resolved_last = _latest_session_name(last_agent)
        if resolved_last is None:
            msgs.warn(msgs.NO_PREVIOUS_SESSION, agent=last_agent)
            return 2
        args.session = resolved_last
    if args.session_key is not None:
        try:
            effective_agent = _invocation_agent(args, sources)
        except ValueError as e:
            msgs.warn(msgs.FAILED, error=e)
            return 2
        args.session = derive_session_name(effective_agent, Path.cwd(), args.session_key)
    session_catalog.session_leases.caller_key = args.session_key
    run_assembly.inherit_session_chat(args.session)
    if sources.presets:
        # Resolve against the now-final cwd (after any -C). A name that matches no
        # file anywhere is almost certainly a typo — say so rather than no-op.
        _warn_unmatched_presets(sources)
    return None


def _prepare_run(args: argparse.Namespace, setup: Setup) -> Setup | int:
    """Past the listings, for a run: the flags that cannot go together, the
    environment the run's tools read, the missing helper binaries, --agent
    validated into the setup, and --lock. An exit code when one refuses."""
    sources = setup.sources
    if args.debug and args.debug_file:
        msgs.warn(msgs.DEBUG_FLAGS_EXCLUSIVE)
        return 2
    if args.im_a_pussy:
        # Inline code runs by default now; the opt-out flag turns it off for this
        # run via the same env knob the config layer reads.
        os.environ["JS_INLINE_CODE"] = "0"
    if args.no_shell_history:
        # envctx reads this; 0 drops its recent_shell block.
        os.environ["ENVCTX_HIST"] = "0"
    # Past the machine-consumed introspection early-exits: this is a real run, so
    # surface any missing helper binary once (never crash, never block startup).
    _warn_missing_binaries()
    cli_agent = None
    if args.agent:
        try:
            cli_agent = validate_agent_id(args.agent)
        except ValueError as e:
            msgs.warn(msgs.FAILED, error=e)
            return 2

    if args.lock:
        try:
            agent_lock.acquire(args.bench and validate_agent_id(args.bench) or _invocation_agent(args, sources))
        except (ValueError, agent_lock.Busy, OSError) as e:
            msgs.warn(msgs.FAILED, error=e)
            return 2

    selected_modes = [name for name, enabled in (("commit", args.commit), ("compact", args.compact)) if enabled]
    if len(selected_modes) > 1:
        msgs.warn(msgs.MODES_EXCLUSIVE)
        return 2
    if args.files and selected_modes:
        msgs.warn(msgs.FILE_NEEDS_PROMPT_MODE)
        return 2
    return replace(setup, agent=cli_agent)


def _run_args(args: argparse.Namespace, dispatch_argv: list[str]) -> int:
    """The mode picker: setup, then each mode in turn, the first named on the
    command line running, then a -p run or the REPL."""
    setup = _setup(args, dispatch_argv)
    if isinstance(setup, int):
        return setup
    if args.verify is not None:
        from . import verify
        return verify.run_verify(args)
    if args.list_sessions:
        return listings.run_list_sessions(args)
    if (code := _settle_session(args, setup)) is not None:
        return code
    if args.login is not None:
        from . import login_cli
        return login_cli.main([args.login] if args.login else [])
    if args.logout:
        from . import login_cli
        return login_cli.main(["logout", args.logout])
    if args.list_agents:
        return listings.run_list_agents(args)
    if args.list_providers:
        return listings.print_json(listings.providers_json())
    if args.list_logins:
        return listings.print_json(listings.logins_json())
    if args.list_models is not None:
        return listings.run_list_models(args, lambda: run_assembly.from_env(
            args.session, save_session=False, sources=setup.sources, agent_id=args.agent))
    if args.refresh_model_catalog and (code := model_metadata.run_refresh_model_catalog(args)) is not None:
        return code
    setup = _prepare_run(args, setup)
    if isinstance(setup, int):
        return setup
    if args.bench:
        return bench.run_bench_mode(args, setup.sources)
    if args.compact:
        return _compact(args, setup)
    if args.migrate_config:
        return migrate_config.run_migrate_config()
    if args.commit:
        return _commit(args, setup)
    if (code := _agent_bench(args, setup)) is not None:
        return code
    if args.files and args.prompt is None and sys.stdin.isatty():
        msgs.warn(msgs.FILE_NEEDS_PROMPT)
        return 2
    exchanges_only = (args.prompt is None and sys.stdin.isatty()
                      and _exchanges_are_the_prompt(args, setup.agent, setup.sources))
    if args.prompt is not None or not sys.stdin.isatty() or exchanges_only:
        return _run_headless(args, setup, exchanges_only=exchanges_only)
    if args.json or args.json_stream:
        msgs.warn(msgs.FAILED, error="--json/--json-stream requires -p, a pipe, an agent run, or --bench")
        return 2
    return repl.start_repl(args, setup)


def _run_headless(args: argparse.Namespace, setup: Setup, *, exchanges_only: bool) -> int:
    """One turn with no REPL: -p PROMPT, piped stdin as the prompt or beside it,
    `-f -` attaching stdin, or the agent's own exchanges when they are the
    prompt."""
    os.environ["JS_MODE"] = "headless"
    stdin_attachment = None
    if "-" in args.files:
        if args.prompt in {None, "-"}:
            msgs.warn(msgs.STDIN_TWICE)
            return 2
        if sys.stdin.isatty():
            msgs.warn(msgs.STDIN_ATTACHMENT_NOT_PIPED)
            return 2
        stdin_attachment = _read_stdin_attachment_if_piped()
        prompt = args.prompt or ""
    elif exchanges_only:
        prompt = None
    elif args.prompt in {None, "-"}:
        prompt = _read_stdin_if_piped()
    elif sys.stdin.isatty():
        prompt = args.prompt
    else:
        stdin_text = _read_stdin_if_piped()
        prompt = args.prompt if not stdin_text.strip() else f"{args.prompt.rstrip()}\n\n{stdin_text.strip()}"
    session_catalog.session_leases.start["mode"] = "-p" if args.prompt is not None or exchanges_only else "pipe"
    run = _run_prompt_json if (args.json or args.json_stream) else _run_prompt
    result = run(prompt, model=args.model, debug=args.debug, debug_file=args.debug_file,
                 agent=args.agent, session=args.session, save=not args.no_save,
                 caller_key=args.session_key,
                 reasoning=args.reasoning, maxout=args.max_out,
                 show_continue=not args.quiet,
                 sources=setup.sources,
                 files=args.files,
                 stdin_attachment=stdin_attachment,
                 stats_json=args.stats_json, stats_csv=args.stats_csv)
    if args.no_save:
        msgs.warn(msgs.NOT_SAVED_NO_RESUME)
    return result


if __name__ == "__main__":
    sys.exit(main())
