"""Tool result adapters shared by output sinks."""

from __future__ import annotations

import json
from typing import Any

from .core import ToolResult


def summary_text(result: Any) -> tuple[str, bool]:
    """A tool result's text and whether it reports an error."""
    if isinstance(result, ToolResult):
        text = result.dehydrated()
        return text, result.is_error or text.startswith("ERROR")
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
    return text, text.startswith("ERROR")
