"""Markdown-backed model-facing tool descriptions with conditional sections.

Descriptions and parameter prose live under ``tool_descriptions/<tool>.md``.
The main text carries contracts and cross-references to co-present tools.
The ``<!--parameters-->`` section supplies the parameter schema descriptions.

Two independent conditional axes flip parts of a description on and off:

1. Build-time FLAGS — resolved once when the tool object is constructed:

       always-shown text
       <!--if:some_flag-->
       only shown when 'some_flag' is in the active flag set
       <!--endif-->

   `load_description(name, flags=...)` keeps a block only when its flag is active
   (e.g. the task tool's `model` section appears unless subagent model override is
   locked off). The default flag set enables `model_override`; the locked path
   passes `flags=()` to strip it.

2. CO-PRESENT TOOL NAMES — resolved at the model-facing boundary
   (`ToolRegistry.openai_specs`) against the tools that share the agent's surface:

       {{#unless fs_search}}
       ...doctrine only a shell-only agent needs...
       {{/unless}}
       {{#if fs_search}}
       ...phrasing for when fs_search is also on the surface...
       {{/if}}

   `render_tool_name_sections(text, present)` keeps an `#unless` block only when
   NONE of its named tools are on the surface, and an `#if` block only when ANY
   are. This is why one description renders several ways: composition across the
   final tool set, not forked files. See `render_tool_name_sections`.
"""
from __future__ import annotations

import re

from .. import messages as msgs
from .. import output, paths, snippets
from collections.abc import Iterable
from functools import cache
from pathlib import Path


_IF_BLOCK = re.compile(r"[ \t]*<!--if:([A-Za-z0-9_]+)-->\n?(.*?)\n?[ \t]*<!--endif-->\n?", re.DOTALL)

# {{#if a b}}...{{/if}} / {{#unless a b}}...{{/unless}}, resolved against the
# co-present tool-name set. A leading ``\`` escapes the whole block (emit verbatim
# minus the backslash); wrapping it in a backtick span keeps it literal too — the
# same escape grammar promptexpand uses. The close tag's kind is backreferenced so
# an `#if` never closes on `{{/unless}}`. Body is captured non-greedily; a nested
# opener inside it is rejected (block content is plain text, never re-scanned —
# the single-pass injection guard).
_NAME_BLOCK = re.compile(
    r"(?P<bs>\\)?(?P<tick>`)?"
    r"\{\{#(?P<kind>if|unless)[ \t]+(?P<names>[^}]*?)\}\}\n?"
    r"(?P<body>.*?)"
    r"\n?[ \t]*\{\{/(?P=kind)\}\}(?(tick)`)[ \t]*\n?",
    re.DOTALL,
)

# Leftover markers after a clean pass mean an unbalanced/mismatched block that the
# block regex could not pair — surfaced once, left literal, never a crash.
_STRAY_MARKER = re.compile(r"\{\{#(?:if|unless)\b|\{\{/(?:if|unless)\}\}")

_PARAMETERS = "<!--parameters-->"
_PARAMETER_HEADING = re.compile(r"^### ([A-Za-z0-9_.-]+)\n", re.MULTILINE)

_WARNED: set[str] = set()


def _warn_once(message: str) -> None:
    if message in _WARNED:
        return
    _WARNED.add(message)
    output.notice(msgs.DESCRIPTION_PROBLEM, problem=message)


def description_dir() -> Path:
    return paths.defaults_dir() / "tool_descriptions"


def load_description(name: str, flags: tuple[str, ...] = ("model_override",)) -> str:
    """Load a non-empty markdown description, keeping only the build-time flag
    blocks whose flag is in ``flags``. Co-present-tool-name blocks
    ({{#if}}/{{#unless}}) are left intact here and resolved later at the
    registry boundary."""
    return _load_description(_description_path(name), tuple(flags))


def _description_path(name: str) -> Path:
    path = paths.find(Path("tool_descriptions") / f"{name}.md")
    if path is None:
        searched = ", ".join(str(root / "tool_descriptions") for root in paths.layers()[:2])
        raise FileNotFoundError(f"missing tool description {name!r}; searched {searched}")
    return path


@cache
def _load_description(path: Path, flags: tuple[str, ...]) -> str:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise FileNotFoundError(f"missing tool description: {path}") from None

    active = frozenset(flags)

    def _resolve(m: re.Match) -> str:
        flag, body = m.group(1), m.group(2)
        return (body + "\n") if flag in active else ""

    return _IF_BLOCK.sub(_resolve, text.split(_PARAMETERS, 1)[0]).strip()


def load_parameter_description(name: str, parameter: str, **values: object) -> str:
    """Read parameter prose from the same markdown file as its tool contract."""
    path = _description_path(name)
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return ""
    _, separator, parameters = text.partition(_PARAMETERS)
    if separator:
        headings = list(_PARAMETER_HEADING.finditer(parameters))
        for index, heading in enumerate(headings):
            if heading[1] == parameter:
                end = headings[index + 1].start() if index + 1 < len(headings) else len(parameters)
                return snippets.substitute(parameters[heading.end():end].strip(), values)
    raise ValueError(f"missing parameter description {parameter!r} in {path}")


def render_tool_name_sections(text: str, present: Iterable[str], *, tool: str = "") -> str:
    """Resolve ``{{#if <names>}}`` / ``{{#unless <names>}}`` blocks against the set
    of tool names sharing the agent's surface.

    ``#if`` keeps its body when ANY named tool is present; ``#unless`` keeps its
    body when NONE are. Multiple names are whitespace-separated. Evaluation is a
    single pass — a kept body is spliced in verbatim and never re-scanned, so a
    body that itself contains directive-like text cannot trigger further
    expansion. A leading ``\\`` or a full backtick-span keeps a block literal.
    Anything malformed (nested opener, no tool name, unbalanced tag) degrades to
    literal text with a one-line stderr warning — never a traceback. ``tool``
    labels the warning with the offending description's name.
    """
    if "{{#" not in text:
        return text
    active = set(present)
    label = f"{tool}: " if tool else ""

    def _resolve(m: re.Match) -> str:
        if m.group("bs") is not None:
            return m.group(0)[1:]
        if m.group("tick") is not None:
            return m.group(0)
        body = m.group("body")
        if "{{#if" in body or "{{#unless" in body:
            _warn_once(msgs.DESCRIPTION_NESTED_BLOCK.said(label=label))
            return m.group(0)
        names = m.group("names").split()
        if not names:
            _warn_once(msgs.DESCRIPTION_BLOCK_NAMES_NO_TOOL.said(label=label))
            return m.group(0)
        keep = any(n in active for n in names)
        if m.group("kind") == "unless":
            keep = not keep
        return (body + "\n") if keep else ""

    rendered = _NAME_BLOCK.sub(_resolve, text)
    # A marker surviving where every BALANCED block (escaped ones still match the
    # block regex) has been stripped is a genuinely unpaired open/close tag.
    if _STRAY_MARKER.search(_NAME_BLOCK.sub("", text)):
        _warn_once(msgs.DESCRIPTION_UNBALANCED.said(label=label))
    return rendered.strip()
