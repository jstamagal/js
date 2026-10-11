"""Markdown commands: `~/.js/commands/NAME.md` is the command `/NAME`.

Typing `/NAME args` sends the file's body as the user message, with the
arguments substituted in (pi's prompt templates). A project's
`.js/commands/NAME.md` shadows the global one of the same name. Optional YAML
frontmatter may give a `description`; without one, the body's first non-blank
line describes the command.

Placeholders, filled from the arguments split shell-style (quotes group words):

- ``$1``, ``$2`` ... one argument; empty when missing
- ``$@`` and ``$ARGUMENTS`` every argument, space-joined
- ``${N:-default}`` argument N, or ``default`` when it is missing or empty
- ``${@:-default}`` every argument, or ``default`` when there are none
- ``${@:N}`` and ``${@:N:L}`` the arguments from the Nth on, or L of them

Substitution is one pass over the body: text an argument brings in is never
substituted again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import paths

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_PLACEHOLDER = re.compile(
    r"\$\$|\$\{(\d+|ARGUMENTS|@):-([^}]*)\}|\$\{@:(\d+)(?::(\d+))?\}|\$(ARGUMENTS|@|\d+)"
)
_DESCRIPTION_CHARS = 60


@dataclass(frozen=True)
class PromptCommand:
    name: str
    path: Path
    description: str
    body: str


def command_dirs(project_dir: Path) -> tuple[Path, ...]:
    """Where commands live, lowest layer first: global, then project."""
    return (paths.global_commands_dir(), project_dir / ".js" / "commands")


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return {}, text
    match = re.search(r"\r?\n---[ \t]*(?:\r?\n|$)", text[3:])
    if match is None:
        return {}, text
    header = text[text.find("\n") + 1:3 + match.start()]
    try:
        loaded = yaml.safe_load(header) if header.strip() else {}
    except yaml.YAMLError:
        loaded = {}
    return (loaded if isinstance(loaded, dict) else {}), text[3 + match.end():]


def _load(path: Path) -> PromptCommand | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    meta, body = _split_frontmatter(text)
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        first = next((line.strip() for line in body.splitlines() if line.strip()), "")
        description = first if len(first) <= _DESCRIPTION_CHARS else first[:_DESCRIPTION_CHARS] + "..."
    return PromptCommand(name=path.stem, path=path, description=description.strip(), body=body)


# Parsed command files by path, with the (mtime_ns, size) they were parsed at.
# Tab completion calls discover on every keypress it completes.
_parsed: dict[Path, tuple[tuple[int, int], PromptCommand | None]] = {}


def _load_cached(path: Path) -> PromptCommand | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _parsed.get(path)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    command = _load(path)
    _parsed[path] = (stamp, command)
    return command


def discover(project_dir: Path) -> dict[str, PromptCommand]:
    """Every command by name. A project command shadows a global one. A file
    is read again only when its mtime or size changed."""
    found: dict[str, PromptCommand] = {}
    for root in command_dirs(project_dir):
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for path in entries:
            if path.suffix != ".md" or not _NAME.match(path.stem) or not path.is_file():
                continue
            command = _load_cached(path)
            if command is not None:
                found[command.name] = command
    return found


def split_args(text: str) -> list[str]:
    """Words of ``text``; single or double quotes group a run with spaces.

    A quote opens a group only at the start of a word, so an apostrophe inside
    one ("don't") is an ordinary character. A group left open at the end of
    the line is taken literally, quote included, instead of swallowing the
    rest of the line."""
    args: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        quote = text[i] if text[i] in ("'", '"') else None
        end = text.find(quote, i + 1) if quote is not None else -1
        if quote is not None and end != -1:
            args.append(text[i + 1:end])
            i = end + 1
            continue
        start = i
        while i < n and not text[i].isspace():
            i += 1
        args.append(text[start:i])
    return args


def substitute(body: str, args: list[str], raw: str | None = None) -> str:
    """``body`` with its placeholders filled from ``args``, in one pass.

    ``$@``/``$ARGUMENTS`` take ``raw``, the argument text as typed, when given,
    so a pasted multi-line argument keeps its line breaks and indentation.
    ``$$`` is a literal ``$``."""
    joined = raw if raw is not None else " ".join(args)

    def fill(match: re.Match) -> str:
        if match.group(0) == "$$":
            return "$"
        default_target, default_value, slice_start, slice_length, simple = match.groups()
        if default_target is not None:
            value = joined if default_target in ("@", "ARGUMENTS") else _nth(args, int(default_target))
            return value or default_value
        if slice_start is not None:
            start = max(int(slice_start) - 1, 0)
            chosen = args[start:] if slice_length is None else args[start:start + int(slice_length)]
            return " ".join(chosen)
        if simple in ("@", "ARGUMENTS"):
            return joined
        return _nth(args, int(simple))

    return _PLACEHOLDER.sub(fill, body)


def _nth(args: list[str], n: int) -> str:
    return args[n - 1] if 1 <= n <= len(args) else ""


def expand(line: str, commands: dict[str, PromptCommand]) -> str | None:
    """The user message a `/NAME args` line sends; None when NAME is no command."""
    stripped = line.lstrip()
    if not stripped.startswith("/"):
        return None
    words = stripped[1:].split(maxsplit=1)
    command = commands.get(words[0]) if words else None
    if command is None:
        return None
    raw = stripped[1:][len(words[0]):].strip() if words else ""
    return substitute(command.body, split_args(raw), raw=raw).strip()
