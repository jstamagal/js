"""Discover compact skill metadata and load instructions on demand."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
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
    # Frontmatter ``paths:`` globs, relative to the session's working
    # directory. The first read, patch or write of a matching file offers the
    # skill to the model once per session.
    paths: tuple[str, ...] = ()

    def matches_path(self, relative: str) -> bool:
        """Whether a path relative to the working directory matches ``paths``.

        A pattern without a ``/`` (a trailing one aside) matches a file or
        directory name at any depth, so ``*.rs`` matches ``src/main.rs``. A
        pattern with a ``/`` is anchored at the working directory; ``**`` spans directories. A pattern
        that names a directory matches everything under it.
        """
        parts = PurePosixPath(relative).parts
        if not parts or parts[0] in {"..", "/"}:
            return False
        for raw in self.paths:
            anchored = "/" in raw.strip().rstrip("/")
            pattern = raw.strip().removeprefix("/").removesuffix("/**").rstrip("/")
            if not pattern:
                continue
            if not anchored:
                if any(fnmatchcase(part, pattern) for part in parts):
                    return True
                continue
            for end in range(1, len(parts) + 1):
                if _glob_match(parts[:end], tuple(pattern.split("/"))):
                    return True
        return False


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
    note_loaded = getattr(tool_registry, "note_skill_loaded", None)
    if callable(note_loaded):
        note_loaded(metadata.name)
    return LoadedSkill(metadata=metadata, instructions=body, activation=activation)


_USER_COMMAND_RE = re.compile(r"/skill(?:\s+(\S+))?(?:\s+(.*))?", re.DOTALL)
_USER_SKILL_BLOCK_RE = re.compile(r'<skill name="([^"]+)">\n')


def user_invoked_skill(content: Any) -> str | None:
    """The skill a ``/skill`` user message carries, from the block
    ``expand_user_invocation`` puts at its start; None for any other message."""
    if isinstance(content, list):
        content = next((part.get("text") for part in content if isinstance(part, dict) and "text" in part), None)
    if not isinstance(content, str):
        return None
    match = _USER_SKILL_BLOCK_RE.match(content)
    return match.group(1) if match else None


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
        raise SkillInvocationError(msgs.USAGE.text(usage="/skill <name> [request]"))
    loaded = load_skill(catalog, name, user=True)
    if loaded is None:
        raise SkillInvocationError(msgs.NO_SUCH_SKILL.text(name=name))
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


def _glob_match(parts: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    """Match path components against pattern components; ``**`` spans any number."""
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return any(_glob_match(parts[index:], pattern[1:]) for index in range(len(parts) + 1))
    return bool(parts) and fnmatchcase(parts[0], pattern[0]) and _glob_match(parts[1:], pattern[1:])


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
        paths=_paths_field(manifest),
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
        # An unquoted glob such as `paths: *.rs` reads as a YAML alias; a
        # second parse quotes the paths globs.
        try:
            manifest = yaml.safe_load(_quote_path_globs(yaml_text))
        except yaml.YAMLError:
            raise ValueError(f"invalid YAML frontmatter: {_yaml_problem(exc)}") from exc
    if manifest is None:
        manifest = {}
    if not isinstance(manifest, dict):
        raise ValueError("frontmatter must be a mapping")
    return manifest, text[end:], end


_YAML_SPECIAL = re.compile(r"[{}\[\]*&#!|>%@`]|: ")
_PATHS_LINE = re.compile(r"^(paths:[ \t]*)(.*?)[ \t]*$")
_ITEM_LINE = re.compile(r"^([ \t]*-[ \t]+)(.+?)[ \t]*$")


def _quote_path_globs(yaml_text: str) -> str:
    """``yaml_text`` with each unquoted glob of the ``paths:`` field (its
    inline value, or the ``- item`` lines under it) that holds a YAML
    indicator character written as a double-quoted string, as Claude Code
    reads it."""

    def quoted(lead: str, value: str) -> str:
        if (len(value) > 1 and value[0] in "'\"" and value[-1] == value[0]) or not _YAML_SPECIAL.search(value):
            return f"{lead}{value}"
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'{lead}"{escaped}"'

    lines = []
    in_paths = False
    for line in yaml_text.split("\n"):
        paths_line = _PATHS_LINE.match(line)
        item = _ITEM_LINE.match(line) if in_paths else None
        if paths_line is not None:
            lead, value = paths_line.groups()
            in_paths = not value
            line = quoted(lead, value) if value else line
        elif item is not None:
            line = quoted(*item.groups())
        elif line.strip():
            in_paths = False
        lines.append(line)
    return "\n".join(lines)


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


def _paths_field(manifest: dict[str, Any]) -> tuple[str, ...]:
    """``paths:`` as a list of globs, or one string of comma-separated globs."""
    value = manifest.get("paths")
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("paths frontmatter must be a list of glob strings")
    patterns = tuple(dict.fromkeys(
        expanded for item in value for part in _split_outside_braces(item)
        for expanded in _expand_braces(part)
    ))
    # A match-all pattern would offer the skill on the first file touched; it
    # scopes nothing, so the skill is left unscoped.
    if all(pattern.strip("/") == "**" for pattern in patterns):
        return ()
    return patterns


def _split_outside_braces(text: str) -> list[str]:
    """``text`` split at the commas outside ``{}``, each part stripped, empty
    parts dropped."""
    parts, current, depth = [], "", 0
    for char in text:
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        depth += {"{": 1, "}": -1}.get(char, 0)
        current += char
    parts.append(current)
    return [part.strip() for part in parts if part.strip()]


def _expand_braces(pattern: str) -> list[str]:
    """``pattern`` with its first ``{a,b}`` group expanded, recursively:
    ``src/*.{ts,tsx}`` gives ``src/*.ts`` and ``src/*.tsx``."""
    match = re.search(r"\{([^{}]*)\}", pattern)
    if match is None:
        return [pattern]
    head, tail = pattern[:match.start()], pattern[match.end():]
    return [
        expanded
        for option in match.group(1).split(",")
        for expanded in _expand_braces(f"{head}{option}{tail}")
    ]


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
