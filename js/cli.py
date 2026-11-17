"""Interactive REPL. prompt_toolkit for proper readline behavior."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import contextvars
import copy
import errno
import functools
import io
import json
import os
import re
import shlex
import signal
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import ai
from prompt_toolkit.application.current import get_app_session
from prompt_toolkit.key_binding import merge_key_bindings

from . import (
    agent_lock,
    agent_messages,
    attach,
    bench,
    clipimage,
    command_line,
    commit_cli,
    compaction,
    display as display_mod,
    dotenv,
    endpoint_uri,
    events,
    exline,
    fset,
    headless,
    home as _home,
    jail as _jail,
    keys as keys_mod,
    listings,
    login_defaults,
    logins,
    memory as M,
    messages as msgs,
    migrate_config,
    model_client,
    model_metadata,
    motd,
    output,
    pastes,
    paths as _paths,
    persona as P,
    prompt_commands,
    prompt_history,
    providers,
    repl_commands,
    repl_state,
    replcomplete,
    routing,
    run_assembly,
    runtime,
    sampling as sampling_mod,
    screen,
    session_picker,
    session_store,
    setcmd,
    settings,
    skills,
    snippets,
    stats,
    stream_transport,
    subagent_windows,
    supervisor,
    transcript as transcript_mod,
    usage as usage_mod,
    usage_display,
)
from .config import (
    Config,
    InvocationSources,
    derive_session_name,
    from_env,
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
from .session_catalog import (
    acquire_session,
    first_metadata,
    record_session_start,
)
from .tool_binaries import resolve_binary
from .toolkit import subagents, task_jobs


def _error_text(e: BaseException) -> str:
    """``e`` for a failure line: a provider's own one-line message as it is,
    a provider's status failure as its status and message, any other error
    after its kind; a `said()` entry inside either keeps its format
    (`msgs.of_error`)."""
    from . import network_display

    failure = network_display.describe_failure(e)
    if network_display.is_outage(failure):
        return msgs.NET_OUTAGE_ACTION.said(failure=failure)
    said = msgs.of_error(e)
    if isinstance(e, routing.ProviderNotLoggedInError):
        return said
    if isinstance(e, ai.ProviderStatusError) and (status := model_client.error_status(e)) is not None:
        return msgs.ERROR_OF_STATUS.said(status=status, error=model_client.error_message(e))
    return msgs.ERROR_OF_KIND.said(kind=type(e).__name__, error=said)


def _blocking_stdio() -> None:
    """Clear O_NONBLOCK on stdin, stdout and stderr. Every program in a
    terminal shares one open file description, and one that set it
    non-blocking (nvim and node do, through libuv) can leave it so; a write
    bigger than the terminal's buffer then stops part way with EAGAIN."""
    for fd in (0, 1, 2):
        with contextlib.suppress(OSError):
            if not os.get_blocking(fd):
                os.set_blocking(fd, True)


# --------------------------------------------------------------------------
# Runtime knobs: name -> (type, label, description)
# --------------------------------------------------------------------------

_MODE_PLACES = {"repl": 'in interactive chat', "-p": 'as a one-shot run'}


def _mode_switch_notice(started: str, previous: str, now: str) -> str:
    """The reminder for a turn in mode ``now`` after a turn in ``previous``, in
    a session whose first turn ran in ``started``."""
    if started == now:
        course = (f'started {_MODE_PLACES[started]}, moved {_MODE_PLACES[previous]}, and now continues {_MODE_PLACES[now]} again')
    else:
        course = f'started {_MODE_PLACES[started]} and now continues {_MODE_PLACES[now]}'
    return snippets.render("mode-switch", COURSE=course, PRESENCE=snippets.render(
        "mode-presence-chat" if now == "repl" else "mode-presence-prompt"))


def _note_mode_switch(cfg: Config, message: dict, mode: str) -> dict:
    """Record that this turn runs in ``mode`` ("repl" or "-p"). When the last
    turn ran in the other mode, this turn's user message carries the
    mode-switch reminder, which names the mode of the session's first turn."""
    if cfg.session_file == Path(os.devnull):
        return message
    previous = M.record_turn_mode(cfg.session_file, mode)
    if previous is None:
        return message
    started = M.first_turn_mode(cfg.session_file)
    if started not in _MODE_PLACES:
        started = previous
    return attach.with_note(message, _mode_switch_notice(started, previous, mode))


def _stamp_label(model: str | None, provider: str | None) -> str:
    return f"{provider}/{model}" if provider else str(model)


def _note_model_switch(cfg: Config, message: dict) -> dict:
    """When the session's newest reply came from a model other than the one
    ``cfg`` runs this turn on, this turn's user message carries the
    model-switch reminder. A stamp without a provider compares on the model id."""
    if cfg.session_file == Path(os.devnull):
        return message
    stamp = M.last_reply_stamp(cfg.session_file)
    if stamp is None:
        return message
    provider = stamp.get("provider") if isinstance(stamp.get("provider"), str) else None
    if stamp["model"] == cfg.model and (provider is None or provider == cfg.provider_id):
        return message
    return attach.with_note(message, snippets.render("model-changed",
        PREVIOUS=_stamp_label(stamp["model"], provider),
        CURRENT=_stamp_label(cfg.model, cfg.provider_id),
    ))


def _inherit_session_chat(session: str | None) -> bool:
    from .cli_options import current, restore_chat

    session = session or os.environ.get("JS_SESSION")
    if not session:
        return False
    try:
        path = resolve_session_file(session_store.folder_for(Path.cwd()), session)
        metadata = first_metadata(path)
    except (ValueError, OSError):
        return False
    if not metadata or not metadata.get("chat"):
        return False
    agent = current().agent
    if agent in (None, "none"):
        agent = metadata.get("agent")
        if agent == "none":
            agent = None
    restore_chat(agent)
    return True


def _from_env(
    session: str | None = None,
    *,
    save_session: bool = True,
    sources: InvocationSources = InvocationSources(),
    agent_id: str | None = None,
) -> Config:
    if _inherit_session_chat(session):
        from .cli_options import current
        agent_id = current().agent or (None if agent_id == "none" else agent_id)
    return from_env(
        save_session=save_session,
        sources=sources,
        session=session,
        agent_id=agent_id,
    )


def _append_turn(cfg: Config, message: dict) -> None:
    M.append_message(cfg.session_file, message)


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


def _session_hint_arg(cfg: Config) -> str:
    try:
        return cfg.session_file.relative_to(cfg.sessions_dir).with_suffix("").as_posix()
    except ValueError:
        return session_store.display_name(cfg.session_file)


_session_leases = threading.local()


def _session_scope(func):
    """Release every saved session activated during one CLI entry point."""

    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        leases = getattr(_session_leases, "items", None)
        if leases is None:
            leases = []
            _session_leases.items = leases
        start = len(leases)
        old_caller_key = getattr(_session_leases, "caller_key", None)
        old_start = getattr(_session_leases, "start", None)
        from .cli_options import current, invocation
        try:
            with invocation(current()):
                return func(*args, **kwargs)
        finally:
            while len(leases) > start:
                leases.pop().release()
            _session_leases.caller_key = old_caller_key
            _session_leases.start = old_start

    return wrapped


def _activate_saved_session(
    cfg: Config,
    *,
    caller_key: str | None = None,
    announce_generated: bool = False,
    model: str | None = None,
) -> None:
    if cfg.session_file == Path(os.devnull):
        return
    effective_key = caller_key if caller_key is not None else getattr(_session_leases, "caller_key", None)
    start = getattr(_session_leases, "start", None) or {}
    provider, bare = _recorded_model(model, cfg)
    from .cli_options import current
    options = current()
    record_session_start(
        cfg.session_file,
        cwd=Path.cwd(),
        caller_key=effective_key,
        # A --chat run with no --agent has no persona: its agent is "none".
        agent="none" if options.chat and options.agent is None else cfg.agent_id,
        model=bare,
        provider=provider,
        mode=start.get("mode"),
        command=start.get("command"),
        chat=options.chat,
    )
    lease = acquire_session(cfg.session_file)
    _session_leases.items.append(lease)
    if announce_generated:
        _announce_generated_session(cfg)


def _announce_generated_session(cfg: Config) -> None:
    payload = {
        "js_session": {
            "agent": cfg.agent_id,
            "session_id": _session_hint_arg(cfg),
            "path": str(cfg.session_file.resolve(strict=False)),
        }
    }
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=False), file=sys.stderr)


def _latest_session_name(agent_id: str) -> str | None:
    """The most recently started session file for *agent_id*, or None when there is none.

    A recorded session that no longer exists is treated as absent rather
    than resurrected, so --last never creates an empty session."""
    latest = session_store.read_latest(_paths.state_root() / agent_id)
    return str(latest) if latest is not None else None


def _recorded_model(model: str | None, cfg: Config) -> tuple[str | None, str | None]:
    """(provider, model id) a session start records for the --model value
    `model`, or for the configured model when there is none. A value without a
    known provider prefix records no provider: which one it lands on is
    resolved later."""
    if not model:
        return cfg.provider_id, cfg.model
    try:
        provider, bare = providers.parse_model_prefix(model)
    except ValueError:
        return None, model
    return provider, bare


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
                               ("--session", _session_hint_arg(cfg)))
    msgs.say(msgs.RESUME_HINT, command=shlex.join(["js", *resume]))


def _exchanges_are_the_prompt(args: argparse.Namespace, agent: str | None,
                              sources: InvocationSources) -> bool:
    """Whether a run with no -p runs the agent's NN-user.md / NN-agent.md
    exchanges as a one-shot: the agent has them and the session is fresh,
    so they are what the run sends."""
    try:
        cfg = _from_env(None, save_session=False, sources=sources, agent_id=agent)
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


def _live_optional_str_setting(live_settings: dict, path: tuple[str, str], default: str | None) -> str | None:
    missing = object()
    raw = settings.get_dotted(live_settings, path, missing)
    if raw is missing:
        return default
    if raw is None or isinstance(raw, str):
        return raw
    return default


def _live_bool_setting(live_settings: dict, path: tuple[str, ...], default: bool) -> bool:
    raw = settings.get_dotted(live_settings, path, default)
    return raw if isinstance(raw, bool) else default


def _debug_log_from_live_settings(cfg: Config, live_settings: dict) -> Path | None:
    enabled = _live_bool_setting(live_settings, ("DEBUG_LOG",), cfg.debug_log is not None)
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
    _sync_transcript_sink(cfg, state.settings, telemetry)


# ---------------------------------------------------------------------------
# Debug trace autolog. The full request trace (unclipped system prompt + full
# tool-schema JSON + the messages sent each call) is far too large for the
# terminal, so it is written ONLY to a file: always-on by default under
# logs/<agent>/<session>.log, plus any explicit --debug-file. Every IO error
# degrades to no logging — the log must never crash a turn.
# ---------------------------------------------------------------------------


class _DebugTraceSink:
    """Append-only tee that fans one text stream to N open files (the autolog and
    any --debug-file). Thread-safe. A file that fails a write is dropped and
    its error reported once, so logging never breaks a turn. Stands in for a
    stdout-like stream (write/flush/isatty) so it can also receive the
    redirected turn output."""

    def __init__(self, files: list, paths: list[Path]) -> None:
        self._files = files
        self.paths = paths
        self._lock = threading.Lock()

    def write(self, text: str) -> None:
        self.emit(output.event("debug_text", text=text))

    def emit(self, event: output.OutputEvent) -> None:
        text = dict(event.fields).get("text", "")
        if text:
            self._each_file(lambda f: (f.write(text), f.flush()))

    def flush(self) -> None:
        self._each_file(lambda f: f.flush())

    def _each_file(self, act) -> None:
        """Apply ``act`` to every open file. Failures are reported after the
        lock is released, since the report may be written back into this sink."""
        failures = []
        with self._lock:
            for f in list(self._files):
                try:
                    act(f)
                except Exception as exc:
                    failures.append((str(getattr(f, "name", "")), exc))
                    with contextlib.suppress(Exception):
                        f.close()
                    self._files = [g for g in self._files if g is not f]
        for name, exc in failures:
            output.failed_once(f"debug trace {name}", exc)

    def isatty(self) -> bool:
        return False

    def close(self) -> None:
        with self._lock:
            for f in self._files:
                try:
                    f.close()
                except Exception:
                    pass
            self._files = []


class _StdoutTee:
    """Write to the real stdout AND a sink — used by `-d` so the concise trace
    lines and the streamed answer show live on the terminal and also land in the
    log. The oversized request trace bypasses this and goes to the sink only."""

    def __init__(self, primary, sink: _DebugTraceSink) -> None:
        self._primary = primary
        self._sink = sink

    def write(self, text: str) -> None:
        try:
            self._primary.write(text)
        except transcript_mod.READER_GONE:
            pass
        self._sink.emit(output.event("debug_text", text=text))

    def write_unlogged(self, text: str) -> None:
        """Terminal-only material (a live region redrawn in place) skips the log."""
        try:
            write = getattr(self._primary, "write_unlogged", self._primary.write)
            write(text)
        except transcript_mod.READER_GONE:
            pass

    def flush(self) -> None:
        try:
            self._primary.flush()
        except transcript_mod.READER_GONE:
            pass
        self._sink.flush()

    def isatty(self) -> bool:
        try:
            return bool(self._primary.isatty())
        except Exception:
            return False


def _transcript_log_path(cfg: Config, live_settings: dict) -> Path | None:
    """Resolve the per-session visible transcript path, or None when disabled."""
    if not _live_bool_setting(
        live_settings, ("TRANSCRIPT_LOG",), getattr(cfg, "transcript_log", True)
    ):
        return None
    session = cfg.session_file
    if not session or session == Path(os.devnull):
        stem = f"{cfg.agent_id}-{os.getpid()}"
    else:
        stem = session.stem
    override = _live_optional_str_setting(
        live_settings, ("TRANSCRIPT_LOG_DIR",), getattr(cfg, "transcript_log_dir", None)
    )
    root = Path(override).expanduser() if override else (_paths.transcript_root() / cfg.agent_id)
    return root / f"{stem}.log"


def _open_transcript_log_sink(paths: list[Path]):
    return transcript_mod.open_transcript_sink(paths)


def _sync_transcript_sink(cfg: Config, live_settings: dict, telemetry: runtime.Telemetry) -> None:
    desired: list[Path] = []
    transcript_path = _transcript_log_path(cfg, live_settings)
    if transcript_path is not None:
        desired.append(transcript_path)
    old = getattr(telemetry, "transcript_log", None)
    if isinstance(old, transcript_mod.TranscriptLogSink) and old.paths == desired:
        old.settings = live_settings
        return
    telemetry.transcript_log = _open_transcript_log_sink(desired) if desired else None
    if isinstance(telemetry.transcript_log, transcript_mod.TranscriptLogSink):
        telemetry.transcript_log.settings = live_settings
    if isinstance(old, transcript_mod.TranscriptLogSink) and old is not telemetry.transcript_log:
        old.close()


def _transcript_sink(telemetry: runtime.Telemetry):
    sink = getattr(telemetry, "transcript_log", None)
    return sink if isinstance(sink, transcript_mod.TranscriptLogSink) else None


def _enter_transcript_stdio(stack: contextlib.ExitStack, telemetry: runtime.Telemetry) -> None:
    stack.enter_context(contextlib.redirect_stdout(
        transcript_mod.TranscriptTee(sys.stdout, lambda: _transcript_sink(telemetry))
    ))
    stack.enter_context(contextlib.redirect_stderr(
        transcript_mod.TranscriptTee(sys.stderr, lambda: _transcript_sink(telemetry))
    ))


@contextlib.contextmanager
def _transcript_stdio(telemetry: runtime.Telemetry):
    with contextlib.ExitStack() as stack:
        _enter_transcript_stdio(stack, telemetry)
        yield


def _mute_transcript_tee(sink):
    mute = getattr(sink, "mute_tee", None)
    return mute() if callable(mute) else contextlib.nullcontext()


def _debug_autolog_path(cfg: Config, live_settings: dict) -> Path | None:
    """Resolve the per-session autolog path, or None when autolog is off. Mirrors
    the session file's name, under logs/<agent>/."""
    if not _live_bool_setting(
        live_settings, ("REQUEST_LOG",), settings.knob_attr(cfg, "debug_autolog", "REQUEST_LOG")
    ):
        return None
    session = cfg.session_file
    if not session or session == Path(os.devnull):
        stem = f"{cfg.agent_id}-{os.getpid()}"  # one-shot with no persisted session
    else:
        stem = session.stem
    override = _live_optional_str_setting(
        live_settings, ("REQUEST_LOG_DIR",), getattr(cfg, "debug_autolog_dir", None)
    )
    root = Path(override).expanduser() if override else (_paths.logs_root() / cfg.agent_id)
    return root / f"{stem}.log"


def _open_debug_trace_sink(paths: list[Path]) -> _DebugTraceSink | None:
    """Open each path for append and return a tee sink, or None if none opened."""
    files: list = []
    opened: list[Path] = []
    for p in paths:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            files.append(open(p, "a", encoding="utf-8", errors="backslashreplace"))  # noqa: SIM115 — sink owns the lifetime
            opened.append(p)
        except OSError:
            pass  # a bad log path degrades to no logging, never a crash
    if not files:
        return None
    return _DebugTraceSink(files, opened)


def _sync_trace_sink(
    cfg: Config, live_settings: dict, telemetry: runtime.Telemetry, extra_paths: tuple[Path, ...] = ()
) -> None:
    """Attach/refresh ``telemetry.trace_sink`` for the always-on autolog. Reopens
    only when the target path set changes, so a long REPL session keeps one open
    handle; toggling ``REQUEST_LOG`` off closes it."""
    desired: list[Path] = []
    autolog = _debug_autolog_path(cfg, live_settings)
    if autolog is not None:
        desired.append(autolog)
    desired.extend(extra_paths)
    old = telemetry.trace_sink
    if isinstance(old, _DebugTraceSink) and old.paths == desired:
        return  # already open on exactly these targets
    telemetry.trace_sink = _open_debug_trace_sink(desired) if desired else None
    if isinstance(old, _DebugTraceSink) and old is not telemetry.trace_sink:
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


def _emit_repl_event(
    state: ReplState,
    telemetry: runtime.Telemetry,
    event: str,
    **payload,
) -> events.EventEmission:
    hook_table = state.events
    if hook_table is None:
        return events.EventEmission(event=event, payload=dict(payload), hooks=[])
    emission = hook_table.emit(event, **payload)
    for result in emission.results:
        if result.error:
            telemetry.event(
                "event_handler_error",
                event=emission.event,
                handler=result.hook.handler,
                error=result.error,
            )
    return emission


def _emit_session_event(state: ReplState, telemetry: runtime.Telemetry, cfg: Config, event: str) -> None:
    """session_start or session_end for the REPL session ``state`` runs."""
    _emit_repl_event(
        state, telemetry, event,
        session=str(cfg.session_file),
        agent=cfg.agent_id,
        model=state.model,
        cwd=str(runtime.T.STOCK_CONTEXT.cwd),
        messages=len(state.messages),
    )


async def _maybe_auto_compact_async(cfg: Config, state: ReplState) -> None:
    """The REPL's between-turn trigger: the policy lives in compaction, the
    printing lives here. The gate reads the live settings, so a /set made
    during the turn decides whether the turn's end compacts."""
    active_cfg = state.config_for_turn(cfg)
    if not compaction.get_bool(active_cfg, "COMPACTION_AUTO"):
        return
    turn_status = runtime.T.STOCK_CONTEXT.turn_status
    turn_status.compacting = True
    try:
        with stream_transport.net_role(msgs.COMPACTING_ROLE.words()):
            outcome = await compaction.maybe_auto_compact_async(
                active_cfg,
                state.auto_compact,
                runtime.T.STOCK_CONTEXT,
                state.system,
                state.messages,
                lambda: runtime._resolve_context_window(
                    active_cfg.model, active_cfg.provider_id, active_cfg.provider_base_url
                ),
                **compaction.compact_hooks(state, active_cfg),
            )
    finally:
        turn_status.compacting = False
    for notice in outcome.notices:
        msgs.say_said(notice)
    if outcome.result is not None:
        msgs.say(msgs.COMPACTION_DONE, result=outcome.result)


def _maybe_auto_compact(cfg: Config, state: ReplState) -> None:
    model_client.run_owning_loop(_maybe_auto_compact_async(cfg, state))


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


def _enter_session_dir(target: session_picker.SessionTarget) -> None:
    """Move to the directory the session started in, when it still exists."""
    if not target.cwd:
        return
    if not Path(target.cwd).is_dir():
        msgs.warn(msgs.SESSIONS_DIR_GONE, dir=target.cwd, cwd=Path.cwd())
        return
    os.chdir(target.cwd)
    runtime.T.STOCK_CONTEXT.cwd = Path.cwd()


# The session `/session` chose, run once the REPL has closed: {"target"}.
_pending_switch: dict[str, object] = {}


def _queue_switch(state: ReplState) -> None:
    target = state.switch_session
    if isinstance(target, session_picker.SessionTarget):
        _pending_switch.update(target=target)


def _exec_session(target: session_picker.SessionTarget) -> None:
    """Replace this process with js resuming `target` in its own directory
    and agent. Resume puts it back on its last-stamped model."""
    _enter_session_dir(target)
    argv = [sys.executable, "-m", "js", *command_line.argv(
        ("--session", str(target.path)),
        ("-a", target.agent or None),
        ("-C", target.cwd if target.jailed and target.cwd else None),
    )]
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, argv)


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


def _restore_workspace(cfg: Config) -> None:
    """Put a resumed session back where its last /cd, /add or /drop left it.
    The /add binds come back only under a jail with the same -C root; the
    working directory only where the jail shows it."""
    mark = M.last_workspace(cfg.session_file)
    if mark is None:
        return
    jail = _jail.active()
    if jail is not None and mark.get("root") == str(jail.root):
        for spec in mark.get("binds") or ():
            try:
                bind = _jail.parse_bind(spec)
            except ValueError:
                continue
            if bind.path.exists():
                jail.add(bind)
    cwd = Path(str(mark.get("cwd") or ""))
    setting = settings.knob(getattr(cfg, "settings", None), "JAIL_BIND")
    if cwd.is_absolute() and cwd.is_dir() and (jail is None or jail.bound(cwd, setting)):
        repl_commands.set_working_dir(cwd)


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


def _live_cfg_snapshot(cfg: Config, state: ReplState) -> Config:
    """The turn cfg from live state, never from a half-applied command."""
    with repl_state.LIVE_STATE_LOCK:
        return state.config_for_turn(cfg)


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


def _format_prompt_load_error(cfg, exc: Exception) -> str:
    """Turn a persona-load failure into an operator-legible line. A missing prompt
    directory means the named agent doesn't exist anywhere on the search path —
    say so plainly and point at the global agents dir where one is created,
    instead of leaking the raw 'prompts directory missing at <path>'."""
    if isinstance(exc, FileNotFoundError) and isinstance(cfg.prompts_dir, Path) and not cfg.prompts_dir.is_dir():
        agents_dir = _paths.global_agents_dir()
        agent_id = getattr(cfg, "agent_id", "?")
        return msgs.NO_SUCH_AGENT.said(agent=agent_id, agents_dir=agents_dir, new_dir=agents_dir / agent_id)
    return msgs.of_error(exc)


def _validate_cli_reasoning(reasoning: str) -> tuple[str | None, str | None]:
    """Check a raw ``--reasoning`` value through the same registry validation as
    ``set EFFORT`` and ``JS_REASONING``: any one word, kept as typed.
    Returns (value, error)."""
    spec = settings.SPEC_BY_KEY["EFFORT"]
    return settings.coerce_value(spec, reasoning)


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
            _seed, effort_error = _validate_cli_reasoning(str(value))
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


@_session_scope
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
                caller_key: str | None = None, announce_generated: bool | None = None,
                events: headless.JsonEvents | None = None,
                config_pins: bool = False,
                call_stats: list[dict] | None = None, opened: dict | None = None) -> int:
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
        reasoning_override, effort_error = _validate_cli_reasoning(reasoning)
        if effort_error is not None:
            msgs.warn(msgs.BAD_REASONING, value=reasoning, error=effort_error)
            return 2
    try:
        cfg = _from_env(session, save_session=save, sources=sources, agent_id=agent)
    except ValueError as e:
        msgs.warn(msgs.FAILED, error=e)
        return 2
    if announce_generated is None:
        announce_generated = save and session is None and os.environ.get("JS_SESSION") is None
    # A resumed session continues on the model, provider and reasoning of its
    # last stamp unless this run names them. Read before this start records its own.
    resumed = run_assembly.resume(cfg, model, reasoning_override)
    model = resumed.model
    if save:
        _activate_saved_session(
            cfg,
            caller_key=caller_key,
            announce_generated=announce_generated,
            model=model,
        )
    telemetry = runtime.Telemetry(debug_log=cfg.debug_log)
    _sync_transcript_sink(cfg, getattr(cfg, "settings", {}) or {}, telemetry)
    if save and show_continue and opened is not None:
        hint = _session_hint_arg(cfg)
        # `model` is read when the line prints: a resumed session's spec by then.
        opened["say"] = lambda: _say_continue(telemetry, hint, resume_prefix=resume_prefix,
                                              agent=agent, model=model, cfg=cfg, reasoning=reasoning_override)

    try:
        # A resumed session stays on its stamp; agent.yaml sets a fresh run.
        settled = run_assembly.settle(
            cfg, reasoning=reasoning_override, resumed=resumed, config_pins=config_pins,
            agent_reasoning=not config_pins and resumed.stamp is None and reasoning_override is None)
    except run_assembly.RunNotSettled as e:
        with _transcript_stdio(telemetry):
            msgs.warn(msgs.FAILED, error=_format_prompt_load_error(cfg, e.cause) if e.loading else e.cause)
        return 2
    # A resumed session's reasoning is this session's own choice, like /set,
    # even when it is what the model would pick: its turns and the children
    # they start run on it.
    cfg, reasoning_override = settled.config, settled.effort
    prompt_spec, system, recorded_system = settled.prompt_spec, settled.system, settled.recorded_system
    if resumed.unreachable is not None:
        with _transcript_stdio(telemetry):
            msgs.warn(msgs.RESUME_MODEL_FALLBACK, stamped=resumed.unreachable,
                      model=_stamp_label(cfg.model, cfg.provider_id))

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
            _append_turn(cfg, notice)
    _restore_workspace(cfg)
    resume_cfg = cfg
    if not save:
        cfg = replace(cfg, session_file=Path(os.devnull))
    before_len = len(messages)
    if events is not None:
        saved = cfg.session_file != Path(os.devnull)
        events.emit(
            "start", version=headless.SCHEMA_VERSION,
            id=_session_hint_arg(cfg) if saved else None,
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
            with _transcript_stdio(telemetry):
                msgs.warn(msgs.FAILED, error=e)
            return 2
        if save:
            user_message = _note_mode_switch(cfg, user_message, "-p")
        user_message = _note_model_switch(resume_cfg, user_message)
        if cut_off:
            user_message = attach.with_note(user_message, snippets.render('turn-cut-off'))
    elif save and cfg.session_file != Path(os.devnull):
        M.record_turn_mode(cfg.session_file, "-p")
    with _transcript_stdio(telemetry):
        hooks = _prompt_hooks(cfg, jsrc_paths(Path(getattr(cfg, "project_dir", None) or Path.cwd()), sources))
    run_state = _run_state(cfg, system, messages, hooks)
    if call_stats is None:
        call_stats = []
    turn_kwargs = settled.turn_kwargs(call_stats=call_stats, event_hooks=hooks)
    if events is not None:
        turn_kwargs["event_sink"] = events.runtime_event
        telemetry.sink = output.Fanout(output.sink_of(telemetry), headless.OutputEvents(events))
    if maxout is not None:
        turn_kwargs["max_output_override"] = maxout
    _t_wall = time.time()
    autolog = _debug_autolog_path(cfg, getattr(cfg, "settings", {}) or {})
    sink_paths: list[Path] = [p for p in (autolog,) if p is not None]
    if debug_file:
        sink_paths.append(Path(debug_file))
    trace_sink = _open_debug_trace_sink(sink_paths)
    telemetry.trace_sink = trace_sink
    turn_transcript = _transcript_sink(telemetry)

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
            _append_turn(cfg, user_message)
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
            _emit_repl_event(run_state, telemetry, "input", text=prompt, attachments=attachments)
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
    _emit_session_event(run_state, telemetry, cfg, "session_start")
    if opened is not None:
        opened["end"] = lambda: _emit_session_event(run_state, telemetry, cfg, "session_end")
    try:
        try:
            if debug:
                # Concise trace + streamed answer live on the terminal; the giant
                # request trace goes to the log only. Tee stdout so the log also
                # keeps the concise lines and the answer.
                with _transcript_stdio(telemetry):
                    _real_stdout = sys.stdout
                    stdout_ctx = (
                        contextlib.redirect_stdout(_StdoutTee(_real_stdout, trace_sink))
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
                    capture = _StdoutTee(sys.stderr, trace_sink) if trace_sink is not None else sys.stderr
                try:
                    with contextlib.redirect_stdout(capture):
                        turns()
                finally:
                    telemetry.transcript_log = visible_transcript
                    telemetry.display_factory = visible_display
        except (KeyboardInterrupt, asyncio.CancelledError):
            _emit_repl_event(run_state, telemetry, "cancel", reason="cancelled")
            with _transcript_stdio(telemetry):
                msgs.warn(msgs.TURN_INTERRUPTED)
            return 130
        except Exception as e:  # noqa: BLE001
            with _transcript_stdio(telemetry):
                msgs.warn(msgs.FAILED, error=_error_text(e))
            code = 2 if isinstance(e, snippets.SnippetError) else 1
            if events is not None and not events.errored:
                _emit_failure(events, _error_text(e), code)
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
                             "session": _session_hint_arg(cfg) if save else None,
                             "effort": reasoning_override if reasoning_override is not None else cfg.reasoning_effort,
                             "usage": (usage_mod.totals(cfg.session_file).as_dict() if save else events.usage.as_dict())}

    if not answer:
        with _transcript_stdio(telemetry):
            msgs.warn(msgs.NO_ANSWER)
        return 1
    # In debug, run_turn already streamed the answer live to stdout — a
    # second print here is the double-print bug. Only print when the
    # stream was suppressed (non-debug one-shot/pipe).
    with _transcript_stdio(telemetry):
        if not debug:
            if (sink := _transcript_sink(telemetry)) is not None:
                sink.emit(output.event("transcript_assistant", text=answer))
            if events is None:
                _blocking_stdio()
                with _mute_transcript_tee(_transcript_sink(telemetry)):
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
    with _transcript_stdio(telemetry):
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
                msgs.warn(msgs.FAILED, error=_error_text(exc))
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
        cfg = _from_env(session, save_session=False, sources=sources, agent_id=agent)
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
        msgs.warn(msgs.FAILED, error=_error_text(e))
        return 1
    if result not in skipped:
        msgs.say(msgs.COMPACTION_DONE, result=result)
    return 0

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


def _session_mcp_host(cfg, telemetry):
    if getattr(cfg, "mcp", None) is None or not getattr(cfg.mcp, "servers", ()):
        return None
    from .mcp.host import MCPHost

    return MCPHost(cfg.mcp, telemetry=telemetry, paranoid_mode=getattr(cfg, "paranoid_mode", "off"))


async def _close_session_mcp_host(state: ReplState) -> None:
    host = state.mcp_host
    if host is not None:
        await host.close()


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
        before_turn_lock = _live_bool_setting(
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
        after_turn_lock = _live_bool_setting(
            state.settings, ("SUBAGENT_MODEL_LOCK",), cfg.lock_subagent_model
        )
        if after_turn_lock != before_turn_lock:
            repl_state.sync_tool_registry_from_live_settings(cfg, state)
        _sync_telemetry_from_live_settings(cfg, state, telemetry)
        _persist_turn_messages(turn_cfg, state.messages)
        await _maybe_auto_compact_async(turn_cfg, state)
    except asyncio.CancelledError:
        _emit_repl_event(state, telemetry, "cancel", reason="cancelled")
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
        msgs.say(msgs.FAILED, error=_error_text(e))
        if isinstance(e, OSError) and e.errno == errno.ESTALE:
            state.messages = M.load_replay_messages(turn_cfg.session_file)
            return
        if _turn_has_progress(state.messages, user_message):
            _persist_turn_messages(turn_cfg, state.messages)
        else:
            _discard_unstarted_turn(cfg, state.messages, user_message)
            _requeue_notes(state, notes)
        M.append_mark(cfg.session_file, f"error: {msgs.words_of(_error_text(e))}")


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
    _emit_repl_event(
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
    user_message = _note_mode_switch(cfg, user_message, "repl")
    user_message = _note_model_switch(turn_cfg, user_message)
    notes = _take_pending_notes(state)
    user_message = _with_notes(user_message, notes)
    state.messages[:] = M.balance_orphaned_tool_calls(state.messages)
    state.messages.append(user_message)
    _append_turn(cfg, user_message)
    # Logged as the turn starts, not as the line was typed: a line queued
    # behind a running turn joins the transcript where the model sees it.
    if (sink := _transcript_sink(telemetry)) is not None:
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
        _emit_repl_event(state, telemetry, "input", text=steer_text, attachments=steer_attachments)
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
        if (sink := _transcript_sink(telemetry)) is not None:
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


def _is_turn_state_command(line: str, state: ReplState | None = None) -> bool:
    """Commands that clear/rotate/compact the live message list. Running one while
    a turn appends to state.messages races the single-writer discipline the turn
    queue enforces (empty the list mid-turn -> IndexError, dropped output, spurious
    rollback marks) — refuse until the active turn ends."""
    parsed = setcmd.split_command(line) if line.startswith("/") else None
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
        throbber = screen.throbber_frame(now)
        phase, output_tokens = screen.turn_centre(context.turn_status, now=now)
    return screen.status_line(
        width,
        clock=time.strftime("%H:%M"),
        provider=provider,
        model=model,
        context_tokens=int(getattr(context, "context_tokens", 0) or 0),
        phase=phase,
        output_tokens=output_tokens,
        throbber=throbber,
        agent_id=agent_id,
        session_short=screen.session_short(session_file) if session_file is not None else None,
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
    for kind, part in display_mod.resume_parts(state.messages, count if isinstance(count, int) else 0,
                                               width=width, markdown=display_mod.markdown_enabled(live)):
        if kind == "text":
            runtime.settle_tool_trace(replay)
            print(msgs.for_stream(part), end="", flush=True)
            continue
        name, args, result = part
        if result is None:
            # A call the session holds no result for is drawn as it was left, running.
            runtime.settle_tool_trace(replay)
            print(msgs.for_stream(display_mod.render_exchange(
                name, args, None, level=display_mod.tools_level(live), preview=display_mod.preview_lines(live),
                settings=live)), end="")
            continue
        opened = runtime._trace_call(replay, context, name, args)
        runtime._trace_result(replay, context, name, result, args=args, exchange=opened)
    runtime.settle_tool_trace(replay)


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
        msgs.warn(msgs.FAILED, error=_error_text(exc))


async def _repl_main(cfg, state, telemetry, history, completer, prompt_spec, banner: str = "") -> int:
    """The REPL: input, the active turn, and subagents all share ONE
    event loop. The screen has three regions (scrollback, status, input); turn
    output lands in the scrollback and never touches the input line. ^C cancels
    the active turn instead of killing the process; a line typed during a turn
    steers it or queues behind it, per STEER_MODE."""
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=32, thread_name_prefix="js-dispatch"))
    sup = supervisor.Supervisor(loop)
    supervisor.set_current(sup)
    queue: asyncio.Queue = asyncio.Queue()
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

    app, scrollback = screen.build_app(
        prompt=display_mod.prompt,
        history=history,
        completer=completer,
        on_line=on_line,
        on_interrupt=on_interrupt,
        on_eof=on_eof,
        turn_active=sup.turn_active,
        status=lambda width: _status_bar_line(cfg, state, sup.turn_active(), width),
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
    ticker = loop.create_task(screen.tick(app, lambda: sup.turn_active() or bool(sup.jobs("subagent"))))
    try:
        with screen.capture_stdio(loop, scrollback, app), _transcript_stdio(telemetry):
            try:
                stream_transport.install_sink(stream_transport.NetSink(
                    level=lambda: _live_level(state, "NET_DISPLAY"),
                    emit=lambda line: print(line, flush=True),
                ))
                if banner:
                    print(banner)
                _emit_session_event(state, telemetry, cfg, "session_start")
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
        stream_transport.install_sink(None)
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
        _emit_session_event(state, telemetry, cfg, "session_end")
        await _close_session_mcp_host(state)
    if snippet_failures:
        raise snippet_failures[0]
    return 0


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


def main(argv: list[str] | None = None) -> int:
    try:
        code = _main(argv)
    except snippets.SnippetError as exc:
        msgs.warn(msgs.FAILED, error=exc)
        code = 2
    pending = dict(_pending_switch)
    _pending_switch.clear()
    if pending:
        _exec_session(pending["target"])
    return code


@_session_scope
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


def _run_args(args: argparse.Namespace, dispatch_argv: list[str]) -> int:
    _session_leases.start = {"mode": None, "command": ["js", *dispatch_argv]}
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
        _enter_session_dir(target)
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
    if args.verify is not None:
        from .verify import verify
        errors = verify(args.verify or None)
        if args.json:
            listings.print_json({'ok': not errors, 'errors': errors})
        else:
            for error in errors:
                print(error, file=sys.stderr)
            print(f'Verification: {len(errors)} error(s).')
        return int(bool(errors))
    if args.list_sessions:
        ambiguous = any(
            (
                args.prompt is not None,
                args.session is not None,
                args.session_key is not None,
                args.agent is not None,
                args.commit,
                args.compact is not None,
                args.bench is not None,
                args.target is not None,
            )
        )
        if ambiguous:
            msgs.warn(msgs.LIST_EXCLUSIVE)
            return 2
        return listings.print_session_list(as_json=args.json)
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
    _session_leases.caller_key = args.session_key
    _inherit_session_chat(args.session)
    if sources.presets:
        # Resolve against the now-final cwd (after any -C). A name that matches no
        # file anywhere is almost certainly a typo — say so rather than no-op.
        _warn_unmatched_presets(sources)
    if args.login is not None:
        from . import login_cli
        return login_cli.main([args.login] if args.login else [])
    if args.logout:
        from . import login_cli
        return login_cli.main(["logout", args.logout])
    if args.list_agents:
        names = _paths.agent_names()
        return listings.print_json(names) if args.json else (print("\n".join(names)) or 0)
    if args.list_providers:
        return listings.print_json(listings.providers_json())
    if args.list_logins:
        return listings.print_json(listings.logins_json())
    if args.list_models is not None:
        try:
            cfg = None
            provider_arg = args.list_models or None
            if provider_arg is None:
                cfg = _from_env(args.session, save_session=False, sources=sources, agent_id=args.agent)
            return listings.print_json(listings.models_json(provider_arg, cfg)) if args.json else listings.print_model_list(provider_arg, cfg)
        except Exception as e:  # noqa: BLE001
            if args.json:
                listings.print_json({"error": f"{type(e).__name__}: {e}"})
            else:
                msgs.warn(msgs.FAILED, error=f"{type(e).__name__}: {e}")
            return 1
    if args.refresh_model_catalog:
        if not model_metadata.force_refresh_model_catalog():
            return 1
        if (
            args.prompt is None
            and not args.commit
            and args.compact is None
            and args.target is None
            and args.bench is None
            and not args.migrate_config
        ):
            return 0
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

    if args.bench:
        if selected_modes or args.agent:
            msgs.warn(msgs.BENCH_EXCLUSIVE)
            return 2
        return bench.run_bench(args.bench, model=args.model, reasoning=args.reasoning,
                          maxout=args.max_out, quiet=args.quiet, sources=sources,
                          stats_json=args.stats_json, stats_csv=args.stats_csv,
                          debug=args.debug)

    if args.compact:
        focus = _read_stdin_if_piped() if args.prompt == "-" else args.prompt or ""
        return _run_compact_offline(args.compact, agent=cli_agent, focus=focus, sources=sources,
                                    model=args.model)

    if args.migrate_config:
        return migrate_config.run_migrate_config()

    if args.commit:
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
        _session_leases.start["mode"] = "commit"
        return commit_cli.run_commit(args.target, model=args.model, debug=args.debug, debug_file=args.debug_file,
                           session=args.session, save=not args.no_save,
                           reasoning=args.reasoning, maxout=args.max_out,
                           extra_context=extra_context, sources=sources)

    try:
        automatic_agent = _invocation_agent(args, sources)
        automatic_dir = _paths.agent_dirs(automatic_agent)
    except (ValueError, OSError) as exc:
        msgs.warn(msgs.FAILED, error=exc)
        return 2
    from .cli_options import current
    if not current().replaces_prompt and not current().chat and P.has_benchmarks(automatic_dir):
        return bench.run_bench(
            automatic_agent, model=args.model, reasoning=args.reasoning,
            maxout=args.max_out, quiet=args.quiet, sources=sources,
            stats_json=args.stats_json, stats_csv=args.stats_csv, debug=args.debug,
        )

    if args.files and args.prompt is None and sys.stdin.isatty():
        msgs.warn(msgs.FILE_NEEDS_PROMPT)
        return 2

    exchanges_only = args.prompt is None and sys.stdin.isatty() and _exchanges_are_the_prompt(args, cli_agent, sources)
    if args.prompt is not None or not sys.stdin.isatty() or exchanges_only:
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
        _session_leases.start["mode"] = "-p" if args.prompt is not None or exchanges_only else "pipe"
        generated_session = (
            not args.no_save
            and args.session is None
            and os.environ.get("JS_SESSION") is None
        )
        run = _run_prompt_json if (args.json or args.json_stream) else _run_prompt
        result = run(prompt, model=args.model, debug=args.debug, debug_file=args.debug_file,
                     agent=args.agent, session=args.session, save=not args.no_save,
                     caller_key=args.session_key, announce_generated=generated_session,
                     reasoning=args.reasoning, maxout=args.max_out,
                     show_continue=not args.quiet,
                     sources=sources,
                     files=args.files,
                     stdin_attachment=stdin_attachment,
                     stats_json=args.stats_json, stats_csv=args.stats_csv)
        if args.no_save:
            msgs.warn(msgs.NOT_SAVED_NO_RESUME)
        return result

    if args.json or args.json_stream:
        msgs.warn(msgs.FAILED, error="--json/--json-stream requires -p, a pipe, an agent run, or --bench")
        return 2
    os.environ["JS_MODE"] = "repl"
    _session_leases.start["mode"] = "repl"
    try:
        cfg = _from_env(args.session, save_session=not args.no_save, sources=sources,
                        agent_id=cli_agent)
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
        msgs.warn(msgs.FAILED, error=_format_prompt_load_error(cfg, e.cause) if e.loading else e.cause)
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
                 model=_stamp_label(cfg.model, cfg.provider_id))

    keymap, key_errors = keys_mod.load(keys_mod.keys_file(cfg.settings))
    completer = replcomplete.JsCompleter(
        commands=lambda: _command_completions(state),
        setting_keys=[spec.key for spec in settings.REGISTRY],
        fset_names=fset.names(),
        names=lambda: sorted(set(providers.known_provider_ids()) | set(logins.load_logins())),
        spell=replcomplete.hunspell_suggest,
        bare_words=repl_state.QUIT_WORDS,
    )
    history = _repl_history(cfg)

    messages = M.load_replay_messages(cfg.session_file)
    cut_off = M.turn_cut_off(messages)
    _restore_workspace(cfg)
    if messages:
        notices.append(msgs.line_for(msgs.RESUMED, messages=msgs.plural(len(messages), "prior message")))
    elif args.session is not None:
        # Asked for a specific session and got nothing. Silence here reads as a
        # successful resume, so an empty one has to say so.
        notices.append(msgs.line_for(msgs.EMPTY_SESSION, path=cfg.session_file))
    if args.no_save:
        # -n with --session reads the named session and writes nothing back.
        cfg = replace(cfg, session_file=Path(os.devnull))
    _activate_saved_session(cfg, caller_key=args.session_key, model=args.model)
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
        _append_turn(cfg, notice)
        notices.append(msgs.line_for(msgs.PROMPT_CHANGED))

    live_settings = copy.deepcopy(cfg.settings) if isinstance(cfg.settings, dict) else {}
    if args.reasoning is not None:
        reasoning_seed, effort_error = _validate_cli_reasoning(args.reasoning)
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
    state = _repl_state(
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
    repl_commands.mark_saved(state)
    telemetry = runtime.Telemetry(debug_log=cfg.debug_log)
    state.mcp_host = _session_mcp_host(cfg, telemetry)
    # Attach the debug autolog sink before the first turn.
    _sync_telemetry_from_live_settings(cfg, state, telemetry)

    banner = _startup_banner(cfg, state, notices, [*rc_errors, *key_errors])
    transcript_stack = contextlib.ExitStack()
    # prompt_toolkit creates its output from sys.stdout on first use. The
    # screen and the pickers draw on the terminal, so that first use is here,
    # before stdout becomes the transcript tee.
    get_app_session().output
    _enter_transcript_stdio(transcript_stack, telemetry)
    try:
        code = model_client.run_owning_loop(
            _repl_main(cfg, state, telemetry, history, completer, prompt_spec, banner)
        )
    finally:
        transcript_stack.close()
    _print_resume_hint(cfg, state)
    _queue_switch(state)
    return code
if __name__ == "__main__":
    sys.exit(main())
