"""The REPL's slash commands: the command table, the dispatcher, aliases and
the startup run of the jsrc files."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import io
import json
import os
import re
import shlex
import subprocess
import textwrap
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from . import (
    compaction,
    context_window,
    display as display_mod,
    events,
    fset,
    hookexec,
    jail as _jail,
    login_defaults,
    logins,
    memory as M,
    messages as msgs,
    model_metadata,
    paths as _paths,
    picker,
    prompt_commands,
    providers,
    repl_state,
    routing,
    run_assembly,
    runtime,
    sampling as sampling_mod,
    session_picker,
    session_store,
    setcmd,
    settings,
    skill_picker,
    skills,
    snippets,
    stream_transport,
    supervisor,
    text_layout,
    usage as usage_mod,
    usage_display,
)
from .config import Config
from .repl_state import ReplState
from .sampling import Sampling
from .session_catalog import (
    append_title,
    record_model_switch,
    session_title,
)
from .toolkit import policy as tool_policy, task_jobs

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


def _live_settings_overlay_for_state(cfg: Config, state: ReplState) -> dict[str, setcmd.LiveValue]:
    """Resolve the same route the next turn uses and diff it against the store."""
    turn_cfg = state.config_for_turn(cfg)
    provider_id = turn_cfg.provider_id
    saved_login = routing._saved_login(provider_id) if provider_id else None
    provider_def = providers.get_provider(provider_id) if provider_id else None
    provider_default_base_url = (
        providers.provider_base_url(provider_def, None, os.environ) if provider_def is not None else None
    )
    eff_sampling = login_defaults.sampling(turn_cfg)
    manifest_sampling = login_defaults.layer_sampling(turn_cfg.layer_agent)
    overlay = _live_settings_overlay(
        state.settings,
        live_model=turn_cfg.model,
        model_source=state.model_source,
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
    masked = setcmd.masks_secrets(state.settings)
    values, sources = login_defaults.resolve(login_defaults.layers_of(turn_cfg))
    for key in login_defaults.KEYS:
        source = sources.get(key)
        if key in overlay and source in (None, "env", "flag"):
            continue  # the overlay already names the env var or flag
        if source in (None, "jsrc") and values.get(key) == settings.get_dotted(state.settings, (key,)):
            continue
        label = (msgs.SETTING_SOURCE_LOGIN_LAYER.words(provider_id=login.provider_id, source=source) if source in ("server", "model") and login is not None
                 else source or msgs.SETTING_SOURCE_UNSET.words())
        overlay[key] = setcmd.LiveValue(
            display=setcmd.render_value(settings.SPEC_BY_KEY[key], values.get(key), masked=masked),
            source=label,
        )
    return overlay


def _rc_lines(state: ReplState) -> list[str]:
    """The session's `on` and `alias` lines, as a jsrc writes them."""
    hooks = state.events
    lines = setcmd.event_lines(hooks) if hooks is not None else []
    return lines + [f"alias {name} {body}" for name, body in state.aliases.items()]


def mark_saved(state: ReplState) -> None:
    """Record the settings, /fset entries, `on` and `alias` lines and model as
    they stand now: what `/save` compares against, so it writes only what
    the operator changed after this."""
    state.saved = {
        "settings": copy.deepcopy(state.settings),
        "fset": fset.overrides(),
        "lines": _rc_lines(state),
        "model": (state.provider_id, state.model),
    }


def _changed_model_key(keys: list[str]) -> bool:
    return "MODEL" in keys


def _changed_provider_key(keys: list[str]) -> bool:
    return any(key in {"PROVIDER", "BASE_URL", "API_KEY"} for key in keys)


def _changed_lock_subagent_model_key(keys: list[str]) -> bool:
    return "SUBAGENT_MODEL_LOCK" in keys


def _apply_settings_result(result: setcmd.CommandResult, state: ReplState, cfg: Config) -> str | None:
    """Print a settings verb's output and carry changed keys into live state."""
    keys = result.changed_keys
    if _changed_model_key(keys):
        session = getattr(cfg, "session_file", None)
        recorded = session is not None and session != Path(os.devnull)
        previous = _live_stamp(cfg, state) if recorded else None
        repl_state.sync_model_from_live_settings(state)
        if recorded:
            stamp = _live_stamp(cfg, state)
            if stamp != previous:
                record_model_switch(session, stamp=stamp, previous=previous)
    if _changed_provider_key(keys):
        repl_state.sync_provider_from_live_settings(state, keys)
    if _changed_lock_subagent_model_key(keys):
        repl_state.sync_tool_registry_from_live_settings(cfg, state)
    if "BANNER" in keys:
        msgs.set_banner(str(settings.knob(state.settings, "BANNER")))
    for out in result.lines:
        print(out)
    if result.error:
        return result.error
    if any(key.startswith("CONTEXT_SIZE") for key in keys):
        context_window.install_context_window_overrides(state.config_for_turn(cfg))
    return None


def _show_settings(state: ReplState, cfg: Config, key: str | None) -> str | None:
    """Render the values the NEXT turn uses, annotating knobs whose live source
    bypasses the store."""
    overlay = _live_settings_overlay_for_state(cfg, state)
    result = setcmd.show_lines_effective(state.settings, overlay, key, columns=text_layout.block_width())
    for out in result.lines:
        print(out)
    return result.error


def _cmd_set(arg: str, state: ReplState, cfg: Config) -> str | None:
    parts = arg.split(maxsplit=1)
    if len(parts) < 2 and not (parts and parts[0].startswith("-") and len(parts[0]) > 1):
        return _show_settings(state, cfg, parts[0] if parts else None)
    # `set -key` goes back to the value the session started with.
    baseline = cfg.settings if isinstance(getattr(cfg, "settings", None), dict) else None
    columns = None if state.rc_replay else text_layout.block_width()
    result = setcmd.set_command(state.settings, arg, baseline, columns=columns)
    if state.rc_replay:
        return _replayed(result)
    if result.error is None:
        unset = parts[0].startswith("-")
        _note_live_set(state, parts[0][1:] if unset else parts[0], unset=unset)
    return _apply_settings_result(result, state, cfg)


def _note_live_set(state: ReplState, name: str, *, unset: bool) -> None:
    """Record one `set` (a typed `/set`, a script or `on` handler line) in the
    `/set` layer of the login-default settings, as it runs: a new dict per
    change. `set -KEY` takes the key out of the layer; any value, even one
    equal to what the session already has, goes in."""
    spec = settings.spec_for(name)
    if spec is None or spec.key not in login_defaults.KEYS:
        return
    live = dict(state.live_set)
    value = None if unset else settings.get_dotted(state.settings, spec.path)
    if value is None:
        live.pop(spec.key, None)
    else:
        live[spec.key] = value
    state.live_set = live
    # A set or unset of the key ends a resumed session's value for it.
    if spec.key in state.live_resume:
        state.live_resume = {k: v for k, v in state.live_resume.items() if k != spec.key}


def _replayed(result: setcmd.CommandResult) -> str | None:
    """A settings verb's output in the startup run of the jsrc files: printed,
    its keys left in the jsrc layer. `run_rc_commands` carries what the run
    changed into live state when it ends."""
    for out in result.lines:
        print(out)
    return result.error


def _cmd_fset(arg: str, state: ReplState, cfg: Config) -> str | None:
    baseline = cfg.settings if isinstance(getattr(cfg, "settings", None), dict) else None
    result = setcmd.fset_command(state.settings, arg, baseline)
    return _replayed(result) if state.rc_replay else _apply_settings_result(result, state, cfg)


def _cmd_show(arg: str, state: ReplState, cfg: Config) -> str | None:
    return _show_settings(state, cfg, arg.split()[0] if arg else None)


def _cmd_on(arg: str, state: ReplState, cfg: Config) -> str | None:
    if state.events is None:
        state.events = events.EventHooks()
    result = setcmd.on_command(state.events, arg)
    for out in result.lines:
        print(out)
    return result.error


def _cmd_save(arg: str, state: ReplState, cfg: Config) -> str | None:
    """Write what the operator changed this session into the global jsrc,
    editing it in place: the settings `/set` changed, the model `/model`
    picked, the /fset entries changed, and new `on` and `alias` lines.
    Nothing else goes in. `/save server` writes this session's model
    settings into the saved login as the active model's defaults."""
    if arg.strip().lower() == "server":
        return _save_server(cfg, state)
    saved = state.saved or {
        "settings": cfg.settings if isinstance(cfg.settings, dict) else {},
        "fset": {}, "lines": [], "model": (cfg.provider_id, cfg.model),
    }
    live = copy.deepcopy(state.settings)
    keys = settings.changed_keys(saved["settings"], live)
    provider_id, model = state.provider_id, state.model
    if (provider_id, model) != saved["model"] and "MODEL" not in keys and isinstance(model, str) and model:
        settings.set_dotted(live, ("MODEL",), providers.qualified_model_id(provider_id, model))
        keys.append("MODEL")
    fset_names = settings.changed_fset_names(saved["fset"])
    extra = [line for line in _rc_lines(state) if line not in saved["lines"]]
    if not (keys or fset_names or extra):
        msgs.say(msgs.SAVE_NOTHING)
        return None
    path = _paths.global_config_file()
    try:
        count = settings.save_settings_to_jsrc(path, live, keys=keys, fset_names=fset_names,
                                               extra_lines=extra)
    except OSError as exc:
        return msgs.SAVE_FAILED.said(error=f"{type(exc).__name__}: {exc}")
    msgs.say(msgs.SAVED, lines=msgs.plural(count, "line"), path=path)
    mark_saved(state)
    return None


def _save_server(cfg: Config, state: ReplState) -> str | None:
    """Write the session's server and model layer values for the active
    model into the login the session routes to."""
    turn = state.config_for_turn(cfg)
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


def _live_stamp(cfg: Config, state: ReplState) -> dict:
    """The stamp a turn started now would write: its model and the effort that
    model resolves to."""
    turn = state.config_for_turn(cfg)
    return M.stamp_for(turn.model, turn.provider_id, turn.reasoning_effort)


def _cmd_compact_auto(arg: str, state: ReplState, cfg: Config) -> str | None:
    value = arg.strip().lower()
    if value not in ("on", "off"):
        return msgs.USAGE.said(usage="/compact-auto on|off")
    return _apply_settings_result(setcmd.apply_set(state.settings, "COMPACTION_AUTO", value), state, cfg)


def _set_model_via_route(state: ReplState, cfg: Config, model_value: str) -> None:
    """Apply `/model <id>` through the SAME saved-login gate startup routing
    uses for authoritative prefix switches. A `vendor/model` prefix may switch
    the direct endpoint only when `vendor` is a saved login; otherwise the
    current endpoint stays and the full id rides it."""
    configured_provider_id = state.provider_id
    configured_base_url = state.provider_base_url
    configured_api_key = state.provider_api_key
    configured_headers = state.provider_headers
    parsed_provider_id, _parsed_model = providers.parse_model_prefix(model_value)
    prefix_login = routing._saved_login(parsed_provider_id) if parsed_provider_id is not None else None
    state.model_source = "/model command"
    if parsed_provider_id is not None and prefix_login is None:
        state.model = model_value
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
    state.model = route.model
    state.provider_id = route.provider_id
    state.provider_base_url = route.base_url
    state.provider_api_key = route.api_key
    state.provider_headers = dict(route.headers)
    msgs.say(msgs.MODEL_SET, model=providers.qualified_model_id(route.provider_id, route.model))


def _pick_model_into_state(state: ReplState, cfg: Config) -> None:
    selected = picker.pick_model(
        provider_id=state.provider_id,
        provider_base_url=state.provider_base_url,
        provider_api_key=state.provider_api_key,
        model=state.model,
    )
    if not selected:
        return
    state.provider_id = selected["provider_id"]
    state.provider_base_url = selected.get("provider_base_url")
    state.provider_api_key = selected.get("provider_api_key")
    state.provider_headers = dict(selected.get("provider_headers") or {})
    state.model = selected["model"]
    chosen = providers.qualified_model_id(selected.get("provider_id"), selected["model"])
    settings.set_dotted(state.settings, ("MODEL",), chosen)
    state.model_source = None  # the store now holds the pick; /save writes it
    msgs.say(msgs.MODEL_SET, model=chosen)


def _cmd_models(arg: str, state: ReplState, cfg: Config) -> str | None:
    max_models = 50
    if arg:
        try:
            max_models = int(arg.split()[0])
        except ValueError:
            return msgs.MODELS_LIMIT_NOT_A_NUMBER.said(value=arg.split()[0])
    provider_id = state.provider_id
    provider_base_url = state.provider_base_url
    provider_api_key = state.provider_api_key
    if not provider_id:
        return msgs.NO_PROVIDER.said()
    try:
        model_ids = logins.models_for_provider(provider_id, provider_base_url, provider_api_key)
    except Exception as e:  # noqa: BLE001
        return msgs.MODELS_NOT_LISTED.said(error=f"{type(e).__name__}: {e}")
    for mid in model_ids[:max_models]:
        msgs.say(msgs.MODEL_ROW, model=mid)
    if len(model_ids) > max_models:
        msgs.say(msgs.MORE_ROWS, count=len(model_ids) - max_models)
    return None


def _cmd_provider(arg: str, state: ReplState, cfg: Config) -> str | None:
    if not arg:
        cur = state.provider_id
        if cur:
            msgs.say(msgs.PROVIDER_IS, provider=cur)
        else:
            msgs.say(msgs.PROVIDER_UNSET)
        return None
    name = arg.split()[0]
    repl_state.set_provider_state(state, name)
    msgs.say(msgs.PROVIDER_SET, provider=providers.normalize_provider_id(name) or name)
    return None


def _cmd_baseurl(arg: str, state: ReplState, cfg: Config) -> str | None:
    state.provider_base_url = arg.split()[0] if arg else None
    msgs.say(msgs.BASE_URL_SET if arg else msgs.BASE_URL_CLEARED)
    return None


def _cmd_apikey(arg: str, state: ReplState, cfg: Config) -> str | None:
    state.provider_api_key = arg.split()[0] if arg else None
    msgs.say(msgs.API_KEY_SET if arg else msgs.API_KEY_CLEARED)
    return None


def _cmd_login(arg: str, state: ReplState, cfg: Config) -> str | None:
    parts = arg.split()
    if not parts:
        return msgs.USAGE.said(usage="/login <name> [apikey] [baseurl] [provider]")
    name = parts[0]
    key, url, ptype = (parts[1:] + [None, None, None])[:3]
    if key is None and url is None and ptype is None:
        # bare `/login <name>`: load a saved login (or fall back to provider defaults)
        repl_state.set_provider_state(state, name)
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
    repl_state.apply_saved_login_to_state(state, canonical)
    msgs.say(msgs.LOGIN_SAVED, provider=canonical)
    return None


def _cmd_logout(arg: str, state: ReplState, cfg: Config) -> str | None:
    state.provider_id = None
    state.provider_base_url = None
    state.provider_api_key = None
    state.provider_headers = {}
    msgs.say(msgs.PROVIDER_CLEARED)
    return None


def _cmd_model(arg: str, state: ReplState, cfg: Config) -> str | None:
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


def _cmd_refresh_model_catalog(arg: str, state: ReplState | None, cfg: Config) -> str | None:
    model_metadata.force_refresh_model_catalog()
    return None


def _cmd_name(arg: str, state: ReplState | None, cfg: Config) -> str | None:
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


def _cmd_session(arg: str, state: ReplState, cfg: Config) -> str | None:
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
    target = session_picker.session_target(choice)
    state.switch_session = target
    state.running = False
    msgs.say(msgs.SESSIONS_SWITCHING, name=session_store.display_name(target.path), dir=target.cwd or Path.cwd())
    return None


def _cmd_reset(arg: str, state: ReplState, cfg: Config) -> str | None:
    state.messages.clear()
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


def _cmd_cost(arg: str, state: ReplState | None, cfg: Config) -> str | None:
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


def _copy_command() -> list[str] | None:
    """COPY_COMMAND split into words, else wl-copy under Wayland, else xclip
    under X11; None when there is no clipboard."""
    if configured := settings.live_value("COPY_COMMAND"):
        return shlex.split(str(configured))
    if os.environ.get("WAYLAND_DISPLAY"):
        return ["wl-copy"]
    if os.environ.get("DISPLAY"):
        return ["xclip", "-selection", "clipboard"]
    return None


def _cmd_copy(arg: str, state: ReplState, cfg: Config) -> str | None:
    """The answer ``arg`` back (1, the last, by default) onto the clipboard,
    as the model wrote it: no wrapping, no marks, no padding."""
    arg = arg.strip() or "1"
    if not arg.isdigit() or int(arg) < 1:
        msgs.say(msgs.COPY_USAGE)
        return None
    n = int(arg)
    answers = [text for m in state.messages if m.get("role") == "assistant"
               if (text := display_mod._content_text(m.get("content"))).strip()]
    if len(answers) < n:
        msgs.say(msgs.COPY_NOTHING, n=n)
        return None
    command = _copy_command()
    if command is None:
        msgs.say(msgs.COPY_NO_CLIPBOARD)
        return None
    text = answers[-n].strip("\n") + "\n"
    try:
        # wl-copy stays behind to serve the clipboard: no pipes it could hold open.
        subprocess.run(command, input=text.encode(), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=5, check=True)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        msgs.say(msgs.FAILED, error=e)
        return None
    msgs.say(msgs.COPY_DONE, n=n, chars=f"{len(text):,}")
    return None


def _cmd_wipe(arg: str, state: ReplState, cfg: Config) -> str | None:
    bak = M.wipe(cfg.session_file)
    usage_mod.forget(cfg.session_file)
    state.messages = M.load_replay_messages(cfg.session_file)
    task_jobs.forget(runtime.T.STOCK_CONTEXT.task_owner)
    compaction.history_rewritten(runtime.T.STOCK_CONTEXT)
    if bak:
        msgs.say(msgs.WIPE_ROTATED, path=bak.name)
    else:
        msgs.say(msgs.WIPE_NOTHING)
    return None


def _cmd_persona(arg: str, state: ReplState, cfg: Config) -> str | None:
    text = state.system
    print(text[:2048])
    if len(text) > 2048:
        msgs.say(msgs.PERSONA_TRUNCATED, size=len(text))
    return None


def _cmd_jobs(arg: str, state: ReplState, cfg: Config) -> str | None:
    sup = supervisor.get_current()
    if sup is None:
        msgs.say(msgs.REPL_ONLY, command="/jobs")
        return None
    jobs = sup.jobs()
    if not jobs:
        msgs.say(msgs.NO_JOBS)
    for j in jobs:
        msgs.say(msgs.JOB_ROW, id=j.id, kind=j.kind, label=f"  {j.label}" if j.label else "")
    return None


def _cmd_cancel(arg: str, state: ReplState, cfg: Config) -> str | None:
    sup = supervisor.get_current()
    if sup is None:
        msgs.say(msgs.REPL_ONLY, command="/cancel")
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


def _cmd_window(arg: str, state: ReplState, cfg: Config) -> str | None:
    """`/window` lists the windows, `/window N` shows window N, `/window close
    [N]` closes N or the window on screen."""
    windows = state.windows
    if windows is None:
        msgs.say(msgs.REPL_ONLY, command="/window")
        return None
    words = arg.split()
    usage = msgs.USAGE.said(usage="/window [N | close [N]]")
    if not words:
        for row in windows.rows():
            mark = (msgs.WINDOW_MARK_CURRENT.words() if row["current"]
                    else msgs.WINDOW_MARK_ACTIVE.words() if row["active"] else "")
            state_words = (msgs.WINDOW_STATE_RUNNING if row["running"] else msgs.WINDOW_STATE_FINISHED).words()
            msgs.say(msgs.WINDOW_ROW, number=row["number"], mark=mark,
                     name=row["agent"] or msgs.WINDOW_MAIN.words(),
                     state=state_words if row["agent"] else "")
        return None
    if words[0] == "close":
        if len(words) > 2 or (len(words) == 2 and not words[1].isdigit()):
            return usage
        number = int(words[1]) if len(words) == 2 else windows.windows.current
        return _on_loop(lambda: windows.close(number))
    if len(words) == 1 and words[0].isdigit():
        number = int(words[0])
        if not _on_loop(lambda: windows.windows.switch(number)):
            return msgs.NO_SUCH_WINDOW.said(number=number)
        return None
    return usage


def _on_loop(call: Callable[[], object]) -> object:
    """``call``'s result, run on the REPL's loop from a command's executor thread."""
    sup = supervisor.get_current()
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if sup is None or running is sup.loop:
        return call()

    async def run():
        return call()

    return asyncio.run_coroutine_threadsafe(run(), sup.loop).result()


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


def _cmd_compact(arg: str, state: ReplState, cfg: Config) -> str | None:
    model, focus, ok = _split_compact_model(arg)
    if not ok:
        return msgs.USAGE.said(usage="/compact [-m model] [focus]")
    forced = focus == repl_state.repl_assembly(state).compact_force_focus
    if forced:
        focus = ""
    try:
        compact_cfg = state.config_for_turn(cfg)
        hooks = compaction.compact_hooks(state, compact_cfg)
        hooks["emit"], skipped = compaction.track_compact_skips(hooks["emit"])
        with stream_transport.net_role(msgs.COMPACTING_ROLE.words()):
            result = compaction.compact_now_sync(compact_cfg, state.system, state.messages, model=model,
                                                 focus=focus, forced=forced, **hooks)
    except snippets.SnippetError:
        raise
    except Exception as e:  # noqa: BLE001
        return msgs.COMPACTION_FAILED.said(error=f"{type(e).__name__}: {e}")
    if result not in skipped:
        msgs.say(msgs.COMPACTION_DONE, result=result)
    return None


def _append_closing_note(cfg: Config, state: ReplState, reminder: str) -> None:
    """Fold a closing note into the conversation without breaking role alternation.

    After a completed turn the note is simply the next user message. When the last
    message is already a user turn it is rewritten to carry the note instead, since
    two user messages in a row are rejected outright by some providers and silently
    mangled by chat templates that assume alternation."""
    messages = state.messages
    if not messages or messages[-1].get("role") != "user":
        message = {"role": "user", "content": reminder}
        messages.append(message)
        M.append_message(cfg.session_file, message)
        return

    last_user = messages[-1]
    content = last_user.get("content")
    if isinstance(content, str):
        last_user["content"] = f"{content}\n\n{reminder}"
    elif isinstance(content, list):
        last_user["content"] = [*content, {"type": "text", "text": reminder}]
    else:
        last_user["content"] = reminder
    M.replace_last_message(cfg.session_file, messages)


def _cmd_quit(arg: str, state: ReplState, cfg: Config) -> str | None:
    if arg:
        # Recorded as a user message so the next turn actually sees it. It is
        # appended to the previous user message rather than added as a second
        # one: providers with strict role alternation reject back-to-back user
        # turns, and chat templates that tolerate it silently degrade.
        reminder = f'<js-reminder>User closed the client with the message: {arg}</js-reminder>'
        _append_closing_note(cfg, state, reminder)
    state.running = False
    return None


def _print_tool_chain(state: ReplState, cfg: Config) -> None:
    """Every tool with its resolved state and the chain entry that decided it."""
    full = run_assembly.registry_for(state.config_for_turn(cfg))
    try:
        config = tool_policy.load_tools_config()
        rules = tool_policy.expand(state.tool_selectors, config, f"agent {cfg.agent_id!r}")
    except tool_policy.ToolPolicyError as exc:
        msgs.say(msgs.FAILED, error=exc)
        return
    for row in tool_policy.render_table(tool_policy.resolve(full.tools, rules), config.bans):
        print(row)


def _cmd_tools(arg: str, state: ReplState, cfg: Config) -> str | None:
    _print_tool_chain(state, cfg)
    return None


def _print_skill_catalog(state: ReplState) -> None:
    """`/skill` alone lists every enabled skill; user-only ones are marked."""
    catalog = skills.discover_skills(Path.cwd(), disabled=repl_state.disabled_skills(state))
    if not catalog.skills:
        msgs.say(msgs.NO_SKILLS)
        return
    for skill in catalog.skills:
        entry = msgs.SKILL_ROW_USER_ONLY if not skill.model_invocable else msgs.SKILL_ROW
        msgs.say(entry, name=skill.name, source=skill.source, description=skill.description)
    msgs.say(msgs.SKILL_HINT)


def _cmd_skill(arg: str, state: ReplState, cfg: Config) -> str | None:
    """Bare `/skill` lists the skills. `/skill <name> [request]` typed at the
    input line never reaches the table: it is a turn (see handle_command)."""
    if arg:
        return msgs.SKILL_IS_A_TURN.said()
    _print_skill_catalog(state)
    return None


def _cmd_skills(arg: str, state: ReplState, cfg: Config) -> str | None:
    """Open the skill picker. Enter there sets SKILLS_DISABLED to the skills
    left switched off."""
    current = repl_state.disabled_skills(state)
    try:
        chosen = skill_picker.pick_skills(Path.cwd(), current)
    except Exception as exc:  # noqa: BLE001
        return msgs.FAILED.said(error=f"{type(exc).__name__}: {exc}")
    if chosen is None or tuple(chosen) == current:
        return None
    return _cmd_set(f"SKILLS_DISABLED {json.dumps(list(chosen))}", state, cfg)


def _cmd_exec(arg: str, state: ReplState | None, cfg: Config) -> str | None:
    """Run ``arg`` in the shell and queue its stdout for the next user message;
    with no ``state`` there is no next message and the stdout goes nowhere.
    Run by an `on` handler, the command reads the event as JSON on stdin with
    JS_EVENT naming it. Exit status 2 or a timeout under tool_call refuses the call."""
    command = arg.strip()
    if not command:
        return msgs.USAGE.said(usage="/exec <command>")
    call = events.current_call()
    live = state.settings if state is not None else cfg.settings
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
            hookexec.refuse_unfinished(call, str(failure))
        elif hookexec.apply_refusal(call, outcome):
            return None
        elif outcome.returncode != 0:
            detail = next((line.strip() for line in (outcome.stderr or outcome.stdout).splitlines()
                           if line.strip()), "")
            failure = msgs.EXEC_EXITED.said(command=command, status=outcome.returncode, detail=detail)
        else:
            note = hookexec.reminder(outcome.stdout)
            if note is not None and state is not None:
                repl_state.queue_note(state, note)
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


def _access(bind: _jail.Bind) -> str:
    return "read-write" if bind.rw else "read-only"


def set_working_dir(path: Path) -> None:
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


def _cmd_cd(arg: str, state: ReplState, cfg: Config) -> str | None:
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
    if jail is not None and not jail.bound(target, settings.knob(state.settings, "JAIL_BIND")):
        return msgs.CD_OUTSIDE_JAIL.said(path=target, root=jail.root)
    set_working_dir(target)
    repl_state.queue_note(state, snippets.render("working-directory", PATH=target))
    _record_workspace(cfg)
    msgs.say(msgs.CD_DONE, path=target)
    return None


def _cmd_add(arg: str, state: ReplState, cfg: Config) -> str | None:
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
    repl_state.queue_note(state, snippets.render("jail-path-added", PATH=bind.path, ACCESS=_access(bind)))
    _record_workspace(cfg)
    msgs.say(msgs.ADD_DONE, path=bind.path, access=_access(bind))
    return None


def _cmd_drop(arg: str, state: ReplState, cfg: Config) -> str | None:
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
    if not jail.bound(runtime.T.STOCK_CONTEXT.cwd, settings.knob(state.settings, "JAIL_BIND")):
        jail.add(dropped)
        return msgs.DROP_CWD.said(path=path)
    repl_state.queue_note(state, snippets.render("jail-path-removed", PATH=path))
    _record_workspace(cfg)
    msgs.say(msgs.DROP_DONE, path=path)
    return None


# --------------------------------------------------------------------------
# The command table. Every REPL command, rc line, /load line, `on` handler
# and ex command dispatches through COMMANDS; completion and /help read it.
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Command:
    """One command: ``run(arg, state, cfg)`` returns an error string or None."""

    run: Callable[[str, ReplState | None, Config], str | None]
    usage: str
    doc: msgs.Message
    complete: str | None = None  # argument completion source (see replcomplete)
    turn_state: bool = False     # clears/rotates/compacts the live message list
    # Reads and changes no REPL state: runs with a ``state`` of None, as an
    # `on` handler of a run outside the REPL.
    stands_alone: bool = False


_MAX_ALIAS_DEPTH = 16


def _cmd_alias(arg: str, state: ReplState, cfg: Config) -> str | None:
    """`alias` lists, `alias name` shows, `alias name command` defines,
    `alias -name` removes. `$*` in the body is the alias's arguments; a body
    without `$*` gets them appended."""
    aliases = state.aliases
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
    if name in commands(state):
        return msgs.ALIAS_IS_COMMAND.said(name=name)
    aliases[name] = parts[1].strip()
    msgs.say(msgs.ALIAS_ROW, name=name, body=aliases[name])
    return None


def _cmd_load(arg: str, state: ReplState, cfg: Config) -> str | None:
    """Run each line of a file through the command table, in order. The first
    error stops the file and names the line."""
    stack = state.load_stack
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


def _cmd_help(arg: str, state: ReplState, cfg: Config) -> str | None:
    topic, _, rest = arg.strip().partition(" ")
    if topic.lower() == "set":
        return _help_set(rest.strip())
    if topic.lower() == "fset":
        return _help_fset(rest.strip())
    rows = [(f"/{c.usage}", c.doc.said()) for c in dict.fromkeys(commands(state).values())]
    rows += [(f"/{name}", msgs.HELP_ALIAS.said(body=body)) for name, body in state.aliases.items()]
    rows += [(f"/{name}", msgs.HELP_PROMPT_COMMAND.said(path=command.path, description=command.description))
             for name, command in prompt_commands.discover(Path.cwd()).items()
             if name not in commands(state) and name not in state.aliases]
    rows += [("@path/to/file", msgs.HELP_ATTACH.said()), ("exit", msgs.HELP_EXIT.said())]
    columns = max(7, text_layout.terminal_width() - 1)
    width = min(max(text_layout.cells(usage) for usage, _doc in rows), (columns - 3) // 2)
    msgs.say(msgs.HELP_HEADING)
    for usage, doc in rows:
        usages = text_layout.wrap_words(usage, width).splitlines()
        docs = text_layout.wrap_words(msgs.words_of(doc), columns - width - 3).splitlines()
        for index in range(max(len(usages), len(docs))):
            label = usages[index] if index < len(usages) else ""
            text = docs[index] if index < len(docs) else ""
            msgs.say(msgs.HELP_ROW, usage=label + " " * (width - text_layout.cells(label)), doc=text)
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
    "turns": Command(lambda arg, state, cfg: msgs.say(msgs.TURNS_COUNT, messages=msgs.plural(len(state.messages), "message")),
                     "turns", msgs.CMD_TURNS),
    "cost": Command(_cmd_cost, "cost", msgs.CMD_COST, stands_alone=True),
    "copy": Command(_cmd_copy, "copy [N]", msgs.CMD_COPY),
    "session": Command(_cmd_session, "session [query]", msgs.CMD_SESSION, turn_state=True),
    "name": Command(_cmd_name, "name [title]", msgs.CMD_NAME, stands_alone=True),
    "jobs": Command(_cmd_jobs, "jobs", msgs.CMD_JOBS),
    "cancel": Command(_cmd_cancel, "cancel [id]", msgs.CMD_CANCEL),
    "window": Command(_cmd_window, "window [N | close [N]]", msgs.CMD_WINDOW),
    # The async REPL drains its loop-owned queue before a line reaches the
    # table; this entry runs everywhere else, where nothing queues.
    "flush": Command(lambda arg, state, cfg: msgs.say(msgs.NO_QUEUED_PROMPTS),
                     "flush", msgs.CMD_FLUSH, stands_alone=True),
    "compact": Command(_cmd_compact, "compact [-m model] [focus]", msgs.CMD_COMPACT,
                       turn_state=True),
    "compact-auto": Command(_cmd_compact_auto, "compact-auto on|off", msgs.CMD_COMPACT_AUTO),
    "refresh-model-catalog": Command(_cmd_refresh_model_catalog, "refresh-model-catalog",
                                     msgs.CMD_REFRESH_MODEL_CATALOG, stands_alone=True),
    "quit": Command(_cmd_quit, "quit [note]", msgs.CMD_QUIT),
    "cd": Command(_cmd_cd, "cd [dir]", msgs.CMD_CD, complete="path", turn_state=True),
    "add": Command(_cmd_add, "add <path>[:rw]", msgs.CMD_ADD, complete="path"),
    "drop": Command(_cmd_drop, "drop <path>", msgs.CMD_DROP, complete="path", turn_state=True),
    "exec": Command(_cmd_exec, "exec <command>", msgs.CMD_EXEC, complete="path", stands_alone=True),
}


def standalone(line: str) -> Command | None:
    """The command ``line`` runs when it stands alone (`Command.stands_alone`);
    None for any other line."""
    parsed = setcmd.split_command(line)
    command = COMMANDS.get(parsed[0]) if parsed is not None else None
    return command if command is not None and command.stands_alone else None


def run_command(line: str, state: ReplState, cfg: Config, depth: int = 0) -> tuple[bool, str | None]:
    """Dispatch one command line (leading `/` optional) through COMMANDS and
    aliases. Returns ``(handled, error)``; ``handled`` is False for a verb that
    is neither."""
    parsed = setcmd.split_command(line)
    if parsed is None:
        return True, None
    verb, arg = parsed
    command = commands(state).get(verb)
    if command is None:
        body = state.aliases.get(verb)
        if body is None:
            return False, None
        if depth >= _MAX_ALIAS_DEPTH:
            return True, msgs.ALIAS_TOO_DEEP.said(name=verb)
        # The body's $NAMEs expand once; the typed arguments never do.
        body = setcmd.expand_line(body, state.settings)
        expanded = body.replace("$*", arg) if "$*" in body else f"{body} {arg}".strip()
        handled, error = run_command(expanded, state, cfg, depth + 1)
        return True, error if handled else msgs.ALIAS_UNKNOWN_COMMAND.said(name=verb, verb=expanded.split()[0])
    if repl_state.repl_assembly(state).guard_turn_state and command.turn_state and (sup := supervisor.get_current()) is not None and sup.turn_active():
        return True, repl_state.repl_hint(msgs.TURN_RUNNING, state).said(command=f"/{verb}")
    return True, command.run(arg, state, cfg)


def _run_script_line(line: str, state: ReplState, cfg: Config) -> str | None:
    """One line from a file of commands, its $NAMEs expanded once: an
    unknown verb is an error. In the startup run of the jsrc files a
    setting's short name is that setting, as config load read it: `model X`
    is `set MODEL X`, not the /model command."""
    line = setcmd.expand_line(line, state.settings)
    parsed = setcmd.split_command(line)
    if state.rc_replay and parsed is not None and parsed[0] in settings.SPEC_BY_ALIAS:
        line = f"set {parsed[0]} {parsed[1]}"
    handled, error = run_command(line, state, cfg)
    if not handled:
        return msgs.UNKNOWN_COMMAND.said(verb=setcmd.split_command(line)[0])
    return error


def commands(state: ReplState | None) -> Mapping[str, Command]:
    supplied = repl_state.repl_assembly(state).commands
    return COMMANDS if supplied is None else supplied


def handle_command(line: str, state: ReplState, cfg: Config) -> bool:
    """Return True if `line` was a command (already handled), False otherwise."""
    if line in repl_state.repl_assembly(state).quit_words:
        state.running = False
        return True
    if not line.startswith("/") or is_skill_invocation(line, state):
        return False
    with repl_state.LIVE_STATE_LOCK:
        handled, error = run_command(line, state, cfg)
    if not handled and repl_state.repl_assembly(state).refuse_unknown and is_unknown_command(line):
        handled, error = True, msgs.UNKNOWN_SLASH_COMMAND.said()
    if isinstance(error, msgs.Said):
        msgs.say_said(error)
    elif error:
        msgs.say(msgs.COMMAND_REFUSED, error=error)
    return handled


# A slash word shaped like a command name. Anything else after the `/`
# (`/usr/bin/python is broken`, `/README.md`, `//text`) is prose for the model.
_COMMAND_WORD = re.compile(r"/[A-Za-z][A-Za-z0-9_-]*")


def is_unknown_command(line: str) -> bool:
    """`line` names a command that is not in the table, not an alias and not
    a markdown command."""
    word = line.split(maxsplit=1)[0]
    return (_COMMAND_WORD.fullmatch(word) is not None
            and prompt_commands.expand(line, prompt_commands.discover(Path.cwd())) is None)


def is_skill_invocation(line: str, state: ReplState | None = None) -> bool:
    """An invocation with a skill name is a turn carrying that skill."""
    assembly = repl_state.repl_assembly(state)
    match = skills.match_user_invocation(line, command=assembly.skill_command, pattern=assembly.skill_pattern)
    return match is not None and match.group(1) is not None


def run_rc_commands(state: ReplState, cfg: Config, paths: list[Path]) -> list[str]:
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
    stack = state.load_stack
    live = state.settings
    found = settings.collect_again(paths)
    layer = settings.seed_defaults()
    _fset.reset()
    state.settings = layer
    state.rc_replay = True
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
        state.settings = live
        state.rc_replay = False
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


def restore_workspace(cfg: Config) -> None:
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
        set_working_dir(cwd)
