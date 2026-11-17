"""The REPL's live state for one session (`cli._repl_state` builds it)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .compaction import AutoCompactState

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
