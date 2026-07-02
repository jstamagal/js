"""Interactive REPL. prompt_toolkit for proper readline behavior."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import functools
import io
import json
import os
import shlex
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import CompleteStyle

from . import supervisor

from . import attach, codex_auth, colors as C
from . import events
from . import logins
from . import memory as M
from . import model_metadata
from . import persona as P
from . import picker
from . import providers
from . import replcomplete
from . import runtime
from . import stats
from . import paths as _paths
from .promptexpand import expand_prompt
from . import setcmd
from . import settings
from . import routing
from .sampling import Sampling
from .config import Config, from_env, validate_agent_id, _norm_effort, vision_enabled_for_model
from .toolkit.artifact import build_artifact_system
from .toolkit.wiki import build_wiki_system, infer_vault
from .toolkit.wiki.helpers import resolve_vault
from .toolkit.registry import build_default_registry
from .toolkit import ToolContext

_FULL_REGISTRY = build_default_registry()


def _registry_for(cfg) -> object:
    # The default registry exposes the subagent `model` override on the task tool.
    # When the operator has locked subagent model selection, rebuild without that
    # flag so the param is gone from both the tool description and its schema.
    if getattr(cfg, "lock_subagent_model", False):
        return build_default_registry(cfg.prompt_roots, flags=())
    return _FULL_REGISTRY

# --------------------------------------------------------------------------
# Runtime knobs: name -> (type, label, description)
# --------------------------------------------------------------------------

_BOOL_WORDS_ON = {"on", "true", "yes", "1"}
_BOOL_WORDS_OFF = {"off", "false", "no", "0"}


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


def _cfg_from_env_compat(
    session: str | None,
    *,
    save_session: bool,
    extras: list[str] | None,
    agent_id: str | None = None,
    ignore_local_config: bool = False,
    ignore_global_config: bool = False,
    presets: list[str] | None = None,
) -> Config:
    try:
        return _from_env(
            session,
            save_session=save_session,
            extras=extras,
            agent_id=agent_id,
            ignore_local_config=ignore_local_config,
            ignore_global_config=ignore_global_config,
            presets=presets,
        )
    except TypeError:
        # Tests and external callers may monkeypatch the old helper signature.
        if agent_id is None:
            return _from_env(session, save_session=save_session, extras=extras)
        return _from_env(session, save_session=save_session, extras=extras, agent_id=agent_id)

def _append_turn(cfg: Config, message: dict) -> None:
    M.append_message(cfg.session_file, message)


def _session_hint_arg(cfg: Config) -> str:
    try:
        return cfg.session_file.relative_to(cfg.sessions_dir).with_suffix("").as_posix()
    except ValueError:
        return str(cfg.session_file)


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




def _compact_cfg(cfg: Config, key: str, default):
    settings = getattr(cfg, "settings", {}) or {}
    compact = settings.get("compact", {}) if isinstance(settings, dict) else {}
    return compact.get(key, default) if isinstance(compact, dict) else default


def _compact_bool(cfg: Config, key: str, default: bool) -> bool:
    value = _compact_cfg(cfg, key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default

def _compact_int(cfg: Config, key: str, default: int) -> int:
    raw = _compact_cfg(cfg, key, default)
    if isinstance(raw, bool):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _compact_float(cfg: Config, key: str, default: float) -> float:
    raw = _compact_cfg(cfg, key, default)
    if isinstance(raw, bool):
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if 0.0 < value <= 1.0 else default


def _compact_thresholds(cfg: Config) -> tuple[float, float, float]:
    notify_at = _compact_float(cfg, "notify_threshold", 0.50)
    trigger_at = _compact_float(cfg, "trigger_threshold", 0.80)
    force_at = _compact_float(cfg, "force_threshold", 0.90)
    if not (notify_at <= trigger_at <= force_at):
        return 0.50, 0.80, 0.90
    return notify_at, trigger_at, force_at

def _cfg_for_active_model(cfg: Config, state: dict) -> Config:
    model = state.get("model")
    if not (isinstance(model, str) and model):
        model = cfg.model
    route = routing.resolve_model_route(
        model,
        configured_provider_id=state.get("provider_id", cfg.provider_id),
        configured_base_url=state.get("provider_base_url", cfg.provider_base_url),
        configured_api_key=state.get("provider_api_key", cfg.provider_api_key),
        configured_headers=state.get("provider_headers", getattr(cfg, "provider_headers", {})),
        env=os.environ,
        explicit_model=True,
        discover_env=False,
        use_saved_login=False,
    )
    route_vision = vision_enabled_for_model(route.model)
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


_LIVE_LIMIT_FIELDS: tuple[tuple[str, tuple[str, str]], ...] = (
    ("max_tool_iterations", ("limits", "max_tool_iterations")),
    ("max_bash_output_bytes", ("limits", "max_bash_output_bytes")),
    ("max_tool_result_bytes", ("limits", "max_tool_result_bytes")),
    ("fetch_timeout_s", ("limits", "fetch_timeout_s")),
    ("inline_code_timeout_s", ("limits", "inline_code_timeout_s")),
    ("max_read_lines", ("limits", "max_read_lines")),
    ("max_line_chars", ("limits", "max_line_chars")),
    ("jsonl_max_line_chars", ("limits", "jsonl_max_line_chars")),
    ("max_file_bytes", ("limits", "max_file_bytes")),
    ("task_max_depth", ("limits", "task_max_depth")),
    ("wiki_vault_lock_timeout_s", ("limits", "wiki_vault_lock_timeout_s")),
)


_LIVE_OPTIONAL_INT_FIELDS: tuple[tuple[str, tuple[str, str]], ...] = (
    ("max_output_tokens", ("model", "max_output_tokens")),
)


_LIVE_OPTIONAL_STRING_FIELDS: tuple[tuple[str, tuple[str, str]], ...] = (
    ("artifact_dir", ("artifact", "dir")),
    ("artifact_url", ("artifact", "url")),
    ("artifact_bin", ("artifact", "bin")),
)


_LIVE_BOOL_FIELDS: tuple[tuple[str, tuple[str, str]], ...] = (
    ("trace", ("runtime", "trace")),
    ("prefer_inherit", ("subagents", "prefer_inherit")),
    ("lock_subagent_model", ("subagents", "lock_model")),
)


def _live_int_setting(live_settings: dict, path: tuple[str, str], default: int) -> int:
    raw = settings.get_dotted(live_settings, path, default)
    if isinstance(raw, bool):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _live_optional_int_setting(live_settings: dict, path: tuple[str, str], default: int | None) -> int | None:
    missing = object()
    raw = settings.get_dotted(live_settings, path, missing)
    if raw is missing:
        return default
    if raw is None:
        return None
    if isinstance(raw, bool):
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


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
    raw = settings.get_dotted(live_settings, ("model", "reasoning_effort"), missing)
    if raw is missing:
        return default
    if raw is None:
        return default
    if isinstance(raw, str):
        return _norm_effort(raw)
    return default


def _live_bool_setting(live_settings: dict, path: tuple[str, str], default: bool) -> bool:
    raw = settings.get_dotted(live_settings, path, default)
    return raw if isinstance(raw, bool) else default


def _debug_log_from_live_settings(cfg: Config, live_settings: dict) -> Path | None:
    enabled = _live_bool_setting(live_settings, ("runtime", "debug"), cfg.debug_log is not None)
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


def _cfg_for_live_state(cfg: Config, state: dict) -> Config:
    active = _cfg_for_active_model(cfg, state)
    live_settings = state["settings"]
    updates = {"settings": live_settings}
    for attr, path in _LIVE_LIMIT_FIELDS:
        updates[attr] = _live_int_setting(live_settings, path, getattr(active, attr))
    for attr, path in _LIVE_OPTIONAL_INT_FIELDS:
        updates[attr] = _live_optional_int_setting(live_settings, path, getattr(active, attr))
    updates["reasoning_effort"] = _live_reasoning_effort_setting(
        live_settings,
        active.reasoning_effort,
    )
    for attr, path in _LIVE_OPTIONAL_STRING_FIELDS:
        updates[attr] = _live_optional_str_setting(live_settings, path, getattr(active, attr))
    for attr, path in _LIVE_BOOL_FIELDS:
        updates[attr] = _live_bool_setting(live_settings, path, getattr(active, attr))
    return replace(active, **updates)


def _sync_tool_registry_from_live_settings(cfg: Config, state: dict) -> None:
    selectors = state.get("tool_selectors", ())
    state["tool_registry"] = _registry_for(_cfg_for_live_state(cfg, state)).select(selectors)


def _state_value(state: dict, key: str, default):
    return state[key] if key in state else default

def _sampling_for_turn(
    cfg: Config,
    prompt_spec,
    cli_override: Sampling | None = None,
) -> Sampling:
    raw_manifest = getattr(prompt_spec, "sampling", {}) if prompt_spec else {}
    manifest_sampling = Sampling.from_mapping(raw_manifest)
    return (
        cfg.sampling_setscript
        .merge(manifest_sampling)
        .merge(cfg.sampling_env)
        .merge(cli_override or Sampling())
    )


def _sampling_override_after_set(line: str, current: Sampling) -> Sampling:
    body = line.strip()
    if body.startswith("/"):
        body = body[1:].lstrip()
    parts = body.split(maxsplit=2)
    if len(parts) < 2 or parts[0].lower() != "set":
        return current
    key = parts[1]
    unset = key.startswith("-") and len(key) > 1
    if unset:
        key = key[1:]
    if not key.startswith("sampling."):
        return current
    field = key.split(".", 1)[1]
    if field not in Sampling.__dataclass_fields__:
        return current
    if unset:
        # `/set -sampling.x` drops the live override so the field reverts to its
        # jsrc/provider default instead of lingering at its last value.
        return replace(current, **{field: None})
    if len(parts) != 3:
        return current
    spec = settings.SPEC_BY_KEY.get(key)
    if spec is None:
        return current
    value, error = settings.coerce_value(spec, parts[2])
    if error is not None:
        return current
    return replace(current, **{field: value})


def _sampling_override_from_live_settings(live_settings: dict) -> Sampling:
    raw = settings.get_dotted(live_settings, ("sampling",), {})
    return Sampling.from_mapping(raw if isinstance(raw, dict) else {})


def _model_from_live_settings(live_settings: dict) -> str | None:
    raw = settings.get_dotted(live_settings, ("model", "id"), None)
    return raw if isinstance(raw, str) and raw else None


def _sync_model_from_live_settings(state: dict) -> None:
    model = _model_from_live_settings(state["settings"])
    if model is not None:
        state["model"] = model


def _provider_from_live_settings(live_settings: dict) -> tuple[str | None, str | None, str | None]:
    def _value(path: tuple[str, str]) -> str | None:
        raw = settings.get_dotted(live_settings, path, None)
        return raw if isinstance(raw, str) and raw else None

    return (
        _value(("provider", "id")),
        _value(("provider", "base_url")),
        _value(("provider", "api_key")),
    )


def _sync_provider_from_live_settings(state: dict, changed_keys: list[str]) -> None:
    provider_id, base_url, api_key = _provider_from_live_settings(state["settings"])
    changed = set(changed_keys)
    if "provider.id" in changed:
        if provider_id is None:
            state["provider_id"] = None
            state["provider_base_url"] = None
            state["provider_api_key"] = None
            state["provider_headers"] = {}
        else:
            _set_provider_state(state, provider_id)
    if "provider.base_url" in changed:
        state["provider_base_url"] = base_url
    if "provider.api_key" in changed:
        state["provider_api_key"] = api_key


def _sync_provider_delta_from_live_settings(
    state: dict,
    before: tuple[str | None, str | None, str | None],
    after: tuple[str | None, str | None, str | None],
) -> None:
    changed_keys: list[str] = []
    if before[0] != after[0]:
        changed_keys.append("provider.id")
    if before[1] != after[1]:
        changed_keys.append("provider.base_url")
    if before[2] != after[2]:
        changed_keys.append("provider.api_key")
    if changed_keys:
        _sync_provider_from_live_settings(state, changed_keys)


def _changed_model_key(keys: list[str]) -> bool:
    return "model.id" in keys


def _changed_provider_key(keys: list[str]) -> bool:
    return any(key in {"provider.id", "provider.base_url", "provider.api_key"} for key in keys)


def _changed_sampling_key(keys: list[str]) -> bool:
    return any(key.startswith("sampling.") for key in keys)


def _changed_lock_subagent_model_key(keys: list[str]) -> bool:
    return "subagents.lock_model" in keys


def _event_result_changed_keys(results: list[events.EventHandlerResult]) -> list[str]:
    return [key for result in results for key in result.changed_keys]


def _event_results_changed_sampling(results: list[events.EventHandlerResult]) -> bool:
    return any(_changed_sampling_key(result.changed_keys) for result in results)


def _event_results_changed_model(results: list[events.EventHandlerResult]) -> bool:
    return any(_changed_model_key(result.changed_keys) for result in results)


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


def _maybe_auto_compact(cfg: Config, state: dict) -> None:
    if not _compact_bool(cfg, "auto", True):
        return
    prompt_tokens = int(getattr(runtime.T.DEFAULT_CONTEXT, "last_prompt_tokens", 0) or 0)
    active_cfg = _cfg_for_active_model(cfg, state)
    inferred_window = runtime._resolve_context_window(active_cfg.model, active_cfg.provider_id)
    context_window = _compact_int(active_cfg, "context_window", inferred_window or 0)
    if prompt_tokens <= 0 or context_window <= 0:
        return
    fullness = prompt_tokens / context_window
    notify_at, trigger_at, force_at = _compact_thresholds(cfg)
    if fullness < trigger_at:
        state["compact_consecutive"] = 0
        state["compact_paused"] = False
        if fullness < notify_at:
            state["compact_notified"] = False
        elif not state.get("compact_notified"):
            print(f"{C.ORANGE}(context {fullness:.0%} full; auto-compaction armed){C.RESET}")
            state["compact_notified"] = True
        return
    if state.get("compact_paused"):
        return
    if fullness >= notify_at and not state.get("compact_notified"):
        print(f"{C.ORANGE}(context {fullness:.0%} full; auto-compaction armed){C.RESET}")
        state["compact_notified"] = True
    forced = fullness >= force_at
    compact_cfg = _cfg_for_active_model(cfg, state)
    result = runtime.compact_messages(compact_cfg, state["system"], state["messages"], forced=forced)
    print(f"{C.GREY}({result}){C.RESET}")
    state["compact_consecutive"] = int(state.get("compact_consecutive", 0) or 0) + 1
    if state["compact_consecutive"] >= 2:
        state["compact_paused"] = True
        print(f"{C.ORANGE}(auto-compaction paused after two consecutive turns; resumes when context drops below trigger){C.RESET}")



def _login_for_provider(provider_id: str | None, base_url: str | None, api_key: str | None) -> logins.Login:
    canonical_id = providers.normalize_provider_id(provider_id) or provider_id
    if not canonical_id:
        raise ValueError("no provider set")
    saved = logins.load_logins().get(canonical_id)
    if saved is not None:
        return saved
    provider_def = providers.provider_for_login(canonical_id)
    return logins.Login(
        provider_id=canonical_id,
        sdk_provider_id=provider_def.effective_sdk_provider_id,
        provider_base_url=providers.provider_base_url(provider_def, base_url, os.environ),
        provider_api_key=providers.provider_api_key(provider_def, api_key, os.environ),
    )


def _models_for_provider(provider_id: str | None, base_url: str | None, api_key: str | None) -> list[str]:
    return logins.test_login(_login_for_provider(provider_id, base_url, api_key))


def _provider_qualified_model_id(provider_id: str | None, model: str) -> str:
    if not provider_id:
        return model
    parsed_provider_id, parsed_model = providers.parse_model_prefix(model)
    if parsed_provider_id == provider_id and parsed_model:
        return model
    return f"{provider_id}/{model}"


def _is_active_model_id_line(line: str) -> bool:
    body = line.strip()
    if not body or body.startswith("#"):
        return False
    parts = body.split(maxsplit=2)
    return len(parts) >= 2 and parts[0].lower() == "set" and parts[1] == "model.id"


def _persist_default_model_id(model_id: str) -> tuple[Path | None, str | None]:
    config_path = _paths.global_config_file()
    try:
        settings.write_default_template(config_path)
        if config_path.exists():
            lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
        else:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            lines = []
        replacement = f"set model.id {model_id}\n"
        changed = False
        saw_model_line = False
        new_lines: list[str] = []
        for line in lines:
            if _is_active_model_id_line(line):
                saw_model_line = True
                if line != replacement:
                    changed = True
                new_lines.append(replacement)
            else:
                new_lines.append(line)
        if not saw_model_line:
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines[-1] = new_lines[-1] + "\n"
            new_lines.append(replacement)
            changed = True
        if changed:
            config_path.write_text("".join(new_lines), encoding="utf-8")
        return config_path, None
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _set_provider_state(state: dict, provider_id: str) -> None:
    canonical_id = providers.normalize_provider_id(provider_id) or provider_id
    if _apply_saved_login_to_state(state, canonical_id):
        return
    provider_def = providers.provider_for_login(canonical_id)
    state["provider_id"] = canonical_id
    state["provider_headers"] = {}
    state["provider_base_url"] = providers.provider_base_url(provider_def, None, os.environ)
    state["provider_api_key"] = providers.provider_api_key(provider_def, None, os.environ)


# --------------------------------------------------------------------------
# Banner / help
# --------------------------------------------------------------------------

BANNER = f"""\
{C.CYAN}me — js agent{C.RESET}
{C.MAGENTA}agent:{C.RESET}  {{agent}}
{C.MAGENTA}model:{C.RESET}  {{model}}
{C.MAGENTA}prompt:{C.RESET} {{prompt}}
{C.MAGENTA}memory:{C.RESET} {{memory}}

{C.GREEN}type 'exit' or Ctrl-D to quit. /help for commands.{C.RESET}
"""


HELP_TEXT = f"""\
{C.MAGENTA}commands:{C.RESET}
  {C.YELLOW}/help{C.RESET}            show this message
  {C.YELLOW}/set [key [val]]{C.RESET} list knobs, show one, or change one (e.g. /set model.reasoning_effort high)
  {C.YELLOW}/set -key{C.RESET}        clear a knob back to its default (e.g. /set -sampling.temperature)
  {C.YELLOW}/show [key]{C.RESET}      list every knob and its current value
  {C.YELLOW}/load <file>{C.RESET}     load a slashless ircII-style script file
  {C.YELLOW}/on [event handler]{C.RESET} list or register an event hook
  {C.YELLOW}/model <name>{C.RESET}    switch model for this session
  {C.YELLOW}/model{C.RESET}             open interactive provider/model picker
  {C.YELLOW}/pick-model{C.RESET}       open interactive provider/model picker
  {C.YELLOW}/provider <id>{C.RESET}  switch provider for this session (e.g. deepseek, ollama, openai-codex)
  {C.YELLOW}/baseurl <url>{C.RESET}  set provider base URL for this session (omit to clear)
  {C.YELLOW}/apikey <key>{C.RESET}   set provider API key for this session (omit to clear)
  {C.YELLOW}/jobs{C.RESET}            list running turns/subagents (--nonblocking)
  {C.YELLOW}/cancel [id]{C.RESET}     cancel a job by id, or the active turn (--nonblocking)
  {C.YELLOW}/compact [focus]{C.RESET} append a compaction summary mark
  {C.YELLOW}/compact-auto on|off{C.RESET} toggle auto-compaction for this process
  {C.YELLOW}/refresh-model-catalog{C.RESET} force-refresh the local models.dev catalog now
  {C.YELLOW}@path/to/file{C.RESET}     attach a file/image to that turn (quote paths with spaces)
  {C.YELLOW}exit{C.RESET}             quit
"""


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
    default_model_id = _provider_qualified_model_id(selected.get("provider_id"), selected["model"])
    if isinstance(state.get("settings"), dict):
        settings.set_dotted(state["settings"], ("model", "id"), default_model_id)
    _saved_path, save_error = _persist_default_model_id(default_model_id)
    if save_error:
        print(f"{C.ORANGE}selected {selected['provider_id']}:{selected['model']} but default save failed: {save_error}{C.RESET}")
    else:
        print(f"{C.GREEN}selected {selected['provider_id']}:{selected['model']} and saved as default{C.RESET}")


def _handle_provider_command(line: str, state: dict, cfg: Config) -> bool:
    """Handle /provider, /baseurl, /apikey, /login, /logout, /models."""
    parts = line.split()
    cmd = parts[0]

    if cmd == "/models":
        max_models = 50
        if len(parts) >= 2:
            try:
                max_models = int(parts[1])
            except ValueError:
                print(f"{C.ORANGE}/models expects an integer limit, got: {parts[1]!r}{C.RESET}")
                return True
        provider_id = _state_value(state, "provider_id", cfg.provider_id)
        provider_base_url = _state_value(state, "provider_base_url", cfg.provider_base_url)
        provider_api_key = _state_value(state, "provider_api_key", cfg.provider_api_key)
        if not provider_id:
            print(f"{C.ORANGE}no provider set; use /provider <id> first{C.RESET}")
            return True
        try:
            model_ids = _models_for_provider(provider_id, provider_base_url, provider_api_key)
        except Exception as e:  # noqa: BLE001
            print(f"{C.ORANGE}could not list models: {type(e).__name__}: {e}{C.RESET}")
        else:
            shown = model_ids[:max_models]
            for mid in shown:
                print(f"  {C.CYAN}{mid}{C.RESET}")
            if len(model_ids) > max_models:
                print(f"{C.GREY}...and {len(model_ids) - max_models} more{C.RESET}")
        return True

    if cmd == "/provider":
        if len(parts) == 1:
            cur = _state_value(state, "provider_id", cfg.provider_id)
            print(f"{C.MAGENTA}current provider:{C.RESET} {cur or '(unset — uses AI Gateway / model prefix)'}")
            return True
        _set_provider_state(state, parts[1])
        print(f"{C.GREEN}provider set to {providers.normalize_provider_id(parts[1]) or parts[1]}{C.RESET}")
        return True

    if cmd == "/baseurl":
        if len(parts) == 1:
            state["provider_base_url"] = None
            print(f"{C.GREEN}base URL cleared{C.RESET}")
            return True
        state["provider_base_url"] = parts[1]
        print(f"{C.GREEN}base URL set{C.RESET}")
        return True

    if cmd == "/apikey":
        if len(parts) == 1:
            state["provider_api_key"] = None
            print(f"{C.GREEN}API key cleared{C.RESET}")
            return True
        state["provider_api_key"] = parts[1]
        print(f"{C.GREEN}API key set{C.RESET}")
        return True

    if cmd == "/login":
        if len(parts) == 1:
            print(f"{C.ORANGE}usage: /login <name> [apikey] [baseurl] [provider]{C.RESET}")
            return True
        name = parts[1]
        key = parts[2] if len(parts) > 2 else None
        url = parts[3] if len(parts) > 3 else None
        ptype = parts[4] if len(parts) > 4 else None
        if key is None and url is None and ptype is None:
            # bare `/login <name>`: load a saved login (or fall back to provider defaults)
            _set_provider_state(state, name)
            print(f"{C.GREEN}active provider loaded: {providers.normalize_provider_id(name) or name}{C.RESET}")
            return True
        # explicit creds: build + persist a login under <name>, then activate it.
        # provider type = explicit 4th arg, else inferred when <name> is itself a known provider.
        prov = providers.get_provider(ptype or name)
        if ptype is None and prov is None:
            print(f"{C.ORANGE}/login: '{name}' is not a known provider — name the provider type: "
                  f"/login {name} <apikey> <baseurl> <provider>{C.RESET}")
            return True
        sdk = prov.effective_sdk_provider_id if prov is not None else ptype
        canonical = providers.normalize_provider_id(name) or name
        logins.save_login(logins.Login(
            provider_id=canonical,
            sdk_provider_id=sdk,
            provider_base_url=url,
            provider_api_key=key,
        ))
        _apply_saved_login_to_state(state, canonical)
        print(f"{C.GREEN}login saved + active: {canonical}{C.RESET}")
        return True

    if cmd == "/logout":
        state["provider_id"] = None
        state["provider_base_url"] = None
        state["provider_api_key"] = None
        state["provider_headers"] = {}
        print(f"{C.GREY}provider credentials cleared for this session{C.RESET}")
        return True
    if cmd == "/pick-model":
        _pick_model_into_state(state, cfg)
        return True

    return False


def _force_refresh_model_catalog() -> bool:
    try:
        status = model_metadata.ensure_fresh_catalog(force=True)
    except Exception as e:  # noqa: BLE001
        print(f"{C.ORANGE}model catalog refresh failed: {type(e).__name__}: {e}{C.RESET}")
        return False
    checked_at = status.refreshed_at or status.generated_at
    stamp = checked_at.isoformat(timespec="seconds") if checked_at else "unknown"
    models = "?" if status.model_count is None else str(status.model_count)
    providers_count = "?" if status.provider_count is None else str(status.provider_count)
    print(f"{C.GREEN}models.dev catalog refreshed{C.RESET} at {stamp} ({providers_count} providers, {models} models)")
    print(f"{C.GREY}{status.db_path}{C.RESET}")
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
        print(f"{C.ORANGE}no legacy config at {legacy}{C.RESET}", file=sys.stderr)
        return 1
    if target.exists():
        print(f"{C.ORANGE}{target} already exists; remove it first to re-migrate{C.RESET}", file=sys.stderr)
        return 1
    with legacy.open("rb") as fp:
        data = tomllib.load(fp)
    lines = ["# migrated from config.toml by `js --migrate-config`", ""]
    lines.extend(f"set {key} {value}" for key, value in _flatten_toml(data))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{C.GREEN}wrote {target}{C.RESET} from {legacy}")
    print(f"{C.GREY}review it, then delete {legacy} when satisfied (this flag is removed after 2 releases){C.RESET}")
    return 0


def _handle_command(line: str, state: dict, cfg: Config) -> bool:
    """Return True if `line` was a command (already handled), False otherwise."""
    if line in {"exit", "quit", ":q"}:
        state["running"] = False
        return True
    if line == "/help":
        print(HELP_TEXT)
        return True
    if line == "/refresh-model-catalog":
        _force_refresh_model_catalog()
        return True
    if setcmd.is_repl_command(line, "/set", "/show", "/load", "/on"):
        context = setcmd.CommandContext(
            cwd=getattr(cfg, "project_dir", Path.cwd()),
            events=state.setdefault("events", events.EventHooks()),
        )
        result = setcmd.run_repl_command(state["settings"], line, context=context)
        if result.changed:
            if setcmd.is_repl_command(line, "/set"):
                state["sampling_cli"] = _sampling_override_after_set(
                    line,
                    state.get("sampling_cli", Sampling()),
                )
            elif _changed_sampling_key(result.changed_keys):
                state["sampling_cli"] = _sampling_override_from_live_settings(state["settings"])
            if _changed_model_key(result.changed_keys):
                _sync_model_from_live_settings(state)
            if _changed_provider_key(result.changed_keys):
                _sync_provider_from_live_settings(state, result.changed_keys)
            if _changed_lock_subagent_model_key(result.changed_keys):
                _sync_tool_registry_from_live_settings(cfg, state)
        for out in result.lines:
            print(out)
        if result.error:
            print(f"{C.ORANGE}{result.error}{C.RESET}")
        return True
    if line.startswith("/model "):
        model_value = line[len("/model "):].strip()
        parsed_provider_id, parsed_model = providers.parse_model_prefix(model_value)
        if parsed_provider_id is not None and parsed_model is not None:
            _set_provider_state(state, parsed_provider_id)
            state["model"] = parsed_model
            print(f"{C.GREEN}model set to {parsed_provider_id}:{parsed_model}{C.RESET}")
            return True
        state["model"] = model_value
        print(f"{C.GREEN}model set to {state['model']}{C.RESET}")
        return True
    if line == "/model":
        _pick_model_into_state(state, cfg)
        return True
    if line == "/reset":
        state["messages"].clear()
        M.append_mark(cfg.session_file, "session_reset")
        print(f"{C.GREY}(conversation cleared in-process; jsonl preserved){C.RESET}")
        return True
    if line == "/wipe":
        bak = M.wipe(cfg.session_file)
        state["messages"].clear()
        if bak:
            print(f"{C.ORANGE}(memory rotated to {bak.name}){C.RESET}")
        else:
            print(f"{C.GREY}(no memory file to rotate){C.RESET}")
        return True
    if line == "/persona":
        text = state["system"]
        print(text[:2048])
        if len(text) > 2048:
            print(f"{C.GREY}...[truncated, {len(text)} bytes total]{C.RESET}")
        return True
    if line == "/turns":
        print(f"{C.CYAN}{len(state['messages'])} messages in context{C.RESET}")
        return True
    if line == "/session":
        print(f"{C.CYAN}{cfg.session_file}{C.RESET}")
        return True
    if line == "/jobs":
        sup = supervisor.get_current()
        if sup is None:
            print(f"{C.GREY}(jobs need --nonblocking){C.RESET}")
            return True
        jobs = sup.jobs()
        if not jobs:
            print(f"{C.GREY}(no running jobs){C.RESET}")
            return True
        for j in jobs:
            label = f"  {j.label}" if j.label else ""
            print(f"{C.CYAN}[{j.id}] {j.kind}{C.RESET}{label}")
        return True
    if line == "/cancel" or line.startswith("/cancel "):
        sup = supervisor.get_current()
        if sup is None:
            print(f"{C.GREY}(cancel needs --nonblocking){C.RESET}")
            return True
        arg = line[len("/cancel"):].strip()
        if arg:
            if not arg.isdigit():
                print(f"{C.ORANGE}usage: /cancel [id]  (bare = active turn){C.RESET}")
                return True
            targets = [j for j in sup.jobs() if j.id == int(arg)]
        else:
            targets = sup.jobs("turn")
        if not targets:
            print(f"{C.GREY}(no matching job to cancel){C.RESET}")
            return True
        # Task.cancel() is not thread-safe; hop to the loop thread to fire it.
        for j in targets:
            sup.loop.call_soon_threadsafe(sup.cancel, j.id)
        ids = ", ".join(f"[{j.id}] {j.kind}" for j in targets)
        print(f"{C.ORANGE}(cancelling {ids}){C.RESET}")
        return True
    if setcmd.is_repl_command(line, "/compact-auto"):
        arg = line[len("/compact-auto"):].strip().lower()
        if arg not in ("on", "off"):
            print(f"{C.ORANGE}usage: /compact-auto on|off{C.RESET}")
            return True
        result = setcmd.run_repl_command(state["settings"], f"/set compact.auto {arg}")
        if result.error:
            print(f"{C.ORANGE}{result.error}{C.RESET}")
        else:
            for out in result.lines:
                print(out)
        return True
    if line == "/compact" or line.startswith("/compact "):
        focus = line[len("/compact"):].strip()
        forced = focus == "up to here"
        if forced:
            focus = ""
        try:
            compact_cfg = _cfg_for_live_state(cfg, state)
            result = runtime.compact_messages(compact_cfg, state["system"], state["messages"], focus=focus, forced=forced)
        except Exception as e:  # noqa: BLE001
            print(f"{C.ORANGE}compact failed: {type(e).__name__}: {e}{C.RESET}")
        else:
            print(f"{C.GREY}({result}){C.RESET}")
        return True

    # Provider commands
    if setcmd.is_repl_command(
        line,
        "/provider",
        "/baseurl",
        "/apikey",
        "/login",
        "/logout",
        "/models",
        "/pick-model",
    ):
        return _handle_provider_command(line, state, cfg)
    return False


def _apply_agent_model(cfg: Config, prompt_spec, model: str | None) -> Config:
    """Apply the active agent's frontmatter `model:` through the resolver, unless
    the operator pinned a model with -m / JS_MODEL / config."""
    agent_model = getattr(prompt_spec, "model", "") if prompt_spec is not None else ""
    if not agent_model or model is not None or getattr(cfg, "explicit_model", False):
        return cfg
    route = routing.resolve_model_route(
        agent_model,
        configured_provider_id=cfg.provider_id,
        configured_base_url=cfg.provider_base_url,
        configured_api_key=cfg.provider_api_key,
        configured_headers=getattr(cfg, "provider_headers", {}),
        explicit_model=True,
        prefix_overrides_provider=True,
        discover_env=False,
    )
    return replace(
        cfg,
        model=route.model,
        provider_id=route.provider_id,
        provider_base_url=route.base_url,
        provider_api_key=route.api_key,
        provider_headers=route.headers,
        vision_enabled=vision_enabled_for_model(route.model),
    )


def _apply_agent_max_tokens(cfg: Config, prompt_spec) -> Config:
    """Apply the agent's `max_tokens:` (00-tools.yaml) as the per-call cap, but
    only when nothing higher-priority set one — config/env/`--max-out` win."""
    agent_max = getattr(prompt_spec, "max_output_tokens", None) if prompt_spec is not None else None
    if agent_max is None or cfg.max_output_tokens is not None:
        return cfg
    return replace(cfg, max_output_tokens=agent_max)


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
                stats_json: str | None = None, stats_csv: str | None = None) -> int:
    attachments = list(files or [])
    if not prompt.strip() and not attachments:
        print(f"{C.ORANGE}error: prompt is empty{C.RESET}", file=sys.stderr)
        return 2
    try:
        cfg = _cfg_from_env_compat(
            session,
            save_session=save,
            extras=extras,
            agent_id=agent,
            ignore_local_config=ignore_local_config,
            ignore_global_config=ignore_global_config,
            presets=presets,
        )
    except ValueError as e:
        print(f"{C.ORANGE}error: {e}{C.RESET}", file=sys.stderr)
        return 2

    prompt_spec = None
    if system_override is not None:
        system = system_override
        active_registry = tool_registry or _FULL_REGISTRY
    else:
        try:
            prompt_spec = P.load_configured_prompt_spec(cfg)
        except (FileNotFoundError, ValueError) as e:
            print(f"{C.ORANGE}{e}{C.RESET}", file=sys.stderr)
            return 2
        system = prompt_spec.system
        active_registry = _registry_for(cfg).select(prompt_spec.tool_selectors)
        cfg = _apply_agent_model(cfg, prompt_spec, model)
        cfg = _apply_agent_max_tokens(cfg, prompt_spec)

    cfg = _resolve_cli_model_override(cfg, model)

    attachment_cfg = (
        replace(cfg, model=model, vision_enabled=vision_enabled_for_model(model))
        if model is not None
        else cfg
    )
    messages = M.load_messages(cfg.session_file)
    before_len = len(messages)
    try:
        user_bundle = attach.build_user_message(
            prompt,
            attachments,
            attachment_cfg,
            stdin_attachment=stdin_attachment,
        )
    except attach.AttachmentError as e:
        print(f"{C.ORANGE}error: {e}{C.RESET}", file=sys.stderr)
        return 2
    messages.append(user_bundle.runtime_message)
    telemetry = runtime.Telemetry(debug_log=cfg.debug_log)
    call_stats: list[dict] = []
    turn_kwargs = {
        "model_override": cfg.model,
        "provider_id_override": cfg.provider_id,
        "provider_base_url_override": cfg.provider_base_url,
        "provider_api_key_override": cfg.provider_api_key,
        "tool_registry": active_registry,
        "sampling": _sampling_for_turn(cfg, prompt_spec, cfg.sampling_cli),
        "call_stats": call_stats,
    }
    if reasoning is not None:
        turn_kwargs["reasoning_effort_override"] = _norm_effort(reasoning)
    if maxout is not None:
        turn_kwargs["max_output_override"] = maxout
    _t_wall = time.time()
    try:
        if debug:
            runtime.run_turn(cfg, system, messages, telemetry, trace_override=True, tool_context=tool_context, **turn_kwargs)
        elif debug_file:
            # Rich trace (incl. the streamed answer) goes to the file; the clean
            # final answer is reprinted to real stdout below once the redirect closes.
            with open(debug_file, "a", encoding="utf-8") as _dbg, contextlib.redirect_stdout(_dbg):
                runtime.run_turn(cfg, system, messages, telemetry, trace_override=True, tool_context=tool_context, **turn_kwargs)
        else:
            with contextlib.redirect_stdout(io.StringIO()):
                runtime.run_turn(cfg, system, messages, telemetry, trace_override=False, tool_context=tool_context, **turn_kwargs)
    except Exception as e:  # noqa: BLE001
        print(f"{C.ORANGE}error: {type(e).__name__}: {e}{C.RESET}", file=sys.stderr)
        return 1

    if stats_json or stats_csv:
        row = {"name": "prompt", "prompt": prompt, "max_tokens": cfg.max_output_tokens if maxout is None else maxout,
               "ok": True, "error": None, **stats.summarize_calls(call_stats, wall_s=time.time() - _t_wall)}
        payload = {"agent": cfg.agent_id, "model": cfg.model, "provider": cfg.provider_id, "turns": [row]}
        if stats_json:
            stats.write_json(stats_json, payload)
        if stats_csv:
            stats.write_csv(stats_csv, [row])

    messages[before_len] = user_bundle.history_message
    for message in reversed(messages):
        if message.get("role") == "assistant" and message.get("content"):
            # In debug, run_turn already streamed the answer live to stdout — a
            # second print here is the double-print bug. Only print when the
            # stream was suppressed (non-debug one-shot/pipe).
            if not debug:
                print(message["content"].strip())
            if save:
                for new_message in messages[before_len:]:
                    _append_turn(cfg, new_message)
                _maybe_auto_compact(cfg, {
                    "system": system,
                    "messages": messages,
                    "model": cfg.model,
                })
                if show_continue:
                    hint = _session_hint_arg(cfg)
                    cont = resume_prefix or "js"
                    # Plain -p has no resume_prefix; the session lives under
                    # sessions/<agent>, so a non-default agent MUST be echoed or
                    # the resume looks in the wrong dir and 404s the .jsonl.
                    # (wiki/artifact/commit already fold the agent into resume_prefix.)
                    if resume_prefix is None and agent:
                        cont += f" --agent {shlex.quote(agent)}"
                    if model:
                        cont += f" --model {shlex.quote(model)}"
                    cont += f" --session {hint}"
                    print(f"Continue: {cont}")
            return 0

    print(f"{C.ORANGE}error: no assistant response{C.RESET}", file=sys.stderr)
    return 1



def _run_prompt_compat(*args, tool_context=None, **kwargs) -> int:
    if tool_context is None:
        return _run_prompt(*args, **kwargs)
    try:
        return _run_prompt(*args, tool_context=tool_context, **kwargs)
    except TypeError:
        return _run_prompt(*args, **kwargs)


def _bench_row_line(row: dict) -> str:
    if not row.get("ok"):
        return f"  {C.ORANGE}{row['name']}: {row.get('error') or 'failed'}{C.RESET}"
    ttft = f"{row['ttft_s'] * 1000:.0f}ms" if row.get("ttft_s") is not None else "—"
    return (f"  {C.GREY}{row['name']}: {row.get('output_tokens', 0)} tok  "
            f"{row.get('tok_per_s', 0.0):.1f} tok/s  ttft {ttft}  "
            f"wall {row.get('wall_s') or 0.0:.2f}s{C.RESET}")


def _run_bench(bench_agent: str, *, model: str | None, reasoning: str | None,
               maxout: int | None, quiet: bool, extras: list[str] | None,
               ignore_local_config: bool, ignore_global_config: bool,
               presets: list[str] | None,
               stats_json: str | None, stats_csv: str | None) -> int:
    """Run an agent's NN-benchmark.md turns, each on a clean slate (fresh
    context, no session), measuring TTFT / tok-s / turn time. The persona
    (NN-prompt.md + 00-tools.yaml) is rebuilt into each benchmark's head;
    benchmarks never see each other."""
    try:
        agent_id = validate_agent_id(bench_agent)
    except ValueError as e:
        print(f"{C.ORANGE}error: {e}{C.RESET}", file=sys.stderr)
        return 2
    try:
        cfg = _cfg_from_env_compat(
            None, save_session=False, extras=extras, agent_id=agent_id,
            ignore_local_config=ignore_local_config,
            ignore_global_config=ignore_global_config, presets=presets,
        )
    except ValueError as e:
        print(f"{C.ORANGE}error: {e}{C.RESET}", file=sys.stderr)
        return 2
    try:
        prompt_spec = P.load_configured_prompt_spec(cfg)
    except (FileNotFoundError, ValueError) as e:
        print(f"{C.ORANGE}{e}{C.RESET}", file=sys.stderr)
        return 2

    benchmarks = P.load_benchmarks(P.resolve_agent_prompt_dir(cfg))
    if not benchmarks:
        print(f"{C.ORANGE}error: agent {agent_id!r} has no NN-benchmark.md files{C.RESET}", file=sys.stderr)
        return 2

    system = prompt_spec.system
    active_registry = _registry_for(cfg).select(prompt_spec.tool_selectors)
    cfg = _apply_agent_model(cfg, prompt_spec, model)
    cfg = _resolve_cli_model_override(cfg, model)
    agent_default_max = prompt_spec.max_output_tokens
    allow_code = bool(getattr(cfg, "allow_inline_code", False))

    rows: list[dict] = []
    interrupted = False
    for bench in benchmarks:
        # max_tokens: --max-out wins; else per-benchmark frontmatter (already
        # -1 -> None=uncapped); else the agent default from 00-tools.yaml.
        if maxout is not None:
            eff_max = maxout
        elif bench.max_tokens_set:
            eff_max = bench.max_tokens
        else:
            eff_max = agent_default_max
        prompt_text = expand_prompt(bench.prompt, allow_code=allow_code, timeout_s=cfg.inline_code_timeout_s)
        messages = [{"role": "user", "content": prompt_text}]
        call_stats: list[dict] = []
        turn_kwargs = {
            "model_override": cfg.model,
            "provider_id_override": cfg.provider_id,
            "provider_base_url_override": cfg.provider_base_url,
            "provider_api_key_override": cfg.provider_api_key,
            "tool_registry": active_registry,
            "sampling": _sampling_for_turn(cfg, prompt_spec, cfg.sampling_cli),
            "max_output_override": eff_max,
            "call_stats": call_stats,
        }
        if reasoning is not None:
            turn_kwargs["reasoning_effort_override"] = _norm_effort(reasoning)
        if not quiet:
            print(f"{C.CYAN}▸ bench {bench.name}{C.RESET}  {C.GREY}{prompt_text.splitlines()[0][:80]}{C.RESET}", file=sys.stderr)
        ok, err = True, None
        t_wall = time.time()
        try:
            sink = io.StringIO() if quiet else None
            with contextlib.redirect_stdout(sink) if sink is not None else contextlib.nullcontext():
                runtime.run_turn(cfg, system, messages, runtime.Telemetry(debug_log=cfg.debug_log),
                                 trace_override=False, **turn_kwargs)
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
        print(_bench_row_line(row), file=sys.stderr)
        if interrupted:
            break

    payload = {"agent": agent_id, "model": cfg.model, "provider": cfg.provider_id, "benchmarks": rows}
    if stats_json:
        stats.write_json(stats_json, payload)
        print(f"{C.GREY}stats → {stats_json}{C.RESET}", file=sys.stderr)
    if stats_csv:
        stats.write_csv(stats_csv, rows)
        print(f"{C.GREY}stats → {stats_csv}{C.RESET}", file=sys.stderr)
    if not stats_json and not stats_csv:
        print(json.dumps(payload, indent=2, default=str))
    return 130 if interrupted else 0


def _wiki_kickoff(mode: str, vault: str, target_desc: str, resuming: bool,
                  immediate_file: str | None = None, immediate_unit: str | None = None) -> str:
    """The single-mode kickoff prompt that opens a wiki turn."""
    # Single-file ingest takes priority over the generic resume/ingest text (even on
    # resume) so it never falls back to a prompt that tells the agent to call wiki_inbox.
    if mode == "ingest" and immediate_file:
        # Ingest exactly this one named file instead of the inbox flow picking a unit.
        # If it IS a top-level inbox unit, close with wiki_finish_ingest so it is
        # archived (the done-marker) and cannot be re-ingested/duplicated later. The
        # inbox tool itself is never modified.
        if immediate_unit:
            close = (f"Then close out with ONE call: wiki_finish_ingest(\"{vault}\", \"{immediate_unit}\", "
                     f"\"<title>\", \"<pages written>\") — it archives inbox/{immediate_unit} to Clippings, "
                     f"writes the log entry, and commits, all at once (the done-marker that stops it duping on "
                     f"a later run). Do NOT also call wiki_log — wiki_finish_ingest already logs.")
        else:
            close = (f"Then call wiki_log(\"{vault}\", \"ingest\", \"<title>\", \"<note>\"). Do NOT call "
                     f"wiki_finish_ingest or wiki_archive — leave the file exactly where it sits.")
        return (f"Wiki mode: ingest ONE FILE (SOURCE PAGE ONLY). vault={vault}. file={immediate_file}. "
                f"Begin: call wiki_purpose(\"{vault}\") for the lens. Convert and read fully: "
                f"wiki_convert(\"{immediate_file}\"). BEFORE writing, check whether a source page already "
                f"covers this file (wiki_search + ls sources/); if so READ it and UPSERT (overwrite=true) — "
                f"never duplicate. Write EXACTLY ONE kind=\"source\" page: a rich factual summary plus "
                f"'## Candidate entities' and '## Candidate concepts' lists (each line: name — one-line why) "
                f"as the synthesize pass's worklist. Do NOT write entity/concept/synthesis pages — the "
                f"synthesize pass owns those (wiki_write refuses them in ingest mode). "
                f"Do NOT call wiki_inbox; do NOT pick any other unit. {close} Report what you wrote, then stop.")
    if resuming:
        return (f"RESUME wiki mode: {mode}. vault={vault}. target={target_desc}. "
                f"You were interrupted mid-task. Re-check state first (wiki_purpose, wiki_inbox, "
                f"and read any pages you already started), then FINISH the {mode} flow. Pages that "
                f"already exist -> read and UPSERT (wiki_write overwrite=true), never recreate.")
    if mode == "ingest":
        return (f"Wiki mode: ingest. vault={vault}. target={target_desc}. "
                f"Begin: call wiki_purpose(\"{vault}\") first, then run the ingest flow — write ONE rich "
                f"kind=\"source\" page (factual summary + '## Candidate entities' / '## Candidate concepts' "
                f"lists, each line: name — one-line why), then wiki_finish_ingest. Do NOT write "
                f"entity/concept/synthesis pages — those are the synthesize pass's job.")
    if mode == "synthesize":
        return (f"Wiki mode: synthesize. vault={vault}. "
                f"Begin: call wiki_purpose(\"{vault}\") first, then run the synthesize flow — derive and "
                f"UPSERT the SHARED entity/concept pages from the source pages' candidate lists, then weave "
                f"synthesis pages citing every source with [[links]] and flagging contradictions, then commit.")
    if mode == "query":
        return (f"Wiki mode: query. vault={vault}. question={target_desc}. "
                f"Begin: call wiki_purpose(\"{vault}\") first, then answer from the wiki with [[links]] "
                f"and file a synthesis page if the answer is substantial.")
    return (f"Wiki mode: lint. vault={vault}. "
            f"Begin: call wiki_purpose(\"{vault}\") first, then health-check the wiki and fix mechanical "
            f"issues (contradictions, orphans, stale claims, missing cross-refs).")


def _run_wiki(wiki_arg: str, target: str | None, vault: str | None,
              model: str | None = None, debug: bool = False, debug_file: str | None = None,
              agent: str | None = None, session: str | None = None, save: bool = True,
              reasoning: str | None = None, maxout: int | None = None,
              extras: list[str] | None = None) -> int:
    """js --wiki=ingest,synthesize [--vault=creative] <target>.

    Built-in wiki prompting (ignores defaultagent). If --agent is given, that
    agent's persona leads and the wiki prompting follows.

    Each mode runs as its OWN kickoff turn over one shared session, in order.
    Cramming every mode section into a single loop made the model obey the
    ingest prompt's "then stop" and never reach synthesize; each mode prompt
    re-orients from disk (wiki_purpose) so sequencing doesn't need shared chat.
    """
    valid = {"ingest", "synthesize", "query", "lint"}
    modes = [m.strip().lower() for m in wiki_arg.split(",") if m.strip()]
    bad = [m for m in modes if m not in valid]
    if not modes or bad:
        print(f"{C.ORANGE}error: --wiki expects a comma list of {sorted(valid)} (got {wiki_arg!r}){C.RESET}", file=sys.stderr)
        return 2

    if not vault:
        vault = infer_vault(target, Path(os.getcwd()))
    if not vault:
        print(f"{C.ORANGE}error: no vault given and none inferred; pass --vault <name|path> or cd into a vault (PURPOSE.md sentinel or wiki-* dir){C.RESET}", file=sys.stderr)
        return 2

    # wiki mode runs under its own agent id ('wiki') unless --agent is given;
    # an explicit --agent loads that persona AND prepends it to the wiki prompting.
    eff_agent = agent or "wiki"
    persona = ""
    if agent:
        try:
            cfg = _cfg_from_env_compat(session, save_session=False, extras=extras, agent_id=eff_agent)
            persona = P.load_configured_prompt_spec(cfg).system + "\n\n"
        except (ValueError, FileNotFoundError) as e:
            print(f"{C.ORANGE}error: {e}{C.RESET}", file=sys.stderr)
            return 2

    # Reserve ONE session up front so every mode turn appends to the same jsonl.
    active_session = session
    if save and active_session is None:
        try:
            cfg = _cfg_from_env_compat(None, save_session=True, extras=extras, agent_id=eff_agent)
        except ValueError as e:
            print(f"{C.ORANGE}error: {e}{C.RESET}", file=sys.stderr)
            return 2
        active_session = _session_hint_arg(cfg)

    modes_arg = ",".join(modes)
    resume_prefix = f"js --wiki={modes_arg} --vault={vault}"
    if agent:
        resume_prefix += f" --agent {agent}"
    target_desc = target if target else "the inbox"
    # Single-file ingest: if `target` is an actual file, ingest THAT one file (instead
    # of the inbox flow picking a unit). Self-contained — uses BASE only, NOT the
    # INGEST/inbox mode prompt, so the inbox tool/flow is never modified. If the file
    # is an inbox unit, it is archived on success (wiki_finish_ingest = the done-marker)
    # so it cannot be re-ingested or duplicated on a later run.
    immediate_file = os.path.abspath(target) if target and os.path.isfile(target) else None
    immediate_unit = None
    if immediate_file:
        # Resolve the vault the SAME way the wiki toolkit does (config aliases + ~ + cwd-relative).
        _alias_cfg = _cfg_from_env_compat(None, save_session=False, extras=extras, agent_id=eff_agent)
        _wiki = (getattr(_alias_cfg, "settings", {}) or {}).get("wiki")
        _aliases = _wiki.get("aliases", {}) if isinstance(_wiki, dict) and isinstance(_wiki.get("aliases"), dict) else {}
        vp = resolve_vault(vault, ToolContext(cwd=Path(os.getcwd()), vault_aliases=_aliases))
        try:
            rel = Path(immediate_file).resolve().relative_to((vp / "inbox").resolve())
        except ValueError:
            rel = None
        # Only a TOP-LEVEL inbox unit is archiveable. A nested file (inbox/proj/child)
        # must NOT archive its parent folder — that would hide unprocessed siblings.
        # Skip _skipped and dotfiles, which the inbox tool itself ignores.
        if rel is not None and len(rel.parts) == 1 and rel.parts[0] != "_skipped" \
                and not rel.parts[0].startswith("."):
            immediate_unit = rel.parts[0]

    rc = 0
    for idx, mode in enumerate(modes):
        if immediate_file and mode == "ingest":
            system = persona + build_wiki_system([])
        else:
            system = persona + build_wiki_system([mode])
        prompt = _wiki_kickoff(mode, vault, target_desc, resuming=(session is not None and idx == 0),
                               immediate_file=immediate_file, immediate_unit=immediate_unit)
        mode_context = ToolContext(cwd=Path.cwd(), wiki_mode=mode)
        rc = _run_prompt_compat(prompt, model=model, debug=debug, agent=eff_agent,
                                debug_file=debug_file, session=active_session, save=save, system_override=system,
                                resume_prefix=resume_prefix, show_continue=(idx == len(modes) - 1),
                                tool_registry=_FULL_REGISTRY, reasoning=reasoning, maxout=maxout,
                                extras=extras, tool_context=mode_context)
        if rc != 0:
            break
    return rc



def _artifact_kickoff(mode: str, target_desc: str, resuming: bool) -> str:
    if resuming:
        return (f"RESUME artifact mode: {mode}. target={target_desc}. "
                f"You were interrupted mid-task. Start with artifact_overview(), inspect current "
                f"manifest/curation state, then finish the {mode} flow without duplicating pages.")
    if mode == "curate":
        return (f"Artifact mode: curate. target={target_desc}. "
                f"Begin: call artifact_overview() first, then classify recent/unassigned artifacts, "
                f"install curation assignments/refs, and stop.")
    if mode == "digest":
        return (f"Artifact mode: digest. target={target_desc}. "
                f"Begin: call artifact_overview() first, then write or update a concise artifact digest.")
    if mode == "query":
        return (f"Artifact mode: query. question={target_desc}. "
                f"Begin: call artifact_overview() first, then search/read artifacts and answer with stable URLs.")
    return (f"Artifact mode: lint. target={target_desc}. "
            f"Begin: call artifact_overview() first, then health-check curation, refs, duplicates, "
            f"and uncategorized artifacts.")


def _run_artifact(artifact_arg: str, target: str | None,
                  model: str | None = None, debug: bool = False, debug_file: str | None = None,
                  agent: str | None = None, session: str | None = None, save: bool = True,
                  reasoning: str | None = None, maxout: int | None = None,
                  extras: list[str] | None = None) -> int:
    valid = {"curate", "digest", "query", "lint"}
    modes = [m.strip().lower() for m in artifact_arg.split(",") if m.strip()]
    bad = [m for m in modes if m not in valid]
    if not modes or bad:
        print(f"{C.ORANGE}error: --artifact expects a comma list of {sorted(valid)} (got {artifact_arg!r}){C.RESET}", file=sys.stderr)
        return 2

    eff_agent = agent or "artifact"
    persona = ""
    if agent:
        try:
            cfg = _cfg_from_env_compat(session, save_session=False, extras=extras, agent_id=eff_agent)
            persona = P.load_configured_prompt_spec(cfg).system + "\n\n"
        except (ValueError, FileNotFoundError) as e:
            print(f"{C.ORANGE}error: {e}{C.RESET}", file=sys.stderr)
            return 2

    active_session = session
    if save and active_session is None:
        try:
            cfg = _cfg_from_env_compat(None, save_session=True, extras=extras, agent_id=eff_agent)
        except ValueError as e:
            print(f"{C.ORANGE}error: {e}{C.RESET}", file=sys.stderr)
            return 2
        active_session = _session_hint_arg(cfg)

    modes_arg = ",".join(modes)
    resume_prefix = f"js --artifact={modes_arg}"
    if agent:
        resume_prefix += f" --agent {agent}"
    target_desc = target if target else "the artifact library"

    rc = 0
    for idx, mode in enumerate(modes):
        system = persona + build_artifact_system([mode])
        prompt = _artifact_kickoff(mode, target_desc, resuming=(session is not None and idx == 0))
        rc = _run_prompt(prompt, model=model, debug=debug, agent=eff_agent,
                         debug_file=debug_file, session=active_session, save=save, system_override=system,
                         resume_prefix=resume_prefix, show_continue=(idx == len(modes) - 1),
                         tool_registry=_FULL_REGISTRY, reasoning=reasoning, maxout=maxout,
                         extras=extras)
        if rc != 0:
            break
    return rc


def _run_commit(target: str | None,
                model: str | None = None, debug: bool = False, debug_file: str | None = None,
                session: str | None = None, save: bool = True,
                reasoning: str | None = None, maxout: int | None = None,
                extra_context: str | None = None,
                extras: list[str] | None = None) -> int:
    target_dir = Path(target).expanduser() if target else Path.cwd()
    if not target_dir.is_absolute():
        target_dir = Path.cwd() / target_dir
    target_dir = target_dir.resolve(strict=False)
    if not target_dir.exists():
        print(f"{C.ORANGE}error: commit target does not exist: {target_dir}{C.RESET}", file=sys.stderr)
        return 2
    if not target_dir.is_dir():
        print(f"{C.ORANGE}error: commit target is not a directory: {target_dir}{C.RESET}", file=sys.stderr)
        return 2

    from . import commit_helper

    probe = commit_helper._git("rev-parse", "--is-inside-work-tree", check=False, repo=target_dir)
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        init = commit_helper._git("init", "-q", "-b", "main", check=False, repo=target_dir)
        if init.returncode != 0:
            init = commit_helper._git("init", "-q", check=False, repo=target_dir)
        if init.returncode != 0:
            detail = (init.stderr or init.stdout).strip() or f"exit {init.returncode}"
            print(f"{C.ORANGE}error: git init failed in {target_dir}: {detail}{C.RESET}", file=sys.stderr)
            return 1

    survey_out = io.StringIO()
    survey_err = io.StringIO()
    with contextlib.redirect_stdout(survey_out), contextlib.redirect_stderr(survey_err):
        survey_rc = commit_helper.main(["-C", str(target_dir), "survey"])
    survey = survey_out.getvalue().rstrip()
    if survey_rc != 0:
        detail = survey_err.getvalue().strip() or survey or f"commit_helper survey exited {survey_rc}"
        print(f"{C.ORANGE}error: commit survey failed for {target_dir}: {detail}{C.RESET}", file=sys.stderr)
        return 1

    helper_stage = f"python -m js.commit_helper -C {shlex.quote(str(target_dir))} stage <file> <hunks|all>"
    helper_survey = f"python -m js.commit_helper -C {shlex.quote(str(target_dir))} survey"
    prompt = (
        f"Commit all work in this target directory: {target_dir}\n\n"
        "The repository has already been initialized if it was missing. "
        "Use this deterministic staging helper for every commit unit:\n"
        f"`{helper_stage}`\n"
        "Use `all` for whole-file staging or comma-separated hunk numbers from the survey "
        "for tracked text files. If you need a fresh snapshot after making changes, run:\n"
        f"`{helper_survey}`\n\n"
        "Initial deterministic commit_helper survey:\n"
        "```text\n"
        f"{survey}\n"
        "```"
    )
    if extra_context and extra_context.strip():
        prompt += f"\n\nOperator context:\n{extra_context.strip()}"

    return _run_prompt_compat(
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
        tool_context=ToolContext(cwd=target_dir),
    )


def _run_compact_offline(session: str, *, agent: str | None = None, focus: str = "", extras: list[str] | None = None, model: str | None = None) -> int:
    try:
        cfg = _cfg_from_env_compat(session, save_session=True, extras=extras, agent_id=agent)
        prompt_spec = P.load_configured_prompt_spec(cfg)
        messages = M.load_messages(cfg.session_file)
        compact_cfg = replace(cfg, model=model) if model is not None else cfg
        result = runtime.compact_messages(compact_cfg, prompt_spec.system, messages, focus=focus, forced=True)
    except Exception as e:  # noqa: BLE001
        print(f"{C.ORANGE}error: {type(e).__name__}: {e}{C.RESET}", file=sys.stderr)
        return 1
    print(result)
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
    for provider_id, login in sorted(logins.load_logins().items()):
        item = asdict(login)
        item["provider_id"] = provider_id
        item["has_api_key"] = bool(item.get("provider_api_key"))
        item["has_codex_refresh_token"] = bool(item.get("codex_refresh_token"))
        if codex_auth.is_codex_provider(provider_id):
            item["provider_api_key"] = None
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
        prefix_overrides_provider=True,
    )
    return replace(
        cfg,
        model=route.model,
        provider_id=route.provider_id,
        provider_base_url=route.base_url,
        provider_api_key=route.api_key,
        provider_headers=route.headers,
        vision_enabled=vision_enabled_for_model(route.model),
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
    if cached:
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
            looked = " or ".join(str(p) for p in candidates) or "(no config dirs enabled)"
            print(f"{C.ORANGE}warning: --preset {name}: no jsrc.{name} found ({looked}){C.RESET}", file=sys.stderr)


def _print_json(value: object) -> int:
    print(json.dumps(value, ensure_ascii=False))
    return 0


async def _do_turn(cfg, state, telemetry, prompt_spec, user_bundle, turn_cfg, before_len, loop) -> None:
    """One main turn on the async loop. Runs the turn, syncs live-settings
    deltas, persists new messages, then auto-compacts (in the executor because
    compaction still calls asyncio.run under the hood). Owns cancellation
    ENTIRELY: on ^C the turn Task is cancelled, and this handler — never the
    caller — persists partial work and heals orphaned tool_calls, mirroring the
    legacy blocking KeyboardInterrupt path, then re-raises so the job ends
    cancelled.
    """
    try:
        before_turn_sampling = _sampling_override_from_live_settings(state["settings"])
        before_turn_model = _model_from_live_settings(state["settings"])
        before_turn_provider = _provider_from_live_settings(state["settings"])
        before_turn_lock = _live_bool_setting(
            state["settings"], ("subagents", "lock_model"), cfg.lock_subagent_model
        )
        await runtime.run_turn_async(
            turn_cfg,
            state["system"],
            state["messages"],
            telemetry,
            trace_override=bool(
                settings.get_dotted(state["settings"], ("runtime", "trace"), cfg.trace)
            ),
            reasoning_effort_override=turn_cfg.reasoning_effort,
            max_output_override=turn_cfg.max_output_tokens,
            tool_registry=state["tool_registry"],
            sampling=_sampling_for_turn(turn_cfg, prompt_spec, state["sampling_cli"]),
            event_hooks=state.get("events"),
        )
        after_turn_sampling = _sampling_override_from_live_settings(state["settings"])
        if after_turn_sampling != before_turn_sampling:
            state["sampling_cli"] = after_turn_sampling
        after_turn_model = _model_from_live_settings(state["settings"])
        if after_turn_model != before_turn_model:
            _sync_model_from_live_settings(state)
        after_turn_provider = _provider_from_live_settings(state["settings"])
        _sync_provider_delta_from_live_settings(state, before_turn_provider, after_turn_provider)
        after_turn_lock = _live_bool_setting(
            state["settings"], ("subagents", "lock_model"), cfg.lock_subagent_model
        )
        if after_turn_lock != before_turn_lock:
            _sync_tool_registry_from_live_settings(cfg, state)
        _sync_telemetry_from_live_settings(cfg, state, telemetry)
        state["messages"][before_len] = user_bundle.history_message
        for m in state["messages"][before_len + 1:]:
            _append_turn(cfg, m)
        await loop.run_in_executor(None, functools.partial(_maybe_auto_compact, turn_cfg, state))
    except asyncio.CancelledError:
        cancel_event = _emit_repl_event(state, telemetry, "cancel", reason="cancelled")
        if _event_results_changed_sampling(cancel_event.results):
            state["sampling_cli"] = _sampling_override_from_live_settings(state["settings"])
        if _event_results_changed_model(cancel_event.results):
            _sync_model_from_live_settings(state)
        cancel_changed_keys = _event_result_changed_keys(cancel_event.results)
        if _changed_provider_key(cancel_changed_keys):
            _sync_provider_from_live_settings(state, cancel_changed_keys)
        if _changed_lock_subagent_model_key(cancel_changed_keys):
            _sync_tool_registry_from_live_settings(cfg, state)
        _sync_telemetry_from_live_settings(cfg, state, telemetry)
        if len(state["messages"]) > before_len + 1:
            print(f"\n{C.ORANGE}(turn interrupted — partial work kept){C.RESET}")
            state["messages"][before_len] = user_bundle.history_message
            for m in state["messages"][before_len + 1:]:
                _append_turn(cfg, m)
            M.append_mark(cfg.session_file, "turn_interrupted")
            state["messages"][:] = M.balance_orphaned_tool_calls(state["messages"])
        else:
            print(f"\n{C.ORANGE}(turn aborted){C.RESET}")
            state["messages"][:] = state["messages"][:before_len]
            M.append_mark(cfg.session_file, f"rollback_to:{before_len}")
            M.append_mark(cfg.session_file, "turn_aborted")
        raise
    except Exception as e:  # noqa: BLE001
        print(f"{C.ORANGE}error: {type(e).__name__}: {e}{C.RESET}")
        state["messages"][:] = state["messages"][:before_len]
        M.append_mark(cfg.session_file, f"rollback_to:{before_len}")
        M.append_mark(cfg.session_file, f"error: {type(e).__name__}: {e}")


async def _turn_consumer(queue, sup, cfg, state, telemetry, prompt_spec, loop) -> None:
    """Serialize main turns: one at a time, pulled FIFO from the input queue.
    This is what keeps state['messages'] single-writer while the input line
    stays live — the producer (`_repl_main`) never blocks on a turn."""
    while True:
        line = await queue.get()
        try:
            prompt_text, line_attachments = attach.split_repl_attachments(line)
            input_event = _emit_repl_event(
                state, telemetry, "input", text=prompt_text, attachments=line_attachments
            )
            if _event_results_changed_sampling(input_event.results):
                state["sampling_cli"] = _sampling_override_from_live_settings(state["settings"])
            if _event_results_changed_model(input_event.results):
                _sync_model_from_live_settings(state)
            input_changed_keys = _event_result_changed_keys(input_event.results)
            if _changed_provider_key(input_changed_keys):
                _sync_provider_from_live_settings(state, input_changed_keys)
            if _changed_lock_subagent_model_key(input_changed_keys):
                _sync_tool_registry_from_live_settings(cfg, state)
            _sync_telemetry_from_live_settings(cfg, state, telemetry)
            try:
                turn_cfg = _cfg_for_live_state(cfg, state)
                user_bundle = attach.build_user_message(prompt_text, line_attachments, turn_cfg)
            except attach.AttachmentError as e:
                print(f"{C.ORANGE}error: {e}{C.RESET}")
                continue
            before_len = len(state["messages"])
            state["messages"].append(user_bundle.runtime_message)
            _append_turn(cfg, user_bundle.history_message)
            job = sup.spawn(
                _do_turn(cfg, state, telemetry, prompt_spec, user_bundle, turn_cfg, before_len, loop),
                kind="turn",
                label=prompt_text[:40],
            )
            with contextlib.suppress(asyncio.CancelledError):
                await job.task  # _do_turn persists partial work on cancel; keep looping
        finally:
            queue.task_done()


async def _repl_main(cfg, state, telemetry, session, prompt_spec) -> int:
    """Non-blocking REPL: input, the active turn, and subagents all share ONE
    event loop. `prompt_async` keeps the input line live while a turn streams
    (output paints above it via patch_stdout); ^C cancels the active turn
    instead of killing the process; new prompts queue behind a running turn."""
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=32, thread_name_prefix="js-dispatch"))
    sup = supervisor.Supervisor(loop)
    supervisor.set_current(sup)
    queue: asyncio.Queue = asyncio.Queue()
    consumer = loop.create_task(
        _turn_consumer(queue, sup, cfg, state, telemetry, prompt_spec, loop)
    )
    try:
        with patch_stdout(raw=True):
            while state["running"]:
                try:
                    line = (await session.prompt_async(ANSI(f"{C.YELLOW}LO> {C.RESET}"))).strip()
                except EOFError:
                    print()
                    break
                except KeyboardInterrupt:
                    if sup.turn_active():
                        n = sup.cancel_kind("turn")
                        print(f"{C.ORANGE}(cancelling {n} turn){C.RESET}")
                    continue
                if not line:
                    continue
                handled = await loop.run_in_executor(None, _handle_command, line, state, cfg)
                if handled:
                    _sync_telemetry_from_live_settings(cfg, state, telemetry)
                    continue
                queue.put_nowait(line)
                if sup.turn_active() or queue.qsize() > 1:
                    print(f"{C.GREY}(queued — {queue.qsize()} ahead){C.RESET}")
    finally:
        supervisor.set_current(None)
        # Graceful quit (EOF / exit): let queued and in-flight turns finish
        # before teardown so submitted work isn't silently dropped. To abandon a
        # long turn, cancel it with ^C first, then quit.
        if not consumer.done():
            with contextlib.suppress(Exception):
                await queue.join()
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer
        for job in sup.jobs():  # backstop: cancel any straggler
            job.task.cancel()
    return 0


def main(argv: list[str] | None = None) -> int:
    # Handle login/logout before argparse so they don't require a valid agent/config.
    if argv and argv[0] in ("--login", "login"):
        from . import login_cli
        return login_cli.main(argv[1:])
    if argv and argv[0] in ("--logout", "logout"):
        from . import login_cli
        return login_cli.main(["logout"] + (argv[1:] if len(argv) > 1 else []))

    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--login", metavar="PROVIDER", nargs="?", const="", help="interactive provider login (omit provider for list)")
    parser.add_argument("--logout", metavar="PROVIDER", help="remove a saved provider login")
    parser.add_argument("-p", "--prompt", nargs="?", const="-", help="run one prompt and print the final answer; reads stdin when value is omitted or '-'")
    parser.add_argument("-f", "--file", dest="files", action="append", default=[], metavar="PATH", help="attach a file/image to a one-shot prompt; repeatable; '-' reads stdin bytes")
    parser.add_argument("-a", "--agent", help="internal agent id; sessions live in platform data sessions/<agent>, runtime state in platform data state/<agent>")
    parser.add_argument("-m", "--model", help="override configured/env model for this session or prompt")
    parser.add_argument("-C", dest="cd", metavar="DIR", help="run as if launched from DIR (like git -C): binds the working directory for every mode (-p, REPL, --commit, --wiki, ...). DIR must exist.")
    parser.add_argument("-d", "--debug", action="store_true", help="show streamed text/tool debug output in prompt mode")
    parser.add_argument("--debug-file", dest="debug_file", metavar="PATH", help="write the rich debug trace (system prompt, messages sent, tool schemas, per-call timings) to PATH and keep the clean answer on stdout")
    parser.add_argument("-s", "--session", help="load existing session id or .jsonl file under platform data sessions/<agent>")
    parser.add_argument("-n", "--no-save", action="store_true", help="run one-shot prompt/pipe mode without writing session state")
    parser.add_argument("-q", "--quiet", action="store_true", help="suppress the 'Continue: ...' resume hint after a one-shot prompt")
    parser.add_argument("-r", "--reasoning", help="thinking effort: off|low|medium|high|max|minimal|xhigh; min=low")
    parser.add_argument("--max-out", dest="max_out", type=int, help="max output tokens per call")
    parser.add_argument("--bench", metavar="AGENT", help="benchmark mode: run AGENT's NN-benchmark.md turns each on a clean slate (no session), measuring TTFT/tok-s/turn-time. Pair with --stats-json/--stats-csv.")
    parser.add_argument("--stats-json", dest="stats_json", metavar="PATH", help="write per-turn stats (ttft, tok/s, turn time, tokens) to PATH as JSON")
    parser.add_argument("--stats-csv", dest="stats_csv", metavar="PATH", help="write per-turn stats to PATH as CSV")
    parser.add_argument("--nonblocking", action="store_true", help="experimental: run the REPL on one async event loop so input stays live while a turn streams and subagents run; ^C cancels the active turn. Legacy blocking REPL is the default.")
    parser.add_argument("--extra", dest="extras", action="append", default=[], metavar="KEY=VALUE",
                        help="set a dotted config key for this run, e.g. --extra limits.task_max_depth=3. "
                             "May be repeated. Wins over env and all config files.")
    parser.add_argument("--preset", dest="presets", action="append", default=[], metavar="NAME[,NAME...]",
                        help="layer jsrc.<name> preset files on top of the base config, in order "
                             "(last wins). Comma-list and/or repeatable: --preset fast,debug. Looks for "
                             "jsrc.<name> beside the global jsrc and in project .js/. Still below env/--extra.")
    parser.add_argument("--ignore-local", action="store_true", help="ignore project .js/jsrc and .js/jsrc.local")
    parser.add_argument("--ignore-global", action="store_true", help="ignore the platform jsrc")
    parser.add_argument("--migrate-config", action="store_true", help="one-shot: convert a legacy config.toml to jsrc, then exit")
    parser.add_argument("--providers-json", action="store_true", help="print provider registry as JSON for external pickers")
    parser.add_argument("--logins-json", action="store_true", help="print saved logins as JSON for external pickers")
    parser.add_argument("--models-json", nargs="?", const="", metavar="PROVIDER", help="print cached/live models for provider as JSON")
    parser.add_argument("--list-models", nargs="?", const="", metavar="PROVIDER", help="print human-readable models for provider and exact --model values to pass")
    parser.add_argument("--refresh-model-catalog", action="store_true", help="force-refresh js's local models.dev catalog now")
    parser.add_argument("--wiki", metavar="MODES", help="wiki mode: comma list of ingest,synthesize,query,lint (e.g. --wiki=ingest,synthesize). Built-in wiki prompting; ignores defaultagent unless --agent is also given (persona + wiki).")
    parser.add_argument("--artifact", metavar="MODES", help="artifact mode: comma list of curate,digest,query,lint (e.g. --artifact=digest). Built-in artifact prompting; ignores defaultagent unless --agent is also given.")
    parser.add_argument("--commit", action="store_true", help="run the built-in commit agent against target dir; auto-inits a missing repo (default: cwd)")
    parser.add_argument("--compact", metavar="SESSION", help="offline compact an existing session id/path append-only")
    parser.add_argument("--vault", help="wiki vault: creative|general|path (default: infer from target/cwd, else creative)")
    parser.add_argument("--dangerously-evaluate-inline-code", "--dangerously-evaluate-shell-commands",
                        dest="dangerously_evaluate_inline_code", action="store_true",
                        help="execute !{sh|python|c|node ...} inline directives / ```!lang fences in the system "
                             "prompt and inject their stdout. Compiles/runs arbitrary code from prompt files — "
                             "only use on prompts you trust. {{VAR}} env expansion and !{env}/!{file} are always "
                             "on and do not need this flag.")
    parser.add_argument("target", nargs="?", help="file or dir to ingest in --wiki mode")
    args = parser.parse_args(argv)
    presets = [name for spec in args.presets for name in spec.split(",") if name.strip()]
    if args.cd:
        cd_target = Path(args.cd).expanduser()
        if not cd_target.is_dir():
            print(f"{C.ORANGE}error: -C target is not a directory: {cd_target}{C.RESET}", file=sys.stderr)
            return 2
        os.chdir(cd_target)
        # DEFAULT_CONTEXT is built at import (before this chdir), so its cwd is
        # stale; rebind it so -p/REPL turns (which fall back to it) run in DIR.
        runtime.T.DEFAULT_CONTEXT.cwd = Path.cwd()
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
                cfg = _cfg_from_env_compat(
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
                cfg = _cfg_from_env_compat(
                    args.session,
                    save_session=False,
                    extras=args.extras,
                    agent_id=args.agent,
                    ignore_local_config=args.ignore_local,
                    ignore_global_config=args.ignore_global,
                )
            provider_ids = _list_models_provider_ids(provider_arg, cfg)
            if not provider_ids:
                print(f"{C.GREY}no providers logged in; run `js --login <provider>`{C.RESET}", file=sys.stderr)
                return 0
            for pid in provider_ids:
                try:
                    model_ids = _models_cached_or_live(pid, cfg)
                except Exception as e:  # noqa: BLE001
                    print(f"# {pid}: {type(e).__name__}: {e}", file=sys.stderr)
                    continue
                for model_id in model_ids:
                    print(f"{pid}/{model_id}" if pid else model_id)
            return 0
        except Exception as e:  # noqa: BLE001
            print(f"{C.ORANGE}error: {type(e).__name__}: {e}{C.RESET}", file=sys.stderr)
            return 1
    if args.refresh_model_catalog:
        if not _force_refresh_model_catalog():
            return 1
        if (
            args.prompt is None
            and args.wiki is None
            and args.artifact is None
            and not args.commit
            and args.compact is None
            and args.target is None
        ):
            return 0
    if args.debug and args.debug_file:
        print(f"{C.ORANGE}error: choose either --debug or --debug-file, not both{C.RESET}", file=sys.stderr)
        return 2
    if args.dangerously_evaluate_inline_code:
        os.environ["JS_ALLOW_INLINE_CODE"] = "1"
    cli_agent = None
    if args.agent:
        try:
            cli_agent = validate_agent_id(args.agent)
        except ValueError as e:
            print(f"{C.ORANGE}error: {e}{C.RESET}", file=sys.stderr)
            return 2

    selected_modes = [name for name, enabled in (("wiki", args.wiki), ("artifact", args.artifact), ("commit", args.commit), ("compact", args.compact)) if enabled]
    if len(selected_modes) > 1:
        print(f"{C.ORANGE}error: choose only one built-in mode: --wiki, --artifact, --commit, or --compact{C.RESET}", file=sys.stderr)
        return 2
    if args.files and selected_modes:
        print(f"{C.ORANGE}error: -f/--file only works with prompt/pipe mode; use @path in the REPL{C.RESET}", file=sys.stderr)
        return 2

    if args.bench:
        if selected_modes or args.agent:
            print(f"{C.ORANGE}error: --bench is its own mode; name the agent as --bench AGENT, not with --agent or a built-in mode{C.RESET}", file=sys.stderr)
            return 2
        return _run_bench(args.bench, model=args.model, reasoning=args.reasoning,
                          maxout=args.max_out, quiet=args.quiet, extras=args.extras,
                          ignore_local_config=args.ignore_local,
                          ignore_global_config=args.ignore_global, presets=presets,
                          stats_json=args.stats_json, stats_csv=args.stats_csv)

    if args.compact:
        return _run_compact_offline(args.compact, agent=cli_agent, focus=args.prompt or "", extras=args.extras, model=args.model)

    if args.wiki:
        return _run_wiki(args.wiki, args.target, args.vault, model=args.model,
                         debug=args.debug, debug_file=args.debug_file, agent=args.agent,
                         session=args.session, save=not args.no_save,
                         reasoning=args.reasoning, maxout=args.max_out,
                         extras=args.extras)

    if args.artifact:
        return _run_artifact(args.artifact, args.target, model=args.model,
                             debug=args.debug, debug_file=args.debug_file, agent=args.agent,
                             session=args.session, save=not args.no_save,
                             reasoning=args.reasoning, maxout=args.max_out,
                             extras=args.extras)

    if args.migrate_config:
        return _run_migrate_config()

    if args.commit:
        if args.agent:
            print(f"{C.ORANGE}error: --commit always uses the built-in commit agent; omit --agent{C.RESET}", file=sys.stderr)
            return 2
        extra_context = None
        if args.prompt is not None:
            if args.prompt == "-":
                extra_context = _read_stdin_if_piped()
            else:
                stdin_text = _read_stdin_if_piped()
                extra_context = args.prompt if not stdin_text.strip() else f"{args.prompt.rstrip()}\n\n{stdin_text.strip()}"
        return _run_commit(args.target, model=args.model, debug=args.debug, debug_file=args.debug_file,
                           session=args.session, save=not args.no_save,
                           reasoning=args.reasoning, maxout=args.max_out,
                           extra_context=extra_context, extras=args.extras)

    if args.files and args.prompt is None and sys.stdin.isatty():
        print(f"{C.ORANGE}error: -f/--file requires -p/--prompt or piped prompt input; use @path in the REPL{C.RESET}", file=sys.stderr)
        return 2

    if args.prompt is not None or not sys.stdin.isatty():
        stdin_attachment = None
        if "-" in args.files:
            if args.prompt in {None, "-"}:
                print(f"{C.ORANGE}error: stdin cannot be both the prompt and an attachment{C.RESET}", file=sys.stderr)
                return 2
            if sys.stdin.isatty():
                print(f"{C.ORANGE}error: -f - requires piped stdin bytes{C.RESET}", file=sys.stderr)
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
        return _run_prompt(prompt, model=args.model, debug=args.debug, debug_file=args.debug_file,
                           agent=args.agent, session=args.session, save=not args.no_save,
                           reasoning=args.reasoning, maxout=args.max_out,
                           show_continue=not args.quiet,
                           extras=args.extras,
                           ignore_local_config=args.ignore_local,
                           ignore_global_config=args.ignore_global,
                           files=args.files,
                           stdin_attachment=stdin_attachment,
                           presets=presets,
                           stats_json=args.stats_json, stats_csv=args.stats_csv)

    try:
        cfg = _cfg_from_env_compat(
            args.session,
            save_session=True,
            extras=args.extras,
            agent_id=cli_agent,
            ignore_local_config=args.ignore_local,
            ignore_global_config=args.ignore_global,
            presets=presets,
        )
    except ValueError as e:
        print(f"{C.ORANGE}error: {e}{C.RESET}", file=sys.stderr)
        return 2

    try:
        prompt_spec = P.load_configured_prompt_spec(cfg)
    except (FileNotFoundError, ValueError) as e:
        print(f"{C.ORANGE}{e}{C.RESET}", file=sys.stderr)
        return 2
    system = prompt_spec.system
    active_registry = _registry_for(cfg).select(prompt_spec.tool_selectors)
    cfg = _apply_agent_model(cfg, prompt_spec, args.model)
    cfg = _apply_agent_max_tokens(cfg, prompt_spec)
    cfg = _resolve_cli_model_override(cfg, args.model)

    cfg.history_file.parent.mkdir(parents=True, exist_ok=True)
    completer = replcomplete.JsCompleter(
        setting_keys=[spec.key for spec in settings.REGISTRY],
        names=lambda: sorted(set(providers.known_provider_ids()) | set(logins.load_logins())),
        spell=replcomplete.hunspell_suggest,
    )
    session = PromptSession(
        history=FileHistory(str(cfg.history_file)),
        completer=completer,
        complete_while_typing=False,  # Tab-triggered, never auto-pops
        complete_style=CompleteStyle.MULTI_COLUMN,  # rotating menu, columned for the long command list
    )

    messages = M.load_messages(cfg.session_file)
    if messages:
        print(f"{C.GREY}(resumed: {len(messages)} prior messages){C.RESET}")
    M.append_mark(cfg.session_file, "session_start")

    live_settings = copy.deepcopy(cfg.settings) if isinstance(cfg.settings, dict) else {}
    if args.reasoning is not None:
        settings.set_dotted(live_settings, ("model", "reasoning_effort"), _norm_effort(args.reasoning))
    if args.max_out is not None:
        settings.set_dotted(live_settings, ("model", "max_output_tokens"), args.max_out)
    elif (prompt_spec.max_output_tokens is not None
          and settings.get_dotted(live_settings, ("model", "max_output_tokens"), None) is None):
        # Agent default from 00-tools.yaml — seed the per-turn source of truth so
        # it survives the _cfg_for_live_state rebuild. Config/env/--max-out win.
        settings.set_dotted(live_settings, ("model", "max_output_tokens"), prompt_spec.max_output_tokens)
    event_hooks = events.EventHooks()
    event_hooks.set_dispatcher(
        setcmd.EventCommandDispatcher(
            settings=live_settings,
            cwd=getattr(cfg, "project_dir", Path.cwd()),
            events=event_hooks,
        )
    )
    state = {
        "running": True,
        "messages": messages,
        "system": system,
        "model": args.model if args.model is not None else cfg.model,
        "provider_id": cfg.provider_id,
        "provider_base_url": cfg.provider_base_url,
        "provider_api_key": cfg.provider_api_key,
        "provider_headers": dict(getattr(cfg, "provider_headers", {}) or {}),
        "settings": live_settings,
        "events": event_hooks,
        "tool_registry": active_registry,
        "tool_selectors": prompt_spec.tool_selectors,
        "sampling_cli": cfg.sampling_cli,
        "compact_notified": False,
        "compact_consecutive": 0,
        "compact_paused": False,
    }
    telemetry = runtime.Telemetry(debug_log=cfg.debug_log)

    print(BANNER.format(agent=cfg.agent_id, model=state["model"], prompt=cfg.prompts_dir, memory=cfg.session_file))

    if args.nonblocking:
        return asyncio.run(_repl_main(cfg, state, telemetry, session, prompt_spec))

    while state["running"]:
        try:
            line = session.prompt(ANSI(f"{C.YELLOW}LO> {C.RESET}")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if _handle_command(line, state, cfg):
            _sync_telemetry_from_live_settings(cfg, state, telemetry)
            continue

        prompt_text, line_attachments = attach.split_repl_attachments(line)
        input_event = _emit_repl_event(
            state,
            telemetry,
            "input",
            text=prompt_text,
            attachments=line_attachments,
        )
        if _event_results_changed_sampling(input_event.results):
            state["sampling_cli"] = _sampling_override_from_live_settings(state["settings"])
        if _event_results_changed_model(input_event.results):
            _sync_model_from_live_settings(state)
        input_changed_keys = _event_result_changed_keys(input_event.results)
        if _changed_provider_key(input_changed_keys):
            _sync_provider_from_live_settings(state, input_changed_keys)
        if _changed_lock_subagent_model_key(input_changed_keys):
            _sync_tool_registry_from_live_settings(cfg, state)
        _sync_telemetry_from_live_settings(cfg, state, telemetry)
        try:
            turn_cfg = _cfg_for_live_state(cfg, state)
            user_bundle = attach.build_user_message(prompt_text, line_attachments, turn_cfg)
        except attach.AttachmentError as e:
            print(f"{C.ORANGE}error: {e}{C.RESET}")
            continue

        before_len = len(state["messages"])
        state["messages"].append(user_bundle.runtime_message)
        _append_turn(cfg, user_bundle.history_message)
        try:
            before_turn_sampling = _sampling_override_from_live_settings(state["settings"])
            before_turn_model = _model_from_live_settings(state["settings"])
            before_turn_provider = _provider_from_live_settings(state["settings"])
            before_turn_lock = _live_bool_setting(
                state["settings"],
                ("subagents", "lock_model"),
                cfg.lock_subagent_model,
            )
            runtime.run_turn(
                turn_cfg,
                state["system"],
                state["messages"],
                telemetry,
                trace_override=bool(
                    settings.get_dotted(state["settings"], ("runtime", "trace"), cfg.trace)
                ),
                reasoning_effort_override=turn_cfg.reasoning_effort,
                max_output_override=turn_cfg.max_output_tokens,
                tool_registry=state["tool_registry"],
                sampling=_sampling_for_turn(turn_cfg, prompt_spec, state["sampling_cli"]),
                event_hooks=state.get("events"),
            )
            after_turn_sampling = _sampling_override_from_live_settings(state["settings"])
            if after_turn_sampling != before_turn_sampling:
                state["sampling_cli"] = after_turn_sampling
            after_turn_model = _model_from_live_settings(state["settings"])
            if after_turn_model != before_turn_model:
                _sync_model_from_live_settings(state)
            after_turn_provider = _provider_from_live_settings(state["settings"])
            _sync_provider_delta_from_live_settings(state, before_turn_provider, after_turn_provider)
            after_turn_lock = _live_bool_setting(
                state["settings"],
                ("subagents", "lock_model"),
                cfg.lock_subagent_model,
            )
            if after_turn_lock != before_turn_lock:
                _sync_tool_registry_from_live_settings(cfg, state)
            _sync_telemetry_from_live_settings(cfg, state, telemetry)
            state["messages"][before_len] = user_bundle.history_message
            # Persist anything new the turn appended.
            for m in state["messages"][before_len + 1:]:
                _append_turn(cfg, m)
            _maybe_auto_compact(turn_cfg, state)
        except KeyboardInterrupt:
            cancel_event = _emit_repl_event(state, telemetry, "cancel", reason="keyboard_interrupt")
            if _event_results_changed_sampling(cancel_event.results):
                state["sampling_cli"] = _sampling_override_from_live_settings(state["settings"])
            if _event_results_changed_model(cancel_event.results):
                _sync_model_from_live_settings(state)
            cancel_changed_keys = _event_result_changed_keys(cancel_event.results)
            if _changed_provider_key(cancel_changed_keys):
                _sync_provider_from_live_settings(state, cancel_changed_keys)
            if _changed_lock_subagent_model_key(cancel_changed_keys):
                _sync_tool_registry_from_live_settings(cfg, state)
            _sync_telemetry_from_live_settings(cfg, state, telemetry)
            if len(state["messages"]) > before_len + 1:
                # Turn did real work (assistant/tool messages beyond the user
                # prompt) before ^C landed. Keep it: persist the partial turn,
                # then heal any orphaned tool_calls in memory so the next turn is
                # valid. The mark is informational ONLY — a `rollback_to:` mark
                # would silently re-truncate this turn on the next session load.
                print(f"\n{C.ORANGE}(turn interrupted — partial work kept){C.RESET}")
                state["messages"][before_len] = user_bundle.history_message
                for m in state["messages"][before_len + 1:]:
                    _append_turn(cfg, m)
                M.append_mark(cfg.session_file, "turn_interrupted")
                state["messages"][:] = M.balance_orphaned_tool_calls(state["messages"])
            else:
                # Stopped before the model produced anything worth keeping — drop
                # the bare user prompt (rollback removes it on reload too).
                print(f"\n{C.ORANGE}(turn aborted){C.RESET}")
                state["messages"][:] = state["messages"][:before_len]
                M.append_mark(cfg.session_file, f"rollback_to:{before_len}")
                M.append_mark(cfg.session_file, "turn_aborted")
        except Exception as e:  # noqa: BLE001
            print(f"{C.ORANGE}error: {type(e).__name__}: {e}{C.RESET}")
            state["messages"][:] = state["messages"][:before_len]
            M.append_mark(cfg.session_file, f"rollback_to:{before_len}")
            M.append_mark(cfg.session_file, f"error: {type(e).__name__}: {e}")
    return 0
if __name__ == "__main__":
    sys.exit(main())
