"""Tool results: the adapters output sinks share, and the spill and cut
that bound the text a model gets."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

from .. import paths
from .. import settings as _settings
from ..capped_process import truncation_marker
from ..text_bytes import byte_prefix, byte_size, cap_text
from .core import Output, ToolResult


def result_text(result: Any, *, screen: bool = False) -> tuple[str, bool]:
    """A tool result's text and whether it reports an error.

    A `ToolResult` is its dehydrated text, an error when it says so. Any
    other result is an error when its text starts with ERROR, unless it is
    an `Output`. ``screen`` reads a result as the screen does; without it, as
    the JSON summary does. The two differ three ways: without ``screen`` a
    `ToolResult` whose text starts with ERROR is an error too; with it ERROR
    may follow leading whitespace; and a result that is not a string is its
    `str()` with it, its JSON without."""
    if isinstance(result, ToolResult):
        text = result.dehydrated()
        return text, result.is_error or (not screen and text.startswith("ERROR"))
    if isinstance(result, str):
        text = result
    else:
        text = str(result) if screen else json.dumps(result, ensure_ascii=False, default=str)
    lead = text.lstrip() if screen else text
    return text, not isinstance(result, Output) and lead.startswith("ERROR")


def truncate(text: str, context: Any, *, cut: bool = False) -> str:
    """``text`` within the TOOL_RESULT_MAX bytes of ``context``. Text cut
    here, or that ``cut`` says was cut before it got here, ends in the line
    that says so. A cap of 0 or less is no cap."""
    budget = int(_settings.knob_attr(context, "max_tool_result_bytes", "TOOL_RESULT_MAX") or 0)
    if budget <= 0:
        return text
    marker = f"\n{truncation_marker(budget, 'TOOL_RESULT_MAX')}"
    return cap_text(f"{text}{marker}" if cut else text, budget, marker)


class _SpilledResult(str):
    """A preview with a recovery notice retained through downstream caps."""

    def __new__(cls, text: str, restart_notice: str):
        value = super().__new__(cls, text)
        value.restart_notice = restart_notice
        return value


class _SpilledOutput(_SpilledResult, Output):
    pass


def cap_result_text(result: str, budget: int, marker: str) -> str:
    """``result`` cut to ``budget`` bytes, ending in ``marker`` when cut. A
    spilled preview keeps its recovery notice in place of the marker."""
    if not isinstance(result, _SpilledResult) or byte_size(result) <= budget:
        return cap_text(result, budget, marker)
    # A shortened preview restarts at byte zero so no unseen bytes are skipped.
    notice = result.restart_notice
    # Recovery pointers take precedence over a text allowance too small to name them.
    if byte_size(notice) > budget:
        return type(result)(notice, notice)
    head = byte_prefix(result, max(0, budget - byte_size(notice) - 2))
    text = f"{head}\n\n{notice}" if head else notice
    return type(result)(text, notice)


def spill_oversized_result(
    result: str,
    inline_cap: int,
    *,
    spill_dir: Path | None = None,
    limit_name: str = "TOOL_RESULT_INLINE_MAX",
    force: bool = False,
) -> str:
    """Write an oversized result to disk and return a preview plus the path.

    Clipping throws the tail away — the model cannot get it back and often
    does not know it existed. Spilling keeps every byte addressable: the notice
    names the `read` call that continues at the byte where the preview stops,
    and, when the text has more than one line, the line-range call too. A
    single-line payload (JSON, say) is reachable only by byte range. 0 or
    less disables.
    """
    if inline_cap <= 0 or (not force and byte_size(result) <= inline_cap):
        return result
    target_dir = spill_dir or paths.tool_results_dir()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(result.encode("utf-8", "replace")).hexdigest()[:16]
        path = target_dir / f"result-{digest}.txt"
        if not path.exists():
            # Two parallel calls can spill the same text; neither may see the
            # other's half-written file.
            partial = target_dir / f".result-{digest}.{threading.get_ident()}.tmp"
            # "replace" as byte_size counts, so the notice's offsets match the file.
            partial.write_bytes(result.encode("utf-8", "replace"))
            os.replace(partial, path)
    except OSError:
        return result  # cannot spill -> the byte cap downstream still applies
    total_bytes = byte_size(result)
    line_count = len(result.splitlines())
    multiline = line_count > 1

    def spill_notice(next_byte: int, next_line: int) -> str:
        by_byte = json.dumps({"start_byte": next_byte})
        if multiline:
            by_line = json.dumps({"start_line": next_line})
            how = f'read it on with range {by_byte}, or by line with range {by_line}'
        else:
            how = f'read it on with range {by_byte}; it is one line, so only a byte range splits it'
        return (
            f'[result was {total_bytes} bytes, over {limit_name} ({inline_cap}); the full text is at {path} — {how}]'
        )

    # Keep a usable pointer even when the notice alone exceeds the text budget.
    # The budget is sized with the widest offsets the notice can name.
    widest = spill_notice(total_bytes, line_count + 1)
    head = byte_prefix(result, min(inline_cap // 2, max(0, inline_cap - byte_size(widest) - 2)))
    head_lines = head.splitlines(keepends=True)
    # The line the preview stops inside, or the next one when it ends on a break.
    next_line = len(head_lines) + (1 if head_lines and head_lines[-1] != head_lines[-1].rstrip("\r\n") else 0)
    notice = spill_notice(byte_size(head), max(1, next_line))
    text = f"{head}\n\n{notice}" if head else notice
    cls = _SpilledOutput if isinstance(result, Output) else _SpilledResult
    return cls(text, spill_notice(0, 1))
