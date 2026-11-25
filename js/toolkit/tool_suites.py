"""Registration and selection metadata for tool compatibility suites."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .core import Tool


@dataclass(frozen=True)
class SuiteTool:
    name: str
    public_name: str
    replaces: str


@dataclass(frozen=True)
class ToolSuite:
    name: str
    members: tuple[SuiteTool, ...]
    build: Callable[[], tuple[Tool, ...]]

    def tools(self) -> tuple[Tool, ...]:
        by_name = {tool.name: tool for tool in self.build()}
        return tuple(by_name[member.name] for member in self.members)

    @property
    def aliases(self) -> dict[str, str]:
        return {member.name: member.public_name for member in self.members}

    @property
    def selectors(self) -> tuple[str, ...]:
        return tuple(f"{member.name}:eager" for member in self.members)

    @property
    def exclusions(self) -> tuple[str, ...]:
        return tuple(f"{member.replaces}:ban" for member in self.members)


def _pi_tools() -> tuple[Tool, ...]:
    from . import pi_bash, pi_files

    return (*pi_files.tools(), *pi_bash.tools())


PI = ToolSuite(
    "pi",
    (
        SuiteTool("pi_read", "read", "read"),
        SuiteTool("pi_bash", "bash", "shell"),
        SuiteTool("pi_edit", "edit", "patch"),
        SuiteTool("pi_write", "write", "write"),
    ),
    _pi_tools,
)
COMPATIBILITY_SUITES = (PI,)
ARGUMENT_BAN_FALLBACKS = {
    member.name: member.replaces for suite in COMPATIBILITY_SUITES for member in suite.members
}
COMPATIBILITY_TOOL_NAMES = frozenset(ARGUMENT_BAN_FALLBACKS)

_DISPLAY_NAMES = {
    member.name: f"{suite.name}:{member.public_name}"
    for suite in COMPATIBILITY_SUITES for member in suite.members
}


def display_name(name: str) -> str:
    """The tool's label on the operator's screen."""
    return _DISPLAY_NAMES.get(name, name)
