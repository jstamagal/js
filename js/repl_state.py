"""The REPL's live state for one session (`repl._repl_state` builds it), the
assembly a session runs with, what carries live settings into the state, and
the `on` events a state emits."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from . import (
    events,
    login_defaults,
    logins,
    messages as msgs,
    providers,
    routing,
    run_assembly,
    runtime,
    settings as _settings,
    turn_settings,
)
from .compaction import AutoCompactState
from .config import Config, _norm_effort, vision_enabled_for_model
from .toolkit import policy as tool_policy

if TYPE_CHECKING:
    import re

    from .keys import Keymap
    from .mcp.host import MCPHost
    from .persona import PromptSpec
    from .repl_commands import Command
    from .session_picker import SessionTarget
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
    events: events.EventHooks | None = None
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
    switch_session: SessionTarget | None = None

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


QUIT_WORDS = ("exit", "quit", ":q")  # quit without the leading '/'


@dataclass(frozen=True)
class ReplAssembly:
    """Commands and input policy supplied to a REPL session."""

    commands: Mapping[str, Command] | None = None
    quit_words: tuple[str, ...] = QUIT_WORDS
    refuse_unknown: bool = True
    guard_turn_state: bool = True
    compact_force_focus: str = "up to here"
    skill_command: str = "/skill"
    skill_pattern: re.Pattern | None = None
    flush_lines: tuple[str, ...] = ("/flush", "/cancel queued")
    interrupt: Callable | None = None
    banner: Callable | None = None
    hint_renderer: Callable[[msgs.Message, ReplState], msgs.Message] | None = None
    input_bindings: Callable | None = None


DEFAULT_REPL_ASSEMBLY = ReplAssembly()


def repl_assembly(state: ReplState | None) -> ReplAssembly:
    if state is None or state.repl_assembly is None:
        return DEFAULT_REPL_ASSEMBLY
    return state.repl_assembly


def repl_hint(message: msgs.Message, state: ReplState) -> msgs.Message:
    render = repl_assembly(state).hint_renderer
    return message if render is None else render(message, state)


# Orders the async REPL's live-state writes against turn starts. A command holds
# it for its whole run on its executor thread; a turn takes its cfg snapshot under
# it, also off the loop thread (cli._live_cfg_snapshot), so a command that waits on
# the loop (a modal picker) never deadlocks it and a slow command never stalls input.
LIVE_STATE_LOCK = threading.Lock()


def queue_note(state: ReplState, note: str) -> None:
    state.pending_notes.append(note)


def disabled_skills(state: ReplState) -> tuple[str, ...]:
    """The SKILLS_DISABLED names in the live settings."""
    return turn_settings.project(state.settings)["skills_disabled"]


def model_from_live_settings(live_settings: dict) -> str | None:
    raw = _settings.get_dotted(live_settings, ("MODEL",), None)
    return raw if isinstance(raw, str) and raw else None


def sync_model_from_live_settings(state: ReplState) -> None:
    model = model_from_live_settings(state.settings)
    if model is not None:
        state.model = model
        # The store now drives the model, so it is no longer a flag/command override.
        state.model_source = None


def provider_from_live_settings(live_settings: dict) -> tuple[str | None, str | None, str | None]:
    def _value(path: tuple[str, str]) -> str | None:
        raw = _settings.get_dotted(live_settings, path, None)
        return raw if isinstance(raw, str) and raw else None

    return (
        _value(("PROVIDER",)),
        _value(("BASE_URL",)),
        _value(("API_KEY",)),
    )


def sync_provider_from_live_settings(state: ReplState, changed_keys: list[str]) -> None:
    provider_id, base_url, api_key = provider_from_live_settings(state.settings)
    changed = set(changed_keys)
    if "PROVIDER" in changed:
        if provider_id is None:
            state.provider_id = None
            state.provider_base_url = None
            state.provider_api_key = None
            state.provider_headers = {}
        else:
            set_provider_state(state, provider_id)
    if "BASE_URL" in changed:
        state.provider_base_url = base_url
    if "API_KEY" in changed:
        state.provider_api_key = api_key


def sync_tool_registry_from_live_settings(cfg: Config, state: ReplState) -> None:
    """Rebuild the live registry. The agent load already printed the
    unmatched-entry notices; a tools.yaml that no longer resolves keeps the
    current registry and prints one line."""
    selectors = state.tool_selectors
    try:
        state.tool_registry = run_assembly.registry_for(state.config_for_turn(cfg)).select(selectors, warn=False)
    except tool_policy.ToolPolicyError as exc:
        msgs.say(msgs.TOOL_SURFACE_KEPT, error=exc)


def apply_saved_login_to_state(state: ReplState, provider_name: str) -> bool:
    provider_id = providers.normalize_provider_id(provider_name) or provider_name
    login = logins.load_logins().get(provider_id)
    if not login:
        return False
    state.provider_id = login.provider_id
    state.provider_headers = dict(login.provider_headers)
    state.provider_base_url = login.provider_base_url
    state.provider_api_key = login.provider_api_key
    return True


def set_provider_state(state: ReplState, provider_id: str) -> None:
    canonical_id = providers.normalize_provider_id(provider_id) or provider_id
    if apply_saved_login_to_state(state, canonical_id):
        return
    provider_def = providers.provider_for_login(canonical_id)
    state.provider_id = canonical_id
    state.provider_headers = {}
    state.provider_base_url = providers.provider_base_url(provider_def, None, os.environ)
    state.provider_api_key = providers.provider_api_key(provider_def, None, os.environ)


def emit_repl_event(
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


def emit_session_event(state: ReplState, telemetry: runtime.Telemetry, cfg: Config, event: str) -> None:
    """session_start or session_end for the REPL session ``state`` runs."""
    emit_repl_event(
        state, telemetry, event,
        session=str(cfg.session_file),
        agent=cfg.agent_id,
        model=state.model,
        cwd=str(runtime.T.STOCK_CONTEXT.cwd),
        messages=len(state.messages),
    )
