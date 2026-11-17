"""The interactive REPL: startup, the loop that reads lines while a turn
runs, the turns it runs one at a time, steering, the status bar, and the
`/session` switch that replaces the process once the screen has closed.
Slash commands live in `js.repl_commands`, the live state in `js.repl_state`."""

from __future__ import annotations

import copy
import os
import shlex
import sys
from pathlib import Path

from . import (
    command_line,
    debug_trace,
    events,
    fset,
    memory as M,
    messages as msgs,
    motd,
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
    transcript as transcript_mod,
)
from .config import Config
from .repl_state import ReplState


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
