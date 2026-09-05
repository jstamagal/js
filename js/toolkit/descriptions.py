"""Markdown-backed model-facing tool descriptions, in two variants, with
conditional sections.

Descriptions live under ``tool_descriptions/<variant>/<tool>.md``. The two
variants hold the same file set and differ only in how much they say:

- ``stock`` — the full text as it has always shipped.
- ``slim`` — cut to what a modern model can act on: contracts it cannot infer,
  cross-references to co-present tools, nothing the schema already carries.

The active variant is the ``tools.descriptions`` knob (``JS_TOOL_DESCRIPTIONS``
in the environment). A registry build passes it down through ``using_variant``;
outside a build the environment decides, so ``JS_TOOL_DESCRIPTIONS=slim`` also
steers ad-hoc callers such as ``js.tooldiag``.

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

import os
import re
import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import cache
from pathlib import Path

from ..settings import DEFAULT_TOOL_DESCRIPTIONS, TOOL_DESCRIPTION_VARIANTS

_DESCRIPTIONS_ROOT = Path(__file__).with_name("tool_descriptions")

# Env names the knob answers to, alias first (settings.env_names_for order).
_VARIANT_ENV_NAMES = ("JS_TOOL_DESCRIPTIONS", "JS_TOOLS_DESCRIPTIONS")

# Set for the duration of a registry build so every module's tools() reads the
# same variant without threading a parameter through ten call sites.
_forced_variant: ContextVar[str | None] = ContextVar("js_tool_description_variant", default=None)

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


def normalize_variant(raw: object) -> str | None:
    """``"Slim "`` -> ``"slim"``; anything that is not a known variant -> None."""
    if raw is None:
        return None
    value = str(raw).strip().lower()
    return value if value in TOOL_DESCRIPTION_VARIANTS else None


def description_dir(variant: str) -> Path:
    return _DESCRIPTIONS_ROOT / variant


def active_variant() -> str:
    """The variant `load_description` reads right now: the one a registry build
    forced, else the environment knob, else the default."""
    forced = _forced_variant.get()
    if forced is not None:
        return forced
    for name in _VARIANT_ENV_NAMES:
        value = normalize_variant(os.environ.get(name))
        if value is not None:
            return value
    return DEFAULT_TOOL_DESCRIPTIONS


@contextmanager
def using_variant(variant: str | None) -> Iterator[str]:
    """Make `load_description` read ``variant`` for the duration of the block.
    ``None`` leaves the ambient choice (environment, then default) in force."""
    if variant is None:
        yield active_variant()
        return
    resolved = normalize_variant(variant)
    if resolved is None:
        raise ValueError(
            f"unknown tool description variant {variant!r}; "
            f"expected one of {', '.join(TOOL_DESCRIPTION_VARIANTS)}"
        )
    token = _forced_variant.set(resolved)
    try:
        yield resolved
    finally:
        _forced_variant.reset(token)


def load_description(name: str, flags: tuple[str, ...] = ("model_override",)) -> str:
    """Load a non-empty markdown description from the active variant, keeping only
    the build-time flag blocks whose flag is in ``flags``. Co-present-tool-name
    blocks ({{#if}}/{{#unless}}) are left intact here and resolved later at the
    registry boundary."""
    return _load_description(name, tuple(flags), active_variant())


@cache
def _load_description(name: str, flags: tuple[str, ...], variant: str) -> str:
    path = description_dir(variant) / f"{name}.md"
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
