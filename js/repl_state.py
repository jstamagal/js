"""The REPL's live state for one session (`cli._repl_state` builds it)."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .compaction import AutoCompactState

if TYPE_CHECKING:
    from .cli import ReplAssembly, _SessionTarget
    from .events import EventHooks
    from .keys import Keymap
    from .mcp.host import MCPHost
    from .persona import PromptSpec
    from .subagent_windows import SubagentWindows
    from .toolkit.registry import ToolRegistry


@dataclass
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

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def __setitem__(self, key: str, value: Any) -> None:
        setattr(self, key, value)

    def __contains__(self, key: str) -> bool:
        return key in _FIELDS

    def keys(self) -> tuple[str, ...]:
        return _FIELDS

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key)

    def setdefault(self, key: str, default: Any) -> Any:
        if getattr(self, key) is None:
            setattr(self, key, default)
        return getattr(self, key)

    def pop(self, key: str, default: Any = None) -> Any:
        value = getattr(self, key)
        setattr(self, key, _DEFAULTS[key]())
        return value


_FIELDS = tuple(f.name for f in dataclasses.fields(ReplState))
_DEFAULTS = {
    f.name: (f.default_factory if f.default is dataclasses.MISSING else (lambda value=f.default: value))
    for f in dataclasses.fields(ReplState)
}
