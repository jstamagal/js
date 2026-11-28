"""Config + agent + model -> a settled run: the turn a run is ready to start.

A settled run holds the Config, system prompt, prompt spec and tool registry
one run's turns use, and what the run's session said when it was read. The
steps run in one order: the agent's prompt spec is loaded unexpanded
(`load_spec`), agent.yaml and the named model route the Config (`route`), the
spec is reloaded for the model it settled on (`for_model`), the registry is
narrowed to the spec's tools, the agent and login layers resolve, and the
system prompt is expanded last (`expand`). The run's tools and persona follow
the model it settled on. `settle` composes these steps for -p, the REPL,
--bench and swarm agents; a subagent composes the same steps with its own
model choice, its own layers and the registry its parent hands it.

`resume` and `settle` read the agent files, the session's last stamp and its
recorded system prompt. Neither writes anything: a caller activates the
session, records the prompt it saw and appends the system prompt.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from . import (
    config,
    login_defaults,
    memory as M,
    messages as msgs,
    persona as P,
    providers,
    routing,
    session_store,
    setting_values,
    settings,
)
from .cli_options import current, restore_chat
from .config import Config, InvocationSources, resolve_session_file
from .session_catalog import first_metadata, last_stamp
from .toolkit import policy
from .toolkit.registry import registry_for_roots


class RunNotSettled(Exception):
    """This run cannot be settled. ``cause`` is what failed; ``loading`` is
    True when the agent's files did not load and False when the model did not
    route."""

    def __init__(self, cause: Exception, *, loading: bool) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.loading = loading


@dataclass(frozen=True)
class Resume:
    """What a session's last stamp says for one run."""

    stamp: dict | None = None        # the newest stamp, read before this run records its own
    model: str | None = None         # the model the run names, else the stamp's
    from_stamp: bool = False         # ``model`` is the stamp's
    unreachable: str | None = None   # provider/model of a stamp whose provider has no login
    layer: dict = field(default_factory=dict)  # the resume layer: the stamp's effort


@dataclass(frozen=True)
class SettledRun:
    config: Any
    system: str
    prompt_spec: P.PromptSpec
    registry: Any
    resume: Resume
    # The system prompt the session recorded, read when the run resumes its
    # session; None when it has none yet.
    recorded_system: str | None
    # The effort every turn is told to run with: the one the run names, else
    # the resumed session's.
    effort: str | None

    def turn_kwargs(self, **more: Any) -> dict[str, Any]:
        """The `runtime.run_turn_async` arguments for this run, with ``more`` on top."""
        cfg = self.config
        kwargs: dict[str, Any] = {
            "model_override": cfg.model,
            "provider_id_override": cfg.provider_id,
            "provider_base_url_override": cfg.provider_base_url,
            "provider_api_key_override": cfg.provider_api_key,
            "tool_registry": self.registry,
            "sampling": login_defaults.sampling(cfg),
        }
        if self.effort is not None:
            kwargs["reasoning_effort_override"] = self.effort
        kwargs.update(more)
        return kwargs


def registry_for(cfg: Any) -> Any:
    """Every tool ``cfg``'s agent roots and tool policy offer."""
    return registry_for_roots(getattr(cfg, "prompt_roots", ()) or (), flags=policy.schema_flags(cfg))


def check_effort(raw: str) -> tuple[str | None, str | None]:
    """``raw`` checked as `set EFFORT` checks it: (value, error)."""
    return setting_values.coerce_value(settings.SPEC_BY_KEY["EFFORT"], raw)


def stamped_effort(stamp: dict) -> str | None:
    """The effort word of a stamp, when it is a valid one."""
    raw = stamp.get("reasoning")
    if not isinstance(raw, str) or not raw:
        return None
    value, error = check_effort(raw)
    return value if error is None else None


def unreachable_stamp(stamp: dict, cfg: Any) -> str | None:
    """``provider/model`` of a stamp whose provider is neither the configured
    one nor logged in, else None. Such a session resumes on the configured model."""
    model = stamp.get("model")
    provider = stamp.get("provider")
    if not isinstance(model, str) or not model or not isinstance(provider, str) or not provider:
        return None
    if provider == cfg.provider_id or routing._saved_login(provider) is not None:
        return None
    return f"{provider}/{model}"


def stamp_model(stamp: dict, cfg: Any) -> str | None:
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
    if unreachable_stamp(stamp, cfg) is not None:
        return None
    prefixed = f"{provider}/{model}"
    if (provider and providers.parse_model_prefix(prefixed) == (provider, model)
            and (provider == cfg.provider_id or routing._saved_login(provider) is not None)):
        return prefixed
    return None if model == cfg.model else model


def resume(cfg: Any, model: str | None, reasoning: str | None) -> Resume:
    """What ``cfg``'s session resumes for a run that names ``model`` and
    ``reasoning``: the stamp's model when the run names none, and the stamp's
    effort as the resume layer when it names no effort. The effort is the
    session's own choice, kept even when it equals what the model would pick."""
    stamp = last_stamp(cfg.session_file) if cfg.session_file != Path(os.devnull) else None
    if stamp is None:
        return Resume(model=model)
    unreachable = None
    from_stamp = False
    if model is None:
        unreachable = unreachable_stamp(stamp, cfg)
        model = stamp_model(stamp, cfg)
        from_stamp = model is not None
    effort = stamped_effort(stamp) if reasoning is None else None
    return Resume(stamp=stamp, model=model, from_stamp=from_stamp, unreachable=unreachable,
                  layer={"EFFORT": effort} if effort else {})


def route(cfg: Any, model: str | None) -> Any:
    """``cfg`` routed to ``model``, a model the run names; ``cfg`` when it is None."""
    if model is None:
        return cfg
    return config.apply_route(cfg, routing.resolve_model_route(
        model,
        configured_provider_id=cfg.provider_id,
        configured_base_url=cfg.provider_base_url,
        configured_api_key=cfg.provider_api_key,
        configured_headers=getattr(cfg, "provider_headers", {}),
        env=os.environ,
        explicit_model=True,
        prefix_overrides_provider=not getattr(cfg, "explicit_provider", False),
    ))


def load_spec(cfg: Any) -> P.PromptSpec:
    """``cfg``'s agent prompt spec, unexpanded. Raises `RunNotSettled`."""
    try:
        return P.load_configured_prompt_spec(cfg, expand=False)
    except (FileNotFoundError, ValueError) as exc:
        raise RunNotSettled(exc, loading=True) from exc


def for_model(prompt_spec: P.PromptSpec, cfg: Any) -> P.PromptSpec:
    """``prompt_spec`` reloaded for the model ``cfg`` is routed to, when that
    model matches a different profile. Raises `RunNotSettled`."""
    try:
        return P.for_model(prompt_spec, cfg)
    except (FileNotFoundError, ValueError) as exc:
        raise RunNotSettled(exc, loading=True) from exc


def expand(prompt_spec: P.PromptSpec, cfg: Any) -> P.PromptSpec:
    """``prompt_spec`` with its system prompt's directives expanded for ``cfg``."""
    return P._expand_spec(prompt_spec, cfg)


def exact_surface(prompt_spec: P.PromptSpec, cfg: Any, tools: Any) -> tuple[P.PromptSpec, Any]:
    """``prompt_spec`` with ``tools`` as its chain, and the registry that chain
    selects. Raises `policy.ToolPolicyError` for a malformed list or an entry,
    other than a tag, that matches no tool."""
    entries = policy.parse_entries(list(tools) if isinstance(tools, tuple) else tools, "tools")
    full = registry_for(cfg)
    config = policy.load_tools_config()
    for rule in policy.expand(entries, config, "tools"):
        if not rule.via and rule.entry.noun != "tag" and not any(rule.matches(tool) for tool in full.tools):
            raise policy.ToolPolicyError(msgs.TOOLS_LIST_ENTRY_MATCHES_NOTHING.text(entry=rule.entry.text))
    prompt_spec = replace(prompt_spec, tool_selectors=entries)
    registry = full.select(entries, config=config, warn=False, tool_aliases=prompt_spec.tool_aliases)
    return prompt_spec, registry


def settle(cfg: Any, *, model: str | None = None, reasoning: str | None = None,
           resumed: Resume | None = None, config_pins: bool = False, agent_reasoning: bool = True,
           layered: bool = True, expand_recorded: bool = False, tools: Any = None) -> SettledRun:
    """The settled run of ``cfg``'s agent on the model this run names.

    ``model`` and ``reasoning`` are what the run names (--model, --reasoning);
    ``resumed`` is `resume` of its session, whose model then stands for
    ``model``, and whose recorded system prompt is sent in place of the
    agent's unless the prompt is replaced or the run is a chat. A run without
    ``resumed`` sends the agent's prompt.

    A resumed session comes back on the model of its last stamp: only a
    model the run names replaces it, and agent.yaml's `model:` is for a fresh
    session. ``config_pins`` makes a jsrc `MODEL` keep agent.yaml's out of a
    fresh session, as -m and a JS_* env var always do.
    ``agent_reasoning``: agent.yaml's reasoning is one of the run's layers.
    ``layered`` False leaves the agent and login layers unresolved, for a
    caller that resolves them every turn. ``expand_recorded``: the agent's
    prompt is expanded even when the recorded one is sent.

    Raises `RunNotSettled`."""
    reads_session = resumed is not None
    if resumed is not None:
        model = resumed.model
    else:
        resumed = Resume(model=model)
    prompt_spec = load_spec(cfg)
    try:
        if resumed.stamp is None:
            cfg = P.apply_agent_model(cfg, prompt_spec, model, config_pins=config_pins)
        cfg = route(cfg, model)
    except ValueError as exc:
        raise RunNotSettled(exc, loading=False) from exc
    prompt_spec = for_model(prompt_spec, cfg)
    try:
        if tools is not None:
            prompt_spec, registry = exact_surface(prompt_spec, cfg, tools)
            cfg = replace(cfg, mcp=None)
        else:
            registry = registry_for(cfg).select(prompt_spec.tool_selectors, tool_aliases=prompt_spec.tool_aliases)
    except policy.ToolPolicyError as exc:
        raise RunNotSettled(exc, loading=True) from exc
    effort = reasoning
    if layered:
        cfg = login_defaults.for_turn(login_defaults.with_agent(cfg, prompt_spec, reasoning=agent_reasoning))
        if resumed.layer:
            cfg = login_defaults.for_turn(replace(cfg, layer_resume=resumed.layer))
            if effort is None:
                effort = resumed.layer["EFFORT"]
    options = current()
    recorded = M.load_system_prompt(cfg.session_file) if reads_session else None
    sends_agent_prompt = recorded is None or options.changes_prompt or options.chat
    if sends_agent_prompt or expand_recorded:
        prompt_spec = expand(prompt_spec, cfg)
    system = prompt_spec.system if sends_agent_prompt else recorded
    return SettledRun(config=cfg, system=system, prompt_spec=prompt_spec, registry=registry,
                      resume=resumed, recorded_system=recorded, effort=effort)


def inherit_session_chat(session: str | None) -> bool:
    session = session or os.environ.get("JS_SESSION")
    if not session:
        return False
    try:
        path = resolve_session_file(session_store.project_folder(Path.cwd()), session)
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


def from_env(
    session: str | None = None,
    *,
    save_session: bool = True,
    sources: InvocationSources = InvocationSources(),
    agent_id: str | None = None,
) -> Config:
    if inherit_session_chat(session):
        agent_id = current().agent or (None if agent_id == "none" else agent_id)
    return config.from_env(
        save_session=save_session,
        sources=sources,
        session=session,
        agent_id=agent_id,
    )
