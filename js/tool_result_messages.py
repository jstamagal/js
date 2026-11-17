"""A tool result as the model gets it: the text a provider reads, the history record it is kept as, and the SDK messages that carry it, images and mixed blocks included."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import ai
import ai.types.messages

from . import history_record, media
from .history_record import MEDIA_LABEL_ID_MAX, media_label


def strip_reasoning_parts(messages: list[ai.messages.Message]) -> list[ai.messages.Message]:
    """Drop reasoning parts for models whose chat-completions API rejects them.

    The caller applies ``reasoning.rejects_reasoning_replay`` before using this
    projection. Accepting models retain reasoning for prefix-stable replay.
    """
    out: list[ai.messages.Message] = []
    for msg in messages:
        if msg.role != "assistant" or not any(
            isinstance(part, ai.types.messages.ReasoningPart) for part in msg.parts
        ):
            out.append(msg)
            continue
        kept = [part for part in msg.parts if not isinstance(part, ai.types.messages.ReasoningPart)]
        if kept:
            out.append(msg.model_copy(update={"parts": kept}))
    return out


def without_empty_text_parts(messages: list[ai.messages.Message]) -> list[ai.messages.Message]:
    out: list[ai.messages.Message] = []
    for message in messages:
        parts = [part for part in message.parts
                 if not (isinstance(part, ai.types.messages.TextPart) and not part.text)]
        if parts:
            out.append(message if len(parts) == len(message.parts)
                       else message.model_copy(update={"parts": parts}))
    return out


def _image_result_payload(result: str) -> tuple[str, str, bytes] | str | None:
    if not isinstance(result, str) or not result.startswith(media.IMAGE_RESULT_PREFIX):
        return None
    parts = result.split("\t", 3)
    if len(parts) != 4:
        return 'ERROR: malformed image result marker'
    _, raw_path, mime, stub = parts
    try:
        data = Path(raw_path).read_bytes()
    except OSError as exc:
        return _visual_read_error(raw_path, exc)
    return stub, mime, data


def _visual_read_error(raw_path: str, exc: OSError) -> str:
    return f'ERROR: could not read visual result {raw_path}: {exc}'


def _mixed_result_parts(result: Any) -> tuple[str, list[Any], bool]:
    from .toolkit.core import ToolResult

    if not isinstance(result, ToolResult):
        return str(result), [], False
    text: list[str] = []
    files: list[Any] = []
    for block in result.blocks:
        kind = block.get("type")
        if kind == "text":
            text.append(str(block.get("text", "")))
        elif kind in {"image", "audio"}:
            try:
                data = base64.b64decode(str(block.get("data", "")), validate=True)
            except (ValueError, TypeError):
                text.append(f'ERROR: invalid {kind} content')
                continue
            mime = str(block.get("mimeType", "application/octet-stream"))
            text.append(f'[{kind} {mime} attached]')
            files.append(ai.types.messages.FilePart(data=data, media_type=mime))
        elif kind == "resource":
            resource = block.get("resource")
            if not isinstance(resource, dict):
                continue
            uri = resource.get("uri", "")
            if isinstance(resource.get("text"), str):
                text.append('[embedded resource {uri}]\n{resource_text}'.format(uri=uri, resource_text=resource['text']))
            elif resource.get("blob") is not None:
                try:
                    data = base64.b64decode(str(resource["blob"]), validate=True)
                except (ValueError, TypeError):
                    text.append(f'ERROR: invalid embedded resource {uri}')
                    continue
                mime = str(resource.get("mimeType", "application/octet-stream"))
                text.append(f'[embedded resource {uri} {mime} attached]')
                files.append(ai.types.messages.FilePart(data=data, media_type=mime))
        elif kind == "resource_link":
            text.append('[resource link {block_get_name}: {block_get_uri}]'.format(block_get_name=block.get('name', ''), block_get_uri=block.get('uri', '')))
        elif kind == "structured":
            text.append(json.dumps(block.get("value"), ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    joined = "\n".join(text)
    if result.is_error and not joined.startswith("ERROR"):
        # A history tool record is an error when its text says so.
        joined = f"ERROR: {joined}"
    return joined, files, result.is_error


def provider_text(result: Any) -> str:
    """Every text the provider gets for ``result``, joined: the tool
    message's, and with media the user message's, its label counted with the
    longest call id. A native read's image marker is its stub and the label,
    as `build_tool_result_messages` sends it."""
    widest = "x" * MEDIA_LABEL_ID_MAX
    if isinstance(result, str) and result.startswith(media.IMAGE_RESULT_PREFIX):
        parts = result.split("\t", 3)
        if len(parts) != 4:
            return 'ERROR: malformed image result marker'
        try:
            with open(parts[1], "rb"):
                pass
        except OSError as exc:
            # The file is gone or unreadable: the error is what is sent.
            return _visual_read_error(parts[1], exc)
        return f"{parts[3]}\n{media_label(widest, parts[3])}"
    text, files, _is_error = _mixed_result_parts(result)
    return f"{text}\n{media_label(widest, text)}" if files else text


def tool_result_record(tool_call_id: str, tool_name: str, result: Any) -> dict:
    """The history record of a completed tool call: its text, and the media
    it returned (a native read's image, a tool's image or audio blocks) kept
    in `js.media` and listed under ``media.MEDIA_KEY``. The model gets the
    record through `history_record.history_to_ai_messages`, this turn and every later one."""
    record = {"role": "tool", "tool_call_id": tool_call_id, "name": tool_name}
    files: list[tuple[bytes, str]] = []
    if isinstance(result, str):
        payload = _image_result_payload(result)
        if payload is None:
            text = result
        elif isinstance(payload, str):
            text = payload
        else:
            text, mime, data = payload
            files.append((data, mime))
    else:
        text, parts, _is_error = _mixed_result_parts(result)
        files.extend((part.data, part.media_type) for part in parts)
    record["content"] = text
    if files:
        record[media.MEDIA_KEY] = [media.store(data, mime) for data, mime in files]
    return record


def build_tool_result_messages(
    tool_call_id: str, tool_name: str, result: Any, *, vision: bool = True,
) -> list[ai.messages.Message]:
    """Provider-facing messages for a completed tool call: its
    `tool_result_record` as `history_record.history_to_ai_messages` sends it."""
    return history_record.history_to_ai_messages("", [tool_result_record(tool_call_id, tool_name, result)], vision=vision)
