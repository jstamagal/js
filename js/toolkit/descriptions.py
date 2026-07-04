"""Markdown-backed model-facing tool descriptions, with conditional sections.

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
import sys
from collections.abc import Iterable
from functools import cache
from pathlib import Path

_DESCRIPTION_DIR = Path(__file__).with_name("tool_descriptions")

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

_WARNED: set[str] = set()


def _warn_once(message: str) -> None:
    if message in _WARNED:
        return
    _WARNED.add(message)
    print(f"warning: tool description: {message}", file=sys.stderr)


@cache
def load_description(name: str, flags: tuple[str, ...] = ("model_override",)) -> str:
    """Load a non-empty markdown description, keeping only the build-time flag
    blocks whose flag is in ``flags``. Co-present-tool-name blocks ({{#if}}/
    {{#unless}}) are left intact here and resolved later at the registry boundary."""
    path = _DESCRIPTION_DIR / f"{name}.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise FileNotFoundError(f"missing tool description: {path}") from None
    if not text:
        raise ValueError(f"empty tool description: {path}")

    active = frozenset(flags)

    def _resolve(m: re.Match) -> str:
        flag, body = m.group(1), m.group(2)
        return (body + "\n") if flag in active else ""

    return _IF_BLOCK.sub(_resolve, text).strip()


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
            _warn_once(f"{label}nested {{{{#if}}}}/{{{{#unless}}}} block is unsupported; left literal")
            return m.group(0)
        names = m.group("names").split()
        if not names:
            _warn_once(f"{label}conditional block names no tool; left literal")
            return m.group(0)
        keep = any(n in active for n in names)
        if m.group("kind") == "unless":
            keep = not keep
        return (body + "\n") if keep else ""

    rendered = _NAME_BLOCK.sub(_resolve, text)
    # A marker surviving where every BALANCED block (escaped ones still match the
    # block regex) has been stripped is a genuinely unpaired open/close tag.
    if _STRAY_MARKER.search(_NAME_BLOCK.sub("", text)):
        _warn_once(f"{label}unbalanced {{{{#if}}}}/{{{{#unless}}}} tag; left literal")
    return rendered.strip()
