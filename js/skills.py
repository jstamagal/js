"""Discover compact skill metadata and load instructions on demand."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

import yaml

from . import paths
from . import messages as msgs

_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,78}[A-Za-z0-9])?$")
_TOOL_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]*$")
_MAX_DESCRIPTION = 500
# Discovery runs on every turn; each distinct warning prints once per process.
_WARNED: set[str] = set()
# The skills js ships, vendored as js/skills/<name>/SKILL.md.
BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent / "skills"


@dataclass(frozen=True)
class SkillMetadata:
    """The bounded, instruction-free portion of a skill."""

    name: str
    description: str
    tools: tuple[str, ...]
    source: str
    path: Path
    # False when the frontmatter sets ``disable-model-invocation: true``: the
    # skill is absent from the model's catalog and only the user loads it.
    model_invocable: bool = True


@dataclass(frozen=True)
class ToolActivationResult:
    """Outcome returned by a registry that can activate lazy tools."""

    activated: tuple[str, ...] = ()
    denied: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class LoadedSkill:
    """A skill's on-demand instructions and declared-tool activation outcome."""

    metadata: SkillMetadata
    instructions: str
    activation: ToolActivationResult = ToolActivationResult()

    def render(self) -> str:
        problems = []
        if self.activation.denied:
            problems.append("policy-denied: " + ", ".join(self.activation.denied))
        if self.activation.missing:
            problems.append("unknown: " + ", ".join(self.activation.missing))
        if not problems:
            return self.instructions
        report = "Skill tool requirements unavailable: " + "; ".join(problems)
        separator = "" if self.instructions.endswith("\n") else "\n"
        return f"{self.instructions}{separator}\n{report}"


@dataclass(frozen=True)
class _SkillRecord:
    metadata: SkillMetadata


class SkillCatalog:
    """A deterministic metadata index whose instruction bodies stay on disk."""

    def __init__(self, records: Iterable[_SkillRecord] = ()) -> None:
        ordered = sorted(records, key=lambda record: record.metadata.name.casefold())
        self._records = {record.metadata.name.casefold(): record for record in ordered}

    @classmethod
    def discover(
        cls,
        project_dir: Path,
        *,
        builtin_dir: Path | None = None,
        global_dir: Path | None = None,
        user_dir: Path | None = None,
    ) -> SkillCatalog:
        return discover_skills(
            project_dir,
            builtin_dir=builtin_dir,
            global_dir=global_dir,
            user_dir=user_dir,
        )

    @property
    def skills(self) -> tuple[SkillMetadata, ...]:
        return tuple(record.metadata for record in self._records.values())

    @property
    def model_skills(self) -> tuple[SkillMetadata, ...]:
        """The skills the model may see and load."""
        return tuple(skill for skill in self.skills if skill.model_invocable)

    def get(self, name: str) -> SkillMetadata | None:
        record = self._records.get(name.casefold())
        return record.metadata if record is not None else None

    lookup = get

    def search(self, query: str = "") -> tuple[SkillMetadata, ...]:
        terms = tuple(term.casefold() for term in query.split() if term)
        matches = []
        for record in self._records.values():
            metadata = record.metadata
            haystack = " ".join(
                (metadata.name, metadata.description, *metadata.tools, metadata.source)
            ).casefold()
            if all(term in haystack for term in terms):
                matches.append(metadata)
        return tuple(
            sorted(
                matches,
                key=lambda item: (
                    0 if terms and item.name.casefold() == " ".join(terms) else 1,
                    item.name.casefold(),
                    str(item.path),
                ),
            )
        )

    def load(self, name: str, *, user: bool = False) -> str | None:
        """Load only the instruction body, preserving the original API."""
        loaded = load_skill(self, name, user=user)
        return loaded.instructions if loaded is not None else None

    def load_exact(self, name: str, tool_registry: Any = None) -> LoadedSkill | None:
        """Load an exact catalog match and request its declared tool surface."""
        return load_skill(self, name, tool_registry=tool_registry)


def load_skill(
    catalog: SkillCatalog, name: str, tool_registry: Any = None, *, user: bool = False
) -> LoadedSkill | None:
    """Load one exact skill and activate declared tools when supported.

    ``user`` marks a load the user asked for; without it a skill whose
    frontmatter disables model invocation is not loaded and None is returned.

    Lazy registries advertise the capability with ``activate_tools(names)`` and
    return ``ToolActivationResult``. Plain registries intentionally remain a
    no-op compatibility path.
    """
    record = catalog._records.get(name.casefold())
    if record is None:
        return None
    metadata = record.metadata
    if not metadata.model_invocable and not user:
        return None
    text = metadata.path.read_text(encoding="utf-8", errors="replace")
    _, body, _ = _split_frontmatter(text)
    activation = ToolActivationResult()
    activate = getattr(tool_registry, "activate_tools", None)
    if metadata.tools and callable(activate):
        outcome = activate(metadata.tools)
        if not isinstance(outcome, ToolActivationResult):
            raise TypeError("activate_tools() must return ToolActivationResult")
        activation = ToolActivationResult(
            activated=_ordered_subset(metadata.tools, outcome.activated),
            denied=_ordered_subset(metadata.tools, outcome.denied),
            missing=_ordered_subset(metadata.tools, outcome.missing),
        )
    return LoadedSkill(metadata=metadata, instructions=body, activation=activation)


_USER_COMMAND_RE = re.compile(r"/skill(?:\s+(\S+))?(?:\s+(.*))?", re.DOTALL)


class SkillInvocationError(ValueError):
    """A ``/skill`` line that names no skill, or one the catalog lacks."""


def expand_user_invocation(catalog: SkillCatalog, text: str) -> str | None:
    """Turn a ``/skill <name> [request]`` line into the user message that carries it.

    This is the user's path into a skill, so it loads skills whose frontmatter
    disables model invocation. Returns None when ``text`` is not a ``/skill``
    line; raises SkillInvocationError when it names no known skill.
    """
    match = _USER_COMMAND_RE.fullmatch(text.strip())
    if match is None:
        return None
    name, request = match.group(1), (match.group(2) or "").strip()
    if not name:
        raise SkillInvocationError("usage: /skill <name> [request]")
    loaded = load_skill(catalog, name, user=True)
    if loaded is None:
        raise SkillInvocationError(f"no skill named {name!r}")
    metadata = loaded.metadata
    header = [f"Base directory for this skill: {metadata.path.parent}"]
    if metadata.tools:
        header.append(
            "Tools this skill declares (load them with tool_discovery): "
            + ", ".join(metadata.tools)
        )
    body = loaded.instructions.strip("\n")
    block = f'<skill name="{metadata.name}">\n' + "\n".join(header) + f"\n\n{body}\n</skill>"
    return f"{block}\n\n{request}" if request else block


def _ordered_subset(required: tuple[str, ...], reported: tuple[str, ...]) -> tuple[str, ...]:
    names = set(reported)
    return tuple(name for name in required if name in names)


def discover_skills(
    project_dir: Path,
    *,
    builtin_dir: Path | None = None,
    global_dir: Path | None = None,
    user_dir: Path | None = None,
) -> SkillCatalog:
    """Index built-in, global, and project skills without retaining their bodies.

    Layers run lowest first, so a global or project skill shadows a built-in
    one of the same name.
    """

    builtin_root = builtin_dir or BUILTIN_SKILLS_DIR
    global_root = global_dir or paths.global_skills_dir()
    user_root = user_dir or paths.shared_skills_dir()
    # Within a scope the cross-client dir (.agents/skills) is scanned first and
    # the js-native dir last, so native wins a name collision — with a warning,
    # because two same-named skills in one scope is ambiguity, not layering.
    layers = (
        ("builtin", (builtin_root,)),
        ("global", (user_root, global_root)),
        ("project", (project_dir / ".agents" / "skills", project_dir / ".js" / "skills")),
    )
    selected: dict[str, _SkillRecord] = {}
    for source, roots in layers:
        layer_records: dict[str, _SkillRecord] = {}
        for root in roots:
            root_records: dict[str, _SkillRecord] = {}
            for path in _skill_paths(root):
                try:
                    record = _index_skill(path, source)
                except ValueError as exc:
                    _warn_once(msgs.SKILL_SKIPPED.text(path=path, error=exc))
                    continue
                key = record.metadata.name.casefold()
                prior = root_records.get(key)
                if prior is not None:
                    _warn_once(msgs.SKILL_DUPLICATE.text(
                        path=path, name=record.metadata.name, root=root, prior=prior.metadata.path))
                    continue
                root_records[key] = record
            for key, record in root_records.items():
                prior = layer_records.get(key)
                if prior is not None:
                    _warn_once(msgs.SKILL_OVERRIDES.text(
                        name=record.metadata.name, path=record.metadata.path, prior=prior.metadata.path))
            layer_records.update(root_records)
        selected.update(layer_records)
    return SkillCatalog(selected.values())


def _skill_paths(root: Path) -> tuple[Path, ...]:
    """A skill is a subdirectory holding a SKILL.md — the Agent Skills format."""
    if not root.is_dir():
        return ()
    return tuple(sorted(root.glob("*/SKILL.md"), key=lambda item: str(item).casefold()))


def _index_skill(path: Path, source: str) -> _SkillRecord:
    text = path.read_text(encoding="utf-8", errors="replace")
    manifest, body, _ = _split_frontmatter(text)
    derived_name = path.parent.name
    name = _string_field(manifest, "name") or derived_name
    _validate_name(name)
    description = _string_field(manifest, "description")
    if not description:
        description = _derive_description(body, name)
    description = " ".join(description.split())[:_MAX_DESCRIPTION]
    tools = _tools_field(manifest)
    user_only = _bool_field(manifest, "disable-model-invocation")
    metadata = SkillMetadata(
        name=name,
        description=description,
        tools=tools,
        source=source,
        path=path,
        model_invocable=not user_only,
    )
    return _SkillRecord(metadata=metadata)


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str, int]:
    if not text.startswith("---") or text[:4] not in {"---\n", "---\r"}:
        return {}, text, 0
    match = re.search(r"\r?\n---[ \t]*(?:\r?\n|$)", text[3:])
    if match is None:
        raise ValueError("frontmatter is missing a closing ---")
    start = 3 + match.start()
    end = 3 + match.end()
    yaml_text = text[text.find("\n") + 1:start]
    try:
        manifest = yaml.safe_load(yaml_text) if yaml_text.strip() else {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML frontmatter: {_yaml_problem(exc)}") from exc
    if manifest is None:
        manifest = {}
    if not isinstance(manifest, dict):
        raise ValueError("frontmatter must be a mapping")
    return manifest, text[end:], end


def _yaml_problem(exc: yaml.YAMLError) -> str:
    """The parser's complaint and its SKILL.md line, without the source excerpt."""
    problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
    mark = getattr(exc, "problem_mark", None)
    # Mark lines count from 0 within the frontmatter, which starts on line 2.
    return f"{problem} (line {mark.line + 2})" if mark is not None else problem


def _warn_once(message: str) -> None:
    """Print a discovery warning as one line, once per process."""
    line = " ".join(message.split())
    if line in _WARNED:
        return
    _WARNED.add(line)
    msgs.warn(msgs.FAILED_WARN, error=line)


def _string_field(manifest: dict[str, Any], field: str) -> str:
    value = manifest.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} frontmatter must be a string")
    return value.strip()


def _bool_field(manifest: dict[str, Any], field: str) -> bool:
    value = manifest.get(field)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ValueError(f"{field} frontmatter must be true or false")


def _tools_field(manifest: dict[str, Any]) -> tuple[str, ...]:
    value = manifest.get("tools")
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("tools frontmatter must be a list of strings")
    tools: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not _TOOL_RE.fullmatch(item.strip()):
            raise ValueError("tools frontmatter must contain safe non-empty strings")
        tool = item.strip()
        if tool in seen:
            raise ValueError(f"tools frontmatter contains duplicate {tool!r}")
        seen.add(tool)
        tools.append(tool)
    return tuple(tools)


def _validate_name(name: str) -> None:
    if not _NAME_RE.fullmatch(name) or ".." in name:
        raise ValueError(f"unsafe skill name {name!r}")


def _derive_description(body: str, name: str) -> str:
    heading = ""
    paragraphs: list[str] = []
    current: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not heading and stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            continue
        if not stripped:
            if current:
                paragraphs.append(" ".join(current))
                break
            continue
        if not stripped.startswith(("#", "```")):
            current.append(stripped)
    if current and not paragraphs:
        paragraphs.append(" ".join(current))
    if paragraphs:
        return paragraphs[0]
    return heading or name.replace("-", " ").replace("_", " ")
