"""Server and per-model setting defaults saved with a login.

A login (`js --login`) can carry values for `KEYS`: one table for the server
(`Login.defaults`) and one per model id (`Login.model_defaults`). A run that
routes to the server takes them, in this order:

    jsrc < server < model < env < flags

`login_for` finds the login a route uses: the saved login whose base URL is
the route's, the provider's own login when the route has no other URL.

Where a `KEYS` value comes from is never stored: each source is a layer, a
dict of only the keys it sets, kept on the Config (`Config.layer_*`) or read
from the login, and `resolve` merges them every time a value is needed:

    agent default < jsrc < server < model < agent < env < flag < resume < /set

The agent default is agent.yaml's `max_tokens`, which applies only when
nothing else sets OUTPUT_TOKENS_MAX; agent.yaml's reasoning and sampling are
the agent layer. The resume layer is a resumed session's stamped effort; a
typed `/set` of the key replaces it. A subagent takes its parent's resume
layer as agent-level effort (`for_child`), so the child's agent.yaml beats it. A model switch changes only which login and model the
server and model layers come from. `for_turn` is the one place a Config gets
the resolved values.
"""

from __future__ import annotations

from . import messages as msgs

from dataclasses import replace
from typing import Any

from . import settings as _settings
from .sampling import SAMPLING_FIELDS, Sampling

KEYS: tuple[str, ...] = (
    "CONTEXT_SIZE",
    "OUTPUT_TOKENS_MAX",
    "VISION",
    "EFFORT",
    "THINKING_BUDGET",
    *(field.upper() for field in SAMPLING_FIELDS),
)


def parse(text: str) -> dict[str, Any]:
    """``NAME=value`` entries split by ',' as setting values, each checked as
    `set` checks it. A name matches in any case. Raises ValueError naming
    the first bad entry."""
    values: dict[str, Any] = {}
    for entry in (text or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, sep, raw = entry.partition("=")
        key = name.strip().upper()
        if not sep or key not in KEYS:
            raise ValueError(msgs.LOGIN_DEFAULT_NOT_NAME_VALUE_WITH_NAME_ONE_OF.format(value=', '.join(KEYS), entry=entry))
        value, error = _settings.coerce_value(_settings.SPEC_BY_KEY[key], raw.strip())
        if error is not None:
            raise ValueError(msgs.LOGIN_DEFAULT_.format(key=key, error=error))
        values[key] = value
    return values


def render(values: dict[str, Any] | None) -> str:
    """``values`` as `parse` reads them."""
    out = []
    for key, value in (values or {}).items():
        if isinstance(value, bool):
            value = "on" if value else "off"
        elif key == "EFFORT" and value == "none":
            value = "off"  # `set` stores EFFORT off as none
        out.append(f"{key}={value}")
    return ", ".join(out)


def checked(values: Any) -> dict[str, Any]:
    """The entries of a stored table `parse` would accept, as values."""
    if not isinstance(values, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in values.items():
        try:
            out.update(parse(render({str(key).upper(): value})))
        except ValueError:
            continue
    return out


def session_values(settings: dict) -> dict[str, Any]:
    """The `KEYS` values ``settings`` holds."""
    out: dict[str, Any] = {}
    for key in KEYS:
        value = _settings.get_dotted(settings, (key,))
        if value is not None:
            out[key] = value
    return out


def _same_url(a: str | None, b: str | None) -> bool:
    return bool(a and b) and a.rstrip("/") == b.rstrip("/")


def login_for(provider_id: str | None, base_url: str | None) -> Any:
    """The saved login a route to ``provider_id`` at ``base_url`` uses, or
    None. The URL decides first: a saved login with that base URL, the
    provider's own when several match. With no match, the provider's own
    login only when it names no URL of its own."""
    from . import logins

    own = logins.get_login(provider_id) if provider_id else None
    if not base_url:
        return own
    if own is not None and _same_url(own.provider_base_url, base_url):
        return own
    for login in logins.load_logins().values():
        if _same_url(login.provider_base_url, base_url):
            return login
    if own is not None and own.provider_base_url is None:
        return own
    return None


# The layers `resolve` merges, lowest first.
LAYERS: tuple[str, ...] = ("agent default", "jsrc", "server", "model", "agent", "env", "flag", "resume", "set")
# The layers `/save server` writes: the rest belong to this run, the agent or
# the server, not the model.
_MODEL_LAYERS = frozenset({"jsrc", "model", "set"})


def env_layer(env: Any) -> dict[str, Any]:
    """The `KEYS` values the environment sets. A value `set` would refuse is
    left out; the settings loader already warned about it."""
    out: dict[str, Any] = {}
    missing = object()
    for key in KEYS:
        value = _settings.env_value(_settings.SPEC_BY_KEY[key], env, missing)
        if value is not missing:
            out[key] = value
    return out


def flag_layer(extras: list[str] | None) -> dict[str, Any]:
    """The `KEYS` values the --extra arguments (and the flags turned into
    them) set; a later argument wins."""
    out: dict[str, Any] = {}
    for arg in extras or ():
        try:
            path, value = _settings.parse_extra_arg(arg)
        except ValueError:
            continue
        if len(path) == 1 and path[0] in KEYS:
            out[path[0]] = value
    return out


def agent_layers(prompt_spec: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """The agent default and agent layers of an agent's agent.yaml."""
    if prompt_spec is None:
        return {}, {}
    default: dict[str, Any] = {}
    if getattr(prompt_spec, "max_output_tokens", None) is not None:
        default["OUTPUT_TOKENS_MAX"] = prompt_spec.max_output_tokens
    agent: dict[str, Any] = {}
    if getattr(prompt_spec, "reasoning_effort", None) is not None:
        agent["EFFORT"] = prompt_spec.reasoning_effort
    for field, value in (getattr(prompt_spec, "sampling", None) or {}).items():
        if str(field).upper() in KEYS and value is not None:
            agent[str(field).upper()] = value
    return default, agent


def resolve(layers: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Merge ``layers`` (name -> the `KEYS` values that layer sets) in `LAYERS`
    order. Returns each key's value and the layer it came from; a key no layer
    sets is in neither. Pure: the same layers give the same answer."""
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for name in LAYERS:
        for key, value in (layers.get(name) or {}).items():
            if key not in KEYS or value is None:
                continue
            if name == "agent default" and key in values:
                continue
            values[key], sources[key] = value, name
    return values, sources


def _jsrc_layer(cfg: Any) -> dict[str, Any]:
    return dict(getattr(cfg, "layer_jsrc", None) or {})


def layers_of(cfg: Any) -> dict[str, Any]:
    """Every layer for ``cfg``'s route and model: its own, and the server and
    model layers of the login it routes to."""
    login = login_for(getattr(cfg, "provider_id", None), getattr(cfg, "provider_base_url", None))
    model = getattr(cfg, "model", None) or ""
    return {
        "agent default": getattr(cfg, "layer_agent_default", None) or {},
        "jsrc": _jsrc_layer(cfg),
        "server": dict((getattr(login, "defaults", None) or {}) if login else {}),
        "model": dict(((getattr(login, "model_defaults", None) or {}).get(model, {})) if login else {}),
        "agent": getattr(cfg, "layer_agent", None) or {},
        "env": getattr(cfg, "layer_env", None) or {},
        "flag": getattr(cfg, "layer_flag", None) or {},
        "resume": getattr(cfg, "layer_resume", None) or {},
        "set": getattr(cfg, "layer_set", None) or {},
    }


def with_agent(cfg: Any, prompt_spec: Any, *, reasoning: bool = True, inherit: bool = False) -> Any:
    """``cfg`` with the agent default and agent layers of ``prompt_spec``.
    ``reasoning`` False leaves agent.yaml's reasoning out (a run that keeps
    its pinned or resumed effort). ``inherit`` keeps ``cfg``'s own agent
    layers for the keys ``prompt_spec`` does not set (a subagent of an agent).
    Either way the agent layer sits below env, flag and /set in `LAYERS`:
    agent.yaml never beats the operator's env, flag or /set value."""
    default, agent = agent_layers(prompt_spec)
    if not reasoning:
        agent.pop("EFFORT", None)
    if inherit:
        default = {**(getattr(cfg, "layer_agent_default", None) or {}), **default}
        agent = {**(getattr(cfg, "layer_agent", None) or {}), **agent}
    return replace(cfg, layer_agent_default=default, layer_agent=agent)


def for_child(cfg: Any) -> Any:
    """``cfg`` (a parent's) as a subagent's start: the parent's resume layer
    moved into the agent layer, below what the child's agent.yaml sets. The
    parent's env, flag and /set layers stay where they are."""
    resumed = dict(getattr(cfg, "layer_resume", None) or {})
    if not resumed:
        return cfg
    return replace(cfg, layer_resume={}, layer_agent={**(getattr(cfg, "layer_agent", None) or {}), **resumed})


def for_turn(cfg: Any) -> Any:
    """``cfg`` with every `KEYS` setting and the Config fields made from them
    taken from `resolve` over its layers. A `KEYS` value already in
    ``cfg.settings`` is not read. A Config with no layers (one not built by
    `config.from_env`, ``layer_jsrc`` None) has nothing to resolve and is
    returned as it is."""
    if getattr(cfg, "layer_jsrc", None) is None:
        return cfg
    from . import providers, turn_settings
    from .config import _norm_effort, vision_enabled_for_model

    layers = layers_of(cfg)
    values, _sources = resolve(layers)
    settings = {k: v for k, v in (getattr(cfg, "settings", None) or {}).items() if k not in KEYS}
    settings.update(values)
    projected = turn_settings.project(settings)
    changes: dict[str, Any] = {
        row.attr: projected[row.attr] for row in turn_settings.TURN_SETTINGS if row.key in KEYS
    }
    provider = providers.get_provider(getattr(cfg, "provider_id", None))
    changes["reasoning_effort"] = _norm_effort(values.get("EFFORT")) or (
        provider.default_effort(settings) if provider is not None else None)
    return replace(
        cfg,
        **changes,
        settings=settings,
        vision_enabled=vision_enabled_for_model(cfg.model, settings),
    )


def sampling(cfg: Any) -> Sampling:
    """The sampling a model call of ``cfg`` sends: its sampling settings
    resolved over every layer. The one place js assembles sampling."""
    values, _ = resolve(layers_of(cfg))
    return Sampling.from_mapping({field: values.get(field.upper()) for field in SAMPLING_FIELDS})


def layer_sampling(layer: dict | None) -> Sampling:
    """The sampling settings one layer sets, for labelling their source."""
    return Sampling.from_mapping({field: (layer or {}).get(field.upper()) for field in SAMPLING_FIELDS})


def sources_of(cfg: Any) -> dict[str, str]:
    """The layer each `KEYS` value of ``cfg`` comes from."""
    return resolve(layers_of(cfg))[1]


def saved_jsrc_values(cfg: Any) -> dict[str, Any]:
    """What `/save` writes for `KEYS`: the jsrc layer with the `/set` layer
    on top. Server, model, agent, env and flag values are not the jsrc's."""
    values, _ = resolve({"jsrc": _jsrc_layer(cfg),
                         "set": getattr(cfg, "layer_set", None) or {}})
    return values


def saved_layer(cfg: Any) -> dict[str, Any]:
    """What `/save server` writes as ``cfg``'s model defaults: the effective
    value of every `KEYS` setting that comes from the jsrc, the model's own
    defaults or `/set`."""
    values, sources = resolve(layers_of(cfg))
    return {key: value for key, value in values.items() if sources[key] in _MODEL_LAYERS}
