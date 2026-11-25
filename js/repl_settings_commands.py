"""The REPL's settings commands: /set, /fset, /show, /save and
/compact-auto, the effective /set view, and live state following a changed
setting (`apply_settings_result`)."""

from __future__ import annotations

import copy
import os
from dataclasses import replace
from pathlib import Path

from . import (
    context_window,
    fset,
    jsrc_save,
    login_defaults,
    logins,
    messages as msgs,
    paths as _paths,
    providers,
    repl_model_commands,
    repl_state,
    routing,
    sampling as sampling_mod,
    setcmd,
    settings,
    text_layout,
)
from .config import Config
from .repl_state import Command, ReplState
from .sampling import Sampling
from .session_catalog import record_model_switch

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


def apply_settings_result(result: setcmd.CommandResult, state: ReplState, cfg: Config) -> str | None:
    """Print a settings verb's output and carry changed keys into live state."""
    keys = result.changed_keys
    if _changed_model_key(keys):
        session = getattr(cfg, "session_file", None)
        recorded = session is not None and session != Path(os.devnull)
        previous = repl_model_commands.live_stamp(cfg, state) if recorded else None
        repl_state.sync_model_from_live_settings(state)
        if recorded:
            stamp = repl_model_commands.live_stamp(cfg, state)
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


def cmd_set(arg: str, state: ReplState, cfg: Config) -> str | None:
    parts = arg.split(maxsplit=1)
    if len(parts) < 2 and not (parts and parts[0].startswith("-") and len(parts[0]) > 1):
        return _show_settings(state, cfg, parts[0] if parts else None)
    # `set -key` goes back to the value the session started with; in a jsrc
    # replay, to what the layers below the running file set.
    if state.rc_replay and state.rc_baseline is not None:
        baseline = state.rc_baseline
    else:
        baseline = cfg.settings if isinstance(getattr(cfg, "settings", None), dict) else None
    columns = None if state.rc_replay else text_layout.block_width()
    result = setcmd.set_command(state.settings, arg, baseline, columns=columns)
    if state.rc_replay:
        return _replayed(result)
    if result.error is None:
        unset = parts[0].startswith("-")
        _note_live_set(state, parts[0][1:] if unset else parts[0], unset=unset)
    return apply_settings_result(result, state, cfg)


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
    return _replayed(result) if state.rc_replay else apply_settings_result(result, state, cfg)


def _cmd_show(arg: str, state: ReplState, cfg: Config) -> str | None:
    return _show_settings(state, cfg, arg.split()[0] if arg else None)


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
    keys = jsrc_save.changed_keys(saved["settings"], live)
    provider_id, model = state.provider_id, state.model
    if (provider_id, model) != saved["model"] and "MODEL" not in keys and isinstance(model, str) and model:
        settings.set_dotted(live, ("MODEL",), providers.qualified_model_id(provider_id, model))
        keys.append("MODEL")
    fset_names = jsrc_save.changed_fset_names(saved["fset"])
    extra = [line for line in _rc_lines(state) if line not in saved["lines"]]
    if not (keys or fset_names or extra):
        msgs.say(msgs.SAVE_NOTHING)
        return None
    path = _paths.global_config_file()
    try:
        count = jsrc_save.save_settings_to_jsrc(path, live, keys=keys, fset_names=fset_names,
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


def _cmd_compact_auto(arg: str, state: ReplState, cfg: Config) -> str | None:
    value = arg.strip().lower()
    if value not in ("on", "off"):
        return msgs.USAGE.said(usage="/compact-auto on|off")
    return apply_settings_result(setcmd.apply_set(state.settings, "COMPACTION_AUTO", value), state, cfg)


COMMANDS: dict[str, Command] = {
    "set": Command(cmd_set, "set [key [value]]", msgs.CMD_SET,
                   complete="set"),
    "fset": Command(_cmd_fset, "fset [name [value]]", msgs.CMD_FSET, complete="fset"),
    "show": Command(_cmd_show, "show [key]", msgs.CMD_SHOW, complete="keys"),
    "save": Command(_cmd_save, "save", msgs.CMD_SAVE),
    "compact-auto": Command(_cmd_compact_auto, "compact-auto on|off", msgs.CMD_COMPACT_AUTO),
}
