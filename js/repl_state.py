"""The REPL's live state for one session (`cli._repl_state` builds it)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from . import login_defaults, routing, settings as _settings, turn_settings
from .compaction import AutoCompactState
from .config import Config, _norm_effort, vision_enabled_for_model

if TYPE_CHECKING:
    from .cli import ReplAssembly, _SessionTarget
    from .events import EventHooks
    from .keys import Keymap
    from .mcp.host import MCPHost
    from .persona import PromptSpec
    from .subagent_windows import SubagentWindows
    from .toolkit.registry import ToolRegistry


@dataclass(slots=True)
class ReplState:
    """What one REPL session holds between turns and commands."""

    # The conversation and the system prompt it is sent with.
    messages: list[dict] = field(default_factory=list)
    system: str = ""
    # The route the next turn takes. `model_source` names where `model` came
    # from when it is not the settings store's MODEL (the --model flag, the
    # agent's agent.yaml, the /model command), for the effective /set view.
    model: str | None = None
    model_source: str | None = None
    provider_id: str | None = None
    provider_base_url: str | None = None
    provider_api_key: str | None = None
    provider_headers: dict[str, str] = field(default_factory=dict)
    # The live settings store /set, /fset and the jsrc run write.
    settings: dict = field(default_factory=dict)
    # The `/set` and resumed-session layers of the login-default settings.
    live_set: dict = field(default_factory=dict)
    live_resume: dict = field(default_factory=dict)
    # What `/save` compares against: settings, /fset entries, rc lines, model.
    saved: dict | None = None
    prompt_spec: PromptSpec | None = None
    tool_selectors: tuple[str, ...] = ()
    tool_registry: ToolRegistry | None = None
    events: EventHooks | None = None
    aliases: dict[str, str] = field(default_factory=dict)
    # Reminders queued for the next user message.
    pending_notes: list[str] = field(default_factory=list)
    auto_compact: AutoCompactState = field(default_factory=AutoCompactState)
    # The files `/load` and the jsrc run are reading, innermost last.
    load_stack: list = field(default_factory=list)
    # True while the startup run of the jsrc files runs.
    rc_replay: bool = False
    repl_assembly: ReplAssembly | None = None
    mcp_host: MCPHost | None = None
    keymap: Keymap | None = None
    # The subagent windows, while the screen is up.
    windows: SubagentWindows | None = None
    running: bool = True
    # The session `/session` chose to continue in.
    switch_session: _SessionTarget | None = None

    def config_for_turn(self, cfg: Config) -> Config:
        """The Config a turn started now runs with: ``cfg`` on the live route
        and settings, and the login-default settings resolved over every layer
        (login_defaults)."""
        active = self._config_for_route(cfg)
        agent_default, agent = login_defaults.agent_layers(self.prompt_spec)
        return login_defaults.for_turn(replace(
            active,
            **turn_settings.project(self.settings, fallback=active),
            settings=self.settings,
            reasoning_effort=_live_reasoning_effort(self.settings, active.reasoning_effort),
            layer_agent_default=agent_default,
            layer_agent=agent,
            layer_set=dict(self.live_set),
            layer_resume=dict(self.live_resume),
        ))

    def _config_for_route(self, cfg: Config) -> Config:
        """``cfg`` on the live model and provider fields, routed."""
        model = self.model
        if not (isinstance(model, str) and model):
            model = cfg.model
        try:
            route = routing.resolve_model_route(
                model,
                configured_provider_id=self.provider_id,
                configured_base_url=self.provider_base_url,
                configured_api_key=self.provider_api_key,
                configured_headers=self.provider_headers,
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
        route_vision = vision_enabled_for_model(route.model, self.settings)
        if (
            route.model != cfg.model
            or route.provider_id != cfg.provider_id
            or route.base_url != cfg.provider_base_url
            or route.api_key != cfg.provider_api_key
            or route.headers != getattr(cfg, "provider_headers", {})
            or route_vision != cfg.vision_enabled
        ):
            return routing.apply_route(cfg, route, settings=self.settings)
        return cfg


def _live_reasoning_effort(live_settings: dict, default: str | None) -> str | None:
    """The live EFFORT setting, normalized; ``default`` when it is unset or not a string."""
    raw = _settings.get_dotted(live_settings, ("EFFORT",), None)
    if isinstance(raw, str):
        return _norm_effort(raw)
    return default
