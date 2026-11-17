"""The history record of a message, and the SDK message it becomes.

A turn keeps its conversation as records: OpenAI-shaped dicts (role,
content, tool_calls, tool_call_id, name) with js's own fields. An assistant
record may carry ``reasoning_content``; signed reasoning as
``reasoning_parts`` with its ``reasoning_layout``, and ``text_before`` on its
tool calls; Claude's ``reasoning_blocks``; ``reasoning_from``, the provider
and model its signed reasoning came from; the reply's ``provider_metadata``;
and an ``incomplete_reason``. A user record may be ``steered`` or a
``resume_nudge``, and a user or tool record may list media (`media.MEDIA_KEY`).

`assistant_record` writes the record of a model reply, `history_to_ai_messages`
turns records into the SDK messages a request sends, and
`drop_signed_reasoning` and `restart_thinking_turn` edit records whose signed
reasoning can no longer be replayed.
"""

from __future__ import annotations

import json
from typing import Any

import ai
import ai.types.messages

# reasoning imports only after settings, which tool_args loads.
from . import media, providers, reasoning, snippets, tool_args
from .text_bytes import byte_prefix, cap_text

# Where js keeps an assistant message's `reasoning_blocks` on the SDK message.
JS_METADATA_KEY = "js"
_REASONING_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})


def valid_reasoning_blocks(blocks: Any) -> list[dict[str, Any]] | None:
    """``blocks`` when it is a non-empty list of thinking blocks, else None."""
    if not isinstance(blocks, list) or not blocks:
        return None
    if not all(isinstance(b, dict) and b.get("type") in _REASONING_BLOCK_TYPES for b in blocks):
        return None
    return blocks


def with_reasoning_blocks(message: ai.messages.Message, blocks: list[dict[str, Any]]) -> ai.messages.Message:
    """``message`` carrying ``blocks`` for a later request to send back."""
    metadata = dict(getattr(message, "provider_metadata", None) or {})
    metadata[JS_METADATA_KEY] = {**dict(metadata.get(JS_METADATA_KEY) or {}), "reasoning_blocks": blocks}
    return message.model_copy(update={"provider_metadata": metadata})


# An assistant message's signed reasoning: the parts with their provider
# metadata, where they sat in the reply, and the provider and model they came
# from (`signed_reasoning_parts`, `reasoning.reasoning_origin`).
SIGNED_REASONING_KEYS = ("reasoning_parts", "reasoning_from", "reasoning_blocks", "reasoning_layout")


def signed_reasoning_parts(message: ai.messages.Message) -> list[dict] | None:
    """The message's reasoning parts as history data, or None when no part
    carries provider metadata (an Anthropic signature, an OpenAI Responses encrypted item).

    Each entry is ``{"text", "provider_metadata"?, "after_calls"?, "text_before"?}``;
    ``after_calls`` counts the tool calls that came before the part in the
    response and ``text_before`` the characters of answer text, so replay puts
    it back between the same calls and at the same place in the text.
    """
    out: list[dict] = []
    calls = 0
    text_before = 0
    signed = False
    for part in message.parts:
        if isinstance(part, ai.types.messages.ToolCallPart):
            calls += 1
        elif isinstance(part, ai.types.messages.TextPart):
            text_before += len(part.text)
        elif isinstance(part, ai.types.messages.ReasoningPart):
            entry: dict[str, Any] = {"text": part.text}
            if part.provider_metadata:
                entry["provider_metadata"] = json.loads(json.dumps(part.provider_metadata, default=str))
                signed = True
            if calls:
                entry["after_calls"] = calls
            if text_before:
                entry["text_before"] = text_before
            out.append(entry)
    return out if signed else None


def tool_call_text_before(message: ai.messages.Message) -> dict[str, int]:
    """The characters of answer text before each tool call that has answer
    text after it in the response, by call id. A call missing here came after
    all of the text."""
    offsets: dict[str, int] = {}
    text_before = 0
    for part in message.parts:
        if isinstance(part, ai.types.messages.ToolCallPart):
            offsets[part.tool_call_id] = text_before
        elif isinstance(part, ai.types.messages.TextPart):
            text_before += len(part.text)
    return {call_id: offset for call_id, offset in offsets.items() if offset < text_before}


def assistant_record(message: ai.messages.Message, *, text: str, tool_calls: list[tuple[str, str, str]],
                     reasoning_content: str, reasoning_blocks: list[dict[str, Any]] | None,
                     provider_metadata: Any, incomplete_reason: str | None,
                     provider_id: str | None, model_id: str | None) -> tuple[dict, ai.messages.Message]:
    """The history record of a model reply, and the SDK message a live turn
    keeps for it.

    ``message`` is the reply as the turn sends it back, its parts in the order
    they came; ``text`` is its answer text and ``tool_calls`` the (id, name,
    arguments) of the calls it makes. ``reasoning_blocks`` are Claude's
    thinking blocks from a chat-completions reply. Signed reasoning is stored
    under ``provider_id`` and ``model_id``. The message returned is ``message``,
    given the reply's ``provider_metadata`` when it carries none of its own."""
    record: dict = {"role": "assistant", "content": text}
    if tool_calls:
        record["tool_calls"] = [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}
            for call_id, name, arguments in tool_calls
        ]
    if reasoning_content:
        record["reasoning_content"] = reasoning_content
    if reasoning_blocks:
        record["reasoning_blocks"] = reasoning_blocks
    if isinstance(provider_metadata, dict):
        # reasoning_blocks are recorded once, at the top level above.
        provider_metadata = {k: v for k, v in provider_metadata.items() if k != JS_METADATA_KEY} or None
    else:
        provider_metadata = None
    if provider_metadata:
        record["provider_metadata"] = provider_metadata
    if incomplete_reason:
        record["incomplete_reason"] = incomplete_reason
    signed = signed_reasoning_parts(message)
    if signed:
        record["reasoning_parts"] = signed
        record["reasoning_layout"] = [
            len(part.text) if isinstance(part, ai.types.messages.TextPart) else part.kind
            for part in message.parts
        ]
        # A signature covers the reply's blocks in their order, so replay puts
        # each call back before the answer text that followed it.
        offsets = tool_call_text_before(message)
        for call in record.get("tool_calls", []):
            if call["id"] in offsets:
                call["text_before"] = offsets[call["id"]]
    if signed or record.get("reasoning_blocks"):
        record["reasoning_from"] = reasoning.reasoning_origin(provider_id, model_id)
    existing_metadata = getattr(message, "provider_metadata", None) or {}
    if provider_metadata and not set(existing_metadata) - {JS_METADATA_KEY}:
        message = message.model_copy(update={"provider_metadata": {**provider_metadata, **existing_metadata}})
    return record, message


def _is_message_part(value: Any) -> bool:
    return hasattr(value, "kind")


def _coerce_parts(content: Any) -> list[Any]:
    """Build ``ai`` message parts from string content or mixed values."""
    if isinstance(content, str):
        return [ai.types.messages.TextPart(text=content)]
    if isinstance(content, (list, tuple)):
        parts: list[Any] = []
        for item in content:
            if isinstance(item, str):
                parts.append(ai.types.messages.TextPart(text=item))
            elif isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(ai.types.messages.TextPart(text=item["text"]))
            elif _is_message_part(item):
                parts.append(item)
            else:
                raise ValueError(f"unsupported message content item: {item!r}")
        return parts
    if _is_message_part(content):
        return [content]
    raise ValueError(f"unsupported message content: {content!r}")


def _replayed_signed_parts(msg: dict, provider_id: str | None, model_id: str | None) -> list[dict] | None:
    parts = msg.get("reasoning_parts")
    if not isinstance(parts, list) or not parts:
        return None
    if not reasoning.replays_signed_reasoning(msg.get("reasoning_from"), provider_id, model_id):
        return None
    return [part for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str)]


def _after_calls(entry: dict, calls: list) -> int:
    """How many of ``calls`` the signed part follows, capped at the calls kept."""
    after = entry.get("after_calls")
    return min(after if isinstance(after, int) and after > 0 else 0, len(calls))


def _text_before(item: dict, default: int) -> int:
    offset = item.get("text_before")
    return offset if isinstance(offset, int) and offset >= 0 else default


def _signed_reply_parts(content: Any, calls: list, signed: list[dict], layout: Any = None) -> list[Any]:
    """An assistant record with signed reasoning as the parts of the response
    it came from, in their order: each tool call at its ``text_before`` offset
    in the answer text (after all of the text when it has none), and each
    signed part between the same calls and at the same place in the text."""
    if not isinstance(content, str):
        parts: list[Any] = [_signed_part(entry) for entry in signed if _after_calls(entry, calls) == 0]
        parts.extend(_coerce_parts(content) if content else [])
        for index, tc in enumerate(calls, start=1):
            parts.append(_tool_call_part(tc))
            parts.extend(_signed_part(entry) for entry in signed if _after_calls(entry, calls) == index)
        return parts
    end = len(content)
    if (isinstance(layout, list)
            and all(type(item) is int and item >= 0 or item in ("reasoning", "tool_call") for item in layout)
            and layout.count("reasoning") == len(signed)
            and layout.count("tool_call") == len(calls)
            and sum(item for item in layout if type(item) is int) == end):
        thoughts = iter(signed)
        tools = iter(calls)
        parts = []
        position = 0
        for item in layout:
            if item == "reasoning":
                parts.append(_signed_part(next(thoughts)))
            elif item == "tool_call":
                parts.append(_tool_call_part(next(tools)))
            else:
                parts.append(ai.types.messages.TextPart(text=content[position:position + item]))
                position += item
        return parts
    # (calls before, text before, 0 for a signed part and 1 for a call, part):
    # a signed part at the same place as a call came before it, or it would
    # count that call.
    items: list[tuple[int, int, int, Any]] = [
        (_after_calls(entry, calls), _text_before(entry, 0), 0, _signed_part(entry)) for entry in signed
    ]
    items += [(index, _text_before(tc, end), 1, _tool_call_part(tc)) for index, tc in enumerate(calls)]
    items.sort(key=lambda item: item[:3])
    parts = []
    position = 0
    for _calls, offset, _kind, part in items:
        offset = min(max(offset, position), end)
        if offset > position:
            parts.append(ai.types.messages.TextPart(text=content[position:offset]))
            position = offset
        parts.append(part)
    if position < end:
        parts.append(ai.types.messages.TextPart(text=content[position:]))
    return parts


def _tool_call_part(tc: dict) -> ai.types.messages.ToolCallPart:
    fn = tc.get("function", {})
    return ai.types.messages.ToolCallPart(
        tool_call_id=tc.get("id", ""),
        tool_name=fn.get("name", ""),
        tool_args=tool_args.sdk_safe_tool_args(fn.get("arguments", "")),
    )


def _signed_part(entry: dict) -> ai.types.messages.ReasoningPart:
    metadata = entry.get("provider_metadata")
    return ai.thinking(entry["text"], provider_metadata=metadata if isinstance(metadata, dict) else None)


def _file_parts(record: dict, vision: bool) -> list[ai.types.messages.FilePart]:
    """The file parts for the media a history record lists; none without
    ``vision``. A part whose bytes the store no longer has is left out."""
    if not vision:
        return []
    parts = []
    for part in record.get(media.MEDIA_KEY) or ():
        data = media.load(part) if media.is_part(part) else None
        if data is not None:
            parts.append(ai.types.messages.FilePart(data=data, media_type=part["mime"]))
    return parts


# The longest tool call id a media label names; the text budgets count every
# label at this width.
MEDIA_LABEL_ID_MAX = 64
# The most bytes of a result's first line a media label repeats.
MEDIA_LABEL_HEAD_MAX = 120


def media_label(tool_call_id: str, text: str) -> str:
    """The text of the user message that carries a tool result's media. A
    batch's media messages all follow its tool messages, so the label names
    the call and the first line of the result's text."""
    head = cap_text(text.split("\n", 1)[0], MEDIA_LABEL_HEAD_MAX, "…")
    return f'[media of tool result {byte_prefix(tool_call_id, MEDIA_LABEL_ID_MAX)}: {head}]'


def history_to_ai_messages(
    system: str, messages: list[dict], *, provider_id: str | None = None,
    model_id: str | None = None, vision: bool = False,
) -> list[ai.messages.Message]:
    """Convert the harness JSONL history into ``ai.messages.Message`` objects.

    The harness history uses OpenAI-shaped dicts. We translate them at the
    provider boundary so the runtime itself keeps the stable JSONL schema.
    OpenAI chat-completions transports replay all reasoning to preserve the
    generated prefix; other transports retain tool-call reasoning only.

    * ``role == "system"`` -> ``ai.system_message(system)``
    * ``role == "user"`` -> ``ai.user_message(content)``
    * ``role == "assistant"`` -> ``ai.assistant_message`` with text,
      reasoning, and tool call parts. Signed reasoning (``reasoning_parts``)
      replays whole, signatures included, when the record's
      ``reasoning_from`` is this ``provider_id`` and ``model_id``; so do
      Claude's thinking blocks (``reasoning_blocks``).
    * ``role == "tool"`` -> ``ai.tool_message`` with a ``ToolResultPart``.

    With ``vision``, the media a user record lists (`js.media`) follow its
    text as file parts. A tool record's media go in a user message after the
    last tool message of its batch: tool results must stay contiguous, and
    the providers' tool-result converters send text only. The same records
    give the same messages on every turn, so the provider's prompt cache
    keeps matching past an image.
    """
    provider = providers.get_provider(provider_id)
    sdk_id = provider.effective_sdk_provider_id if provider is not None else provider_id
    preserve_reasoning = sdk_id == "openai" and (
        provider is None or provider.transport != "custom_responses"
    )
    out: list[ai.messages.Message] = []
    if system:
        out.append(ai.system_message(system))
    batch_media: list[ai.messages.Message] = []

    for msg in messages:
        role = msg.get("role")
        if role != "tool" and batch_media:
            out.extend(batch_media)
            batch_media = []
        if role == "system":
            # History system entries are rare; treat like explicit system text.
            out.append(ai.system_message(str(msg.get("content", ""))))
            continue
        if role == "user":
            content = msg.get("content", "")
            files = _file_parts(msg, vision)
            if not vision and msg.get(media.MEDIA_KEY) and isinstance(content, str):
                content = f"{content}\n{'[Not shown to you: this model accepts text only, so the attached image was dropped.]'}"
            if isinstance(content, str) and not files:
                out.append(ai.user_message(content))
            else:
                out.append(ai.user_message(*(_coerce_parts(content) if content else []), *files))
            continue
        if role == "assistant":
            parts: list[Any] = []
            calls = msg.get("tool_calls", []) or []
            signed = _replayed_signed_parts(msg, provider_id, model_id)
            content = msg.get("content")
            if signed is not None:
                parts.extend(_signed_reply_parts(content, calls, signed, msg.get("reasoning_layout")))
            else:
                reasoning_text = msg.get("reasoning_content")
                if reasoning_text and (preserve_reasoning or msg.get("tool_calls")):
                    parts.append(ai.thinking(str(reasoning_text)))
                if content:
                    parts.extend(_coerce_parts(content))
                parts.extend(_tool_call_part(tc) for tc in calls)
            if not calls and not any(isinstance(part, ai.types.messages.TextPart) and part.text
                                     for part in parts):
                # A reply cancelled while it was still thinking holds only
                # reasoning. Providers refuse an assistant message with no
                # content and no tool calls (llama.cpp: "Assistant message must
                # contain either 'content' or 'tool_calls'"), so it is not sent.
                continue
            assistant = ai.assistant_message(*parts)
            provider_metadata = msg.get("provider_metadata")
            if isinstance(provider_metadata, dict):
                # The record's top-level reasoning_blocks is the one copy js
                # replays; a js entry here is never read.
                provider_metadata = {k: v for k, v in provider_metadata.items() if k != JS_METADATA_KEY}
                if provider_metadata:
                    assistant = assistant.model_copy(update={"provider_metadata": provider_metadata})
            # Claude's thinking blocks are signed: they go back only to the
            # provider and model that wrote them, as signed reasoning parts do.
            blocks = valid_reasoning_blocks(msg.get("reasoning_blocks"))
            if blocks is not None and reasoning.replays_signed_reasoning(
                    msg.get("reasoning_from"), provider_id, model_id):
                assistant = with_reasoning_blocks(assistant, blocks)
            out.append(assistant)
            continue
        if role == "tool":
            content = msg.get("content")
            if not vision and msg.get(media.MEDIA_KEY) and isinstance(content, str):
                content = f"{content}\n{'[Not shown to you: this model accepts text only, so the attached image was dropped.]'}"
            is_error = isinstance(content, str) and content.startswith("ERROR")
            out.append(
                ai.tool_message(
                    tool_call_id=msg.get("tool_call_id", ""),
                    tool_name=msg.get("name", ""),
                    result=content,
                    is_error=is_error,
                )
            )
            files = _file_parts(msg, vision)
            if files:
                label = media_label(str(msg.get("tool_call_id", "")), str(content or ""))
                batch_media.append(ai.user_message(ai.types.messages.TextPart(text=label), *files))
            continue
        raise ValueError(f"unsupported history role: {role!r}")

    out.extend(batch_media)
    return out


def drop_signed_reasoning(messages: list[dict], start: int = 0) -> int:
    """Remove the signed reasoning of every message from ``start`` on, in place.

    A signature (an Anthropic thinking signature, an OpenAI Responses encrypted item) is
    bound to the history before it, so once js edits an earlier message the
    signed reasoning after that edit cannot be replayed. Each changed message
    is replaced by a copy that keeps ``reasoning_content``. Returns how many
    messages changed.
    """
    changed = 0
    for index in range(max(0, start), len(messages)):
        msg = messages[index]
        if isinstance(msg, dict) and any(key in msg for key in SIGNED_REASONING_KEYS):
            messages[index] = {k: v for k, v in msg.items() if k not in SIGNED_REASONING_KEYS}
            changed += 1
    return changed


def restart_thinking_turn(messages: list[dict], *, require_tool_calls: bool) -> int:
    """Recast an unrestorable assistant turn as user context with completed results.

    Earlier turns keep their signed blocks. The new user context contains the
    visible replies, calls and results so the next reply can use work already done.
    """
    from . import memory

    latest = next((i for i in range(len(messages) - 1, -1, -1)
                   if messages[i].get("role") == "assistant"), None)
    if latest is None or (require_tool_calls and not messages[latest].get("tool_calls")):
        return 0

    def opens_turn(msg: dict) -> bool:
        return msg.get("role") == "user" and not msg.get("steered") and not msg.get("resume_nudge")

    start = 0
    for index in range(latest - 1, -1, -1):
        msg = messages[index]
        if opens_turn(msg):
            start = index + 1
            break
    end = next((i for i in range(latest + 1, len(messages)) if opens_turn(messages[i])), len(messages))
    tail = messages[start:end]
    rewritten: list[dict] = []
    context: list[dict] = []

    def flush_context() -> None:
        if context:
            rewritten.append(memory.note_time({"role": "user", "content": (
                snippets.render('flush-context')
                + json.dumps(context, ensure_ascii=False)
            )}))
            context.clear()

    for msg in tail:
        if msg.get("role") == "user":
            flush_context()
            rewritten.append(msg)
        else:
            context.append({key: value for key, value in msg.items()
                            if key in {"role", "content", "tool_calls", "tool_call_id", "name"}})
    flush_context()
    messages[start:end] = rewritten
    return len(tail)
