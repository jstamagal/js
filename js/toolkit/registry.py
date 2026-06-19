"""Tool registry assembly and per-agent selection."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from collections.abc import Iterable, Sequence
from pathlib import Path

from .core import Tool
from . import artifact, fs, meta, process_net, wiki


@dataclass(frozen=True)
class ToolRegistry:
    tools: tuple[Tool, ...]
    aliases: dict[str, str]

    def resolve(self, name: str) -> Tool | None:
        trimmed = str(name).strip()
        if trimmed in self.by_name:
            return self.by_name[trimmed]
        canonical = self.aliases.get(trimmed.lower(), trimmed)
        return self.by_name.get(canonical)

    @property
    def by_name(self) -> dict[str, Tool]:
        return {tool.name: tool for tool in self.tools}

    def openai_specs(self) -> list[dict]:
        return [tool.openai_spec() for tool in self.tools]

    def names(self) -> str:
        return "/".join(tool.name for tool in self.tools)

    def select(self, selectors: Iterable[str] | None) -> ToolRegistry:
        wanted = _selected_names(self, selectors or ())
        selected = tuple(tool for tool in self.tools if tool.name in wanted)
        return _registry_from_tools(selected)

    def aliased(self, profile: dict[str, str] | None) -> ToolRegistry:
        """Return a registry that also resolves model-facing aliases back to
        canonical handlers.

        ``profile`` maps canonical tool name -> model-facing alias (e.g.
        ``{"read": "Read"}``). Only entries whose canonical name is a real
        tool are honoured. The original tool set is untouched; only the alias
        resolution table grows, so ``resolve("Read")`` dispatches to ``read``.
        Empty/falsey ``profile`` returns ``self`` unchanged.
        """
        if not profile:
            return self
        merged = dict(self.aliases)
        names = self.by_name
        for canonical, alias in profile.items():
            key = str(alias).strip().lower()
            if canonical in names and key:
                merged[key] = canonical
        return ToolRegistry(tools=self.tools, aliases=merged)


def _registry_from_tools(tools: tuple[Tool, ...]) -> ToolRegistry:
    aliases: dict[str, str] = {}
    for tool in tools:
        aliases[tool.name.lower()] = tool.name
        for alias in tool.aliases:
            aliases[alias.lower()] = tool.name
    return ToolRegistry(tools=tools, aliases=aliases)


def _selected_names(registry: ToolRegistry, selectors: Iterable[str]) -> set[str]:
    selected: set[str] = set()
    full_aliases = registry.aliases
    full_names = registry.by_name
    for raw in selectors:
        selector = str(raw).strip()
        if not selector:
            continue
        folded = selector.lower()
        if folded == "*":
            selected.update(full_names)
            continue
        if "*" in folded:
            for public_name, canonical in full_aliases.items():
                if fnmatchcase(public_name, folded):
                    selected.add(canonical)
            continue
        canonical = full_aliases.get(folded)
        if canonical is not None:
            selected.add(canonical)
    return selected


def _default_prompts_root() -> Path:
    return Path(__file__).resolve().parents[2] / "prompts"


def _agent_tools(prompts_root: Path | Sequence[Path], reserved: set[str]) -> tuple[Tool, ...]:
    """Build direct agent tools from roots ordered least- to most-specific.

    ``from_env`` passes repo ``prompts/``, then platform config ``agents/``,
    then project ``.js/agents/``. Later roots shadow earlier roots for the same
    agent id, matching prompt loading for main agents and subagents.
    """
    roots = tuple(prompts_root) if isinstance(prompts_root, (list, tuple)) else (prompts_root,)
    by_id: dict[str, Path] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for agent_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            agent_id = agent_dir.name
            if agent_id in reserved or not any(agent_dir.glob("*.md")):
                continue
            # Later roots are more specific and shadow earlier prompt dirs.
            by_id[agent_id] = agent_dir
    return tuple(meta.named_agent_tool(agent_id) for agent_id in sorted(by_id))


def build_default_registry(prompts_root: Path | Sequence[Path] | None = None, flags: tuple[str, ...] = ("model_override",)) -> ToolRegistry:
    base_tools = fs.tools() + process_net.tools() + meta.tools(flags) + wiki.tools() + artifact.tools()
    reserved = {tool.name for tool in base_tools}
    all_tools = base_tools + _agent_tools(prompts_root or _default_prompts_root(), reserved)
    return _registry_from_tools(all_tools)


def select(selectors: Iterable[str] | None, prompts_root: Path | Sequence[Path] | None = None) -> ToolRegistry:
    return build_default_registry(prompts_root=prompts_root).select(selectors)
