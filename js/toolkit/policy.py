"""Tool policy: the operator's `noun:modifier` chains and `tools.yaml`.

Every chain entry is `noun:modifier`. The noun is a tool name or a glob over
tool names; the modifier is `eager` (in the boot surface), `lazy` (loadable
through tool_discovery) or `ban` (absent for this agent). `tag:NAME` expands
the tag's own entries in place.

Resolution walks the expanded chain twice. First pass: the first entry that
names the tool (its exact name, or an intrinsic tag it carries) decides.
Second pass, only when nothing named it: the first glob that matches decides.
So `"*:ban"` denies by default wherever it sits and a name allows past it,
while two entries of the same kind resolve by position: in
`[tavily_search:ban, tag:read_only]` the ban fires, in the reverse order the
tag claims tavily_search first. A tool no entry matches is not on the
agent's surface.

`~/.config/js/tools.yaml` (platform config dir) holds the shared parts:

    tags:                 # named entry lists, usable as tag:NAME
      code_editor: ["*:ban", read:eager]
    ban:                  # argument patterns refused before dispatch
      shell: ["rm -rf"]
    skills: [...]         # parsed and validated

Intrinsic tags are computed from tool properties: `tag:read_only` matches
every tool whose `read_only` is true. An intrinsic tag takes an optional
modifier (`tag:read_only:lazy`); without one it means eager.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yaml

from .. import paths
from .core import Tool

MODIFIERS = ("eager", "lazy", "ban")
_GLOB_CHARS = "*?["

INTRINSIC_TAGS: dict[str, Callable[[Tool], bool]] = {
    "read_only": lambda tool: tool.read_only,
}


class ToolPolicyError(ValueError):
    """A chain entry, tag or tools.yaml that cannot be resolved."""


def tools_config_path() -> Path:
    return paths.config_dir() / "tools.yaml"


@dataclass(frozen=True)
class ToolsConfig:
    tags: Mapping[str, tuple[Any, ...]] = field(default_factory=dict)
    bans: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    skills: tuple[str, ...] = ()
    path: Path | None = None

    @property
    def where(self) -> str:
        return str(self.path) if self.path is not None else "tools.yaml"


def load_tools_config(path: Path | None = None) -> ToolsConfig:
    """Read tools.yaml; a missing file is an empty config."""
    path = path or tools_config_path()
    if not path.is_file():
        return ToolsConfig(path=path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ToolPolicyError(f"{path}: cannot read tools.yaml: {' '.join(str(exc).split())}") from exc
    if data is None:
        return ToolsConfig(path=path)
    if not isinstance(data, dict):
        raise ToolPolicyError(f"{path}: tools.yaml must be a mapping of tags, ban and skills")
    unknown = sorted(str(key) for key in data if key not in ("tags", "ban", "skills"))
    if unknown:
        raise ToolPolicyError(f"{path}: unknown key(s) {', '.join(unknown)}; allowed: tags, ban, skills")

    tags: dict[str, tuple[Any, ...]] = {}
    raw_tags = data.get("tags") or {}
    if not isinstance(raw_tags, dict):
        raise ToolPolicyError(f"{path}: tags must map a tag name to a list of entries")
    for name, entries in raw_tags.items():
        if not isinstance(name, str) or not name.strip():
            raise ToolPolicyError(f"{path}: tag name {name!r} must be a non-empty string")
        if name in INTRINSIC_TAGS:
            raise ToolPolicyError(f"{path}: tag {name!r} is intrinsic and cannot be redefined")
        if not isinstance(entries, list):
            raise ToolPolicyError(f"{path}: tags.{name} must be a list of noun:modifier entries")
        tags[name] = tuple(entries)

    bans: dict[str, tuple[str, ...]] = {}
    raw_bans = data.get("ban") or {}
    if not isinstance(raw_bans, dict):
        raise ToolPolicyError(f"{path}: ban must map a tool name to a list of argument patterns")
    for tool_name, patterns in raw_bans.items():
        if not isinstance(patterns, list) or not all(isinstance(p, str) and p for p in patterns):
            raise ToolPolicyError(f"{path}: ban.{tool_name} must be a list of non-empty strings")
        bans[str(tool_name).strip().lower()] = tuple(patterns)

    raw_skills = data.get("skills") or []
    if not isinstance(raw_skills, list) or not all(isinstance(s, str) and s.strip() for s in raw_skills):
        raise ToolPolicyError(f"{path}: skills must be a list of family:name strings")
    return ToolsConfig(tags=tags, bans=bans, skills=tuple(s.strip() for s in raw_skills), path=path)


@dataclass(frozen=True)
class Entry:
    """One parsed `noun:modifier` or `tag:NAME[:modifier]` entry."""

    text: str
    noun: str
    modifier: str
    tag: str = ""

    @property
    def is_glob(self) -> bool:
        return any(ch in self.noun for ch in _GLOB_CHARS)


def parse_entry(raw: Any, where: str) -> Entry:
    if not isinstance(raw, str):
        raise ToolPolicyError(f"{where}: tool entry {raw!r} must be a string like read:eager")
    text = raw.strip()
    noun, sep, rest = text.partition(":")
    noun, rest = noun.strip().lower(), rest.strip()
    if not sep or not noun or not rest:
        raise ToolPolicyError(
            f"{where}: tool entry {text!r} is not noun:modifier (read:eager, shell:ban, tag:NAME)"
        )
    if noun == "tag":
        name, _, modifier = rest.partition(":")
        name, modifier = name.strip(), modifier.strip()
        if modifier and name not in INTRINSIC_TAGS:
            raise ToolPolicyError(f"{where}: tool entry {text!r}: only an intrinsic tag takes a modifier")
        if modifier and modifier not in MODIFIERS:
            raise ToolPolicyError(f"{where}: tool entry {text!r}: modifier must be eager, lazy or ban")
        return Entry(text, "tag", modifier or "eager", tag=name)
    if rest not in MODIFIERS:
        raise ToolPolicyError(f"{where}: tool entry {text!r}: modifier must be eager, lazy or ban")
    return Entry(text, noun, rest)


def parse_entries(raw: Any, where: str) -> tuple[str, ...]:
    """Validate an entry list's grammar; return the entries as written."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ToolPolicyError(f"{where}: tools must be a list of noun:modifier entries")
    return tuple(parse_entry(item, where).text for item in raw)


@dataclass(frozen=True)
class Rule:
    """A leaf entry of the expanded chain and the tag entries that led to it."""

    entry: Entry
    via: tuple[str, ...] = ()
    where: str = ""

    @property
    def label(self) -> str:
        return " > ".join((*self.via, self.entry.text))

    def matches(self, tool: Tool) -> bool:
        if self.entry.noun == "tag":
            return INTRINSIC_TAGS[self.entry.tag](tool)
        names = (tool.name.lower(), *(alias.lower() for alias in tool.aliases))
        if self.entry.is_glob:
            return any(fnmatchcase(name, self.entry.noun) for name in names)
        return self.entry.noun in names


def expand(entries: Iterable[Any], config: ToolsConfig, where: str) -> tuple[Rule, ...]:
    """Expand tags in place, keeping each leaf's position in the chain."""
    rules: list[Rule] = []

    def walk(items: Iterable[Any], where: str, via: tuple[str, ...], stack: tuple[str, ...]) -> None:
        for raw in items:
            entry = parse_entry(raw, where)
            if entry.noun != "tag":
                rules.append(Rule(entry, via, where))
                continue
            if entry.tag in INTRINSIC_TAGS:
                rules.append(Rule(entry, via, where))
                continue
            if entry.tag not in config.tags:
                known = ", ".join(sorted((*config.tags, *INTRINSIC_TAGS)))
                raise ToolPolicyError(
                    f"{where}: {entry.text!r} names no tag in {config.where} (known: {known})"
                )
            if entry.tag in stack:
                cycle = " > ".join(f"tag:{name}" for name in (*stack, entry.tag))
                raise ToolPolicyError(f"{config.where}: tag cycle {cycle}")
            walk(config.tags[entry.tag], f"{config.where} tags.{entry.tag}",
                 (*via, entry.text), (*stack, entry.tag))

    walk(entries, where, (), ())
    return tuple(rules)


@dataclass(frozen=True)
class Decision:
    tool: Tool
    modifier: str | None
    rule: Rule | None


def resolve(tools: Sequence[Tool], rules: Sequence[Rule]) -> tuple[Decision, ...]:
    """Names and tags first, then globs, first match within each pass."""
    named = [rule for rule in rules if not rule.entry.is_glob]
    globs = [rule for rule in rules if rule.entry.is_glob]
    decisions = []
    for tool in tools:
        rule = next((rule for rule in (*named, *globs) if rule.matches(tool)), None)
        decisions.append(Decision(tool, rule.entry.modifier if rule else None, rule))
    return tuple(decisions)


def warn_unmatched(tools: Sequence[Tool], rules: Sequence[Rule], agent_id: str | None = None) -> None:
    """One stderr line per exact noun that names no tool at all."""
    for rule in rules:
        if rule.entry.noun == "tag" or rule.entry.is_glob:
            continue
        if not any(rule.matches(tool) for tool in tools):
            owner = f" for agent {agent_id!r}" if agent_id else ""
            print(f"js: tool entry {rule.label!r}{owner} matched no tool; ignoring", file=sys.stderr)


@dataclass(frozen=True)
class Selection:
    eager: frozenset[str]
    lazy: frozenset[str]
    decisions: tuple[Decision, ...]


def select(
    tools: Sequence[Tool],
    entries: Iterable[Any],
    config: ToolsConfig,
    *,
    agent_id: str | None = None,
    where: str = "",
) -> Selection:
    rules = expand(entries, config, where or (f"agent {agent_id!r}" if agent_id else "tools"))
    warn_unmatched(tools, rules, agent_id)
    decisions = resolve(tools, rules)
    return Selection(
        eager=frozenset(d.tool.name for d in decisions if d.modifier == "eager"),
        lazy=frozenset(d.tool.name for d in decisions if d.modifier == "lazy"),
        decisions=decisions,
    )


def _string_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _string_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _string_values(item)


def argument_refusal(tool_name: str, args: Any, bans: Mapping[str, Sequence[str]]) -> str | None:
    """The one-line ERROR for a call whose arguments hit a ban pattern.

    A pattern with glob characters must match a whole string argument; any
    other pattern matches as a substring. Matching ignores case.
    """
    patterns = bans.get(tool_name.lower())
    if not patterns:
        return None
    values = [value.casefold() for value in _string_values(args)]
    for pattern in patterns:
        folded = pattern.casefold()
        glob = any(ch in folded for ch in _GLOB_CHARS)
        for value in values:
            if fnmatchcase(value, folded) if glob else folded in value:
                return (
                    f"ERROR: {tool_name} call refused before running: an argument matches "
                    f"ban pattern {pattern!r} in tools.yaml."
                )
    return None


def render_table(decisions: Sequence[Decision], bans: Mapping[str, Sequence[str]] | None = None) -> list[str]:
    """The resolved chain as rows: tool, modifier, deciding entry."""
    bans = bans or {}
    order = {"eager": 0, "lazy": 1, "ban": 2, None: 3}
    rows = sorted(decisions, key=lambda d: (order[d.modifier], d.tool.name))
    width = max((len(d.tool.name) for d in rows), default=4)
    lines = [f"{'tool':<{width}}  {'state':<8}decided by"]
    for d in rows:
        state = d.modifier or "-"
        decided = d.rule.label if d.rule is not None else "(no entry)"
        lines.append(f"{d.tool.name:<{width}}  {state:<8}{decided}")
    shown = {d.tool.name.lower() for d in rows if d.modifier in ("eager", "lazy")}
    for name, patterns in sorted(bans.items()):
        if name in shown:
            lines.append(f"ban {name}: " + ", ".join(repr(p) for p in patterns))
    return lines
