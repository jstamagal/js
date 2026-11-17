"""A resumed session's last exchanges, drawn in the order a turn drew them."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from .control_sequences import clean
from .display import agent_speaker, hang, render_markdown, user_line
from .text_layout import wrap_words
from .tool_display import render_exchange


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    return "\n".join(
        part["text"] for part in content
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def _call_args(call: dict) -> tuple[str, dict]:
    function = call.get("function") or {}
    raw = function.get("arguments")
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        args = None
    return str(function.get("name") or "?"), args if isinstance(args, dict) else {}


def resume_parts(messages: list[dict], count: int, *, width: int,
                 markdown: bool = True) -> Iterator[tuple[str, Any]]:
    """The last `count` exchanges of `messages` in the order a turn drew
    them: ("text", rendered) for the user's line (`user_line`) and the answer
    hung off the agent's mark as Markdown at `width` (plain text when
    `markdown` is off), ("call", (name, args, result)) for each tool call,
    result None when none came back. An exchange starts at a user message
    that was not steered into a turn."""
    starts = [i for i, message in enumerate(messages)
              if message.get("role") == "user" and not message.get("steered")]
    if count <= 0 or not starts:
        return
    tail = messages[starts[-min(count, len(starts))]:]
    results = {message.get("tool_call_id"): message for message in tail
               if message.get("role") == "tool" and message.get("tool_call_id")}
    for message in tail:
        role = message.get("role")
        text = clean(content_text(message.get("content"))).strip("\n")
        if role == "user":
            yield "text", user_line(text)
        elif role == "assistant":
            if text.strip():
                mark, indent = agent_speaker()
                body = render_markdown(text, width - indent) if markdown else wrap_words(text, width - indent) + "\n"
                yield "text", hang(body, mark, indent)
            for call in message.get("tool_calls") or []:
                name, args = _call_args(call)
                result = results.get(call.get("id"))
                yield "call", (name, args, None if result is None else content_text(result.get("content")))


def render_exchanges(messages: list[dict], count: int, *, width: int,
                     level: int, preview: int, markdown: bool = True,
                     settings: Any = None) -> str:
    """`resume_parts` as one string, each tool exchange at TOOLS_DISPLAY
    `level` on its own."""
    out: list[str] = []
    for kind, part in resume_parts(messages, count, width=width, markdown=markdown):
        if kind == "text":
            out.append(part)
        else:
            name, args, result = part
            out.append(render_exchange(name, args, result, level=level, preview=preview, width=width,
                                       settings=settings))
    return "".join(out)
