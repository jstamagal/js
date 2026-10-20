"""Interactive REPL. prompt_toolkit for proper readline behavior."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import errno
import functools
import io
import json
import os
import re
import shlex
import shutil
import sys
import signal
import threading
import textwrap
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import merge_key_bindings
from prompt_toolkit.shortcuts import CompleteStyle

from . import supervisor
from . import swarm as swarm_mod

from . import attach, clipimage, codex_auth
from . import agent_messages
from . import compaction
from . import rename
from . import motd
from . import display as display_mod
from . import dotenv
from . import endpoint_uri
from . import events
from . import fset
from . import exline
from . import hookexec
from . import login_defaults, logins
from . import memory as M
from . import messages as msgs
from . import model_client
from . import model_metadata
from . import persona as P
from . import pastes
from . import paranoid
from . import picker
from . import providers
from . import replcomplete
from . import runtime
from . import stats
from . import home as _home
from . import jail as _jail
from . import keys as keys_mod
from . import paths as _paths
from . import prompt_commands
from . import prompt_history
from . import transcript as transcript_mod
from .promptexpand import expand_prompt, session_variables
from . import screen
from . import session_query
from . import setcmd
from . import skills
from . import settings
from . import stream_transport
from . import turn_settings
from . import headless
from . import usage as usage_mod
from . import usage_display
from . import routing
from . import sampling as sampling_mod
from .sampling import Sampling
from .config import (
    Config,
    _norm_effort,
    derive_session_name,
    from_env,
    jsrc_paths,
    resolve_agent_id,
    run_settings,
    set_invocation,
    validate_agent_id,
    vision_enabled_for_model,
)
from . import session_store
from . import session_picker
from . import skill_picker
from . import agent_lock
from .session_catalog import (
    acquire_session,
    append_title,
    branch_session,
    catalog_sessions,
    last_stamp,
    record_model_switch,
    record_session_start,
    session_title,
)
from .tool_binaries import resolve_binary
from .toolkit import policy as tool_policy
from .toolkit import task_jobs
from .toolkit.registry import registry_for_roots
from .toolkit import ToolContext
from .prompt_text import load_catalog

_PROMPTS = load_catalog('cli')



class _ShortHelpAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        parser._print_message(msgs.SHORT_HELP.words(), sys.stdout)
        parser.exit()


def _error_text(e: BaseException) -> str:
    """``e`` for a failure line: a provider's own one-line message as it is,
    any other error after its kind; a `said()` entry inside either keeps its
    format (`msgs.of_error`)."""
    said = msgs.of_error(e)
    if isinstance(e, routing.ProviderNotLoggedInError):
        return said
    return msgs.ERROR_OF_KIND.said(kind=type(e).__name__, error=said)


def _registry_for(cfg) -> object:
    # Always build from THIS cfg's prompt roots. The old code returned a module-level
    # registry built with no roots on the normal path, and only passed cfg.prompt_roots
    # on the locked branch — so every agent in ~/.js/agents and .js/agents was
    # invisible as a tool unless the operator happened to lock subagent models.
    #
    # The `model_override` flag exposes the subagent `model` param on the task tool;
    # when the operator has locked subagent model selection it is dropped from both the
    # schema and the description rather than merely ignored.
    flags = () if getattr(cfg, "lock_subagent_model", False) else ("model_override",)
    return registry_for_roots(getattr(cfg, "prompt_roots", ()) or (), flags=flags).with_kernel(
        getattr(cfg, "kernel_enabled", False)
    )

# --------------------------------------------------------------------------
# Runtime knobs: name -> (type, label, description)
# --------------------------------------------------------------------------

_BOOL_WORDS_ON = {"on", "true", "yes", "1"}
_BOOL_WORDS_OFF = {"off", "false", "no", "0"}
_PROMPT_CHANGED_NOTICE = (
    _PROMPTS['default_js_reminder_the_agent_prompt_files']
)


_MODE_PLACES = {"repl": _PROMPTS['default_in_interactive_chat'], "-p": _PROMPTS['default_as_a_one_shot_run']}
_MODE_PRESENCE = {
    "repl": _PROMPTS['default_the_human_is_here_and_can'],
    "-p": _PROMPTS['default_the_human_is_not_here_and'],
}


def _mode_switch_notice(started: str, previous: str, now: str) -> str:
    """The reminder for a turn in mode ``now`` after a turn in ``previous``, in
    a session whose first turn ran in ``started``."""
    if started == now:
        course = (_PROMPTS['mode_switch_notice_started_moved_and_now_continues_again'].format(MODE_PLACES_started=_MODE_PLACES[started], MODE_PLACES_previous=_MODE_PLACES[previous], MODE_PLACES_now=_MODE_PLACES[now]))
    else:
        course = _PROMPTS['mode_switch_notice_started_and_now_continues'].format(MODE_PLACES_started=_MODE_PLACES[started], MODE_PLACES_now=_MODE_PLACES[now])
    return _PROMPTS['mode_switch_notice_js_reminder_this_conversation_js_reminder'].format(course=course, MODE_PRESENCE_now=_MODE_PRESENCE[now])


def _note_mode_switch(cfg: Config, bundle: attach.UserMessageBundle, mode: str) -> attach.UserMessageBundle:
    """Record that this turn runs in ``mode`` ("repl" or "-p"). When the last
    turn ran in the other mode, this turn's user message carries the
    mode-switch reminder, which names the mode of the session's first turn."""
    if cfg.session_file == Path(os.devnull):
        return bundle
    previous = M.record_turn_mode(cfg.session_file, mode)
    if previous is None:
        return bundle
    started = M.first_turn_mode(cfg.session_file)
    if started not in _MODE_PLACES:
        started = previous
    return attach.with_note(bundle, _mode_switch_notice(started, previous, mode))


_MODEL_SWITCH_NOTICE = (
    _PROMPTS['default_js_reminder_the_model_changed_from']
)
_CUT_OFF_NOTICE = (
    _PROMPTS['default_js_reminder_the_last_turn_in']
)


def _stamp_label(model: str | None, provider: str | None) -> str:
    return f"{provider}/{model}" if provider else str(model)


def _note_model_switch(cfg: Config, bundle: attach.UserMessageBundle) -> attach.UserMessageBundle:
    """When the session's newest reply came from a model other than the one
    ``cfg`` runs this turn on, this turn's user message carries the
    model-switch reminder. A stamp without a provider compares on the model id."""
    if cfg.session_file == Path(os.devnull):
        return bundle
    stamp = M.last_reply_stamp(cfg.session_file)
    if stamp is None:
        return bundle
    provider = stamp.get("provider") if isinstance(stamp.get("provider"), str) else None
    if stamp["model"] == cfg.model and (provider is None or provider == cfg.provider_id):
        return bundle
    return attach.with_note(bundle, _MODEL_SWITCH_NOTICE.format(
        previous=_stamp_label(stamp["model"], provider),
        current=_stamp_label(cfg.model, cfg.provider_id),
    ))


def _parse_bool(raw: str) -> bool | None:
    r = raw.lower().strip()
    if r in _BOOL_WORDS_ON:
        return True
    if r in _BOOL_WORDS_OFF:
        return False
    return None


def _from_env(
    session: str | None = None,
    *,
    save_session: bool = True,
    extras: list[str] | None = None,
    agent_id: str | None = None,
    ignore_local_config: bool = False,
    ignore_global_config: bool = False,
    presets: list[str] | None = None,
) -> Config:
    return from_env(
        save_session=save_session,
        extras=extras,
        session=session,
        agent_id=agent_id,
        ignore_local_config=ignore_local_config,
        ignore_global_config=ignore_global_config,
        presets=presets,
    )


def _append_turn(cfg: Config, message: dict) -> None:
    M.append_message(cfg.session_file, message)


def _replace_runtime_user_message(
    messages: list[dict],
    runtime_message: dict,
    history_message: dict,
    fallback_index: int,
) -> None:
    """Swap the provider-facing user message for its history form. When the two
    differ (attached files), the signed reasoning after it is dropped: it was
    produced with the provider-facing form in front of it."""
    index = None
    if 0 <= fallback_index < len(messages) and messages[fallback_index] == runtime_message:
        index = fallback_index
    else:
        for idx in range(len(messages) - 1, -1, -1):
            if messages[idx] == runtime_message:
                index = idx
                break
    if index is None:
        return
    messages[index] = history_message
    if history_message != runtime_message:
        M.drop_signed_reasoning(messages, index + 1)


def _session_stamp(cfg: Config, reasoning: str | None = None) -> dict:
    """The stamp for assistant messages a turn on `cfg` writes."""
    return M.stamp_for(getattr(cfg, "model", None), getattr(cfg, "provider_id", None),
                       reasoning if reasoning is not None else getattr(cfg, "reasoning_effort", None))


def _persist_turn_messages(cfg: Config, messages: list[dict], reasoning: str | None = None) -> None:
    """Persist the turn; `cfg` is the one the turn ran on, so its stamp is the turn's model."""
    M.persist_messages(cfg.session_file, messages, stamp=_session_stamp(cfg, reasoning))


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
        try:
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
    record_session_start(
        cfg.session_file,
        cwd=Path.cwd(),
        caller_key=effective_key,
        agent=cfg.agent_id,
        model=model or cfg.model,
        mode=start.get("mode"),
        command=start.get("command"),
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


def _resume_model_spec(stamp: dict, cfg: Config) -> str | None:
    """The --model value that puts a resumed session back on its stamped model,
    or None when the configuration already resolves to it.

    A stamp whose provider is neither the configured one nor logged in gives
    None: the session continues on the configured model. The stamped provider
    rides as a prefix when it parses as a known provider and is the configured
    one or has a saved login. A stamp without a provider, or one whose prefix
    does not parse, gives the model id alone."""
    model = stamp.get("model")
    if not isinstance(model, str) or not model:
        return None
    provider = stamp.get("provider") if isinstance(stamp.get("provider"), str) else None
    if model == cfg.model and (not provider or provider == cfg.provider_id):
        return None
    if _stamp_without_login(stamp, cfg) is not None:
        return None
    prefixed = f"{provider}/{model}"
    if (provider and providers.parse_model_prefix(prefixed) == (provider, model)
            and (provider == cfg.provider_id or routing._saved_login(provider) is not None)):
        return prefixed
    return None if model == cfg.model else model


def _stamp_without_login(stamp: dict, cfg: Config) -> str | None:
    """``provider/model`` of a stamp whose provider is neither the configured
    one nor logged in, else None. Such a session resumes on the configured model."""
    model = stamp.get("model")
    provider = stamp.get("provider")
    if not isinstance(model, str) or not model or not isinstance(provider, str) or not provider:
        return None
    if provider == cfg.provider_id or routing._saved_login(provider) is not None:
        return None
    return f"{provider}/{model}"


def _stamped_reasoning(stamp: dict) -> str | None:
    """The reasoning level of a stamp, when it is a valid one. A stamp
    stores a disabled effort as `none`, which is `off`."""
    raw = stamp.get("reasoning")
    if not isinstance(raw, str) or not raw:
        return None
    value, error = _validate_cli_reasoning("off" if _norm_effort(raw) == "none" else raw)
    return value if error is None else None


def _resumed_layer(stamp: dict | None, cli_reasoning: str | None) -> dict:
    """The resume layer a resumed session starts with: its stamped effort,
    the session's own choice, pinned even when it equals what the startup
    model would pick. A -r on the command line wins instead."""
    effort = _stamped_reasoning(stamp) if stamp is not None and cli_reasoning is None else None
    return {"EFFORT": effort} if effort else {}


def _resume_reasoning(stamp: dict, cfg: Config) -> str | None:
    """The reasoning level of the last stamp, when it is valid and differs from the configured one."""
    value = _stamped_reasoning(stamp)
    return value if value is not None and value != cfg.reasoning_effort else None


def _print_resume_hint(cfg: Config, state: dict) -> None:
    """Print the command that reopens this session.

    Without it the name is only recoverable by hunting through the sessions
    directory, and the session is effectively lost the moment the screen scrolls.
    The model is folded in so the resume comes back on the one in use."""
    if cfg.session_file == Path(os.devnull) or not state.get("messages"):
        return
    resume = "js"
    # A bare `js --session` runs the `DEFAULT_AGENT` setting, so the hint names the
    # agent unless both this run and that setting are on the js/jsrc default.
    stock = settings.default_value("DEFAULT_AGENT")
    if cfg.agent_id != stock or settings.knob(cfg.settings, "DEFAULT_AGENT") != stock:
        resume += f" --agent {shlex.quote(cfg.agent_id)}"
    model = state.get("model")
    if isinstance(model, str) and model:
        resume += f" --model {shlex.quote(model)}"
    resume += f" --session {shlex.quote(_session_hint_arg(cfg))}"
    msgs.say(msgs.RESUME_HINT, command=resume)


def _invocation_agent(args: argparse.Namespace, presets: list[str]) -> str:
    """The agent this invocation runs: --agent, --commit, then the `DEFAULT_AGENT`
    setting, resolved as `from_env` resolves it."""
    return resolve_agent_id(
        args.agent or ("commit" if args.commit else None),
        extras=args.extras,
        ignore_local_config=args.ignore_local,
        ignore_global_config=args.ignore_global,
        presets=presets,
    )


def _print_session_list(*, json_lines: bool) -> int:
    """Every session, newest first; the table shows times in local time as the
    picker does."""
    records = sorted(catalog_sessions(_paths.sessions_root()), key=lambda record: -record["mtime"])
    if json_lines:
        for record in records:
            print(json.dumps(record, separators=(",", ":"), ensure_ascii=False))
        return 0

    headings = tuple(msgs.LIST_HEADINGS.words().split())
    now = time.time()
    rows = []
    for record in records:
        identity = record["caller_key"]
        if record["job_id"] is not None:
            identity = str(record["job_id"]) if identity is None else f"{identity}/{record['job_id']}"
        rows.append(
            (
                record["agent"] or "-",
                record["name"],
                session_query.when_text(record["mtime"], now),
                str(record["size"]),
                str(record["user_turns"]),
                (msgs.LIST_YES if record["in_flight"] else msgs.LIST_NO).words(),
                record["cwd"] or "-",
                identity or "-",
            )
        )
    widths = [len(value) for value in headings]
    for row in rows:
        widths = [max(width, len(value)) for width, value in zip(widths, row, strict=True)]
    print("  ".join(value.ljust(width) for value, width in zip(headings, widths, strict=True)))
    for row in rows:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)))
    return 0


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


def _cfg_for_active_model(cfg: Config, state: dict) -> Config:
    model = state.get("model")
    if not (isinstance(model, str) and model):
        model = cfg.model
    try:
        route = routing.resolve_model_route(
            model,
            configured_provider_id=state.get("provider_id", cfg.provider_id),
            configured_base_url=state.get("provider_base_url", cfg.provider_base_url),
            configured_api_key=state.get("provider_api_key", cfg.provider_api_key),
            configured_headers=state.get("provider_headers", getattr(cfg, "provider_headers", {})),
            env=os.environ,
            explicit_model=True,
            use_saved_login=False,
        )
    except routing.ProviderNotLoggedInError:
        # A live `/model vendor/x` naming an un-logged-in provider with nothing
        # pinned can't resolve. This runs on every /set display and tool-registry
        # sync, not just turns, so it must never crash: carry the raw request with
        # no provider so the turn surfaces the friendly not-logged-in error at the
        # model boundary instead.
        route = routing.ModelRoute(model=model, provider_id=None, base_url=None, api_key=None)
    route_vision = vision_enabled_for_model(route.model, state.get("settings", cfg.settings))
    if (
        route.model != cfg.model
        or route.provider_id != cfg.provider_id
        or route.base_url != cfg.provider_base_url
        or route.api_key != cfg.provider_api_key
        or route.headers != getattr(cfg, "provider_headers", {})
        or route_vision != cfg.vision_enabled
    ):
        return replace(
            cfg,
            model=route.model,
            provider_id=route.provider_id,
            provider_base_url=route.base_url,
            provider_api_key=route.api_key,
            provider_headers=route.headers,
            vision_enabled=route_vision,
        )
    return cfg


def _live_optional_str_setting(live_settings: dict, path: tuple[str, str], default: str | None) -> str | None:
    missing = object()
    raw = settings.get_dotted(live_settings, path, missing)
    if raw is missing:
        return default
    if raw is None or isinstance(raw, str):
        return raw
    return default


def _live_reasoning_effort_setting(live_settings: dict, default: str | None) -> str | None:
    missing = object()
    raw = settings.get_dotted(live_settings, ("EFFORT",), missing)
    if raw is missing:
        return default
    if raw is None:
        return default
    if isinstance(raw, str):
        return _norm_effort(raw)
    return default


def _live_bool_setting(live_settings: dict, path: tuple[str, ...], default: bool) -> bool:
    raw = settings.get_dotted(live_settings, path, default)
    return raw if isinstance(raw, bool) else default


def _debug_log_from_live_settings(cfg: Config, live_settings: dict) -> Path | None:
    enabled = _live_bool_setting(live_settings, ("DEBUG_LOG",), cfg.debug_log is not None)
    if not enabled:
        return None
    return cfg.debug_log or (_paths.state_root() / cfg.agent_id / "debug.log")


def _sync_telemetry_from_live_settings(cfg: Config, state: dict, telemetry: runtime.Telemetry) -> None:
    debug_log = _debug_log_from_live_settings(cfg, state["settings"])
    if debug_log is not None:
        try:
            debug_log.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    telemetry.debug_log = debug_log
    _sync_trace_sink(cfg, state["settings"], telemetry)
    _sync_transcript_sink(cfg, state["settings"], telemetry)


# ---------------------------------------------------------------------------
# Debug trace autolog. The full request trace (unclipped system prompt + full
# tool-schema JSON + the messages sent each call) is far too large for the
# terminal, so it is written ONLY to a file: always-on by default under
# logs/<agent>/<session>.log, plus any explicit --debug-file. Every IO error
# degrades to no logging — the log must never crash a turn.
# ---------------------------------------------------------------------------


class _DebugTraceSink:
    """Append-only tee that fans one text stream to N open files (the autolog and
    any --debug-file). Thread-safe; swallows IO errors so logging never breaks a
    turn. Stands in for a stdout-like stream (write/flush/isatty) so it can also
    receive the redirected turn output."""

    def __init__(self, files: list, paths: list[Path]) -> None:
        self._files = files
        self.paths = paths
        self._lock = threading.Lock()

    def write(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            for f in self._files:
                try:
                    f.write(text)
                    f.flush()
                except Exception:
                    pass

    def flush(self) -> None:
        with self._lock:
            for f in self._files:
                try:
                    f.flush()
                except Exception:
                    pass

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
        except Exception:
            pass
        self._sink.write(text)

    def write_unlogged(self, text: str) -> None:
        """Terminal-only material (a live region redrawn in place) skips the log."""
        try:
            write = getattr(self._primary, "write_unlogged", self._primary.write)
            write(text)
        except Exception:
            pass

    def flush(self) -> None:
        try:
            self._primary.flush()
        except Exception:
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
        return
    telemetry.transcript_log = _open_transcript_log_sink(desired) if desired else None
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
            files.append(open(p, "a", encoding="utf-8"))  # noqa: SIM115 — sink owns the lifetime
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


def _cfg_for_live_state(cfg: Config, state: dict) -> Config:
    """The Config a turn started now runs with: the live settings, and the
    login-default settings resolved over every layer (login_defaults)."""
    active = _cfg_for_active_model(cfg, state)
    live_settings = state["settings"]
    agent_default, agent = login_defaults.agent_layers(state.get("prompt_spec"))
    return login_defaults.for_turn(replace(
        active,
        **turn_settings.project(live_settings, fallback=active),
        settings=live_settings,
        reasoning_effort=_live_reasoning_effort_setting(live_settings, active.reasoning_effort),
        layer_agent_default=agent_default,
        layer_agent=agent,
        layer_set=dict(state.get("live_set") or {}),
        layer_resume=dict(state.get("live_resume") or {}),
    ))

def _sync_tool_registry_from_live_settings(cfg: Config, state: dict) -> None:
    """Rebuild the live registry. The agent load already printed the
    unmatched-entry notices; a tools.yaml that no longer resolves keeps the
    current registry and prints one line."""
    selectors = state.get("tool_selectors", ())
    try:
        state["tool_registry"] = _registry_for(_cfg_for_live_state(cfg, state)).select(selectors, warn=False)
    except tool_policy.ToolPolicyError as exc:
        msgs.say(msgs.TOOL_SURFACE_KEPT, error=exc)


def _state_value(state: dict, key: str, default):
    return state[key] if key in state else default

# ---------------------------------------------------------------------------
# Effective /set view — annotate knobs whose live value bypasses the store
# ---------------------------------------------------------------------------

_SAMPLING_FIELDS: tuple[str, ...] = sampling_mod.SAMPLING_FIELDS


def _sampling_env_var(field: str, environ) -> str:
    """The env var actually setting ``field``, or ''."""
    for name in sampling_mod._ENV_KEYS.get(field, ()):
        if environ.get(name):
            return name
    return ""


def _live_settings_overlay(
    settings_store: dict,
    *,
    live_model: str | None,
    model_source: str | None,
    provider_id: str | None,
    provider_base_url: str | None,
    provider_api_key: str | None,
    saved_login,
    provider_default_base_url: str | None,
    eff_sampling: Sampling,
    env_sampling: Sampling,
    manifest_sampling: Sampling,
    environ,
) -> dict[str, setcmd.LiveValue]:
    """Build the per-knob live overlay for `show_lines_effective`.

    Pure: every effective value and its source is passed in, so the display and
    source-detection logic is testable without a Config/login store. Only knobs
    whose effective value differs from the settings store get an entry."""
    overlay: dict[str, setcmd.LiveValue] = {}

    def _mark(key: str, value, source: str) -> None:
        spec = settings.SPEC_BY_KEY[key]
        overlay[key] = setcmd.LiveValue(
            display=setcmd.render_value(spec, value, masked=setcmd.masks_secrets(settings_store)),
            source=source,
        )

    # MODEL — the --model flag / `/model` command never lands in the store.
    store_model = settings.get_dotted(settings_store, ("MODEL",))
    if live_model and model_source and live_model != store_model:
        _mark("MODEL", live_model, model_source)

    # provider.* — a saved login / provider default fills these outside the store.
    store_pid = settings.get_dotted(settings_store, ("PROVIDER",))
    if provider_id and provider_id != store_pid:
        _mark("PROVIDER", provider_id, msgs.SETTING_SOURCE_LOGIN.words(provider_id=provider_id) if saved_login is not None else msgs.SETTING_SOURCE_SESSION.words())

    store_base = settings.get_dotted(settings_store, ("BASE_URL",))
    if provider_base_url and provider_base_url != store_base:
        if saved_login is not None and saved_login.provider_base_url == provider_base_url:
            source = msgs.SETTING_SOURCE_LOGIN.words(provider_id=provider_id)
        elif provider_default_base_url and provider_default_base_url == provider_base_url:
            source = msgs.SETTING_SOURCE_PROVIDER_DEFAULT.words()
        else:
            source = msgs.SETTING_SOURCE_SESSION.words()
        _mark("BASE_URL", provider_base_url, source)

    store_key = settings.get_dotted(settings_store, ("API_KEY",))
    if provider_api_key and provider_api_key != store_key:
        if saved_login is not None and saved_login.provider_api_key == provider_api_key:
            source = msgs.SETTING_SOURCE_LOGIN.words(provider_id=provider_id)
        else:
            source = msgs.SETTING_SOURCE_SESSION.words()
        _mark("API_KEY", provider_api_key, source)

    # Sampling — js/sampling.py reads JS_TEMPERATURE/JS_TOP_K/... itself, and
    # the agent manifest is off-store too.
    for field in _SAMPLING_FIELDS:
        eff = getattr(eff_sampling, field)
        if eff is None:
            continue
        store_val = settings.get_dotted(settings_store, (field.upper(),))
        if store_val is not None and _same_number(store_val, eff):
            continue
        if getattr(env_sampling, field) is not None and _same_number(getattr(env_sampling, field), eff):
            env_var = _sampling_env_var(field, environ)
            source = msgs.SETTING_SOURCE_ENVIRONMENT_VARIABLE.words(env_var=env_var) if env_var else msgs.SETTING_SOURCE_ENVIRONMENT.words()
        elif getattr(manifest_sampling, field) is not None and _same_number(getattr(manifest_sampling, field), eff):
            source = msgs.SETTING_SOURCE_AGENT_SAMPLING.words()
        else:
            source = msgs.SETTING_SOURCE_SESSION.words()
        _mark(field.upper(), eff, source)

    return overlay


def _same_number(a, b) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return a == b


def _live_settings_overlay_for_state(cfg: Config, state: dict) -> dict[str, setcmd.LiveValue]:
    """Resolve the same route the next turn uses and diff it against the store."""
    turn_cfg = _cfg_for_live_state(cfg, state)
    provider_id = turn_cfg.provider_id
    saved_login = routing._saved_login(provider_id) if provider_id else None
    provider_def = providers.get_provider(provider_id) if provider_id else None
    provider_default_base_url = (
        providers.provider_base_url(provider_def, None, os.environ) if provider_def is not None else None
    )
    eff_sampling = login_defaults.sampling(turn_cfg)
    manifest_sampling = login_defaults.layer_sampling(turn_cfg.layer_agent)
    overlay = _live_settings_overlay(
        state["settings"],
        live_model=turn_cfg.model,
        model_source=state.get("model_source"),
        provider_id=provider_id,
        provider_base_url=turn_cfg.provider_base_url,
        provider_api_key=turn_cfg.provider_api_key,
        saved_login=saved_login,
        provider_default_base_url=provider_default_base_url,
        eff_sampling=eff_sampling,
        env_sampling=login_defaults.layer_sampling(turn_cfg.layer_env),
        manifest_sampling=manifest_sampling,
        environ=os.environ,
    )
    # The login-default settings show their resolved value and its layer.
    login = login_defaults.login_for(turn_cfg.provider_id, turn_cfg.provider_base_url)
    masked = setcmd.masks_secrets(state["settings"])
    values, sources = login_defaults.resolve(login_defaults.layers_of(turn_cfg))
    for key in login_defaults.KEYS:
        source = sources.get(key)
        if key in overlay and source in (None, "env", "flag"):
            continue  # the overlay already names the env var or flag
        if source in (None, "jsrc") and values.get(key) == settings.get_dotted(state["settings"], (key,)):
            continue
        label = (msgs.SETTING_SOURCE_LOGIN_LAYER.words(provider_id=login.provider_id, source=source) if source in ("server", "model") and login is not None
                 else source or msgs.SETTING_SOURCE_UNSET.words())
        overlay[key] = setcmd.LiveValue(
            display=setcmd.render_value(settings.SPEC_BY_KEY[key], values.get(key), masked=masked),
            source=label,
        )
    return overlay


def _effective_settings_snapshot(cfg: Config, state: dict) -> dict:
    """A copy of the live settings store with the off-store live values (flag
    model, login provider, env/manifest sampling) folded in — what `/save` writes
    so a reload reproduces the running configuration. A value the saved login's
    server or model defaults put in is not the live state's to save: the login
    keeps it, so the snapshot holds the jsrc-layer value, or none."""
    eff = copy.deepcopy(state["settings"]) if isinstance(state.get("settings"), dict) else {}
    turn_cfg = _cfg_for_live_state(cfg, state)
    live_model = state.get("model")
    if isinstance(live_model, str) and live_model:
        settings.set_dotted(eff, ("MODEL",), live_model)
    settings.set_dotted(eff, ("PROVIDER",), turn_cfg.provider_id)
    settings.set_dotted(eff, ("BASE_URL",), turn_cfg.provider_base_url)
    settings.set_dotted(eff, ("API_KEY",), turn_cfg.provider_api_key)
    # The login-default settings save their jsrc and `/set` layers only.
    saved = login_defaults.saved_jsrc_values(turn_cfg)
    for key in login_defaults.KEYS:
        if key in saved:
            eff[key] = saved[key]
        else:
            eff.pop(key, None)
    return eff



def _model_from_live_settings(live_settings: dict) -> str | None:
    raw = settings.get_dotted(live_settings, ("MODEL",), None)
    return raw if isinstance(raw, str) and raw else None


def _sync_model_from_live_settings(state: dict) -> None:
    model = _model_from_live_settings(state["settings"])
    if model is not None:
        state["model"] = model
        # The store now drives the model, so it is no longer a flag/command override.
        state["model_source"] = None


def _provider_from_live_settings(live_settings: dict) -> tuple[str | None, str | None, str | None]:
    def _value(path: tuple[str, str]) -> str | None:
        raw = settings.get_dotted(live_settings, path, None)
        return raw if isinstance(raw, str) and raw else None

    return (
        _value(("PROVIDER",)),
        _value(("BASE_URL",)),
        _value(("API_KEY",)),
    )


def _sync_provider_from_live_settings(state: dict, changed_keys: list[str]) -> None:
    provider_id, base_url, api_key = _provider_from_live_settings(state["settings"])
    changed = set(changed_keys)
    if "PROVIDER" in changed:
        if provider_id is None:
            state["provider_id"] = None
            state["provider_base_url"] = None
            state["provider_api_key"] = None
            state["provider_headers"] = {}
        else:
            _set_provider_state(state, provider_id)
    if "BASE_URL" in changed:
        state["provider_base_url"] = base_url
    if "API_KEY" in changed:
        state["provider_api_key"] = api_key


def _sync_provider_delta_from_live_settings(
    state: dict,
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
        _sync_provider_from_live_settings(state, changed_keys)


def _changed_model_key(keys: list[str]) -> bool:
    return "MODEL" in keys


def _changed_provider_key(keys: list[str]) -> bool:
    return any(key in {"PROVIDER", "BASE_URL", "API_KEY"} for key in keys)


def _changed_sampling_key(keys: list[str]) -> bool:
    return any(key.lower() in _SAMPLING_FIELDS for key in keys)


def _changed_lock_subagent_model_key(keys: list[str]) -> bool:
    return "SUBAGENT_MODEL_LOCK" in keys


def _emit_repl_event(
    state: dict,
    telemetry: runtime.Telemetry,
    event: str,
    **payload,
) -> events.EventEmission:
    hook_table = state.get("events")
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


def _compact_hooks(state: dict, cfg: Config) -> dict:
    """The ``emit`` and ``log_dir`` keywords a compaction call takes: notices
    print, other events go to the state's `on` handler table when it has one,
    and the flight log goes to js's flight log directory."""
    hook_table = state.get("events")
    return {"emit": compaction.printing(None if hook_table is None else hook_table.emit),
            "log_dir": compaction.flight_log_dir(cfg)}


def _emit_session_event(state: dict, telemetry: runtime.Telemetry, cfg: Config, event: str) -> None:
    """session_start or session_end for the REPL session ``state`` runs."""
    _emit_repl_event(
        state, telemetry, event,
        session=str(cfg.session_file),
        agent=cfg.agent_id,
        model=state.get("model"),
        cwd=str(runtime.T.STOCK_CONTEXT.cwd),
        messages=len(state.get("messages") or []),
    )


def _apply_saved_login_to_state(state: dict, provider_name: str) -> bool:
    provider_id = providers.normalize_provider_id(provider_name) or provider_name
    login = logins.load_logins().get(provider_id)
    if not login:
        return False
    state["provider_id"] = login.provider_id
    state["provider_headers"] = dict(login.provider_headers)
    state["provider_base_url"] = login.provider_base_url
    state["provider_api_key"] = login.provider_api_key
    return True


async def _maybe_auto_compact_async(cfg: Config, state: dict) -> None:
    """The REPL's between-turn trigger: the policy lives in compaction, the
    printing lives here."""
    if not compaction.get_bool(cfg, "COMPACTION_AUTO"):
        return
    active_cfg = _cfg_for_live_state(cfg, {**state, "settings": state.get("settings", cfg.settings)})
    turn_status = runtime.T.STOCK_CONTEXT.turn_status
    turn_status.compacting = True
    try:
        with stream_transport.net_role(msgs.COMPACTING_ROLE.words()):
            outcome = await compaction.maybe_auto_compact_async(
                active_cfg,
                state.setdefault("auto_compact", compaction.AutoCompactState()),
                runtime.T.STOCK_CONTEXT,
                state.get("system") or "",
                state.get("messages") or [],
                lambda: runtime._resolve_context_window(
                    active_cfg.model, active_cfg.provider_id, active_cfg.provider_base_url
                ),
                **_compact_hooks(state, active_cfg),
            )
    finally:
        turn_status.compacting = False
    for notice in outcome.notices:
        msgs.say_said(notice)
    if outcome.result is not None:
        msgs.say(msgs.COMPACTION_DONE, result=outcome.result)


def _maybe_auto_compact(cfg: Config, state: dict) -> None:
    asyncio.run(_maybe_auto_compact_async(cfg, state))


def _login_for_provider(provider_id: str | None, base_url: str | None, api_key: str | None) -> logins.Login:
    canonical_id = providers.normalize_provider_id(provider_id) or provider_id
    if not canonical_id:
        raise ValueError(msgs.NO_PROVIDER_SET.said())
    saved = logins.load_logins().get(canonical_id)
    if saved is not None:
        return saved
    provider_def = providers.provider_for_login(canonical_id)
    resolved_api_key = providers.provider_api_key(provider_def, api_key, os.environ)
    if provider_def.requires_api_key and not resolved_api_key:
        raise ValueError(msgs.PROVIDER_NEEDS_KEY.said(provider=canonical_id))
    return logins.Login(
        provider_id=canonical_id,
        sdk_provider_id=provider_def.effective_sdk_provider_id,
        provider_base_url=providers.provider_base_url(provider_def, base_url, os.environ),
        provider_api_key=resolved_api_key,
    )


def _models_for_provider(provider_id: str | None, base_url: str | None, api_key: str | None) -> list[str]:
    return logins.test_login(_login_for_provider(provider_id, base_url, api_key))


def _persist_default_model_id(model_id: str) -> tuple[Path | None, str | None]:
    config_path = _paths.global_config_file()
    try:
        settings.write_model_id(config_path, model_id)
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return config_path, None


def _set_provider_state(state: dict, provider_id: str) -> None:
    canonical_id = providers.normalize_provider_id(provider_id) or provider_id
    if _apply_saved_login_to_state(state, canonical_id):
        return
    provider_def = providers.provider_for_login(canonical_id)
    state["provider_id"] = canonical_id
    state["provider_headers"] = {}
    state["provider_base_url"] = providers.provider_base_url(provider_def, None, os.environ)
    state["provider_api_key"] = providers.provider_api_key(provider_def, None, os.environ)


def _set_model_via_route(state: dict, cfg: Config, model_value: str) -> None:
    """Apply `/model <id>` through the SAME saved-login gate startup routing
    uses for authoritative prefix switches. A `vendor/model` prefix may switch
    the direct endpoint only when `vendor` is a saved login; otherwise the
    current endpoint stays and the full id rides it."""
    configured_provider_id = _state_value(state, "provider_id", cfg.provider_id)
    configured_base_url = _state_value(state, "provider_base_url", cfg.provider_base_url)
    configured_api_key = _state_value(state, "provider_api_key", cfg.provider_api_key)
    configured_headers = _state_value(state, "provider_headers", getattr(cfg, "provider_headers", {}))
    parsed_provider_id, _parsed_model = providers.parse_model_prefix(model_value)
    prefix_login = routing._saved_login(parsed_provider_id) if parsed_provider_id is not None else None
    state["model_source"] = "/model command"
    if parsed_provider_id is not None and prefix_login is None:
        state["model"] = model_value
        msgs.say(msgs.MODEL_SET, model=providers.qualified_model_id(configured_provider_id, model_value))
        return

    route = routing.resolve_model_route(
        model_value,
        configured_provider_id=parsed_provider_id or configured_provider_id,
        configured_base_url=None if parsed_provider_id else configured_base_url,
        configured_api_key=None if parsed_provider_id else configured_api_key,
        configured_headers={} if parsed_provider_id else configured_headers,
        env=os.environ,
        explicit_model=True,
        prefix_overrides_provider=parsed_provider_id is not None,
        use_saved_login=parsed_provider_id is not None,
    )
    state["model"] = route.model
    state["provider_id"] = route.provider_id
    state["provider_base_url"] = route.base_url
    state["provider_api_key"] = route.api_key
    state["provider_headers"] = dict(route.headers)
    msgs.say(msgs.MODEL_SET, model=providers.qualified_model_id(route.provider_id, route.model))


def _pick_model_into_state(state: dict, cfg: Config) -> None:
    selected = picker.pick_model(
        provider_id=_state_value(state, "provider_id", cfg.provider_id),
        provider_base_url=_state_value(state, "provider_base_url", cfg.provider_base_url),
        provider_api_key=_state_value(state, "provider_api_key", cfg.provider_api_key),
        model=_state_value(state, "model", cfg.model),
    )
    if not selected:
        return
    state["provider_id"] = selected["provider_id"]
    state["provider_base_url"] = selected.get("provider_base_url")
    state["provider_api_key"] = selected.get("provider_api_key")
    state["provider_headers"] = dict(selected.get("provider_headers") or {})
    state["model"] = selected["model"]
    default_model_id = providers.qualified_model_id(selected.get("provider_id"), selected["model"])
    if isinstance(state.get("settings"), dict):
        settings.set_dotted(state["settings"], ("MODEL",), default_model_id)
    state["model_source"] = None  # the pick was persisted into the store

    _saved_path, save_error = _persist_default_model_id(default_model_id)
    chosen = default_model_id
    if save_error:
        msgs.say(msgs.MODEL_NOT_SAVED_AS_DEFAULT, model=chosen, error=save_error)
    else:
        msgs.say(msgs.MODEL_SAVED_AS_DEFAULT, model=chosen)


def _cmd_models(arg: str, state: dict, cfg: Config) -> str | None:
    max_models = 50
    if arg:
        try:
            max_models = int(arg.split()[0])
        except ValueError:
            return msgs.MODELS_LIMIT_NOT_A_NUMBER.said(value=arg.split()[0])
    provider_id = _state_value(state, "provider_id", cfg.provider_id)
    provider_base_url = _state_value(state, "provider_base_url", cfg.provider_base_url)
    provider_api_key = _state_value(state, "provider_api_key", cfg.provider_api_key)
    if not provider_id:
        return msgs.NO_PROVIDER.said()
    try:
        model_ids = _models_for_provider(provider_id, provider_base_url, provider_api_key)
    except Exception as e:  # noqa: BLE001
        return msgs.MODELS_NOT_LISTED.said(error=f"{type(e).__name__}: {e}")
    for mid in model_ids[:max_models]:
        msgs.say(msgs.MODEL_ROW, model=mid)
    if len(model_ids) > max_models:
        msgs.say(msgs.MORE_ROWS, count=len(model_ids) - max_models)
    return None


def _cmd_provider(arg: str, state: dict, cfg: Config) -> str | None:
    if not arg:
        cur = _state_value(state, "provider_id", cfg.provider_id)
        if cur:
            msgs.say(msgs.PROVIDER_IS, provider=cur)
        else:
            msgs.say(msgs.PROVIDER_UNSET)
        return None
    name = arg.split()[0]
    _set_provider_state(state, name)
    msgs.say(msgs.PROVIDER_SET, provider=providers.normalize_provider_id(name) or name)
    return None


def _cmd_baseurl(arg: str, state: dict, cfg: Config) -> str | None:
    state["provider_base_url"] = arg.split()[0] if arg else None
    msgs.say(msgs.BASE_URL_SET if arg else msgs.BASE_URL_CLEARED)
    return None


def _cmd_apikey(arg: str, state: dict, cfg: Config) -> str | None:
    state["provider_api_key"] = arg.split()[0] if arg else None
    msgs.say(msgs.API_KEY_SET if arg else msgs.API_KEY_CLEARED)
    return None


def _cmd_login(arg: str, state: dict, cfg: Config) -> str | None:
    parts = arg.split()
    if not parts:
        return msgs.USAGE.said(usage="/login <name> [apikey] [baseurl] [provider]")
    name = parts[0]
    key, url, ptype = (parts[1:] + [None, None, None])[:3]
    if key is None and url is None and ptype is None:
        # bare `/login <name>`: load a saved login (or fall back to provider defaults)
        _set_provider_state(state, name)
        msgs.say(msgs.PROVIDER_SET, provider=providers.normalize_provider_id(name) or name)
        return None
    # explicit creds: build + persist a login under <name>, then activate it.
    # provider type = explicit 4th arg, else inferred when <name> is itself a known provider.
    prov = providers.get_provider(ptype or name)
    if ptype is None and prov is None:
        return msgs.LOGIN_UNKNOWN_PROVIDER.said(name=name)
    sdk = prov.effective_sdk_provider_id if prov is not None else ptype
    canonical = providers.normalize_provider_id(name) or name
    try:
        logins.save_login(logins.Login(
            provider_id=canonical,
            sdk_provider_id=sdk,
            provider_base_url=url,
            provider_api_key=key,
        ))
    except (logins.LoginsCorruptError, logins.LoginStoreBusyError, OSError) as exc:
        return msgs.LOGIN_NOT_SAVED.said(error=f"{type(exc).__name__}: {exc}")
    _apply_saved_login_to_state(state, canonical)
    msgs.say(msgs.LOGIN_SAVED, provider=canonical)
    return None


def _cmd_logout(arg: str, state: dict, cfg: Config) -> str | None:
    state["provider_id"] = None
    state["provider_base_url"] = None
    state["provider_api_key"] = None
    state["provider_headers"] = {}
    msgs.say(msgs.PROVIDER_CLEARED)
    return None


def _force_refresh_model_catalog() -> bool:
    try:
        status = model_metadata.ensure_fresh_catalog(force=True)
    except Exception as e:  # noqa: BLE001
        msgs.say(msgs.CATALOG_REFRESH_FAILED, error=f"{type(e).__name__}: {e}")
        return False
    checked_at = status.refreshed_at or status.generated_at
    stamp = checked_at.isoformat(timespec="seconds") if checked_at else "unknown"
    models = "?" if status.model_count is None else str(status.model_count)
    providers_count = "?" if status.provider_count is None else str(status.provider_count)
    msgs.say(msgs.CATALOG_REFRESHED, stamp=stamp, providers=providers_count, models=models, path=status.db_path)
    return True


def _flatten_toml(data: dict, prefix: str = "") -> list[tuple[str, str]]:
    """Flatten a nested TOML dict into (dotted_key, scalar_str) set-line pairs.
    Tables recurse; lists / arrays-of-tables become a JSON value."""
    out: list[tuple[str, str]] = []
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            out.extend(_flatten_toml(value, prefix=f"{dotted}."))
        elif isinstance(value, bool):
            out.append((dotted, "on" if value else "off"))
        elif isinstance(value, (list, tuple)):
            out.append((dotted, json.dumps(value)))
        else:
            out.append((dotted, str(value)))
    return out


def _run_migrate_config() -> int:
    """One-shot: convert a legacy config.toml into a jsrc set-script, then exit."""
    import tomllib

    legacy = _paths.legacy_global_config_file()
    target = _paths.global_config_file()
    if not legacy.exists():
        msgs.warn(msgs.MIGRATE_NO_LEGACY, path=legacy)
        return 1
    if target.exists():
        msgs.warn(msgs.MIGRATE_TARGET_EXISTS, path=target)
        return 1
    with legacy.open("rb") as fp:
        data = tomllib.load(fp)
    lines = ["# migrated from config.toml by `js --migrate-config`", ""]
    deleted = []
    for key, value in _flatten_toml(data):
        if rename.is_deleted(key):
            deleted.append(key)
            lines.append(f"# deleted: set {key} {value}")
        else:
            new = rename.renamed_key(key) or key
            verb = "fset" if rename.is_fset(new) else "set"
            lines.append(f"{verb} {new} {settings.jsrc_text(new, rename.renamed_value(key, str(value)))}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    msgs.say(msgs.MIGRATE_WROTE, path=target, legacy=legacy)
    for key in deleted:
        msgs.warn(msgs.MIGRATE_DELETED, key=key)
    return 1 if deleted else 0


def _append_closing_note(cfg: Config, state: dict, reminder: str) -> None:
    """Fold a closing note into the conversation without breaking role alternation.

    After a completed turn the note is simply the next user message. When the last
    message is already a user turn it is rewritten to carry the note instead, since
    two user messages in a row are rejected outright by some providers and silently
    mangled by chat templates that assume alternation."""
    messages = state["messages"]
    if not messages or messages[-1].get("role") != "user":
        message = {"role": "user", "content": reminder}
        messages.append(message)
        _append_turn(cfg, message)
        return

    last_user = messages[-1]
    content = last_user.get("content")
    if isinstance(content, str):
        last_user["content"] = f"{content}\n\n{reminder}"
    elif isinstance(content, list):
        last_user["content"] = [*content, {"type": "text", "text": reminder}]
    else:
        last_user["content"] = reminder
    # Append-only history cannot edit in place: drop the stale copy, write the
    # rewritten one, so a reload sees exactly what memory holds.
    M.append_mark(cfg.session_file, f"rollback_to:{len(messages) - 1}")
    _append_turn(cfg, last_user)


def _split_compact_model(arg: str) -> tuple[str | None, str, bool]:
    """Pull a leading ``-m <model>`` (or ``--model``) off a /compact argument.

    Returns ``(model, focus, ok)``; ``ok`` is False when the flag carries no
    value. The flag is leading-only because focus is free text.
    """
    tokens = arg.split(maxsplit=2)
    if tokens and tokens[0] in ("-m", "--model"):
        if len(tokens) < 2:
            return None, "", False
        return tokens[1], tokens[2].strip() if len(tokens) > 2 else "", True
    return None, arg.strip(), True


def _print_tool_chain(state: dict, cfg: Config) -> None:
    """Every tool with its resolved state and the chain entry that decided it."""
    full = _registry_for(_cfg_for_live_state(cfg, state))
    try:
        config = tool_policy.load_tools_config()
        rules = tool_policy.expand(state.get("tool_selectors", ()), config, f"agent {cfg.agent_id!r}")
    except tool_policy.ToolPolicyError as exc:
        msgs.say(msgs.FAILED, error=exc)
        return
    for row in tool_policy.render_table(tool_policy.resolve(full.tools, rules), config.bans):
        print(row)


# --------------------------------------------------------------------------
# The command table. Every REPL command, rc line, /load line, `on` handler
# and ex command dispatches through COMMANDS; completion and /help read it.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Command:
    """One command: ``run(arg, state, cfg)`` returns an error string or None."""

    run: Callable[[str, dict, Config], str | None]
    usage: str
    doc: msgs.Message
    complete: str | None = None  # argument completion source (see replcomplete)
    turn_state: bool = False     # clears/rotates/compacts the live message list


_MAX_ALIAS_DEPTH = 16


def _apply_settings_result(result: setcmd.CommandResult, state: dict, cfg: Config) -> str | None:
    """Print a settings verb's output and carry changed keys into live state."""
    keys = result.changed_keys
    if _changed_model_key(keys):
        session = getattr(cfg, "session_file", None)
        recorded = session is not None and session != Path(os.devnull)
        previous = _live_stamp(cfg, state) if recorded else None
        _sync_model_from_live_settings(state)
        if recorded:
            stamp = _live_stamp(cfg, state)
            if stamp != previous:
                record_model_switch(session, stamp=stamp, previous=previous)
    if _changed_provider_key(keys):
        _sync_provider_from_live_settings(state, keys)
    if _changed_lock_subagent_model_key(keys) or "PYTHON_KERNEL" in keys:
        _sync_tool_registry_from_live_settings(cfg, state)
    if "BANNER" in keys:
        msgs.set_banner(str(settings.knob(state["settings"], "BANNER")))
    for out in result.lines:
        print(out)
    if result.error:
        return result.error
    if any(key.startswith("CONTEXT_SIZE") for key in keys):
        runtime.install_context_window_overrides(_cfg_for_live_state(cfg, state))
    return None


def _show_settings(state: dict, cfg: Config, key: str | None) -> str | None:
    """Render the values the NEXT turn uses, annotating knobs whose live source
    bypasses the store."""
    overlay = _live_settings_overlay_for_state(cfg, state)
    result = setcmd.show_lines_effective(state["settings"], overlay, key)
    for out in result.lines:
        print(out)
    return result.error


def _cmd_set(arg: str, state: dict, cfg: Config) -> str | None:
    parts = arg.split(maxsplit=1)
    if len(parts) < 2 and not (parts and parts[0].startswith("-") and len(parts[0]) > 1):
        return _show_settings(state, cfg, parts[0] if parts else None)
    # `set -key` goes back to the value the session started with.
    baseline = cfg.settings if isinstance(getattr(cfg, "settings", None), dict) else None
    result = setcmd.set_command(state["settings"], arg, baseline)
    if state.get("rc_replay"):
        return _replayed(result)
    if result.error is None:
        unset = parts[0].startswith("-")
        _note_live_set(state, parts[0][1:] if unset else parts[0], unset=unset)
    return _apply_settings_result(result, state, cfg)


def _note_live_set(state: dict, name: str, *, unset: bool) -> None:
    """Record one `set` (a typed `/set`, a script or `on` handler line) in the
    `/set` layer of the login-default settings, as it runs: a new dict per
    change. `set -KEY` takes the key out of the layer; any value, even one
    equal to what the session already has, goes in."""
    spec = settings.spec_for(name)
    if spec is None or spec.key not in login_defaults.KEYS:
        return
    live = dict(state.get("live_set") or {})
    value = None if unset else settings.get_dotted(state["settings"], spec.path)
    if value is None:
        live.pop(spec.key, None)
    else:
        live[spec.key] = value
    state["live_set"] = live
    # A set or unset of the key ends a resumed session's value for it.
    if spec.key in (state.get("live_resume") or {}):
        state["live_resume"] = {k: v for k, v in state["live_resume"].items() if k != spec.key}


def _replayed(result: setcmd.CommandResult) -> str | None:
    """A settings verb's output in the startup run of the jsrc files: printed,
    its keys left in the jsrc layer. `_run_rc_commands` carries what the run
    changed into live state when it ends."""
    for out in result.lines:
        print(out)
    return result.error


def _cmd_fset(arg: str, state: dict, cfg: Config) -> str | None:
    baseline = cfg.settings if isinstance(getattr(cfg, "settings", None), dict) else None
    result = setcmd.fset_command(state["settings"], arg, baseline)
    return _replayed(result) if state.get("rc_replay") else _apply_settings_result(result, state, cfg)


def _cmd_show(arg: str, state: dict, cfg: Config) -> str | None:
    return _show_settings(state, cfg, arg.split()[0] if arg else None)


def _cmd_on(arg: str, state: dict, cfg: Config) -> str | None:
    result = setcmd.on_command(state.setdefault("events", events.EventHooks()), arg)
    for out in result.lines:
        print(out)
    return result.error


def _cmd_alias(arg: str, state: dict, cfg: Config) -> str | None:
    """`alias` lists, `alias name` shows, `alias name command` defines,
    `alias -name` removes. `$*` in the body is the alias's arguments; a body
    without `$*` gets them appended."""
    aliases = state.setdefault("aliases", {})
    parts = arg.split(maxsplit=1)
    if not parts:
        for name, body in aliases.items():
            msgs.say(msgs.ALIAS_ROW, name=name, body=body)
        if not aliases:
            msgs.say(msgs.NO_ALIASES)
        return None
    name = parts[0].lower().lstrip("/")
    if name.startswith("-") and len(name) > 1:
        if aliases.pop(name[1:], None) is None:
            return msgs.NO_ALIAS.said(name=name[1:])
        return None
    if len(parts) == 1:
        if name not in aliases:
            return msgs.NO_ALIAS.said(name=name)
        msgs.say(msgs.ALIAS_ROW, name=name, body=aliases[name])
        return None
    if name in _commands(state):
        return msgs.ALIAS_IS_COMMAND.said(name=name)
    aliases[name] = parts[1].strip()
    msgs.say(msgs.ALIAS_ROW, name=name, body=aliases[name])
    return None


def _cmd_load(arg: str, state: dict, cfg: Config) -> str | None:
    """Run each line of a file through the command table, in order. The first
    error stops the file and names the line."""
    stack = state.setdefault("load_stack", [])
    base = stack[-1].parent if stack else Path(getattr(cfg, "project_dir", None) or Path.cwd())
    path, error = setcmd.load_path(arg, base)
    if error:
        return error
    if len(stack) >= setcmd.MAX_LOAD_DEPTH:
        return msgs.LOAD_TOO_DEEP.said()
    if path in stack:
        return msgs.LOAD_CYCLE.said(path=path)
    if not path.is_file():
        return msgs.LOAD_NOT_FOUND.said(path=path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as e:
        return msgs.LOAD_UNREADABLE.said(path=path, error=f"{type(e).__name__}: {e}")
    stack.append(path)
    try:
        for lineno, raw in enumerate(lines, 1):
            error = _run_script_line(raw, state, cfg)
            if error:
                return msgs.SCRIPT_LINE_FAILED.said(path=path, lineno=lineno, error=error)
    finally:
        stack.pop()
    msgs.say(msgs.LOADED, path=path)
    return None


def _cmd_save(arg: str, state: dict, cfg: Config) -> str | None:
    """Write the changed settings, `on` handlers and aliases into the global
    jsrc, editing it in place. `/save server` writes this session's model
    settings into the saved login as the active model's defaults."""
    if arg.strip().lower() == "server":
        return _save_server(cfg, state)
    eff = _effective_settings_snapshot(cfg, state)
    hooks = state.get("events")
    extra = setcmd.event_lines(hooks) if hooks is not None else []
    extra += [f"alias {name} {body}" for name, body in (state.get("aliases") or {}).items()]
    path = _paths.global_config_file()
    try:
        count = settings.save_settings_to_jsrc(path, eff, extra_lines=extra)
    except OSError as exc:
        return msgs.SAVE_FAILED.said(error=f"{type(exc).__name__}: {exc}")
    msgs.say(msgs.SAVED, lines=msgs.plural(count, "line"), path=path)
    return None


def _save_server(cfg: Config, state: dict) -> str | None:
    """Write the session's server and model layer values for the active
    model into the login the session routes to."""
    turn = _cfg_for_live_state(cfg, state)
    login = login_defaults.login_for(turn.provider_id, turn.provider_base_url)
    if login is None:
        return msgs.SAVE_NO_SERVER.said(provider=turn.provider_id or turn.provider_base_url or turn.model)
    values = login_defaults.saved_layer(turn)
    def change(current):
        model_defaults = dict(current.model_defaults or {})
        # Merged into what the model already has: a key another layer decides for
        # this run keeps its stored value.
        model_defaults[turn.model] = {**model_defaults.get(turn.model, {}), **values}
        return replace(current, model_defaults={k: v for k, v in model_defaults.items() if v})

    try:
        if logins.update_login(login.provider_id, change) is None:
            return msgs.SAVE_NO_SERVER.said(provider=turn.provider_id or turn.provider_base_url or turn.model)
    except logins.LoginsCorruptError as exc:
        return msgs.SAVE_FAILED.said(error=exc)
    except OSError as exc:
        return msgs.SAVE_FAILED.said(error=f"{type(exc).__name__}: {exc}")
    msgs.say(msgs.SAVED_SERVER, settings=login_defaults.render(values) or msgs.NONE_VALUE.said(),
             model=turn.model, provider=login.provider_id)
    return None


def _live_stamp(cfg: Config, state: dict) -> dict:
    """The stamp a turn started now would write: its model and the effort that
    model resolves to."""
    if not isinstance(state.get("settings"), dict):
        active = _cfg_for_active_model(cfg, state)
        return M.stamp_for(active.model, active.provider_id, active.reasoning_effort)
    turn = _cfg_for_live_state(cfg, state)
    return M.stamp_for(turn.model, turn.provider_id, turn.reasoning_effort)


def _cmd_model(arg: str, state: dict, cfg: Config) -> str | None:
    previous = _live_stamp(cfg, state)
    if arg:
        _set_model_via_route(state, cfg, arg)
    else:
        _pick_model_into_state(state, cfg)
    stamp = _live_stamp(cfg, state)
    if stamp != previous and cfg.session_file != Path(os.devnull):
        # A resume comes back on the switched-to model before any turn ran on it.
        record_model_switch(cfg.session_file, stamp=stamp, previous=previous)
    return None


def _cmd_name(arg: str, state: dict, cfg: Config) -> str | None:
    if cfg.session_file == Path(os.devnull):
        msgs.say(msgs.SESSION_NOT_SAVED_TITLE)
        return None
    title = " ".join(arg.split())
    if title:
        append_title(cfg.session_file, title)
        msgs.say(msgs.SESSION_TITLED, title=title)
        return None
    current = session_title(cfg.session_file)
    if current:
        msgs.say(msgs.SESSION_TITLE, title=current)
    else:
        msgs.say(msgs.SESSION_UNTITLED)
    return None


@dataclass(frozen=True)
class _SessionTarget:
    """A session the picker chose to continue in: its file, start directory and
    agent, and whether it started under a -C jail rooted at that directory."""

    path: Path
    cwd: str | None
    agent: str | None
    jailed: bool = False


def _session_target(choice: session_picker.Choice) -> _SessionTarget:
    """The session a picker choice continues in. A branch choice creates the
    branch first, in the parent's folder."""
    session = choice.session
    path = Path(session.path)
    if choice.action == session_picker.BRANCH and choice.message_id is not None:
        branch = branch_session(path, choice.message_id, cwd=session.cwd or Path.cwd(),
                                agent=session.agent, mode="repl")
        msgs.say(msgs.SESSIONS_BRANCHED, parent=session_store.display_name(path),
                 point=session_store.point_label(choice.message) if choice.message is not None else "-",
                 name=session_store.display_name(branch))
        path = branch
    return _SessionTarget(path, session.cwd, session.agent, session.jailed)


def _enter_session_dir(target: _SessionTarget) -> None:
    """Move to the directory the session started in, when it still exists."""
    if not target.cwd:
        return
    if not Path(target.cwd).is_dir():
        msgs.warn(msgs.SESSIONS_DIR_GONE, dir=target.cwd, cwd=Path.cwd())
        return
    os.chdir(target.cwd)
    runtime.T.STOCK_CONTEXT.cwd = Path.cwd()


def _cmd_session(arg: str, state: dict, cfg: Config) -> str | None:
    """Open the session picker. A chosen session other than this one ends the
    REPL, and js starts again in that session's directory, agent and model."""
    try:
        choice = session_picker.pick_session(Path.cwd(), query=arg)
    except Exception as exc:  # noqa: BLE001
        return msgs.FAILED.said(error=f"{type(exc).__name__}: {exc}")
    if choice is None:
        return None
    if (choice.action == session_picker.RESUME and cfg.session_file != Path(os.devnull)
            and Path(choice.session.path) == cfg.session_file.resolve(strict=False)):
        msgs.say(msgs.SESSIONS_ALREADY_HERE)
        return None
    target = _session_target(choice)
    state["switch_session"] = target
    state["running"] = False
    msgs.say(msgs.SESSIONS_SWITCHING, name=session_store.display_name(target.path), dir=target.cwd or Path.cwd())
    return None


# The session `/session` chose, run once the REPL has closed: {"target", "blocking"}.
_pending_switch: dict[str, object] = {}


def _queue_switch(state: dict, *, blocking: bool) -> None:
    target = state.get("switch_session")
    if isinstance(target, _SessionTarget):
        _pending_switch.update(target=target, blocking=blocking)


def _exec_session(target: _SessionTarget, *, blocking: bool) -> None:
    """Replace this process with js resuming `target` in its own directory
    and agent. Resume puts it back on its last-stamped model."""
    _enter_session_dir(target)
    argv = [sys.executable, "-m", "js", "--session", str(target.path)]
    if target.agent:
        argv += ["-a", target.agent]
    if target.jailed and target.cwd:
        argv += ["-C", target.cwd]
    if blocking:
        argv.append("--blocking")
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, argv)


def _cmd_reset(arg: str, state: dict, cfg: Config) -> str | None:
    state["messages"].clear()
    task_jobs.forget(runtime.T.STOCK_CONTEXT.task_owner)
    compaction.history_rewritten(runtime.T.STOCK_CONTEXT)
    M.append_mark(cfg.session_file, "session_reset")
    msgs.say(msgs.RESET_DONE)
    return None


def _cost_text(tally: usage_mod.Tally) -> str:
    if not tally.priced:
        return msgs.COST_UNPRICED.said()
    dollars = usage_display.format_dollars(tally.cost)
    if tally.unpriced_calls:
        return msgs.COST_PARTLY_PRICED.said(cost=dollars, calls=msgs.plural(tally.unpriced_calls, "call"))
    return dollars


def _cmd_cost(arg: str, state: dict, cfg: Config) -> str | None:
    live = usage_mod.totals(cfg.session_file)
    if live.calls == 0:
        msgs.say(msgs.COST_NONE)
        return None
    msgs.say(msgs.COST_TOTAL, calls=msgs.plural(live.calls, "call"), cost=_cost_text(live))
    msgs.say(msgs.COST_TOKENS, input=f"{live.input_tokens:,}", cache_read=f"{live.cache_read_tokens:,}",
             cache_write=f"{live.cache_write_tokens:,}", output=f"{live.output_tokens:,}",
             reasoning=f"{live.reasoning_tokens:,}")
    for model, tally in live.by_model.items():
        msgs.say(msgs.COST_MODEL, model=model, calls=msgs.plural(tally.calls, "call"),
                 tokens=f"{tally.input_tokens + tally.output_tokens:,}", cost=_cost_text(tally))
    return None


def _cmd_wipe(arg: str, state: dict, cfg: Config) -> str | None:
    bak = M.wipe(cfg.session_file)
    usage_mod.forget(cfg.session_file)
    state["messages"].clear()
    task_jobs.forget(runtime.T.STOCK_CONTEXT.task_owner)
    compaction.history_rewritten(runtime.T.STOCK_CONTEXT)
    if bak:
        msgs.say(msgs.WIPE_ROTATED, path=bak.name)
    else:
        msgs.say(msgs.WIPE_NOTHING)
    return None


def _cmd_persona(arg: str, state: dict, cfg: Config) -> str | None:
    text = state["system"]
    print(text[:2048])
    if len(text) > 2048:
        msgs.say(msgs.PERSONA_TRUNCATED, size=len(text))
    return None


def _cmd_jobs(arg: str, state: dict, cfg: Config) -> str | None:
    sup = supervisor.get_current()
    if sup is None:
        msgs.say(msgs.BLOCKING_NO_JOBS, command="/jobs")
        return None
    jobs = sup.jobs()
    if not jobs:
        msgs.say(msgs.NO_JOBS)
    for j in jobs:
        msgs.say(msgs.JOB_ROW, id=j.id, kind=j.kind, label=f"  {j.label}" if j.label else "")
    return None


def _cmd_cancel(arg: str, state: dict, cfg: Config) -> str | None:
    sup = supervisor.get_current()
    if sup is None:
        msgs.say(msgs.BLOCKING_NO_JOBS, command="/cancel")
        return None
    if arg:
        if not arg.isdigit():
            return msgs.USAGE.said(usage="/cancel [id]. Bare cancels the active turn.")
        targets = [j for j in sup.jobs() if j.id == int(arg)]
    else:
        targets = sup.jobs("turn")
    if not targets:
        msgs.say(msgs.NO_JOB_TO_CANCEL)
        return None
    # Task.cancel() is not thread-safe; hop to the loop thread to fire it.
    for j in targets:
        sup.loop.call_soon_threadsafe(sup.cancel, j.id)
    ids = ", ".join(f"[{j.id}] {j.kind}" for j in targets)
    msgs.say(msgs.CANCELLING, jobs=ids)
    return None


def _cmd_compact_auto(arg: str, state: dict, cfg: Config) -> str | None:
    value = arg.strip().lower()
    if value not in ("on", "off"):
        return msgs.USAGE.said(usage="/compact-auto on|off")
    return _apply_settings_result(setcmd.apply_set(state["settings"], "COMPACTION_AUTO", value), state, cfg)


def _cmd_compact(arg: str, state: dict, cfg: Config) -> str | None:
    model, focus, ok = _split_compact_model(arg)
    if not ok:
        return msgs.USAGE.said(usage="/compact [-m model] [focus]")
    forced = focus == _repl_assembly(state).compact_force_focus
    if forced:
        focus = ""
    try:
        compact_cfg = _cfg_for_live_state(cfg, state)
        with stream_transport.net_role(msgs.COMPACTING_ROLE.words()):
            result = compaction.compact_now_sync(compact_cfg, state["system"], state["messages"], model=model,
                                                 focus=focus, forced=forced, **_compact_hooks(state, compact_cfg))
    except Exception as e:  # noqa: BLE001
        return msgs.COMPACTION_FAILED.said(error=f"{type(e).__name__}: {e}")
    msgs.say(msgs.COMPACTION_DONE, result=result)
    return None


def _cmd_refresh_model_catalog(arg: str, state: dict, cfg: Config) -> str | None:
    _force_refresh_model_catalog()
    return None


def _cmd_quit(arg: str, state: dict, cfg: Config) -> str | None:
    if arg:
        # Recorded as a user message so the next turn actually sees it. It is
        # appended to the previous user message rather than added as a second
        # one: providers with strict role alternation reject back-to-back user
        # turns, and chat templates that tolerate it silently degrade.
        reminder = _PROMPTS['cmd_quit_js_reminder_user_closed_the_client'].format(arg=arg)
        _append_closing_note(cfg, state, reminder)
    state["running"] = False
    return None


def _cmd_tools(arg: str, state: dict, cfg: Config) -> str | None:
    _print_tool_chain(state, cfg)
    return None


def _cmd_skill(arg: str, state: dict, cfg: Config) -> str | None:
    """Bare `/skill` lists the skills. `/skill <name> [request]` typed at the
    input line never reaches the table: it is a turn (see _handle_command)."""
    if arg:
        return msgs.SKILL_IS_A_TURN.said()
    _print_skill_catalog(state)
    return None


def _cmd_skills(arg: str, state: dict, cfg: Config) -> str | None:
    """Open the skill picker. Enter there sets SKILLS_DISABLED to the skills
    left switched off."""
    current = _disabled_skills(state)
    try:
        chosen = skill_picker.pick_skills(Path.cwd(), current)
    except Exception as exc:  # noqa: BLE001
        return msgs.FAILED.said(error=f"{type(exc).__name__}: {exc}")
    if chosen is None or tuple(chosen) == current:
        return None
    return _cmd_set(f"SKILLS_DISABLED {json.dumps(list(chosen))}", state, cfg)


def _help_set(name: str) -> str | None:
    """`/help set NAME`: the setting's help text, from `js/help/set.txt`."""
    if not name:
        return msgs.USAGE.said(usage="/help set NAME")
    spec = settings.spec_for(name)
    if spec is None:
        return msgs.UNKNOWN_SETTING.said(key=name)
    msgs.say(msgs.HELP_SETTING, key=spec.key.upper())
    for line in textwrap.wrap(spec.doc, 72):
        msgs.say(msgs.HELP_SETTING_LINE, text=line)
    return None


def _help_fset(name: str) -> str | None:
    """`/help fset NAME`: the /fset entry's help text, from `js/help/fset.txt`."""
    if not name:
        return msgs.USAGE.said(usage="/help fset NAME")
    key = name.upper()
    doc = settings.read_help(settings.FSET_HELP_FILE).get(key)
    if doc is None:
        return msgs.FSET_UNKNOWN.said(name=name)
    msgs.say(msgs.HELP_SETTING, key=key)
    for line in textwrap.wrap(doc, 72):
        msgs.say(msgs.HELP_SETTING_LINE, text=line)
    return None


def _cmd_help(arg: str, state: dict, cfg: Config) -> str | None:
    topic, _, rest = arg.strip().partition(" ")
    if topic.lower() == "set":
        return _help_set(rest.strip())
    if topic.lower() == "fset":
        return _help_fset(rest.strip())
    rows = [(f"/{c.usage}", c.doc.said()) for c in dict.fromkeys(_commands(state).values())]
    rows += [(f"/{name}", msgs.HELP_ALIAS.said(body=body)) for name, body in (state.get("aliases") or {}).items()]
    rows += [(f"/{name}", msgs.HELP_PROMPT_COMMAND.said(path=command.path, description=command.description))
             for name, command in prompt_commands.discover(Path.cwd()).items()
             if name not in _commands(state) and name not in (state.get("aliases") or {})]
    rows += [("@path/to/file", msgs.HELP_ATTACH.said()), ("exit", msgs.HELP_EXIT.said())]
    width = max(len(usage) for usage, _doc in rows)
    msgs.say(msgs.HELP_HEADING)
    for usage, doc in rows:
        msgs.say(msgs.HELP_ROW, usage=usage.ljust(width), doc=doc)
    return None


def _cmd_exec(arg: str, state: dict, cfg: Config) -> str | None:
    """Run ``arg`` in the shell and queue its stdout for the next user message.
    Run by an `on` handler, the command reads the event as JSON on stdin with
    JS_EVENT naming it, and exit status 2 under tool_call refuses the call."""
    command = arg.strip()
    if not command:
        return msgs.USAGE.said(usage="/exec <command>")
    call = events.current_call()
    live = state.get("settings")
    timeout = int(settings.knob(live, "EXEC_TIMEOUT"))
    env = dict(os.environ)
    stdin_text = ""
    if call is not None:
        env["JS_EVENT"] = call.emission.event
        stdin_text = hookexec.event_json(call)
    try:
        outcome = hookexec.run(
            command,
            cwd=str(runtime.T.STOCK_CONTEXT.cwd),
            timeout=timeout,
            cap=int(settings.knob(live, "EXEC_OUTPUT_MAX") or 0),
            stdin_text=stdin_text,
            env=env,
        )
    except OSError as e:
        failure = msgs.EXEC_FAILED.said(command=command, error=e)
    else:
        if outcome.returncode is None:
            failure = msgs.EXEC_TIMED_OUT.said(command=command, seconds=timeout)
        elif (call is not None and outcome.returncode == hookexec.REFUSE_STATUS
              and call.emission.event in events.REFUSABLE_EVENTS):
            call.refusal = hookexec.refusal(outcome, str(call.emission.payload.get("name") or "the call"))
            return None
        elif outcome.returncode != 0:
            detail = next((line.strip() for line in (outcome.stderr or outcome.stdout).splitlines()
                           if line.strip()), "")
            failure = msgs.EXEC_EXITED.said(command=command, status=outcome.returncode, detail=detail)
        else:
            note = hookexec.reminder(outcome.stdout)
            if note is not None:
                _queue_note(state, note)
            if call is None:
                # Typed at the prompt: show what the model will read.
                if note is None:
                    msgs.say(msgs.EXEC_NO_OUTPUT)
                else:
                    print(outcome.stdout.rstrip("\n"))
                    msgs.say(msgs.EXEC_QUEUED, lines=msgs.plural(len(outcome.stdout.strip().splitlines()), "line"))
            return None
    if call is not None:
        # An `on` handler's errors otherwise reach only the debug log.
        msgs.say_said(failure)
    return failure


# /cd, /add and /drop: where the session works. Each change queues one
# reminder for the next user message and records a `workspace:` mark, so a
# resumed session works in the same place.

_CD_NOTICE = _PROMPTS['default_js_reminder_the_working_directory_is']
_ADD_NOTICE = _PROMPTS['default_js_reminder_is_now_visible_inside']
_DROP_NOTICE = _PROMPTS['default_js_reminder_is_no_longer_visible']


def _access(bind: _jail.Bind) -> str:
    return "read-write" if bind.rw else "read-only"


def _queue_note(state: dict, note: str) -> None:
    state.setdefault("pending_notes", []).append(note)


def _take_pending_notes(state: dict) -> list[str]:
    """The reminders queued since the last user message, and one for each
    background task that finished unread since then, removed from the queue."""
    queued = list(state.pop("pending_notes", None) or ())
    return [*queued, *task_jobs.completion_notes(runtime.T.STOCK_CONTEXT.task_owner)]


def _with_notes(bundle: attach.UserMessageBundle, notes: list[str]) -> attach.UserMessageBundle:
    for note in notes:
        bundle = attach.with_note(bundle, note)
    return bundle


def _requeue_notes(state: dict, notes) -> None:
    """Put ``notes`` back in front of the queue: the turn that carried them was discarded."""
    if notes:
        state["pending_notes"] = [*notes, *(state.get("pending_notes") or ())]


def _set_working_dir(path: Path) -> None:
    os.chdir(path)
    runtime.T.STOCK_CONTEXT.cwd = path


def _record_workspace(cfg: Config) -> None:
    if cfg.session_file == Path(os.devnull):
        return
    jail = _jail.active()
    M.append_workspace_mark(
        cfg.session_file,
        root=str(jail.root) if jail is not None else None,
        cwd=str(runtime.T.STOCK_CONTEXT.cwd),
        binds=[bind.spec() for bind in jail.added] if jail is not None else [],
    )


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
        _set_working_dir(cwd)


def _cmd_cd(arg: str, state: dict, cfg: Config) -> str | None:
    context = runtime.T.STOCK_CONTEXT
    raw = arg.strip()
    if not raw:
        msgs.say(msgs.CWD_IS, path=context.cwd)
        return None
    target = Path(os.path.expanduser(raw))
    target = (target if target.is_absolute() else context.cwd / target).resolve()
    if not target.is_dir():
        return msgs.CD_NOT_A_DIR.said(path=target)
    jail = _jail.active()
    if jail is not None and not jail.bound(target, settings.knob(state.get("settings"), "JAIL_BIND")):
        return msgs.CD_OUTSIDE_JAIL.said(path=target, root=jail.root)
    _set_working_dir(target)
    _queue_note(state, _CD_NOTICE.format(path=target))
    _record_workspace(cfg)
    msgs.say(msgs.CD_DONE, path=target)
    return None


def _cmd_add(arg: str, state: dict, cfg: Config) -> str | None:
    jail = _jail.active()
    if jail is None:
        return msgs.NO_JAIL.said(verb="add")
    spec = arg.strip()
    if spec and not spec.startswith(("/", "~")):
        spec = str(runtime.T.STOCK_CONTEXT.cwd / spec)
    try:
        bind = _jail.parse_bind(spec)
    except ValueError as exc:
        return msgs.ADD_BAD.said(error=exc)
    if not bind.path.exists():
        return msgs.ADD_MISSING.said(path=bind.path)
    jail.add(bind)
    _queue_note(state, _ADD_NOTICE.format(path=bind.path, access=_access(bind)))
    _record_workspace(cfg)
    msgs.say(msgs.ADD_DONE, path=bind.path, access=_access(bind))
    return None


def _cmd_drop(arg: str, state: dict, cfg: Config) -> str | None:
    jail = _jail.active()
    if jail is None:
        return msgs.NO_JAIL.said(verb="drop")
    raw = arg.strip()
    path = Path(os.path.abspath(runtime.T.STOCK_CONTEXT.cwd / os.path.expanduser(raw)))
    if path.resolve() == jail.root:
        return msgs.DROP_ROOT.said(path=path)
    dropped = next((bind for bind in jail.added if bind.path == path), None)
    if dropped is None:
        return msgs.DROP_UNKNOWN.said(path=path)
    jail.drop(path)
    if not jail.bound(runtime.T.STOCK_CONTEXT.cwd, settings.knob(state.get("settings"), "JAIL_BIND")):
        jail.add(dropped)
        return msgs.DROP_CWD.said(path=path)
    _queue_note(state, _DROP_NOTICE.format(path=path))
    _record_workspace(cfg)
    msgs.say(msgs.DROP_DONE, path=path)
    return None


_LOAD = Command(_cmd_load, "load <file>", msgs.CMD_LOAD, complete="path")

COMMANDS: dict[str, Command] = {
    "help": Command(_cmd_help, "help [set NAME]", msgs.CMD_HELP),
    "set": Command(_cmd_set, "set [key [value]]", msgs.CMD_SET,
                   complete="set"),
    "fset": Command(_cmd_fset, "fset [name [value]]", msgs.CMD_FSET, complete="fset"),
    "show": Command(_cmd_show, "show [key]", msgs.CMD_SHOW, complete="keys"),
    "save": Command(_cmd_save, "save", msgs.CMD_SAVE),
    **{verb: _LOAD for verb in setcmd.LOAD_VERBS},
    "on": Command(_cmd_on, "on [[^]event command]", msgs.CMD_ON, complete="events"),
    "alias": Command(_cmd_alias, "alias [name [command]]", msgs.CMD_ALIAS),
    "model": Command(_cmd_model, "model [name]", msgs.CMD_MODEL),
    "pick-model": Command(lambda arg, state, cfg: _cmd_model("", state, cfg), "pick-model", msgs.CMD_PICK_MODEL),
    "provider": Command(_cmd_provider, "provider [id]", msgs.CMD_PROVIDER,
                        complete="names"),
    "baseurl": Command(_cmd_baseurl, "baseurl [url]", msgs.CMD_BASEURL),
    "apikey": Command(_cmd_apikey, "apikey [key]", msgs.CMD_APIKEY),
    "login": Command(_cmd_login, "login <name> [apikey] [baseurl] [provider]", msgs.CMD_LOGIN,
                     complete="names"),
    "logout": Command(_cmd_logout, "logout", msgs.CMD_LOGOUT),
    "models": Command(_cmd_models, "models [limit]", msgs.CMD_MODELS),
    "reset": Command(_cmd_reset, "reset", msgs.CMD_RESET, turn_state=True),
    "wipe": Command(_cmd_wipe, "wipe", msgs.CMD_WIPE, turn_state=True),
    "persona": Command(_cmd_persona, "persona", msgs.CMD_PERSONA),
    "tools": Command(_cmd_tools, "tools", msgs.CMD_TOOLS),
    "skill": Command(_cmd_skill, "skill [name [request]]",
                     msgs.CMD_SKILL),
    "skills": Command(_cmd_skills, "skills", msgs.CMD_SKILLS),
    "turns": Command(lambda arg, state, cfg: msgs.say(msgs.TURNS_COUNT, messages=msgs.plural(len(state["messages"]), "message")),
                     "turns", msgs.CMD_TURNS),
    "cost": Command(_cmd_cost, "cost", msgs.CMD_COST),
    "session": Command(_cmd_session, "session [query]", msgs.CMD_SESSION, turn_state=True),
    "name": Command(_cmd_name, "name [title]", msgs.CMD_NAME),
    "jobs": Command(_cmd_jobs, "jobs", msgs.CMD_JOBS),
    "cancel": Command(_cmd_cancel, "cancel [id]", msgs.CMD_CANCEL),
    # The async REPL drains its loop-owned queue before a line reaches the
    # table; this entry runs everywhere else, where nothing queues.
    "flush": Command(lambda arg, state, cfg: msgs.say(msgs.NO_QUEUED_PROMPTS),
                     "flush", msgs.CMD_FLUSH),
    "compact": Command(_cmd_compact, "compact [-m model] [focus]", msgs.CMD_COMPACT,
                       turn_state=True),
    "compact-auto": Command(_cmd_compact_auto, "compact-auto on|off", msgs.CMD_COMPACT_AUTO),
    "refresh-model-catalog": Command(_cmd_refresh_model_catalog, "refresh-model-catalog",
                                     msgs.CMD_REFRESH_MODEL_CATALOG),
    "quit": Command(_cmd_quit, "quit [note]", msgs.CMD_QUIT),
    "cd": Command(_cmd_cd, "cd [dir]", msgs.CMD_CD, complete="path", turn_state=True),
    "add": Command(_cmd_add, "add <path>[:rw]", msgs.CMD_ADD, complete="path"),
    "drop": Command(_cmd_drop, "drop <path>", msgs.CMD_DROP, complete="path", turn_state=True),
    "exec": Command(_cmd_exec, "exec <command>", msgs.CMD_EXEC, complete="path"),
}


def _run_command(line: str, state: dict, cfg: Config, depth: int = 0) -> tuple[bool, str | None]:
    """Dispatch one command line (leading `/` optional) through COMMANDS and
    aliases. Returns ``(handled, error)``; ``handled`` is False for a verb that
    is neither."""
    parsed = setcmd.split_command(line)
    if parsed is None:
        return True, None
    verb, arg = parsed
    command = _commands(state).get(verb)
    if command is None:
        body = (state.get("aliases") or {}).get(verb)
        if body is None:
            return False, None
        if depth >= _MAX_ALIAS_DEPTH:
            return True, msgs.ALIAS_TOO_DEEP.said(name=verb)
        # The body's $NAMEs expand once; the typed arguments never do.
        body = setcmd.expand_line(body, state.get("settings"))
        expanded = body.replace("$*", arg) if "$*" in body else f"{body} {arg}".strip()
        handled, error = _run_command(expanded, state, cfg, depth + 1)
        return True, error if handled else msgs.ALIAS_UNKNOWN_COMMAND.said(name=verb, verb=expanded.split()[0])
    if _repl_assembly(state).guard_turn_state and command.turn_state and (sup := supervisor.get_current()) is not None and sup.turn_active():
        return True, msgs.TURN_RUNNING.said(command=f"/{verb}")
    return True, command.run(arg, state, cfg)


def _run_script_line(line: str, state: dict, cfg: Config) -> str | None:
    """One line from a file of commands, its $NAMEs expanded once: an
    unknown verb is an error. In the startup run of the jsrc files a
    setting's short name is that setting, as config load read it: `model X`
    is `set MODEL X`, not the /model command."""
    line = setcmd.expand_line(line, state.get("settings"))
    parsed = setcmd.split_command(line)
    if state.get("rc_replay") and parsed is not None and parsed[0] in settings.SPEC_BY_ALIAS:
        line = f"set {parsed[0]} {parsed[1]}"
    handled, error = _run_command(line, state, cfg)
    if not handled:
        return msgs.UNKNOWN_COMMAND.said(verb=setcmd.split_command(line)[0])
    return error


QUIT_WORDS = ("exit", "quit", ":q")  # quit without the leading '/'


def _repl_prompt_options() -> dict:
    return dict(search_ignore_case=True, complete_while_typing=False,
                complete_style=CompleteStyle.MULTI_COLUMN, enable_suspend=True, erase_when_done=True)


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


@dataclass(frozen=True)
class ReplAssembly:
    """Commands and input policy supplied to a REPL session."""

    commands: Mapping[str, Command] | None = None
    quit_words: tuple[str, ...] = QUIT_WORDS
    refuse_unknown: bool = True
    guard_turn_state: bool = True
    compact_force_focus: str = "up to here"
    flush_lines: tuple[str, ...] = ("/flush", "/cancel queued")
    interrupt: Callable | None = None
    blocking_interrupts_max: int = 2
    banner: Callable | None = None
    input_bindings: Callable | None = None
    prompt_options: Callable[[], dict] = _repl_prompt_options


DEFAULT_REPL_ASSEMBLY = ReplAssembly()


def _repl_assembly(state: dict | None) -> ReplAssembly:
    return (state or {}).get("repl_assembly", DEFAULT_REPL_ASSEMBLY)


def _commands(state: dict | None) -> Mapping[str, Command]:
    supplied = _repl_assembly(state).commands
    return COMMANDS if supplied is None else supplied


# Orders the async REPL's live-state writes against turn starts. A command holds
# it for its whole run on its executor thread; a turn takes its cfg snapshot under
# it, also off the loop thread (_live_cfg_snapshot), so a command that waits on the
# loop (a modal picker) never deadlocks it and a slow command never stalls input.
_LIVE_STATE_LOCK = threading.Lock()


def _live_cfg_snapshot(cfg: Config, state: dict) -> Config:
    """The turn cfg from live state, never from a half-applied command."""
    with _LIVE_STATE_LOCK:
        return _cfg_for_live_state(cfg, state)


def _handle_command(line: str, state: dict, cfg: Config) -> bool:
    """Return True if `line` was a command (already handled), False otherwise."""
    if line in _repl_assembly(state).quit_words:
        state["running"] = False
        return True
    if not line.startswith("/") or _is_skill_invocation(line):
        return False
    with _LIVE_STATE_LOCK:
        handled, error = _run_command(line, state, cfg)
    if not handled and _repl_assembly(state).refuse_unknown and _is_unknown_command(line):
        handled, error = True, msgs.UNKNOWN_SLASH_COMMAND.said()
    if isinstance(error, msgs.Said):
        msgs.say_said(error)
    elif error:
        msgs.say(msgs.COMMAND_REFUSED, error=error)
    return handled


# A slash word shaped like a command name. Anything else after the `/`
# (`/usr/bin/python is broken`, `/README.md`, `//text`) is prose for the model.
_COMMAND_WORD = re.compile(r"/[A-Za-z][A-Za-z0-9_-]*")


def _is_unknown_command(line: str) -> bool:
    """`line` names a command that is not in the table, not an alias and not
    a markdown command."""
    word = line.split(maxsplit=1)[0]
    return (_COMMAND_WORD.fullmatch(word) is not None
            and prompt_commands.expand(line, prompt_commands.discover(Path.cwd())) is None)


def _command_completions(state: dict) -> dict[str, str | None]:
    """Verb -> argument completion source, for every command and alias."""
    table: dict[str, str | None] = {name: None for name in prompt_commands.discover(Path.cwd())}
    table.update({verb: command.complete for verb, command in _commands(state).items()})
    table.update({name: None for name in state.get("aliases") or {}})
    return table


def _event_dispatcher(state: dict, cfg: Config):
    """Run `on` handler text through the command table."""

    def dispatch(hook: events.EventHook, emission: events.EventEmission) -> events.EventHandlerResult:
        handled, error = _run_command(setcmd.expand_line(hook.handler, state.get("settings")), state, cfg)
        if not handled:
            parsed = setcmd.split_command(hook.handler)
            error = msgs.EVENT_HANDLER_COMMAND_UNSUPPORTED.words(value=parsed[0] if parsed else '<blank>')
        return events.EventHandlerResult(hook=hook, error=error)

    return dispatch


def _run_rc_commands(state: dict, cfg: Config, paths: list[Path]) -> list[str]:
    """Run each jsrc file a line at a time, in order, through the same line
    runner `/load` uses (`_run_script_line`), its settings and its commands
    together: a `load` in it is `/load`, so a loaded file stops at its first
    error and so does every loaded file containing it; the jsrc itself goes
    on past an error. A relative `load` resolves against the jsrc's
    directory. The run starts from the stock settings and no /fset entries,
    as config load did; each setting it leaves different from what config
    load found takes its value in the live settings, under the env and CLI
    layers, and what the session put on top of the jsrc layer stays. Output
    is dropped; the errors come back for the banner."""
    from . import fset as _fset

    errors: list[str] = []
    stack = state.setdefault("load_stack", [])
    live = state.get("settings")
    found = settings.collect_again(paths)
    layer = settings.seed_defaults()
    _fset.reset()
    state["settings"] = layer
    state["rc_replay"] = True
    try:
        for path in paths:
            if not path.exists():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            stack.append(path.resolve(strict=False))
            try:
                for lineno, raw in enumerate(lines, 1):
                    with contextlib.redirect_stdout(io.StringIO()):
                        error = _run_script_line(raw, state, cfg)
                    if error:
                        errors.append(msgs.SCRIPT_LINE_FAILED.said(path=path, lineno=lineno, error=error))
            finally:
                stack.pop()
    finally:
        state["settings"] = live
        state.pop("rc_replay", None)
    if isinstance(live, dict):
        ran = settings.over_jsrc_layer(paths, layer)
        changed: list[str] = []
        for spec in settings.REGISTRY:
            value = settings.get_dotted(ran, spec.path)
            if settings.get_dotted(found, spec.path) != value:
                settings.set_dotted(live, spec.path, copy.deepcopy(value))
                changed.append(spec.key)
        # The model, provider and the rest of the state follow only what the
        # run changed: the flags and the resumed session stay over the jsrc.
        _apply_settings_result(setcmd.CommandResult(handled=True, changed=bool(changed),
                                                    changed_keys=changed), state, cfg)
        msgs.set_banner(str(settings.knob(live, "BANNER")))
    return errors


def _agent_model_applies(cfg: Config, prompt_spec, model: str | None, *, config_pins: bool = True) -> bool:
    """Whether the active agent's agent.yaml `model:` picks the model: it
    names one and the operator pinned none (`_apply_agent_model`)."""
    agent_model = getattr(prompt_spec, "model", "") if prompt_spec is not None else ""
    pinned = getattr(cfg, "explicit_model" if config_pins else "invocation_model", False)
    return bool(agent_model) and model is None and not pinned


def _apply_agent_model(cfg: Config, prompt_spec, model: str | None, *, config_pins: bool = True) -> Config:
    """Apply the active agent's agent.yaml `model:` through the resolver, unless
    the operator pinned a model. -m, a JS_* env var and --extra always pin; a
    jsrc `MODEL` pins only when ``config_pins``."""
    if not _agent_model_applies(cfg, prompt_spec, model, config_pins=config_pins):
        return cfg
    agent_model = prompt_spec.model
    route = routing.resolve_model_route(
        agent_model,
        configured_provider_id=cfg.provider_id,
        configured_base_url=cfg.provider_base_url,
        configured_api_key=cfg.provider_api_key,
        configured_headers=getattr(cfg, "provider_headers", {}),
        explicit_model=True,
        prefix_overrides_provider=not getattr(cfg, "explicit_provider", False),
    )
    return replace(
        cfg,
        model=route.model,
        provider_id=route.provider_id,
        provider_base_url=route.base_url,
        provider_api_key=route.api_key,
        provider_headers=route.headers,
        vision_enabled=vision_enabled_for_model(route.model, getattr(cfg, "settings", None)),
    )


def _apply_agent_reasoning(cfg: Config, prompt_spec) -> Config:
    """Apply the active agent's agent.yaml `reasoning:` unless a JS_* env var
    or --extra set the effort for this run."""
    effort = getattr(prompt_spec, "reasoning_effort", None) if prompt_spec is not None else None
    if effort is None or getattr(cfg, "invocation_reasoning", False):
        return cfg
    return replace(cfg, reasoning_effort=effort)


def _apply_agent_max_tokens(cfg: Config, prompt_spec) -> Config:
    return P.apply_agent_max_tokens(cfg, prompt_spec)


def _format_prompt_load_error(cfg, exc: Exception) -> str:
    """Turn a persona-load failure into an operator-legible line. A missing prompt
    directory means the named agent doesn't exist anywhere on the search path —
    say so plainly and point at the global agents dir where one is created,
    instead of leaking the raw 'prompts directory missing at <path>'."""
    if isinstance(exc, FileNotFoundError):
        try:
            resolved = P.resolve_agent_prompt_dir(cfg)
        except Exception:  # noqa: BLE001
            resolved = None
        if resolved is None or not Path(resolved).is_dir():
            agents_dir = _paths.global_agents_dir()
            agent_id = getattr(cfg, "agent_id", "?")
            return msgs.NO_SUCH_AGENT.said(agent=agent_id, agents_dir=agents_dir, new_dir=agents_dir / agent_id)
    return msgs.of_error(exc)


def _validate_cli_reasoning(reasoning: str) -> tuple[str | None, str | None]:
    """Coerce a raw ``--reasoning`` value through the SAME registry validation as
    ``set EFFORT`` and ``JS_REASONING`` (ruling B): ``off``
    disables (stored as ``none``); every other value must be a ladder stop. Any
    other token yields a clean local error instead of shipping garbage to the
    provider. Returns (value, error)."""
    spec = settings.SPEC_BY_KEY["EFFORT"]
    return settings.coerce_value(spec, reasoning)


# The command-line flags that set a setting: (args attribute, setting). Each
# reaches every run path as an --extra, which also records it as a flag value
# that a saved login's defaults never replace (js.login_defaults).
SETTING_FLAGS: tuple[tuple[str, str], ...] = (
    ("reasoning", "EFFORT"),
    ("max_out", "OUTPUT_TOKENS_MAX"),
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
        extras.append(f"{key}={value}")
    return extras, None


def _raise_keyboard_interrupt(*_args) -> None:
    raise KeyboardInterrupt


@_session_scope
def _run_prompt(prompt: str, model: str | None = None, debug: bool = False,
                debug_file: str | None = None,
                agent: str | None = None, session: str | None = None, save: bool = True,
                system_override: str | None = None, resume_prefix: str | None = None,
                reasoning: str | None = None, maxout: int | None = None,
                show_continue: bool = True, tool_registry=None,
                extras: list[str] | None = None, tool_context=None,
                ignore_local_config: bool = False, ignore_global_config: bool = False,
                files: list[str] | None = None, stdin_attachment: bytes | None = None,
                presets: list[str] | None = None,
                stats_json: str | None = None, stats_csv: str | None = None,
                caller_key: str | None = None, announce_generated: bool | None = None,
                events: headless.JsonEvents | None = None,
                config_pins: bool = False, swarm: str | None = None) -> int:
    attachments = list(files or [])
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
        cfg = _from_env(
            session,
            save_session=save,
            extras=extras,
            agent_id=agent,
            ignore_local_config=ignore_local_config,
            ignore_global_config=ignore_global_config,
            presets=presets,
        )
    except ValueError as e:
        msgs.warn(msgs.FAILED, error=e)
        return 2
    if announce_generated is None:
        announce_generated = save and session is None and os.environ.get("JS_SESSION") is None
    # A resumed session continues on the model, provider and reasoning of its
    # last stamp unless this run names them. Read before this start records its own.
    resumed = last_stamp(cfg.session_file) if cfg.session_file != Path(os.devnull) else None
    unreachable_stamp = None
    if resumed is not None:
        if model is None:
            unreachable_stamp = _stamp_without_login(resumed, cfg)
            model = _resume_model_spec(resumed, cfg)
    if save:
        _activate_saved_session(
            cfg,
            caller_key=caller_key,
            announce_generated=announce_generated,
            model=model,
        )
    telemetry = runtime.Telemetry(debug_log=cfg.debug_log)
    _sync_transcript_sink(cfg, getattr(cfg, "settings", {}) or {}, telemetry)

    prompt_spec = None
    if system_override is not None:
        system = system_override
        active_registry = tool_registry or _registry_for(cfg)
    else:
        try:
            prompt_spec = P.load_configured_prompt_spec(cfg, expand=False)
        except (FileNotFoundError, ValueError) as e:
            with _transcript_stdio(telemetry):
                msgs.warn(msgs.FAILED, error=_format_prompt_load_error(cfg, e))
            return 2
        system = prompt_spec.system
        active_registry = _registry_for(cfg).select(prompt_spec.tool_selectors)
        try:
            # A resumed session stays on its stamp; agent.yaml sets a fresh run.
            if config_pins or resumed is None:
                cfg = _apply_agent_model(cfg, prompt_spec, model, config_pins=config_pins)
        except ValueError as e:
            with _transcript_stdio(telemetry):
                msgs.warn(msgs.FAILED, error=e)
            return 2
        if not config_pins and resumed is None and reasoning_override is None:
            cfg = _apply_agent_reasoning(cfg, prompt_spec)
        cfg = _apply_agent_max_tokens(cfg, prompt_spec)

    try:
        cfg = _resolve_cli_model_override(cfg, model)
    except ValueError as e:
        with _transcript_stdio(telemetry):
            msgs.warn(msgs.FAILED, error=e)
        return 2
    # The model and agent this run uses are settled: the login-default
    # settings resolve before sampling and attachments are worked out.
    cfg = login_defaults.for_turn(login_defaults.with_agent(
        cfg, prompt_spec, reasoning=not config_pins and resumed is None and reasoning_override is None))
    if resumed is not None and reasoning is None:
        # A resumed session's reasoning is this session's own choice, like
        # /set, even when it is what the model would pick: its turns and the
        # children they start run on it.
        resumed_layer = _resumed_layer(resumed, None)
        if resumed_layer:
            cfg = login_defaults.for_turn(replace(cfg, layer_resume=resumed_layer))
            reasoning_override = _stamped_reasoning(resumed)
    if prompt_spec is not None:
        recorded_system = M.load_system_prompt(cfg.session_file)
        if recorded_system is None:
            prompt_spec = P._expand_spec(prompt_spec, cfg)
            system = prompt_spec.system
        else:
            system = recorded_system
    if unreachable_stamp is not None:
        with _transcript_stdio(telemetry):
            msgs.warn(msgs.RESUME_MODEL_FALLBACK, stamped=unreachable_stamp,
                      model=_stamp_label(cfg.model, cfg.provider_id))

    bus = swarm_mod.Agent(swarm) if swarm else None
    if bus is not None:
        # On a bus the agent gets send/who, reads its inbox at every tool
        # boundary, and sleeps on the inbox instead of exiting after the turn.
        active_registry = swarm_mod.with_bus_tools(active_registry)
        (tool_context or runtime.T.STOCK_CONTEXT).swarm = bus
    attachment_cfg = (
        replace(cfg, model=model, vision_enabled=vision_enabled_for_model(model, getattr(cfg, "settings", None)))
        if model is not None
        else cfg
    )
    messages = M.load_replay_messages(cfg.session_file)
    cut_off = M.turn_cut_off(messages)
    if prompt_spec is not None and save:
        prompt_changed = M.record_prompt_seen(cfg.session_file, prompt_spec.source)
        if recorded_system is None:
            M.append_system_prompt(cfg.session_file, system)
        elif prompt_changed:
            notice = {"role": "user", "content": _PROMPT_CHANGED_NOTICE}
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
            "session", version=headless.SCHEMA_VERSION,
            id=_session_hint_arg(cfg) if saved else None,
            file=str(cfg.session_file) if saved else None,
            resumed=before_len > 0, agent=cfg.agent_id, model=cfg.model, provider=cfg.provider_id,
            cwd=str(Path.cwd()), usage=usage_mod.totals(cfg.session_file).as_dict(),
        )
    try:
        user_bundle = attach.build_user_message(
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
        user_bundle = _note_mode_switch(cfg, user_bundle, "-p")
    user_bundle = _note_model_switch(resume_cfg, user_bundle)
    if cut_off:
        user_bundle = attach.with_note(user_bundle, _CUT_OFF_NOTICE)
    call_stats: list[dict] = []
    turn_kwargs = {
        "model_override": cfg.model,
        "provider_id_override": cfg.provider_id,
        "provider_base_url_override": cfg.provider_base_url,
        "provider_api_key_override": cfg.provider_api_key,
        "tool_registry": active_registry,
        "sampling": login_defaults.sampling(cfg),
        "call_stats": call_stats,
    }
    if events is not None:
        turn_kwargs["event_sink"] = events.runtime_event
    if bus is not None:
        turn_kwargs["steer"] = bus.steer
    if reasoning_override is not None:
        turn_kwargs["reasoning_effort_override"] = reasoning_override
    if maxout is not None:
        turn_kwargs["max_output_override"] = maxout
    _t_wall = time.time()
    autolog = _debug_autolog_path(cfg, getattr(cfg, "settings", {}) or {})
    sink_paths: list[Path] = [p for p in (autolog,) if p is not None]
    if debug_file:
        sink_paths.append(Path(debug_file))
    trace_sink = _open_debug_trace_sink(sink_paths)
    telemetry.trace_sink = trace_sink
    user_started = False
    turn_transcript = _transcript_sink(telemetry)

    # -d shows the conversation as the REPL draws it: each user line, typed or
    # fired from the agent's files, ahead of the answer it got.
    echo_user = display_mod.echo_user if debug else None
    progress_interval = 0.0 if events is not None else float(
        settings.knob(getattr(cfg, "settings", None), "PROGRESS_INTERVAL") or 0)

    def run() -> None:
        # Startup exchanges are model turns too, so progress covers them.
        with headless.Progress((tool_context or runtime.T.STOCK_CONTEXT).turn_status, progress_interval):
            turns()

    def turns() -> None:
        nonlocal before_len, user_started
        if prompt_spec is not None and prompt_spec.exchanges and not messages:
            model_client.run_owning_loop(agent_messages.initialize(
                cfg, prompt_spec, messages, replace(telemetry, transcript_log=turn_transcript),
                system=system, save=save,
                trace_override=debug or bool(debug_file) or cfg.trace,
                tool_context=tool_context, echo_user=echo_user, **turn_kwargs,
            ))
        before_len = len(messages)
        messages.append(user_bundle.runtime_message)
        user_started = True
        if save:
            _append_turn(cfg, user_bundle.history_message)
        if turn_transcript is not None:
            turn_transcript.write_user(prompt)
        if echo_user is not None:
            echo_user(prompt)
        runtime.run_turn(cfg, system, messages, telemetry,
                         trace_override=debug or bool(debug_file) or cfg.trace,
                         tool_context=tool_context, **turn_kwargs)
        if bus is None:
            return
        # On a bus, a turn ending is the agent going idle, not the run ending.
        # Sleep on the inbox (no model call) and wake into the next turn with
        # whatever landed. A `stop` message ends it after the turn it lands in.
        # The between-turn compaction trigger runs before each sleep, as it
        # does between REPL turns, so a long-lived agent stays inside its window.
        auto_state = {"system": system, "messages": messages, "model": cfg.model}
        while not bus.stop_seen:
            if save:
                _persist_turn_messages(cfg, messages, reasoning_override)
                with _transcript_stdio(telemetry):
                    _maybe_auto_compact(cfg, auto_state)
            if events is not None:
                events.emit("sleep", agent=bus.name)
            landed = bus.sleep()
            if events is not None:
                events.emit("wake", agent=bus.name, count=len(landed), seqs=[m.seq for m in landed if m.seq],
                            kinds=sorted({m.kind for m in landed}))
            if all(m.kind == swarm_mod.STOP for m in landed):
                return
            wake = M.note_time(bus.wake_message(landed))
            messages.append(wake)
            if save:
                _append_turn(cfg, wake)
            if turn_transcript is not None:
                turn_transcript.write_user(wake["content"])
            if echo_user is not None:
                echo_user(wake["content"])
            runtime.run_turn(cfg, system, messages, telemetry,
                             trace_override=debug or bool(debug_file) or cfg.trace,
                             tool_context=tool_context, **turn_kwargs)

    # On a bus, SIGTERM (the runner stopping the swarm) ends the run like ^C:
    # the session is persisted and the exit code says interrupted. The
    # handler this run replaces is put back when it ends.
    previous_sigterm = None
    if bus is not None:
        with contextlib.suppress(ValueError):  # not the main thread: no signal hook
            previous_sigterm = signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)
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
                        run()
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
                        run()
                finally:
                    telemetry.transcript_log = visible_transcript
                    telemetry.display_factory = visible_display
        except (KeyboardInterrupt, asyncio.CancelledError):
            with _transcript_stdio(telemetry):
                msgs.warn(msgs.TURN_INTERRUPTED)
            return 130
        except Exception as e:  # noqa: BLE001
            with _transcript_stdio(telemetry):
                msgs.warn(msgs.FAILED, error=_error_text(e))
            return 1
    finally:
        try:
            if user_started:
                _replace_runtime_user_message(
                    messages, user_bundle.runtime_message, user_bundle.history_message, before_len,
                )
            if save:
                _persist_turn_messages(cfg, messages, reasoning_override)
        finally:
            if trace_sink is not None:
                trace_sink.close()
            telemetry.trace_sink = None
            if previous_sigterm is not None:
                signal.signal(signal.SIGTERM, previous_sigterm)

    answer = None
    for message in reversed(messages):
        if message.get("role") == "user":
            break
        if message.get("role") == "assistant" and message.get("content"):
            answer = message
            break

    if stats_json or stats_csv:
        error = None if answer is not None else msgs.NO_ASSISTANT_RESPONSE.words()
        row = {"name": "prompt", "prompt": prompt, "max_tokens": cfg.max_output_tokens if maxout is None else maxout,
               "ok": answer is not None, "error": error, **stats.summarize_calls(call_stats, wall_s=time.time() - _t_wall)}
        payload = {"agent": cfg.agent_id, "model": cfg.model, "provider": cfg.provider_id, "turns": [row]}
        if stats_json:
            stats.write_json(stats_json, payload)
        if stats_csv:
            stats.write_csv(stats_csv, [row])

    if answer is not None:
        # In debug, run_turn already streamed the answer live to stdout — a
        # second print here is the double-print bug. Only print when the
        # stream was suppressed (non-debug one-shot/pipe).
        with _transcript_stdio(telemetry):
            if not debug:
                content = answer["content"].strip()
                if (sink := _transcript_sink(telemetry)) is not None:
                    sink.write_assistant(content)
                if events is None:
                    with _mute_transcript_tee(_transcript_sink(telemetry)):
                        display_mod.print_answer(
                            content, sys.stdout,
                            markdown=display_mod.markdown_enabled(getattr(cfg, "settings", None)),
                        )
            if save:
                _maybe_auto_compact(cfg, {
                    "system": system,
                    "messages": messages,
                    "model": cfg.model,
                })
                if show_continue:
                    hint = _session_hint_arg(cfg)
                    cont = resume_prefix or "js"
                    # Plain -p has no resume_prefix; a resume runs the `DEFAULT_AGENT`
                    # setting unless told otherwise, so a non-default agent MUST
                    # be echoed or the session continues under the wrong agent.
                    # (wiki/commit already fold the agent into resume_prefix.)
                    if resume_prefix is None and agent:
                        cont += f" --agent {shlex.quote(agent)}"
                    if model:
                        cont += f" --model {shlex.quote(model)}"
                    cont += f" --session {hint}"
                    msgs.warn(msgs.CONTINUE_HINT, command=cont)
        return 0

    with _transcript_stdio(telemetry):
        msgs.warn(msgs.NO_ASSISTANT_RESPONSE)
    return 1


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


_json_events: headless.JsonEvents | None = None


def _json_run(run: Callable[[], int]) -> int:
    """Run `run` as a `--json` headless run: stdout carries only JSON events,
    everything else printed goes to stderr, and the last event is `result`. A
    run that fails without an `error` event gets one carrying the last line
    printed to stderr."""
    global _json_events
    events = headless.JsonEvents(sys.stdout)
    err = headless.LastLine(sys.stderr)
    _json_events = events
    try:
        with contextlib.redirect_stdout(err), contextlib.redirect_stderr(err):
            try:
                code = run()
            except Exception as exc:  # noqa: BLE001 - the stream must still end with a result
                msgs.warn(msgs.FAILED, error=_error_text(exc))
                code = 1
    finally:
        _json_events = None
    if code != 0 and not events.errored:
        _emit_failure(events, err.last_line, code)
    opened = events.session
    events.emit(
        "result", ok=code == 0, exit_code=code, text=events.final_text if code == 0 else "",
        session=None if opened is None else opened.get("id"),
        usage=None if opened is None else usage_mod.totals(
            Path(opened["file"]) if opened.get("file") else None).as_dict(),
    )
    return code


def _emit_failure(events: headless.JsonEvents, line: str, code: int) -> None:
    message = _WARN_MARK.sub("", _ANSI.sub("", line))
    events.emit("error", message=message or f"exit {code}", retryable=False)


_WARN_MARK = re.compile(r"^\*+\s*")


def _run_prompt_json(prompt: str, **kwargs) -> int:
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


def _bench_row_line(row: dict) -> msgs.Said:
    if not row.get("ok"):
        return msgs.BENCH_FAILED.said(name=row["name"], error=row.get("error") or "failed")
    ttft = f"{row['ttft_s'] * 1000:.0f}ms" if row.get("ttft_s") is not None else "—"
    return msgs.BENCH_ROW.said(name=row["name"], tokens=row.get("output_tokens", 0),
                               tps=row.get("tok_per_s", 0.0), ttft=ttft, wall=row.get("wall_s") or 0.0)


def _run_bench(bench_agent: str, *, model: str | None, reasoning: str | None,
               maxout: int | None, quiet: bool, extras: list[str] | None,
               ignore_local_config: bool, ignore_global_config: bool,
               presets: list[str] | None,
               stats_json: str | None, stats_csv: str | None,
               debug: bool = False) -> int:
    """Run an agent's NN-benchmark.md turns, each on a clean slate (fresh
    context, no session), measuring TTFT / tok-s / turn time. The persona
    (NN-prompt.md + agent.yaml) is rebuilt into each benchmark's head;
    benchmarks never see each other."""
    try:
        agent_id = validate_agent_id(bench_agent)
    except (ValueError, OSError) as e:
        msgs.warn(msgs.FAILED, error=e)
        return 2
    reasoning_override = None
    if reasoning is not None:
        reasoning_override, effort_error = _validate_cli_reasoning(reasoning)
        if effort_error is not None:
            msgs.warn(msgs.BAD_REASONING, value=reasoning, error=effort_error)
            return 2
    try:
        cfg = _from_env(
            None, save_session=False, extras=extras, agent_id=agent_id,
            ignore_local_config=ignore_local_config,
            ignore_global_config=ignore_global_config, presets=presets,
        )
        cfg = replace(cfg, session_file=Path(os.devnull))
    except ValueError as e:
        msgs.warn(msgs.FAILED, error=e)
        return 2
    try:
        prompt_spec = P.load_configured_prompt_spec(cfg, expand=False)
        benchmarks = P.load_benchmarks(P.resolve_agent_prompt_dir(cfg))
    except (FileNotFoundError, ValueError) as e:
        msgs.warn(msgs.FAILED, error=_format_prompt_load_error(cfg, e))
        return 2

    if not benchmarks:
        msgs.warn(msgs.NO_BENCHMARKS, agent=agent_id)
        return 2

    system = prompt_spec.system
    active_registry = _registry_for(cfg).select(prompt_spec.tool_selectors)
    try:
        cfg = _apply_agent_model(cfg, prompt_spec, model)
        cfg = _resolve_cli_model_override(cfg, model)
        cfg = login_defaults.for_turn(login_defaults.with_agent(cfg, prompt_spec))
    except ValueError as e:
        msgs.warn(msgs.FAILED, error=e)
        return 2
    # The resolved OUTPUT_TOKENS_MAX: agent.yaml's max_tokens is one of its layers.
    agent_default_max = cfg.max_output_tokens
    prompt_spec = P._expand_spec(prompt_spec, cfg)
    system = prompt_spec.system

    # The request trace (system prompt, tools, messages, FLIGHT lines) goes to
    # logs/<agent>/<agent>-<pid>.log like every other run. A bench keeps no session,
    # so without this nothing records what the model was sent.
    autolog = _debug_autolog_path(cfg, getattr(cfg, "settings", {}) or {})
    trace_sink = _open_debug_trace_sink([autolog] if autolog is not None else [])

    rows: list[dict] = []
    interrupted = False
    for bench in benchmarks:
        # max_tokens: --max-out wins; else per-benchmark frontmatter (already
        # -1 -> None=uncapped); else the agent default from agent.yaml.
        if maxout is not None:
            eff_max = maxout
        elif bench.max_tokens_set:
            eff_max = bench.max_tokens
        else:
            eff_max = agent_default_max
        prompt_text = bench.prompt
        messages = []
        call_stats: list[dict] = []
        turn_kwargs = {
            "model_override": cfg.model,
            "provider_id_override": cfg.provider_id,
            "provider_base_url_override": cfg.provider_base_url,
            "provider_api_key_override": cfg.provider_api_key,
            "tool_registry": active_registry,
            "sampling": login_defaults.sampling(cfg),
            "max_output_override": eff_max,
            "call_stats": call_stats,
        }
        if reasoning is not None:
            turn_kwargs["reasoning_effort_override"] = reasoning_override
        ok, err = True, None
        t_wall = time.time()
        try:
            sink = io.StringIO() if quiet else None
            out = sink if sink is not None else (_StdoutTee(sys.stdout, trace_sink) if trace_sink is not None else None)
            with contextlib.redirect_stdout(out) if out is not None else contextlib.nullcontext():
                # -d turns on the per-turn trace here too. Without it a bench run
                # records only the model's prose, which is its own claim about
                # what it called, not evidence — useless for measuring tool use.
                async def run_benchmark():
                    nonlocal prompt_text
                    telemetry = runtime.Telemetry(debug_log=cfg.debug_log)
                    telemetry.trace_sink = trace_sink
                    await agent_messages.initialize(
                        cfg, prompt_spec, messages, telemetry, save=False,
                        trace_override=bool(debug), **turn_kwargs,
                    )
                    prompt_text = P.expand_agent_text(bench.prompt, cfg)
                    if not quiet:
                        first = (prompt_text.splitlines() or [""])[0][:80]
                        msgs.warn(msgs.BENCH_START, name=bench.name, prompt=first)
                    messages.append({"role": "user", "content": prompt_text})
                    await runtime.run_turn_async(
                        cfg, system, messages, telemetry, trace_override=bool(debug), **turn_kwargs,
                    )

                model_client.run_owning_loop(run_benchmark())
        except KeyboardInterrupt:
            interrupted, ok, err = True, False, "interrupted"
        except Exception as e:  # noqa: BLE001
            ok, err = False, f"{type(e).__name__}: {e}"
        row = {
            "name": bench.name, "prompt": prompt_text, "max_tokens": eff_max,
            "ok": ok, "error": err,
            **stats.summarize_calls(call_stats, wall_s=time.time() - t_wall),
        }
        rows.append(row)
        msgs.say_said(_bench_row_line(row), file=sys.stderr)
        if interrupted:
            break

    if trace_sink is not None:
        trace_sink.close()

    payload = {"agent": agent_id, "model": cfg.model, "provider": cfg.provider_id, "benchmarks": rows}
    if stats_json:
        stats.write_json(stats_json, payload)
        msgs.warn(msgs.STATS_WRITTEN, path=stats_json)
    if stats_csv:
        stats.write_csv(stats_csv, rows)
        msgs.warn(msgs.STATS_WRITTEN, path=stats_csv)
    if not stats_json and not stats_csv:
        print(json.dumps(payload, indent=2, default=str))
    return 130 if interrupted else 1 if any(not row["ok"] for row in rows) else 0


def _commit_backup_root() -> Path:
    return _paths.commit_backups_dir()


def _worktree_patch(repo_dir: Path, commit_helper) -> str:
    """The unified diff of every tracked uncommitted change.

    `git diff HEAD` is staged+unstaged against the last commit. A repo with no
    commits yet has no HEAD, so fall back to staged+unstaged separately.
    """
    flags = (*commit_helper._DIFF_FLAGS, "--binary")
    diff = commit_helper._git("diff", *flags, "HEAD", check=False, repo=repo_dir)
    if diff.returncode == 0:
        return diff.stdout
    staged = commit_helper._git("diff", "--cached", *flags, check=False, repo=repo_dir)
    unstaged = commit_helper._git("diff", *flags, check=False, repo=repo_dir)
    return (staged.stdout if staged.returncode == 0 else "") + (unstaged.stdout if unstaged.returncode == 0 else "")


def _prune_commit_backups(root: Path, keep: int = 10) -> None:
    dirs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda p: p.name)
    for old in dirs[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


def _snapshot_worktree(repo_dir: Path) -> Path | None:
    """Copy all uncommitted work into a timestamped backup before the agent touches the tree.

    The commit agent can revert files while splitting commits; a snapshot means any
    failure path is recoverable. Returns the backup dir, or None on a clean tree.
    """
    from . import commit_helper

    patch = _worktree_patch(repo_dir, commit_helper)
    others = commit_helper._git("ls-files", "--others", "--exclude-standard", "-z", check=False, repo=repo_dir)
    untracked = [p for p in others.stdout.split("\0") if p] if others.returncode == 0 else []
    if not patch.strip() and not untracked:
        return None

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup_dir = _commit_backup_root() / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    if patch:
        (backup_dir / "tracked.patch").write_bytes(patch.encode("utf-8", "surrogateescape"))
    for rel in untracked:
        dest = backup_dir / "untracked" / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(repo_dir / rel, dest, follow_symlinks=False)
        except OSError:
            continue  # a vanished/unreadable file must not abort the whole snapshot
    _prune_commit_backups(_commit_backup_root())
    return backup_dir


def _run_commit(target: str | None,
                model: str | None = None, debug: bool = False, debug_file: str | None = None,
                session: str | None = None, save: bool = True,
                reasoning: str | None = None, maxout: int | None = None,
                extra_context: str | None = None,
                extras: list[str] | None = None,
                ignore_local_config: bool = False, ignore_global_config: bool = False,
                presets: list[str] | None = None) -> int:
    target_dir = Path(target).expanduser() if target else Path.cwd()
    if not target_dir.is_absolute():
        target_dir = Path.cwd() / target_dir
    target_dir = target_dir.resolve(strict=False)
    if not target_dir.exists():
        msgs.warn(msgs.COMMIT_TARGET_MISSING, path=target_dir)
        return 2
    if not target_dir.is_dir():
        msgs.warn(msgs.COMMIT_TARGET_NOT_DIR, path=target_dir)
        return 2

    from . import commit_helper

    probe = commit_helper._git("rev-parse", "--is-inside-work-tree", check=False, repo=target_dir)
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        init = commit_helper._git("init", "-q", "-b", "main", check=False, repo=target_dir)
        if init.returncode != 0:
            init = commit_helper._git("init", "-q", check=False, repo=target_dir)
        if init.returncode != 0:
            detail = (init.stderr or init.stdout).strip() or f"exit {init.returncode}"
            msgs.warn(msgs.GIT_INIT_FAILED, path=target_dir, error=detail)
            return 1

    try:
        backup_dir = _snapshot_worktree(target_dir)
    except Exception as e:  # noqa: BLE001 — a snapshot failure must never block the commit run
        msgs.warn(msgs.SNAPSHOT_FAILED, error=f"{type(e).__name__}: {e}")
    else:
        if backup_dir is not None:
            msgs.say(msgs.SNAPSHOT_SAVED, path=backup_dir)

    survey_out = io.StringIO()
    survey_err = io.StringIO()
    with contextlib.redirect_stdout(survey_out), contextlib.redirect_stderr(survey_err):
        survey_rc = commit_helper.main(["-C", str(target_dir), "survey"])
    survey = survey_out.getvalue().rstrip()
    if survey_rc != 0:
        detail = survey_err.getvalue().strip() or survey or msgs.SURVEY_EXITED.said(code=survey_rc)
        msgs.warn(msgs.SURVEY_FAILED, path=target_dir, error=detail)
        return 1

    helper_stage = _PROMPTS['run_commit_python_m_js_commit_helper_c'].format(shlex_quote_str_target_dir=shlex.quote(str(target_dir)))
    helper_survey = _PROMPTS['run_commit_python_m_js_commit_helper_c_2'].format(shlex_quote_str_target_dir=shlex.quote(str(target_dir)))
    helper_commit = _PROMPTS['run_commit_python_m_js_commit_helper_c_3'].format(shlex_quote_str_target_dir=shlex.quote(str(target_dir)))
    prompt = (
        _PROMPTS['run_commit_commit_all_work_in_this_target'].format(target_dir=target_dir, helper_stage=helper_stage, helper_commit=helper_commit, helper_survey=helper_survey, survey=survey)
    )
    if extra_context and extra_context.strip():
        prompt += _PROMPTS['run_commit_operator_context'].format(extra_context_strip=extra_context.strip())

    return _run_prompt(
        prompt,
        model=model,
        debug=debug,
        debug_file=debug_file,
        agent="commit",
        session=session,
        save=save,
        resume_prefix=f"js --commit {target_dir}",
        reasoning=reasoning,
        maxout=maxout,
        extras=extras,
        config_pins=True,
        tool_context=ToolContext(cwd=target_dir),
        ignore_local_config=ignore_local_config,
        ignore_global_config=ignore_global_config,
        presets=presets,
    )


def _run_compact_offline(session: str, *, agent: str | None = None, focus: str = "", extras: list[str] | None = None, model: str | None = None) -> int:
    try:
        cfg = _from_env(session, save_session=True, extras=extras, agent_id=agent)
        prompt_spec = P.load_configured_prompt_spec(cfg)
        messages = M.load_replay_messages(cfg.session_file)
        compact_cfg = replace(cfg, model=model) if model is not None else cfg
        result = compaction.compact_now_sync(compact_cfg, prompt_spec.system, messages, focus=focus, forced=True,
                                             emit=compaction.printing(),
                                             log_dir=compaction.flight_log_dir(compact_cfg))
    except Exception as e:  # noqa: BLE001
        msgs.warn(msgs.FAILED, error=_error_text(e))
        return 1
    msgs.say(msgs.COMPACTION_DONE, result=result)
    return 0

def _providers_json() -> list[dict]:
    saved = logins.load_logins()
    known_ids = {p.id for p in providers.login_providers()}
    rows: list[dict] = []
    for provider in providers.login_providers():
        env_configured = providers.first_env(provider.api_key_env + provider.base_url_env + provider.model_env) is not None
        source = "login" if provider.id in saved else ("env" if env_configured else "registry")
        rows.append({"id": provider.id, "name": provider.display_name, "source": source})
    for provider_id in sorted(set(saved) - known_ids):
        rows.append({"id": provider_id, "name": provider_id, "source": "custom"})
    return rows


def _logins_json() -> list[dict]:
    rows: list[dict] = []
    masked = paranoid.masks(paranoid.mode())
    for provider_id, login in sorted(logins.load_logins().items()):
        item = asdict(login)
        item["provider_id"] = provider_id
        item["has_api_key"] = bool(item.get("provider_api_key"))
        item["has_codex_refresh_token"] = bool(item.get("codex_refresh_token"))
        if masked or codex_auth.is_codex_provider(provider_id):
            item["provider_api_key"] = None
        if masked:
            item["provider_headers"] = {}
            item["xai_refresh_token"] = None
        item["codex_refresh_token"] = None
        rows.append(item)
    return rows


def _resolve_cli_model_override(cfg: Config, model: str | None) -> Config:
    if model is None:
        return cfg
    route = routing.resolve_model_route(
        model,
        configured_provider_id=cfg.provider_id,
        configured_base_url=cfg.provider_base_url,
        configured_api_key=cfg.provider_api_key,
        configured_headers=getattr(cfg, "provider_headers", {}),
        env=os.environ,
        explicit_model=True,
        prefix_overrides_provider=not getattr(cfg, "explicit_provider", False),
    )
    return replace(
        cfg,
        model=route.model,
        provider_id=route.provider_id,
        provider_base_url=route.base_url,
        provider_api_key=route.api_key,
        provider_headers=route.headers,
        vision_enabled=vision_enabled_for_model(route.model, getattr(cfg, "settings", None)),
    )



def _list_models_payload(provider_id: str | None, cfg: Config | None = None) -> dict:
    if provider_id:
        provider = providers.normalize_provider_id(provider_id) or provider_id
        model_ids = _models_for_provider(provider, None, None)
    elif cfg is not None:
        provider = cfg.provider_id
        model_ids = _models_for_provider(cfg.provider_id, cfg.provider_base_url, cfg.provider_api_key)
    else:
        raise ValueError("provider required")
    return {
        "provider_id": provider,
        "models": model_ids,
        "pass_via_model": [f"{provider}/{model_id}" for model_id in model_ids] if provider else list(model_ids),
    }


def _models_cached_or_live(provider_id: str, cfg: Config | None) -> list[str]:
    """Models for one provider, cache first (offline-friendly, fast), live on miss."""
    cached = logins.load_model_cache().get(provider_id)
    if cached is not None:
        return cached
    if cfg is not None and provider_id == cfg.provider_id:
        return _models_for_provider(provider_id, cfg.provider_base_url, cfg.provider_api_key)
    return _models_for_provider(provider_id, None, None)


def _list_models_provider_ids(provider_arg: str | None, cfg: Config | None) -> list[str]:
    """Which providers `--list-models` covers: the named one, else every provider
    the operator has a stake in — saved logins plus the active provider."""
    if provider_arg:
        return [providers.normalize_provider_id(provider_arg) or provider_arg]
    ids = set(logins.load_logins())
    if cfg is not None and cfg.provider_id:
        ids.add(cfg.provider_id)
    return sorted(ids)



def _print_model_list(provider_arg: str | None, cfg: Config | None) -> int:
    """Print one `provider/model` line per model of `provider_arg`, or of
    every saved login when it is None. A provider whose models cannot be
    listed gets a `#` line on stderr and the rest still print."""
    provider_ids = _list_models_provider_ids(provider_arg, cfg)
    if not provider_ids:
        msgs.warn(msgs.NO_LOGINS)
        return 0
    for pid in provider_ids:
        try:
            model_ids = _models_cached_or_live(pid, cfg)
        except Exception as e:  # noqa: BLE001
            msgs.warn(msgs.MODEL_LIST_FAILED, provider=pid, error=f"{type(e).__name__}: {e}")
            continue
        for model_id in model_ids:
            print(f"{pid}/{model_id}" if pid else model_id)
    return 0


def _models_json(provider_id: str | None, cfg: Config | None = None) -> dict:
    payload = _list_models_payload(provider_id, cfg)
    return {"models": payload["models"]}


def _warn_unmatched_presets(presets: list[str], *, ignore_local: bool, ignore_global: bool) -> None:
    from .config import _preset_config_paths

    project_dir = Path.cwd()
    for name in presets:
        candidates = _preset_config_paths(
            [name], project_dir,
            ignore_local_config=ignore_local,
            ignore_global_config=ignore_global,
        )
        if not any(path.exists() for path in candidates):
            if candidates:
                msgs.warn(msgs.PRESET_NOT_FOUND, name=name, looked=" or ".join(str(p) for p in candidates))
            else:
                msgs.warn(msgs.PRESET_NO_CONFIG_DIRS, name=name)


def _print_json(value: object) -> int:
    print(json.dumps(value, ensure_ascii=False))
    return 0


# External binaries js leans on. Assume NO box has them (owner runs many); warn
# once per missing one at startup so a run without one has an obvious cause, but
# NEVER crash or block — `just install` provisions them.
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


async def _close_session_mcp_host(state: dict) -> None:
    host = state.get("mcp_host")
    if host is not None:
        await host.close()


def _restore_history_forms(messages: list[dict], user_bundle, steered: list, before_len: int) -> None:
    """Swap the turn's provider-facing user messages for their history forms."""
    _replace_runtime_user_message(messages, user_bundle.runtime_message,
                                  user_bundle.history_message, before_len)
    for bundle in steered:
        _replace_runtime_user_message(messages, bundle.runtime_message,
                                      bundle.history_message, before_len)


async def _do_turn(cfg, state, telemetry, prompt_spec, user_bundle, turn_cfg, before_len, loop,
                   steer=None, steered=(), notes=()) -> None:
    """One main turn on the async loop. Runs the turn, syncs live-settings
    deltas, persists new messages, then awaits auto-compaction. Owns cancellation
    ENTIRELY: on ^C the turn Task is cancelled, and this handler — never the
    caller — persists partial work and heals orphaned tool_calls, mirroring the
    legacy blocking KeyboardInterrupt path, then re-raises so the job ends
    cancelled.

    ``steer`` hands the runtime lines typed during the turn; ``steered`` holds
    the bundles it has handed over, so their history forms are persisted.
    ``notes`` are the queued reminders ``user_bundle`` carries; a discarded
    turn puts them back in the queue.
    """
    try:
        before_turn_model = _model_from_live_settings(state["settings"])
        before_turn_provider = _provider_from_live_settings(state["settings"])
        before_turn_lock = _live_bool_setting(
            state["settings"], ("SUBAGENT_MODEL_LOCK",), cfg.lock_subagent_model
        )
        await runtime.run_turn_async(
            turn_cfg,
            state["system"],
            state["messages"],
            telemetry,
            trace_override=bool(
                settings.get_dotted(state["settings"], ("TRACE",), cfg.trace)
            ),
            reasoning_effort_override=turn_cfg.reasoning_effort,
            max_output_override=turn_cfg.max_output_tokens,
            tool_registry=state["tool_registry"],
            sampling=login_defaults.sampling(turn_cfg),
            event_hooks=state.get("events"),
            mcp_host=state.get("mcp_host"),
            steer=steer,
        )
        after_turn_model = _model_from_live_settings(state["settings"])
        if after_turn_model != before_turn_model:
            _sync_model_from_live_settings(state)
        after_turn_provider = _provider_from_live_settings(state["settings"])
        _sync_provider_delta_from_live_settings(state, before_turn_provider, after_turn_provider)
        after_turn_lock = _live_bool_setting(
            state["settings"], ("SUBAGENT_MODEL_LOCK",), cfg.lock_subagent_model
        )
        if after_turn_lock != before_turn_lock:
            _sync_tool_registry_from_live_settings(cfg, state)
        _sync_telemetry_from_live_settings(cfg, state, telemetry)
        _restore_history_forms(state["messages"], user_bundle, steered, before_len)
        _persist_turn_messages(turn_cfg, state["messages"])
        await _maybe_auto_compact_async(turn_cfg, state)
    except asyncio.CancelledError:
        _emit_repl_event(state, telemetry, "cancel", reason="cancelled")
        _sync_telemetry_from_live_settings(cfg, state, telemetry)
        if _turn_has_progress(state["messages"], user_bundle.runtime_message):
            print()
            msgs.say(msgs.TURN_INTERRUPTED_KEPT)
            _restore_history_forms(state["messages"], user_bundle, steered, before_len)
            _persist_turn_messages(turn_cfg, state["messages"])
            M.append_mark(cfg.session_file, "turn_interrupted")
            state["messages"][:] = M.balance_orphaned_tool_calls(state["messages"])
        else:
            print()
            msgs.say(msgs.TURN_ABORTED)
            _discard_unstarted_turn(cfg, state["messages"], user_bundle.runtime_message)
            _requeue_notes(state, notes)
            M.append_mark(cfg.session_file, "turn_aborted")
        raise
    except Exception as e:  # noqa: BLE001
        msgs.say(msgs.FAILED, error=_error_text(e))
        if isinstance(e, OSError) and e.errno == errno.ESTALE:
            state["messages"] = M.load_replay_messages(turn_cfg.session_file)
            return
        if _turn_has_progress(state["messages"], user_bundle.runtime_message):
            _restore_history_forms(state["messages"], user_bundle, steered, before_len)
            _persist_turn_messages(turn_cfg, state["messages"])
        else:
            _discard_unstarted_turn(cfg, state["messages"], user_bundle.runtime_message)
            _requeue_notes(state, notes)
        M.append_mark(cfg.session_file, f"error: {msgs.words_of(_error_text(e))}")


def _steer_mode(state: dict) -> str:
    return settings.steer_mode(
        settings.knob(state["settings"], "STEER_MODE")
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
    This is what keeps state['messages'] single-writer while the input line
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
                continue
            if _steer_mode(state) == "batch":
                line = "\n".join([line, *_take_queued(queue)])
            await _run_repl_turn(line, sup, cfg, state, telemetry, prompt_spec, loop, steer_inbox)
            while steer_inbox:
                leftover = "\n".join(steer_inbox)
                steer_inbox.clear()
                await _run_repl_turn(leftover, sup, cfg, state, telemetry, prompt_spec, loop, steer_inbox)
        finally:
            queue.task_done()


async def _run_repl_turn(line, sup, cfg, state, telemetry, prompt_spec, loop, steer_inbox: list[str]) -> None:
    """Run ``line`` as one supervised turn and wait for it to end."""
    prompt_text, line_attachments = attach.split_repl_attachments(line)
    _emit_repl_event(
        state, telemetry, "input", text=prompt_text, attachments=line_attachments
    )
    _sync_telemetry_from_live_settings(cfg, state, telemetry)
    try:
        prompt_text = _expand_skill_line(prompt_text, state)
        turn_cfg = await loop.run_in_executor(None, _live_cfg_snapshot, cfg, state)
        user_bundle = attach.build_user_message(prompt_text, line_attachments, turn_cfg)
    except ValueError as e:
        # AttachmentError / SkillInvocationError (ValueErrors) or a login-gate
        # routing error from re-resolving the live model: degrade to one friendly
        # line, keep the REPL.
        msgs.say(msgs.FAILED, error=e)
        return
    user_bundle = _note_mode_switch(cfg, user_bundle, "repl")
    user_bundle = _note_model_switch(turn_cfg, user_bundle)
    notes = _take_pending_notes(state)
    user_bundle = _with_notes(user_bundle, notes)
    state["messages"][:] = M.balance_orphaned_tool_calls(state["messages"])
    before_len = len(state["messages"])
    state["messages"].append(user_bundle.runtime_message)
    _append_turn(cfg, user_bundle.history_message)
    # Logged as the turn starts, not as the line was typed: a line queued
    # behind a running turn joins the transcript where the model sees it.
    if (sink := _transcript_sink(telemetry)) is not None:
        sink.write_user(line)
    steered: list[attach.UserMessageBundle] = []

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
                    _expand_skill_line(steer_text, state), steer_attachments, turn_cfg
                ),
            )
        except ValueError as e:
            msgs.say(msgs.FAILED, error=e)
            return None
        bundle = attach.UserMessageBundle(
            {**built.runtime_message, "steered": True},
            {**built.history_message, "steered": True},
        )
        steered.append(bundle)
        if (sink := _transcript_sink(telemetry)) is not None:
            sink.write_user(text)
        return bundle.runtime_message

    job = sup.spawn(
        _do_turn(cfg, state, telemetry, prompt_spec, user_bundle, turn_cfg, before_len, loop,
                 steer=take_steer, steered=steered, notes=notes),
        kind="turn",
        label=prompt_text[:40],
    )
    with contextlib.suppress(asyncio.CancelledError):
        await job.task  # _do_turn persists partial work on cancel; keep looping


def _disabled_skills(state: dict) -> tuple[str, ...]:
    """The SKILLS_DISABLED names in the live settings."""
    return turn_settings.project(state.get("settings"))["skills_disabled"]


def _print_skill_catalog(state: dict) -> None:
    """`/skill` alone lists every enabled skill; user-only ones are marked."""
    catalog = skills.discover_skills(Path.cwd(), disabled=_disabled_skills(state))
    if not catalog.skills:
        msgs.say(msgs.NO_SKILLS)
        return
    for skill in catalog.skills:
        entry = msgs.SKILL_ROW_USER_ONLY if not skill.model_invocable else msgs.SKILL_ROW
        msgs.say(entry, name=skill.name, source=skill.source, description=skill.description)
    msgs.say(msgs.SKILL_HINT)


def _default_startup_banner(cfg: Config, state: dict, notices: list[str], errors: list[str]) -> str:
    """What the REPL prints into its scrollback when it is up: the MOTD
    (`js.motd`) raw with its colours reset after, then the startup line,
    notices and errors."""
    text = motd.pick(settings.knob(state["settings"], "MOTD_FILE"), settings.knob(state["settings"], "MOTD_DIR"))
    return "\n".join([
        *([motd.lines_for_scrollback(text.rstrip("\r\n")) + motd.RESET] if text else []),
        msgs.STARTUP_TITLE.line(),
        msgs.STARTUP_AGENT.line(agent=cfg.agent_id),
        msgs.STARTUP_MODEL.line(model=state["model"]),
        msgs.STARTUP_PROMPT.line(prompt=cfg.prompts_dir),
        msgs.STARTUP_MEMORY.line(memory=cfg.session_file),
        "",
        msgs.STARTUP_HINT.line(),
        "",
        *notices,
        *(msgs.failure_line(error) for error in errors),
    ])



def _startup_banner(cfg: Config, state: dict, notices: list[str], errors: list[str]) -> str:
    render = _repl_assembly(state).banner or _default_startup_banner
    return render(cfg, state, notices, errors)

def _echoes(line: str, state: dict) -> bool:
    """Whether a typed line shows in the scrollback: not when dispatch
    (`_handle_command`) takes it as a command, a command or alias from the
    table or an invalid one; yes for what goes to the model, `/skill`, a
    markdown command and a slash-led message (`/usr/bin/x is broken`)."""
    if not line.startswith("/") or _is_skill_invocation(line):
        return True
    verb = line[1:].split(maxsplit=1)[0].lower() if line[1:].strip() else ""
    if verb in _commands(state) or verb in (state.get("aliases") or {}):
        return False
    return not _is_unknown_command(line)


def _is_skill_invocation(line: str) -> bool:
    """`/skill <name> [request]`: a turn whose user message carries the skill."""
    words = line.split(maxsplit=1)
    return len(words) == 2 and words[0] == "/skill"


def _expand_skill_line(prompt_text: str, state: dict) -> str:
    """A `/skill <name> [request]` line becomes the user message carrying that
    skill; SkillInvocationError (a ValueError) names an unknown or disabled
    (SKILLS_DISABLED) skill. A
    `/NAME args` line naming a markdown command (`js.prompt_commands`) becomes
    that command's text with the arguments filled in."""
    words = prompt_text.split(maxsplit=1)
    if not words or not words[0].startswith("/"):
        return prompt_text
    if words[0] != "/skill":
        expanded = prompt_commands.expand(prompt_text, prompt_commands.discover(Path.cwd()))
        return prompt_text if expanded is None else expanded
    catalog = skills.discover_skills(Path.cwd(), disabled=_disabled_skills(state))
    expanded = skills.expand_user_invocation(catalog, prompt_text)
    return prompt_text if expanded is None else expanded


def _is_turn_state_command(line: str, state: dict | None = None) -> bool:
    """Commands that clear/rotate/compact the live message list. Running one while
    a turn appends to state["messages"] races the single-writer discipline the turn
    queue enforces (empty the list mid-turn -> IndexError, dropped output, spurious
    rollback marks) — refuse until the active turn ends."""
    parsed = setcmd.split_command(line) if line.startswith("/") else None
    table = _commands(state)
    return (_repl_assembly(state).guard_turn_state and parsed is not None
            and parsed[0] in table and table[parsed[0]].turn_state)


def _drain_queue(queue: asyncio.Queue) -> int:
    """Drop every pending input line, balancing task_done() so the teardown
    queue.join() still completes. Loop-thread only — asyncio.Queue is not
    thread-safe, so this never runs from the _handle_command executor hop."""
    return len(_take_queued(queue))


def _live_level(state: dict, key: str) -> int:
    """A 0-3 display level from live settings; its js/jsrc value when bad."""
    default = settings.default_value(key)
    value = settings.knob(state.get("settings") or {}, key)
    return value if isinstance(value, int) and value in range(4) else default


def _status_bar_line(cfg: Config, state: dict, turn_active: bool, width: int) -> str:
    """The REPL status bar from live state: what is running, on what, how full."""
    context = runtime.T.STOCK_CONTEXT
    provider = state.get("provider_id") or cfg.provider_id
    model = state.get("model") or cfg.model
    if provider and isinstance(model, str) and model.startswith(f"{provider}/"):
        model = model[len(provider) + 1:]
    prompt_tokens = int(getattr(context, "last_prompt_tokens", 0) or 0)
    cached_tokens = int(getattr(context, "last_cached_tokens", 0) or 0)
    now = time.monotonic()
    phase, output_tokens, throbber = "", None, ""
    if turn_active:
        throbber = screen.throbber_frame(now)
        phase, output_tokens = screen.turn_centre(
            context.turn_status, now=now, show_bytes=_live_level(state, "NET_DISPLAY") >= 2,
        )
    return screen.status_line(
        width,
        clock=time.strftime("%H:%M"),
        provider=provider,
        model=model,
        context_tokens=int(getattr(context, "context_tokens", 0) or 0),
        phase=phase,
        output_tokens=output_tokens,
        throbber=throbber,
        agent_id=cfg.agent_id,
        session_short=screen.session_short(cfg.session_file),
        cache_pct=int(100 * cached_tokens / prompt_tokens) if prompt_tokens else None,
        cost=usage_display.status_text(usage_mod.totals(cfg.session_file)),
    )


def _show_resume(state: dict, telemetry: runtime.Telemetry, width: int) -> None:
    """Draw the last RESUME_EXCHANGES exchanges of the session the way a turn
    draws them: its text printed, its tool calls through the turn's tool trace,
    so a resumed row folds, carries its whole form and unfolds as a live one
    does. ``telemetry``'s exchange factory is the screen's, when there is one."""
    live = state.get("settings") or {}
    count = settings.knob(live, "RESUME_EXCHANGES")
    replay = runtime.Telemetry(None)
    replay.exchange_factory = getattr(telemetry, "exchange_factory", None)
    context = SimpleNamespace(config=SimpleNamespace(settings=live))
    for kind, part in display_mod.resume_parts(state["messages"], count if isinstance(count, int) else 0,
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
    if not prompt_spec.exchanges or state["messages"]:
        return
    turn_cfg = _cfg_for_live_state(cfg, state)
    try:
        await agent_messages.initialize(
            turn_cfg, prompt_spec, state["messages"], telemetry, system=state["system"],
            tool_registry=state["tool_registry"],
            sampling=login_defaults.sampling(turn_cfg),
            event_hooks=state.get("events"), mcp_host=state.get("mcp_host"),
            echo_user=display_mod.echo_user,
        )
    except (KeyboardInterrupt, asyncio.CancelledError):
        msgs.warn(msgs.TURN_INTERRUPTED)
    except Exception as exc:  # noqa: BLE001
        msgs.warn(msgs.FAILED, error=_error_text(exc))


async def _repl_main(cfg, state, telemetry, session, prompt_spec, banner: str = "") -> int:
    """Non-blocking REPL: input, the active turn, and subagents all share ONE
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
    if prompt_spec.exchanges and not state["messages"]:
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

    async def on_line(line: str) -> None:
        if not line:
            return
        if line in _repl_assembly(state).flush_lines:
            # Drop pending input without touching the active turn. Handled
            # here (not in _handle_command) because the queue is loop-owned
            # and _handle_command runs on an executor thread.
            flushed = drop_pending()
            msgs.say(msgs.DROPPED_QUEUED, prompts=msgs.plural(flushed, "queued prompt"))
            return
        if _is_turn_state_command(line, state) and sup.turn_active():
            msgs.say(msgs.TURN_RUNNING, command=line.split()[0])
            return
        handled = await loop.run_in_executor(None, _handle_command, line, state, cfg)
        if handled:
            _sync_telemetry_from_live_settings(cfg, state, telemetry)
            if not state["running"]:
                await close()
            return
        if sup.turn_active() and _steer_mode(state) == "now":
            # The turn writes it to the transcript where the model receives it.
            steer_inbox.append(line)
            return
        queue.put_nowait(line)
        if sup.turn_active() or queue.qsize() > 1:
            msgs.say(msgs.QUEUED, ahead=queue.qsize())

    def on_interrupt() -> bool:
        handler = _repl_assembly(state).interrupt or _interrupt_turn_and_queue
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
        state["running"] = False
        return close()

    async def on_ex(line: str, editor: screen.InputEditor) -> None:
        await exline.run_ex(
            line,
            editor,
            is_command=lambda verb: verb in _commands(state) or verb in (state.get("aliases") or {}),
            dispatch=on_line,
        )

    app, scrollback = screen.build_app(
        prompt=display_mod.prompt,
        history=session.history,
        completer=session.completer,
        on_line=on_line,
        on_interrupt=on_interrupt,
        on_eof=on_eof,
        status=lambda width: _status_bar_line(cfg, state, sup.turn_active(), width),
        editing_mode=lambda: settings.knob(state["settings"], "EDITING_MODE"),
        echo=lambda line: _echoes(line, state),
        on_ex=on_ex,
        key_bindings=merge_key_bindings([
            *(_repl_assembly(state).input_bindings or _repl_input_bindings)(lambda: state["settings"]),
        ]),
        keymap=state.get("keymap"),
    )
    previous_reasoning_factory = telemetry.reasoning_factory
    previous_display_factory = telemetry.display_factory
    previous_exchange_factory = telemetry.exchange_factory
    telemetry.reasoning_factory = lambda level: screen.ScreenReasoningDisplay(loop, scrollback, app, level)
    telemetry.display_factory = lambda markdown: display_mod.Display(
        sys.stdout.write,
        live=screen.ScreenLive(loop, scrollback, app) if markdown else None,
        flush=sys.stdout.flush,
    )
    telemetry.exchange_factory = lambda: screen.ScreenExchange(loop, scrollback, app)
    ticker = loop.create_task(screen.tick(app, sup.turn_active))
    try:
        with screen.capture_stdio(loop, scrollback, app):
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
        stream_transport.install_sink(None)
        ticker.cancel()
        telemetry.reasoning_factory = previous_reasoning_factory
        telemetry.display_factory = previous_display_factory
        telemetry.exchange_factory = previous_exchange_factory
        supervisor.set_current(None)
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer
        for job in sup.jobs():  # backstop: cancel any straggler
            job.task.cancel()
        _emit_session_event(state, telemetry, cfg, "session_end")
        await _close_session_mcp_host(state)
    return 0


def _prompt_origin(cfg: Config) -> prompt_history.Origin:
    """Where a prompt typed now is recorded as typed: the working directory,
    the session's `--session` name, the agent."""
    session = "" if cfg.session_file == Path(os.devnull) else session_store.display_name(cfg.session_file)
    return prompt_history.Origin(cwd=os.getcwd(), session=session, agent=cfg.agent_id)


def _repl_state(cfg, prompt_spec, *, messages: list[dict] | None = None,
                system: str | None = None, model: str | None = None,
                live_settings: dict | None = None, tool_registry=None,
                assembly: ReplAssembly | None = None) -> dict:
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
        tool_registry = _registry_for(cfg).select(prompt_spec.tool_selectors)
    event_hooks = events.EventHooks()
    state = {
        "running": True,
        "messages": messages,
        "system": system,
        "model": model if model is not None else cfg.model,
        # Where state["model"] came from, for the effective /set view. A --model
        # flag never lands in the settings store, so record it here so /set can
        # annotate `MODEL = ... (live: --model flag)`; agent.yaml's `model:` is
        # recorded the same way.
        "model_source": "--model flag" if model is not None else (
            f"agent {cfg.agent_id}" if _agent_model_applies(cfg, prompt_spec, None) else None),
        "provider_id": cfg.provider_id,
        "provider_base_url": cfg.provider_base_url,
        "provider_api_key": cfg.provider_api_key,
        "provider_headers": dict(getattr(cfg, "provider_headers", {}) or {}),
        "settings": live_settings,
        "prompt_spec": prompt_spec,
        "events": event_hooks,
        "tool_registry": tool_registry,
        "tool_selectors": prompt_spec.tool_selectors,
        "compact_notified": False,
        "compact_consecutive": 0,
        "compact_incomplete_consecutive": 0,
        "compact_paused": False,
    }
    if assembly is not None:
        state["repl_assembly"] = assembly
    event_hooks.set_dispatcher(_event_dispatcher(state, cfg))
    return state


def _repl_prompt_session(cfg: Config, keymap, completer, *, assembly: ReplAssembly | None = None) -> PromptSession:
    """The prompt session of the REPL: its history and completer serve both
    REPLs; the --blocking one reads from it. It erases each sent line, and
    `_blocking_echo` prints back the ones that are messages."""
    return PromptSession(
        history=prompt_history.PromptHistory(
            cfg.history_file,
            lambda: _prompt_origin(cfg),
            cwd_first=settings.knob(cfg.settings, "HISTORY_ORDER") == "cwd",
            limit=settings.knob(cfg.settings, "HISTORY_SIZE"),
            state_root=_paths.state_root(),
        ),
        key_bindings=keys_mod.prompt_bindings(keymap),
        completer=completer,
        **(assembly or DEFAULT_REPL_ASSEMBLY).prompt_options(),
    )


def _blocking_echo(raw: str, state: dict) -> str | None:
    """The line the --blocking REPL leaves for a sent line ``raw``: the
    prompt and the line as typed, or None when it ran as a command
    (`_echoes`). The prompt session erases its own copy."""
    line = pastes.expand(raw).strip()
    if not line or not _echoes(line, state):
        return None
    return display_mod.user_line(raw).rstrip("\n")


def _blocking_repl(cfg, state, telemetry, session, prompt_spec) -> None:
    """The --blocking REPL: read a line from ``session``, run it as a command
    or a turn, repeat until exit, EOF, or a second ^C at an idle prompt."""
    mcp_loop = asyncio.Runner()
    interrupt_count = 0
    session.key_bindings = merge_key_bindings([
        *(_repl_assembly(state).input_bindings or _repl_input_bindings)(lambda: state["settings"]),
    ])
    _emit_session_event(state, telemetry, cfg, "session_start")
    if prompt_spec.exchanges and not state["messages"]:
        mcp_loop.run(_initialize_repl_agent(cfg, state, telemetry, prompt_spec))
    while state["running"]:
        try:
            raw = session.prompt(ANSI(display_mod.prompt()))
            if (echo := _blocking_echo(raw, state)) is not None:
                print(echo)
            line = pastes.expand(raw).strip()
            interrupt_count = 0
        except KeyboardInterrupt:
            # One stray ^C at the prompt should not end a session that took real
            # work to build; the second one within the same idle stretch does.
            interrupt_count += 1
            if interrupt_count < _repl_assembly(state).blocking_interrupts_max:
                print()
                msgs.say(msgs.PRESS_CTRL_C_AGAIN)
                continue
            print()
            break
        except EOFError:
            print()
            break
        if not line:
            continue
        if _handle_command(line, state, cfg):
            _sync_telemetry_from_live_settings(cfg, state, telemetry)
            continue
        if (sink := _transcript_sink(telemetry)) is not None:
            sink.write_user(line)

        prompt_text, line_attachments = attach.split_repl_attachments(line)
        _emit_repl_event(
            state,
            telemetry,
            "input",
            text=prompt_text,
            attachments=line_attachments,
        )
        _sync_telemetry_from_live_settings(cfg, state, telemetry)
        try:
            prompt_text = _expand_skill_line(prompt_text, state)
            turn_cfg = _cfg_for_live_state(cfg, state)
            user_bundle = attach.build_user_message(prompt_text, line_attachments, turn_cfg)
        except ValueError as e:
            # AttachmentError / SkillInvocationError (ValueErrors) or a login-gate
            # routing error from re-resolving the live model: degrade to one friendly
            # line, keep the REPL.
            msgs.say(msgs.FAILED, error=e)
            continue

        user_bundle = _note_mode_switch(cfg, user_bundle, "repl")
        user_bundle = _note_model_switch(turn_cfg, user_bundle)
        notes = _take_pending_notes(state)
        user_bundle = _with_notes(user_bundle, notes)
        state["messages"][:] = M.balance_orphaned_tool_calls(state["messages"])
        before_len = len(state["messages"])
        state["messages"].append(user_bundle.runtime_message)
        _append_turn(cfg, user_bundle.history_message)
        try:
            before_turn_model = _model_from_live_settings(state["settings"])
            before_turn_provider = _provider_from_live_settings(state["settings"])
            before_turn_lock = _live_bool_setting(
                state["settings"],
                ("SUBAGENT_MODEL_LOCK",),
                cfg.lock_subagent_model,
            )
            runtime.run_turn(
                turn_cfg,
                state["system"],
                state["messages"],
                telemetry,
                trace_override=bool(
                    settings.get_dotted(state["settings"], ("TRACE",), cfg.trace)
                ),
                reasoning_effort_override=turn_cfg.reasoning_effort,
                max_output_override=turn_cfg.max_output_tokens,
                tool_registry=state["tool_registry"],
                sampling=login_defaults.sampling(turn_cfg),
                event_hooks=state.get("events"),
                mcp_host=state.get("mcp_host"),
                loop_runner=mcp_loop,
            )
            after_turn_model = _model_from_live_settings(state["settings"])
            if after_turn_model != before_turn_model:
                _sync_model_from_live_settings(state)
            after_turn_provider = _provider_from_live_settings(state["settings"])
            _sync_provider_delta_from_live_settings(state, before_turn_provider, after_turn_provider)
            after_turn_lock = _live_bool_setting(
                state["settings"],
                ("SUBAGENT_MODEL_LOCK",),
                cfg.lock_subagent_model,
            )
            if after_turn_lock != before_turn_lock:
                _sync_tool_registry_from_live_settings(cfg, state)
            _sync_telemetry_from_live_settings(cfg, state, telemetry)
            _replace_runtime_user_message(
                state["messages"],
                user_bundle.runtime_message,
                user_bundle.history_message,
                before_len,
            )
            _persist_turn_messages(turn_cfg, state["messages"])
            _maybe_auto_compact(turn_cfg, state)
        except KeyboardInterrupt:
            _emit_repl_event(state, telemetry, "cancel", reason="keyboard_interrupt")
            _sync_telemetry_from_live_settings(cfg, state, telemetry)
            if _turn_has_progress(state["messages"], user_bundle.runtime_message):
                # Turn did real work (assistant/tool messages beyond the user
                # prompt) before ^C landed. Keep it: persist the partial turn,
                # then heal any orphaned tool_calls in memory so the next turn is
                # valid. The mark is informational ONLY — a `rollback_to:` mark
                # would silently re-truncate this turn on the next session load.
                print()
                msgs.say(msgs.TURN_INTERRUPTED_KEPT)
                _replace_runtime_user_message(
                    state["messages"],
                    user_bundle.runtime_message,
                    user_bundle.history_message,
                    before_len,
                )
                _persist_turn_messages(turn_cfg, state["messages"])
                M.append_mark(cfg.session_file, "turn_interrupted")
                state["messages"][:] = M.balance_orphaned_tool_calls(state["messages"])
            else:
                # Stopped before the model produced anything worth keeping — drop
                # the bare user prompt (rollback removes it on reload too).
                print()
                msgs.say(msgs.TURN_ABORTED)
                _discard_unstarted_turn(cfg, state["messages"], user_bundle.runtime_message)
                _requeue_notes(state, notes)
                M.append_mark(cfg.session_file, "turn_aborted")
        except Exception as e:  # noqa: BLE001
            msgs.say(msgs.FAILED, error=_error_text(e))
            if _turn_has_progress(state["messages"], user_bundle.runtime_message):
                _replace_runtime_user_message(state["messages"], user_bundle.runtime_message,
                                              user_bundle.history_message, before_len)
                _persist_turn_messages(turn_cfg, state["messages"])
            else:
                _discard_unstarted_turn(cfg, state["messages"], user_bundle.runtime_message)
                _requeue_notes(state, notes)
            M.append_mark(cfg.session_file, f"error: {msgs.words_of(_error_text(e))}")
    _emit_session_event(state, telemetry, cfg, "session_end")
    mcp_loop.run(_close_session_mcp_host(state))
    model_client.install_asyncgen_shutdown_filter(mcp_loop.get_loop())
    mcp_loop.close()


# --------------------------------------------------------------------------
# --printonly: dry-run dump of what would be sent to the model. NEVER errors —
# every step degrades to a warning and keeps going (that is the whole point).
# --------------------------------------------------------------------------

_PRINTONLY_KNOWN = "tpeibx"  # a = all of these; s/f dropped (PATH slot decides file vs stdout)
_PRINTONLY_TITLES = {
    "t": "TOOLS",
    "p": "PROMPT (raw)",
    "e": "PROMPT (env-expanded)",
    "i": "PROMPT (inlines-expanded)",
    "b": "BENCHMARKS",
    "x": "EXCHANGES",
}


def _printonly_slots(spec: str) -> tuple[str, int | None, str | None]:
    """Parse ``LETTERS[:COUNT][:PATH]``. Positional slots; an empty slot skips.
    ``p::/tmp/x.md`` -> letters 'p', no count, path '/tmp/x.md'. Never raises."""
    parts = (spec or "").split(":", 2)
    letters = parts[0].strip()
    count: int | None = None
    if len(parts) >= 2 and parts[1].strip():
        try:
            n = int(parts[1].strip())
            count = n if n >= 0 else None
        except ValueError:
            msgs.warn(msgs.PRINTONLY_BAD_COUNT, count=parts[1])
    path = parts[2].strip() if len(parts) >= 3 and parts[2].strip() else None
    return letters, count, path


def _printonly_letters(letters: str) -> list[str]:
    """Resolve the letter cluster to an ordered, de-duplicated section list.
    'a' means everything; unknown letters warn and skip; nothing valid -> all."""
    if "a" in letters:
        return list(_PRINTONLY_KNOWN)
    chosen: list[str] = []
    for ch in letters:
        if ch in _PRINTONLY_KNOWN:
            if ch not in chosen:
                chosen.append(ch)
        elif ch == "o":
            msgs.warn(msgs.PRINTONLY_LETTER_NOT_IMPLEMENTED, letter=ch)
        else:
            msgs.warn(msgs.PRINTONLY_UNKNOWN_LETTER, letter=ch)
    return chosen or list(_PRINTONLY_KNOWN)


def _raw_configured_spec(cfg):
    """The assembled prompt spec BEFORE inline-directive expansion — what
    load_configured_prompt_spec builds, minus the expansion pass."""
    roots = tuple(getattr(cfg, "prompt_roots", ()))
    if len(roots) >= 3:
        return P.load_agent_prompt_spec(
            cfg.agent_id, repo_prompts_root=roots[0], global_agents_root=roots[1],
            project_agents_root=roots[2], agents_files=getattr(cfg, "agents_files", ()),
        )
    return P.load_prompt_spec(cfg.prompts_dir)


def _printonly_run(spec: str, *, agent: str | None = None, session: str | None = None,
                   model: str | None = None,
                   extras: list[str] | None = None, ignore_local_config: bool = False,
                   ignore_global_config: bool = False, presets: list[str] | None = None) -> int:
    letters, count, path = _printonly_slots(spec)
    sections = _printonly_letters(letters)

    try:
        cfg = _from_env(
            session, save_session=False, extras=extras, agent_id=agent,
            ignore_local_config=ignore_local_config, ignore_global_config=ignore_global_config,
            presets=presets,
        )
    except Exception as e:  # noqa: BLE001 — printonly never errors
        msgs.warn(msgs.PRINTONLY_NO_CONFIG, error=f"{type(e).__name__}: {e}")
        return 0

    try:
        raw_spec = _raw_configured_spec(cfg)
    except Exception as e:  # noqa: BLE001
        msgs.warn(msgs.PRINTONLY_NO_PROMPT, error=f"{type(e).__name__}: {e}")
        raw_spec = None

    try:
        cfg = _apply_agent_model(cfg, raw_spec, model, config_pins=False)
        cfg = _apply_agent_reasoning(cfg, raw_spec)
        cfg = _resolve_cli_model_override(cfg, model)
    except Exception as e:  # noqa: BLE001 — model routing cannot prevent a prompt preview
        msgs.warn(msgs.PRINTONLY_NO_CONFIG, error=f"{type(e).__name__}: {e}")

    allow_code = bool(getattr(cfg, "allow_inline_code", True))
    timeout_s = int(settings.knob_attr(cfg, "inline_code_timeout_s", "INLINE_CODE_TIMEOUT"))
    system = raw_spec.system if raw_spec is not None else ""
    selectors = raw_spec.tool_selectors if raw_spec is not None else ()

    def _tools() -> str:
        specs = _registry_for(cfg).select(selectors).openai_specs()
        return json.dumps(specs, indent=2, ensure_ascii=False)

    def _bench() -> str:
        benches = P.load_benchmarks(P.resolve_agent_prompt_dir(cfg))
        if not benches:
            return msgs.PRINTONLY_NO_BENCHMARKS.said()
        blocks = []
        for bm in benches:
            body = expand_prompt(
                bm.prompt,
                variables=session_variables(cfg),
                allow_code=allow_code,
                timeout_s=timeout_s,
                max_output_bytes=cfg.max_bash_output_bytes,
                on_error="warn",
            )
            blocks.append(f"# bench {bm.name}\n{body}")
        return "\n\n".join(blocks)

    def _exchanges() -> str:
        """The agent's NN-user.md / NN-agent.md startup exchanges as
        initialize() injects them: expanded, in order, one block per message."""
        exchanges = raw_spec.exchanges if raw_spec is not None else ()
        if not exchanges:
            return msgs.PRINTONLY_NO_EXCHANGES.said()
        blocks = []
        for exchange in exchanges:
            for role in ("user", "agent"):
                text = getattr(exchange, role)
                if text is not None:
                    blocks.append(f"# {role} {exchange.number:02d}\n{P.expand_agent_text(text, cfg)}")
        return "\n\n".join(blocks)

    builders = {
        "t": _tools,
        "p": lambda: system,
        "e": lambda: expand_prompt(
            system,
            variables=session_variables(cfg),
            allow_code=False,
            max_output_bytes=cfg.max_bash_output_bytes,
            on_error="warn",
        ),
        "i": lambda: expand_prompt(
            system,
            variables=session_variables(cfg),
            allow_code=allow_code,
            timeout_s=timeout_s,
            max_output_bytes=cfg.max_bash_output_bytes,
            on_error="warn",
        ),
        "b": _bench,
        "x": _exchanges,
    }

    chunks: list[str] = []
    for ch in sections:
        try:
            body = msgs.words_of(builders[ch]())
        except Exception as e:  # noqa: BLE001 — a failed section is reported, never kills the run
            error = f"{type(e).__name__}: {e}"
            msgs.warn(msgs.PRINTONLY_SECTION_FAILED, section=_PRINTONLY_TITLES[ch], error=error)
            body = msgs.PRINTONLY_SECTION_FAILED.plain(section=_PRINTONLY_TITLES[ch], error=error)
        chunks.append(f"===== {_PRINTONLY_TITLES[ch]} =====\n{body}" if len(sections) > 1 else body)

    text = "\n\n".join(chunks)
    if count is not None:
        text = "\n".join(text.splitlines()[:count])

    if path:
        try:
            Path(path).expanduser().write_text(text + "\n", encoding="utf-8")
            return 0
        except OSError as e:
            msgs.warn(msgs.PRINTONLY_NOT_WRITTEN, path=path, error=e)
    print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    code = _main(argv)
    pending = dict(_pending_switch)
    _pending_switch.clear()
    if pending:
        _exec_session(pending["target"], blocking=pending["blocking"])
    return code


@_session_scope
def _main(argv: list[str] | None = None) -> int:
    dispatch_argv = argv if argv is not None else sys.argv[1:]
    # Before anything reads or writes ~/.js: move the old locations in, once,
    # then make sure every directory of the layout and the user jsrc are there.
    _home.migrate_once()
    with contextlib.suppress(OSError):
        _paths.ensure_home()
        settings.ensure_user_jsrc(_paths.global_config_file())
    _home.sweep_tmp()
    # Handle login/logout before argparse so they don't require a valid agent/config.
    # None of them take -C, so the cwd is already final and .env can load here;
    # otherwise `js --login x` and `js --login=x` (argparse path) would disagree.
    if dispatch_argv and dispatch_argv[0] in (
        "--login", "login", "--logout", "logout", "--models-edit", "models-edit"
    ):
        dotenv.load()
    if dispatch_argv and dispatch_argv[0] in ("--login", "login"):
        from . import login_cli
        return login_cli.main(dispatch_argv[1:])
    if dispatch_argv and dispatch_argv[0] in ("--logout", "logout"):
        from . import login_cli
        return login_cli.main(["logout"] + dispatch_argv[1:])
    if dispatch_argv and dispatch_argv[0] in ("--models-edit", "models-edit"):
        from . import login_cli
        return login_cli.main(["models-edit"] + dispatch_argv[1:])

    parser = msgs.ArgumentParser(add_help=False)
    parser.add_argument("-h", "--help", action=_ShortHelpAction, nargs=0, help=msgs.OPT_HELP.words())
    parser.add_argument("--help-full", action="help", help=msgs.OPT_HELP_FULL.words())
    parser.add_argument("--login", metavar="PROVIDER", nargs="?", const="", help=msgs.OPT_LOGIN.words())
    parser.add_argument("--logout", metavar="PROVIDER", help=msgs.OPT_LOGOUT.words())
    parser.add_argument("-p", "--prompt", nargs="?", const="-", help=msgs.OPT_PROMPT.words())
    parser.add_argument("-f", "--file", dest="files", action="append", default=[], metavar="PATH", help=msgs.OPT_FILE.words())
    parser.add_argument("-a", "--agent", help=msgs.OPT_AGENT.words())
    parser.add_argument("-m", "--model", help=msgs.OPT_MODEL.words())
    parser.add_argument("-u", "--url", dest="url", metavar="SPEC",
                        help=msgs.OPT_URL.words())
    parser.add_argument("-C", dest="cd", metavar="DIR", help=msgs.OPT_CD.words())
    parser.add_argument("-d", "--debug", action="store_true", help=msgs.OPT_DEBUG.words())
    parser.add_argument("--debug-file", dest="debug_file", metavar="PATH", help=msgs.OPT_DEBUG_FILE.words())
    session_group = parser.add_mutually_exclusive_group()
    session_group.add_argument("-s", "--session", nargs="?", const="", metavar="NAME", help=msgs.OPT_SESSION.words())
    session_group.add_argument("--session-key", metavar="KEY", help=msgs.OPT_SESSION_KEY.words())
    parser.add_argument("-n", "--no-save", action="store_true", help=msgs.OPT_NO_SAVE.words())
    parser.add_argument("-q", "--quiet", action="store_true", help=msgs.OPT_QUIET.words())
    parser.add_argument("-r", "--reasoning", help=msgs.OPT_REASONING.words())
    parser.add_argument("--max-out", dest="max_out", type=int, help=msgs.OPT_MAX_OUT.words())
    parser.add_argument("--bench", metavar="AGENT", help=msgs.OPT_BENCH.words())
    parser.add_argument("--loop", nargs="?", const="", metavar="N|NM|NH", help=msgs.OPT_LOOP.words())
    parser.add_argument("--lock", action="store_true", help=msgs.OPT_LOCK.words())
    parser.add_argument("--stats-json", dest="stats_json", metavar="PATH", help=msgs.OPT_STATS_JSON.words())
    parser.add_argument("--stats-csv", dest="stats_csv", metavar="PATH", help=msgs.OPT_STATS_CSV.words())
    parser.add_argument("--blocking", action="store_true", help=msgs.OPT_BLOCKING.words())
    parser.add_argument("--extra", dest="extras", action="append", default=[], metavar="KEY=VALUE",
                        help=msgs.OPT_EXTRA.words())
    parser.add_argument("--preset", dest="presets", action="append", default=[], metavar="NAME[,NAME...]",
                        help=msgs.OPT_PRESET.words())
    parser.add_argument("--ignore-local", action="store_true", help=msgs.OPT_IGNORE_LOCAL.words())
    parser.add_argument("--ignore-global", action="store_true", help=msgs.OPT_IGNORE_GLOBAL.words())
    parser.add_argument("--migrate-config", action="store_true", help=msgs.OPT_MIGRATE_CONFIG.words())
    parser.add_argument("--list", action="store_true", help=msgs.OPT_LIST.words())
    parser.add_argument("--last", action="store_true", help=msgs.OPT_LAST.words())
    parser.add_argument("--json", action="store_true", help=msgs.OPT_JSON.words())
    parser.add_argument("--swarm", metavar="ROOT/NAME", help=msgs.OPT_SWARM.words())
    parser.add_argument("--providers-json", action="store_true", help=msgs.OPT_PROVIDERS_JSON.words())
    parser.add_argument("--logins-json", action="store_true", help=msgs.OPT_LOGINS_JSON.words())
    parser.add_argument("--models-json", nargs="?", const="", metavar="PROVIDER", help=msgs.OPT_MODELS_JSON.words())
    parser.add_argument("--list-models", nargs="?", const="", metavar="PROVIDER", help=msgs.OPT_LIST_MODELS.words())
    parser.add_argument("--refresh-model-catalog", action="store_true", help=msgs.OPT_REFRESH_MODEL_CATALOG.words())
    parser.add_argument("--commit", action="store_true", help=msgs.OPT_COMMIT.words())
    parser.add_argument("--compact", metavar="SESSION", help=msgs.OPT_COMPACT.words())
    parser.add_argument("--im-a-pussy", dest="im_a_pussy", action="store_true",
                        help=msgs.OPT_IM_A_PUSSY.words())
    parser.add_argument("-i", "--no-shell-history", dest="no_shell_history", action="store_true",
                        help=msgs.OPT_NO_SHELL_HISTORY.words())
    parser.add_argument("--printonly", dest="printonly", metavar="LETTERS[:COUNT][:PATH]", nargs="?", const="a",
                        help=msgs.OPT_PRINTONLY.words())
    parser.add_argument("target", nargs="?", help=msgs.OPT_TARGET.words())
    args = parser.parse_args(argv)
    if args.loop is not None:
        from . import loop_controller
        try:
            loop_settings = settings.collect_settings(
                config_paths=jsrc_paths(
                    Path(args.cd).expanduser().resolve() if args.cd else Path.cwd(),
                    ignore_local_config=args.ignore_local, ignore_global_config=args.ignore_global,
                    presets=[name for spec in args.presets for name in spec.split(",") if name.strip()],
                ), extras=args.extras, migrate=False,
            )
            return loop_controller.run(dispatch_argv, args.loop, bench=args.bench, prompt=args.prompt,
                                       settings=loop_settings)
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
    if _json_run_requested(args):
        return _json_run(lambda: _run_args(args, dispatch_argv))
    return _run_args(args, dispatch_argv)


def _json_run_requested(args: argparse.Namespace) -> bool:
    """Whether the command line is a `--json` headless run: a prompt or piped
    stdin, and no mode that refuses `--json` or prints its own output."""
    if not args.json or args.list or (args.prompt is None and sys.stdin.isatty()):
        return False
    return not (
        args.commit or args.bench or args.compact or args.printonly is not None
        or args.login is not None or args.logout or args.providers_json or args.logins_json
        or args.models_json is not None or args.list_models is not None
    )


def _run_args(args: argparse.Namespace, dispatch_argv: list[str]) -> int:
    _session_leases.start = {"mode": None, "command": ["js", *dispatch_argv]}
    if args.url:
        # Desugar before anything reads args.extras. Prepended, not appended, so
        # an explicit --extra on the same command line still overrides a field
        # the spec filled in.
        try:
            endpoint_settings = settings.collect_settings(
                config_paths=jsrc_paths(
                    Path(args.cd).expanduser().resolve() if args.cd else Path.cwd(),
                    ignore_local_config=args.ignore_local, ignore_global_config=args.ignore_global,
                    presets=[name for spec in args.presets for name in spec.split(",") if name.strip()],
                ), extras=args.extras, migrate=False,
            )
            args.extras = endpoint_uri.parse(args.url, settings=endpoint_settings).as_settings() + list(args.extras)
        except endpoint_uri.EndpointSpecError as exc:
            msgs.warn(msgs.BAD_URL_SPEC, error=exc)
            return 2
    flag_extras, flag_error = _setting_flag_extras(args)
    if flag_error is not None:
        return flag_error
    # After --extra, so a flag beats an --extra naming the same setting.
    args.extras = [*args.extras, *flag_extras]
    presets = [name for spec in args.presets for name in spec.split(",") if name.strip()]
    set_invocation(extras=args.extras, presets=presets,
                   ignore_local_config=args.ignore_local, ignore_global_config=args.ignore_global)
    if args.session == "":
        # A bare --session opens the picker; the chosen session is resumed in
        # its own directory and agent, jailed there when it started under -C.
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            msgs.warn(msgs.SESSIONS_NEEDS_TERMINAL)
            return 2
        try:
            choice = session_picker.pick_session(Path.cwd())
            target = _session_target(choice) if choice is not None else None
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
    if args.json and not args.list and (
        (args.prompt is None and sys.stdin.isatty())
        or args.commit or args.bench or args.compact or args.printonly is not None
    ):
        msgs.warn(msgs.JSON_NEEDS_LIST)
        return 2
    if args.list:
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
        return _print_session_list(json_lines=args.json)
    if args.last:
        if args.session is not None:
            msgs.warn(msgs.LAST_WITH_SESSION)
            return 2
        try:
            last_agent = _invocation_agent(args, presets)
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
            effective_agent = _invocation_agent(args, presets)
        except ValueError as e:
            msgs.warn(msgs.FAILED, error=e)
            return 2
        args.session = derive_session_name(effective_agent, Path.cwd(), args.session_key)
    _session_leases.caller_key = args.session_key
    if presets:
        # Resolve against the now-final cwd (after any -C). A name that matches no
        # file anywhere is almost certainly a typo — say so rather than no-op.
        _warn_unmatched_presets(presets, ignore_local=args.ignore_local, ignore_global=args.ignore_global)
    if args.login is not None:
        from . import login_cli
        return login_cli.main([args.login] if args.login else [])
    if args.logout:
        from . import login_cli
        return login_cli.main(["logout", args.logout])
    if args.providers_json:
        return _print_json(_providers_json())
    if args.logins_json:
        return _print_json(_logins_json())
    if args.models_json is not None:
        try:
            cfg = None
            provider_arg = args.models_json or None
            if provider_arg is None:
                cfg = _from_env(
                    args.session,
                    save_session=False,
                    extras=args.extras,
                    agent_id=args.agent,
                    ignore_local_config=args.ignore_local,
                    ignore_global_config=args.ignore_global,
                )
            return _print_json(_models_json(provider_arg, cfg))
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
            return 1
    if args.list_models is not None:
        try:
            cfg = None
            provider_arg = args.list_models or None
            if provider_arg is None:
                cfg = _from_env(
                    args.session,
                    save_session=False,
                    extras=args.extras,
                    agent_id=args.agent,
                    ignore_local_config=args.ignore_local,
                    ignore_global_config=args.ignore_global,
                )
            return _print_model_list(provider_arg, cfg)
        except Exception as e:  # noqa: BLE001
            msgs.warn(msgs.FAILED, error=f"{type(e).__name__}: {e}")
            return 1
    if args.refresh_model_catalog:
        if not _force_refresh_model_catalog():
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

    if args.printonly is not None:
        return _printonly_run(
            args.printonly, agent=cli_agent, session=args.session, model=args.model, extras=args.extras,
            ignore_local_config=args.ignore_local, ignore_global_config=args.ignore_global,
            presets=presets,
        )

    if args.lock:
        try:
            agent_lock.acquire(args.bench and validate_agent_id(args.bench) or _invocation_agent(args, presets))
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
        return _run_bench(args.bench, model=args.model, reasoning=args.reasoning,
                          maxout=args.max_out, quiet=args.quiet, extras=args.extras,
                          ignore_local_config=args.ignore_local,
                          ignore_global_config=args.ignore_global, presets=presets,
                          stats_json=args.stats_json, stats_csv=args.stats_csv,
                          debug=args.debug)

    if args.compact:
        return _run_compact_offline(args.compact, agent=cli_agent, focus=args.prompt or "", extras=args.extras, model=args.model)

    if args.migrate_config:
        return _run_migrate_config()

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
        return _run_commit(args.target, model=args.model, debug=args.debug, debug_file=args.debug_file,
                           session=args.session, save=not args.no_save,
                           reasoning=args.reasoning, maxout=args.max_out,
                           extra_context=extra_context, extras=args.extras,
                           ignore_local_config=args.ignore_local,
                           ignore_global_config=args.ignore_global, presets=presets)

    try:
        automatic_agent = _invocation_agent(args, presets)
        automatic_dir = P._most_specific_prompt_dir(
            automatic_agent, Path(P.__file__).resolve().parents[1] / "prompts",
            _paths.global_agents_dir(), Path.cwd() / ".js" / "agents",
        )
    except ValueError as exc:
        msgs.warn(msgs.FAILED, error=exc)
        return 2
    if P.has_benchmarks(automatic_dir):
        if args.json:
            msgs.warn(msgs.JSON_NEEDS_LIST)
            return 2
        return _run_bench(
            automatic_agent, model=args.model, reasoning=args.reasoning,
            maxout=args.max_out, quiet=args.quiet, extras=args.extras,
            ignore_local_config=args.ignore_local, ignore_global_config=args.ignore_global,
            presets=presets, stats_json=args.stats_json, stats_csv=args.stats_csv, debug=args.debug,
        )

    if args.files and args.prompt is None and sys.stdin.isatty():
        msgs.warn(msgs.FILE_NEEDS_PROMPT)
        return 2

    if args.prompt is not None or not sys.stdin.isatty():
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
        elif args.prompt in {None, "-"}:
            prompt = _read_stdin_if_piped()
        elif sys.stdin.isatty():
            prompt = args.prompt
        else:
            stdin_text = _read_stdin_if_piped()
            prompt = args.prompt if not stdin_text.strip() else f"{args.prompt.rstrip()}\n\n{stdin_text.strip()}"
        _session_leases.start["mode"] = "-p" if args.prompt is not None else "pipe"
        generated_session = (
            not args.no_save
            and args.session is None
            and os.environ.get("JS_SESSION") is None
        )
        run = _run_prompt_json if args.json else _run_prompt
        with headless.watch_output():
            result = run(prompt, model=args.model, debug=args.debug, debug_file=args.debug_file,
                         agent=args.agent, session=args.session, save=not args.no_save,
                         caller_key=args.session_key, announce_generated=generated_session,
                         reasoning=args.reasoning, maxout=args.max_out,
                         show_continue=not args.quiet,
                         extras=args.extras,
                         ignore_local_config=args.ignore_local,
                         ignore_global_config=args.ignore_global,
                         files=args.files,
                         stdin_attachment=stdin_attachment,
                         presets=presets,
                         stats_json=args.stats_json, stats_csv=args.stats_csv,
                         swarm=args.swarm)
        if args.no_save:
            msgs.warn(msgs.NOT_SAVED_NO_RESUME)
        return result

    os.environ["JS_MODE"] = "repl"
    _session_leases.start["mode"] = "repl"
    try:
        cfg = _from_env(
            args.session,
            save_session=not args.no_save,
            extras=args.extras,
            agent_id=cli_agent,
            ignore_local_config=args.ignore_local,
            ignore_global_config=args.ignore_global,
            presets=presets,
        )
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
    resumed = last_stamp(cfg.session_file)
    unreachable_stamp = (_stamp_without_login(resumed, cfg)
                         if args.model is None and resumed is not None else None)
    if args.model is None and resumed is not None:
        remembered_model = _resume_model_spec(resumed, cfg)
        if remembered_model:
            args.model = remembered_model
            notices.append(msgs.line_for(msgs.RESUMED_MODEL, model=remembered_model))
    try:
        prompt_spec = P.load_configured_prompt_spec(cfg, expand=False)
    except (FileNotFoundError, ValueError) as e:
        msgs.warn(msgs.FAILED, error=_format_prompt_load_error(cfg, e))
        return 2
    system = prompt_spec.system
    # A session sends the system prompt it was born with, byte for byte, for its
    # whole life. Rebuilding it per launch puts a fresh clock and load average in
    # front of an append-only history and breaks the shared prefix at byte zero.
    # A change to the prompt files on disk is reported to the model as a message
    # at the end instead. The change is judged on the files before directive
    # expansion, since expanded output such as a clock differs on every launch.
    recorded_system = M.load_system_prompt(cfg.session_file)
    if recorded_system is not None:
        system = recorded_system
    active_registry = _registry_for(cfg).select(prompt_spec.tool_selectors)
    try:
        cfg = _apply_agent_model(cfg, prompt_spec, args.model)
        cfg = _apply_agent_max_tokens(cfg, prompt_spec)
        cfg = _resolve_cli_model_override(cfg, args.model)
    except ValueError as e:
        msgs.warn(msgs.FAILED, error=e)
        return 2
    # Compared with what the resumed model and agent resolve to, not the
    # startup model.
    resumed_reasoning = (
        _resume_reasoning(resumed, login_defaults.for_turn(login_defaults.with_agent(cfg, prompt_spec)))
        if resumed is not None and args.reasoning is None else None)
    if unreachable_stamp is not None:
        msgs.say(msgs.RESUME_MODEL_FALLBACK, stamped=unreachable_stamp,
                 model=_stamp_label(cfg.model, cfg.provider_id))
    prompt_spec = P._expand_spec(prompt_spec, cfg)
    if recorded_system is None:
        system = prompt_spec.system

    keymap, key_errors = keys_mod.load(keys_mod.keys_file(cfg.settings))
    completer = replcomplete.JsCompleter(
        commands=lambda: _command_completions(state),
        setting_keys=[spec.key for spec in settings.REGISTRY if spec.command == "set"],
        fset_names=[*(spec.key for spec in settings.REGISTRY if spec.command == "fset"), *fset.names()],
        names=lambda: sorted(set(providers.known_provider_ids()) | set(logins.load_logins())),
        spell=replcomplete.hunspell_suggest,
        bare_words=QUIT_WORDS,
    )
    session = _repl_prompt_session(cfg, keymap, completer)

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
        notice = {"role": "user", "content": _PROMPT_CHANGED_NOTICE}
        messages.append(notice)
        _append_turn(cfg, notice)
        notices.append(msgs.line_for(msgs.PROMPT_CHANGED))

    live_settings = copy.deepcopy(cfg.settings) if isinstance(cfg.settings, dict) else {}
    # A resumed session's reasoning is this session's own choice.
    resumed_layer = _resumed_layer(resumed, args.reasoning)
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
        # it survives the _cfg_for_live_state rebuild. Config/env/--max-out win.
        settings.set_dotted(live_settings, ("OUTPUT_TOKENS_MAX",), prompt_spec.max_output_tokens)
    state = _repl_state(
        cfg, prompt_spec,
        messages=messages,
        system=system,
        model=args.model,
        live_settings=live_settings,
        tool_registry=active_registry,
    )
    state["keymap"] = keymap
    state["live_resume"] = resumed_layer
    if cut_off:
        _queue_note(state, _CUT_OFF_NOTICE)
    rc_errors = _run_rc_commands(state, cfg, jsrc_paths(
        Path(getattr(cfg, "project_dir", None) or Path.cwd()),
        ignore_local_config=args.ignore_local,
        ignore_global_config=args.ignore_global,
        presets=presets,
    ))
    telemetry = runtime.Telemetry(debug_log=cfg.debug_log)
    state["mcp_host"] = _session_mcp_host(cfg, telemetry)
    # Attach the debug autolog sink before the first turn (both REPL
    # variants below share this telemetry object).
    _sync_telemetry_from_live_settings(cfg, state, telemetry)

    banner = _startup_banner(cfg, state, notices, [*rc_errors, *key_errors])
    transcript_stack = contextlib.ExitStack()
    _enter_transcript_stdio(transcript_stack, telemetry)
    if not args.blocking:
        try:
            code = model_client.run_owning_loop(
                _repl_main(cfg, state, telemetry, session, prompt_spec, banner)
            )
        finally:
            transcript_stack.close()
        _queue_switch(state, blocking=False)
        return code
    print(msgs.for_stream(banner))
    _show_resume(state, telemetry, display_mod.terminal_width() - 1)
    try:
        _blocking_repl(cfg, state, telemetry, session, prompt_spec)
    finally:
        transcript_stack.close()
    _print_resume_hint(cfg, state)
    _queue_switch(state, blocking=True)
    return 0
if __name__ == "__main__":
    sys.exit(main())
