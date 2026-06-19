"""Markdown-backed model-facing tool descriptions, with conditional sections.

A description may carry sections that flip on/off by runtime state:

    always-shown text
    <!--if:some_flag-->
    only shown when 'some_flag' is in the active flag set
    <!--endif-->

`load_description(name, flags=...)` keeps a block only when its flag is active and
strips it otherwise, so one `.md` serves both states (e.g. the task tool's `model`
section appears unless subagent model override is locked off). The default flag
set enables `model_override`; the locked path passes `flags=()` to strip it.
"""
from __future__ import annotations

import re
from functools import cache
from pathlib import Path

_DESCRIPTION_DIR = Path(__file__).with_name("tool_descriptions")

_IF_BLOCK = re.compile(r"[ \t]*<!--if:([A-Za-z0-9_]+)-->\n?(.*?)\n?[ \t]*<!--endif-->\n?", re.DOTALL)


@cache
def load_description(name: str, flags: tuple[str, ...] = ("model_override",)) -> str:
    """Load a non-empty markdown description, keeping only the conditional blocks
    whose flag is in ``flags`` and stripping the rest."""
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
