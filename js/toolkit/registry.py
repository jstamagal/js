"""Tool registry assembly and per-agent selection."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from collections.abc import Iterable, Sequence
from pathlib import Path
import sys

from .core import CatalogEntry, Tool
from .descriptions import render_tool_name_sections
from . import artifact, browser, discovery, fs, meta, process_net, search, terminal, wiki


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
        # Resolve each description's co-present-tool-name blocks against the tools
        # actually on this surface. This is the one model-facing chokepoint every
        # path funnels through (select()'d surfaces and the raw full registry that
        # commit/wiki hand straight to the model), so it is leak-proof: a
        # {{#unless fs_search}} block never reaches the model unresolved.
        present = set(self.by_name)
        specs = []
        for tool in self.tools:
            spec = tool.openai_spec()
            spec["function"]["description"] = render_tool_name_sections(
                tool.description, present, tool=tool.name
            )
            specs.append(spec)
        return specs

    def names(self) -> str:
        return "/".join(tool.name for tool in self.tools)

    def select(self, selectors: Iterable[str] | None, agent_id: str | None = None) -> ToolRegistry:
        wanted = _selected_names(self, selectors or (), agent_id)
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
            existing = merged.get(key)
            if canonical in names and key and existing in (None, canonical):
                merged[key] = canonical
        return ToolRegistry(tools=self.tools, aliases=merged)

    def lazy_surface(self, cwd: Path, mcp_host: object | None = None) -> TurnToolSurface:
        """Create fresh lazy state for one model turn without changing selection."""
        return TurnToolSurface(self, cwd, mcp_host=mcp_host)


_LAZY_SUITES = {
    **{tool.name: "browser" for tool in browser.tools()},
    **{tool.name: "terminal" for tool in terminal.tools()},
    **{tool.name: "wiki" for tool in wiki.tools()},
    **{tool.name: "artifact" for tool in artifact.tools()},
}


class TurnToolSurface:
    """A selected registry split into deterministic eager and loaded subsets."""

    def __init__(self, allowed: ToolRegistry, cwd: Path, mcp_host: object | None = None) -> None:
        self.allowed = allowed
        self.mcp_host = mcp_host
        self.aliases = {
            alias: canonical
            for alias, canonical in allowed.aliases.items()
            if alias != discovery.DISCOVERY_TOOL_NAME
        }
        self._skills = (
            {skill.id: skill for skill in discovery.discover_skills(cwd)}
            if allowed.resolve("skill") is not None
            else {}
        )
        self._lazy: dict[str, Tool] = {}
        self._sources: dict[str, str] = {}
        for tool in allowed.tools:
            source = _LAZY_SUITES.get(tool.name)
            if source is None and getattr(tool.handler, "_js_agent_id", None) is not None:
                source = "agent"
            if source is not None:
                self._lazy[f"native:{tool.name}"] = tool
                self._sources[tool.name] = source
        # Core meta tools stay eager: "skill" is the dispatch tool itself;
        # only the skill catalog and specialist suites load lazily.
        self._eager = tuple(
            tool for tool in allowed.tools if tool.name not in self._sources
        )
        self._loaded: set[str] = set()
        self._mcp_loaded: set[str] = set()
        self._discovery = discovery.discovery_tool(self)

    @property
    def tools(self) -> tuple[Tool, ...]:
        eager_names = {tool.name for tool in self._eager}
        loaded = tuple(
            tool for tool in self.allowed.tools
            if tool.name in self._loaded and tool.name not in eager_names
        )
        mcp_tools = self.mcp_host.tools(self._mcp_loaded) if self.mcp_host is not None else ()
        include_discovery = bool(self._lazy or self._skills or self.mcp_host is not None)
        return self._eager + loaded + tuple(mcp_tools) + ((self._discovery,) if include_discovery else ())

    def dispatch_registry(self) -> ToolRegistry:
        """Freeze the tools available for one model response's dispatch batch."""
        return ToolRegistry(tools=self.tools, aliases=self.aliases)

    @property
    def by_name(self) -> dict[str, Tool]:
        return {tool.name: tool for tool in self.tools}

    def resolve(self, name: str) -> Tool | None:
        trimmed = str(name).strip()
        if trimmed in self.by_name:
            return self.by_name[trimmed]
        canonical = self.aliases.get(trimmed.lower(), trimmed)
        return self.by_name.get(canonical)

    def openai_specs(self) -> list[dict]:
        return _registry_from_tools(self.tools).openai_specs()

    def names(self) -> str:
        return "/".join(tool.name for tool in self.tools)

    def catalog(self) -> tuple[CatalogEntry, ...]:
        native = (
            CatalogEntry(
                item_id,
                tool.name,
                tool.description.split("\n", 1)[0][:240],
                "native",
                self._sources[tool.name],
            )
            for item_id, tool in self._lazy.items()
        )
        skills = (
            CatalogEntry(skill.id, skill.name, skill.description, "skill", skill.source)
            for skill in self._skills.values()
        )
        mcp = self.mcp_host.initial_catalog() if self.mcp_host is not None else ()
        return tuple(sorted((*native, *skills, *mcp), key=lambda item: item.id))

    async def discover_async(self, *, query: str = "", kind: str = "", source: str = "", load: str = "") -> str:
        folded_source = str(source).strip().casefold()
        mcp_source = (
            self.mcp_host is not None
            and bool(folded_source)
            and self.mcp_host.is_server_source(source)
        )
        if self.mcp_host is not None and not load and (
            str(kind).strip().lower() == "mcp"
            or folded_source == "mcp"
            or mcp_source
            or "mcp" in str(query).casefold().split()
        ):
            entries = await self.mcp_host.discover(
                query=query,
                source="" if folded_source == "mcp" else source,
            )
            return discovery.compact_result({"results": [
                {"id": item.id, "kind": item.kind, "name": item.name,
                 "description": item.description, "source": item.source,
                 "loaded": item.name in self._mcp_loaded}
                for item in entries
            ]})
        return self.discover(query=query, kind=kind, source=source, load=load)

    def discover(self, *, query: str = "", kind: str = "", source: str = "", load: str = "") -> str:
        load_id = str(load).strip()
        if load_id:
            return self._load(load_id)
        folded_kind = str(kind).strip().lower()
        folded_source = str(source).strip().lower()
        if folded_kind and folded_kind not in {"native", "skill", "mcp"}:
            return "ERROR: kind must be native, skill, or mcp"
        terms = str(query).casefold().split()
        matches = []
        for item in self.catalog():
            haystack = f"{item.id} {item.name} {item.description} {item.source}".casefold()
            if folded_kind and item.kind != folded_kind:
                continue
            if folded_source and item.source.casefold() != folded_source:
                continue
            if terms and not all(term in haystack for term in terms):
                continue
            matches.append({
                "id": item.id,
                "kind": item.kind,
                "name": item.name,
                "description": item.description,
                "source": item.source,
                "loaded": item.name in self._loaded,
            })
        return discovery.compact_result({"results": matches})

    def _load(self, item_id: str) -> str:
        if self.mcp_host is not None:
            loaded = self.mcp_host.load(item_id)
            if loaded is not None:
                self._mcp_loaded.update(loaded)
                return discovery.compact_result({"loaded": loaded, "id": item_id})
        tool = self._lazy.get(item_id)
        if tool is not None:
            self._loaded.add(tool.name)
            return discovery.compact_result({"loaded": [tool.name], "id": item_id})
        skill = self._skills.get(item_id)
        if skill is None:
            return f"ERROR: no allowed catalog entry with id {item_id!r}"
        activated: list[str] = []
        denied: list[str] = []
        seen: set[str] = set()
        for requested in skill.tools:
            tool = self.allowed.resolve(requested)
            if tool is None:
                # Policy denies activation of this tool, not the skill's
                # instructions; the model still gets the text plus the list
                # of requirements that were withheld.
                if requested not in denied:
                    denied.append(requested)
            elif tool.name not in seen:
                activated.append(tool.name)
                seen.add(tool.name)
        instructions = skill.load_instructions()
        if not instructions:
            return f"ERROR: skill {skill.name!r} instructions could not be read"
        self._loaded.update(activated)
        result: dict[str, object] = {"id": item_id, "instructions": instructions, "loaded": activated}
        if denied:
            result["denied_tools"] = denied
        return discovery.compact_result(result)


def _registry_from_tools(tools: tuple[Tool, ...]) -> ToolRegistry:
    aliases: dict[str, str] = {}
    for tool in tools:
        aliases[tool.name.lower()] = tool.name
        for alias in tool.aliases:
            aliases[alias.lower()] = tool.name
    return ToolRegistry(tools=tools, aliases=aliases)


def _selected_names(registry: ToolRegistry, selectors: Iterable[str], agent_id: str | None = None) -> set[str]:
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
        if any(ch in folded for ch in "*?["):
            for public_name, canonical in full_aliases.items():
                if fnmatchcase(public_name, folded):
                    selected.add(canonical)
            continue
        canonical = full_aliases.get(folded)
        if canonical is not None:
            selected.add(canonical)
        else:
            # An exact (non-glob) selector that matches nothing is almost always
            # a typo or a removed tool name — a silent drop shrinks the agent's
            # surface with no signal until a mid-run dispatch error. Warn at load;
            # glob misses stay silent (leniency is correct for patterns).
            where = f" for agent {agent_id!r}" if agent_id else ""
            print(f"js: tool selector {selector!r}{where} matched no tool; ignoring", file=sys.stderr)
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
    base_tools = (
        fs.tools()
        + process_net.tools()
        + search.tools()
        + terminal.tools()
        + browser.tools()
        + meta.tools(flags)
        + wiki.tools()
        + artifact.tools()
    )
    reserved = {tool.name for tool in base_tools}
    reserved.add(discovery.DISCOVERY_TOOL_NAME)
    all_tools = base_tools + _agent_tools(prompts_root or _default_prompts_root(), reserved)
    return _registry_from_tools(all_tools)


def select(selectors: Iterable[str] | None, prompts_root: Path | Sequence[Path] | None = None) -> ToolRegistry:
    return build_default_registry(prompts_root=prompts_root).select(selectors)
