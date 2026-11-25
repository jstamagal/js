"""The REPL's model, provider and login commands: /model, /pick-model,
/provider, /baseurl, /apikey, /login, /logout, /models and
/refresh-model-catalog."""

from __future__ import annotations

import os
from pathlib import Path

from . import (
    logins,
    memory as M,
    messages as msgs,
    model_metadata,
    picker,
    providers,
    repl_state,
    routing,
    settings,
)
from .config import Config
from .repl_state import Command, ReplState
from .session_catalog import record_model_switch


def live_stamp(cfg: Config, state: ReplState) -> dict:
    """The stamp a turn started now would write: its model and the effort that
    model resolves to."""
    turn = state.config_for_turn(cfg)
    return M.stamp_for(turn.model, turn.provider_id, turn.reasoning_effort)


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
    previous = live_stamp(cfg, state)
    if arg:
        _set_model_via_route(state, cfg, arg)
    else:
        _pick_model_into_state(state, cfg)
    stamp = live_stamp(cfg, state)
    if stamp != previous and cfg.session_file != Path(os.devnull):
        # A resume comes back on the switched-to model before any turn ran on it.
        record_model_switch(cfg.session_file, stamp=stamp, previous=previous)
    return None


def _cmd_refresh_model_catalog(arg: str, state: ReplState | None, cfg: Config) -> str | None:
    model_metadata.force_refresh_model_catalog()
    return None


COMMANDS: dict[str, Command] = {
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
    "refresh-model-catalog": Command(_cmd_refresh_model_catalog, "refresh-model-catalog",
                                     msgs.CMD_REFRESH_MODEL_CATALOG, stands_alone=True),
}
