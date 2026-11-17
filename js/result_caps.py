"""A tool result held to the byte caps a turn gives it: text cut or spilled, media fitted or dropped, and a batch's results sharing the batch cap."""

from __future__ import annotations

import base64
import math
import os
from dataclasses import replace
from typing import Any

from . import imagefit, tool_result_messages, toolkit as T
from .media import IMAGE_RESULT_PREFIX
from .text_bytes import byte_size, cap_text, well_formed
from .toolkit.core import Output, ToolContext, ToolResult
from .toolkit.results import cap_result_text, spill_oversized_result

# Blocks a model sees as media, not text: TOOL_MEDIA_MAX caps them, and the
# text budgets (TOOL_RESULT_MAX, PARALLEL_RESULTS_MAX) do not count them. An
# embedded resource with a blob is media too: it is attached the same way,
# except a text/* blob, which the SDK sends as text (`_text_blobs_as_text`).
_MEDIA_BLOCKS = frozenset({"image", "audio"})


def _text_blobs_as_text(result: ToolResult) -> ToolResult:
    """``result`` with each embedded resource whose blob is text/* given as
    its decoded text, so both text budgets count and clip it; unchanged (same
    object) when there is none. A blob that is not valid base64 stays."""
    blocks: list[dict] = []
    changed = False
    for block in result.blocks:
        resource = block.get("resource") if block.get("type") == "resource" else None
        if (isinstance(resource, dict) and resource.get("blob") is not None
                and not isinstance(resource.get("text"), str)
                and str(resource.get("mimeType", "")).lower().startswith("text/")):
            try:
                data = base64.b64decode(str(resource["blob"]), validate=True)
            except (ValueError, TypeError):
                blocks.append(block)
                continue
            text = data.decode("utf-8", "replace")
            blocks.append({**block, "resource": {k: v for k, v in resource.items() if k != "blob"} | {"text": text}})
            changed = True
        else:
            blocks.append(block)
    return replace(result, blocks=blocks) if changed else result


def _media_payload(block: dict) -> str | None:
    """The base64 data of a media block, or None for a block that is not media."""
    kind = block.get("type")
    if kind in _MEDIA_BLOCKS:
        return str(block.get("data", ""))
    resource = block.get("resource") if kind == "resource" else None
    if isinstance(resource, dict) and not isinstance(resource.get("text"), str) and resource.get("blob") is not None:
        return str(resource["blob"])
    return None


def _final_texts(result: ToolResult) -> tuple[str, str]:
    """The text ``result`` becomes: what the provider gets (media rendered
    as their metadata lines) and what the session keeps."""
    return tool_result_messages.provider_text(result), result.dehydrated()


def _tool_result_content_size(result: ToolResult) -> int:
    """The text bytes ``result`` costs, measured on its final forms: the
    provider text and the persisted text, whichever is longer. A media
    block's data is not text; its metadata line is."""
    return max(byte_size(text) for text in _final_texts(result))


def _result_size(result: Any) -> int:
    if isinstance(result, ToolResult):
        return _tool_result_content_size(result)
    if _image_marker_stub(result) is not None:
        return byte_size(tool_result_messages.provider_text(result))
    return byte_size(result)


# The mark on a notice `_cap_media` writes. An object, not a value: a tool or
# MCP server can put any JSON in a block, but never this.
_MEDIA_NOTICE = object()


# The longest one notice gets, so the notices fit a text budget.
_MEDIA_NOTICE_MAX = 240


def _clip_name(text: str) -> str:
    return text if len(text) <= 48 else text[:47] + "…"


def _is_media_notice(block: dict) -> bool:
    return block.get("media_notice") is _MEDIA_NOTICE


def _without_notice_marks(result: ToolResult) -> ToolResult:
    """``result`` with any ``media_notice`` key a tool or server sent dropped."""
    if not any("media_notice" in block for block in result.blocks):
        return result
    return replace(result, blocks=[{k: v for k, v in block.items() if k != "media_notice"}
                                   for block in result.blocks])


def _media_notice(kind: str, mime: str, reason: str) -> str:
    """js's one notice for a media block it did not send."""
    notice = f'[{_clip_name(str(kind))} {_clip_name(str(mime))} dropped: {reason}]'
    return cap_text(notice, _MEDIA_NOTICE_MAX, "]")


def _media_names(block: dict) -> tuple[str, str]:
    resource = block.get("resource") if block.get("type") == "resource" else None
    if isinstance(resource, dict):
        return ('embedded resource {resource_get_uri}'.format(resource_get_uri=resource.get('uri', '')),
                str(resource.get("mimeType", "application/octet-stream")))
    return str(block.get("type")), str(block.get("mimeType", "application/octet-stream"))


def _with_short_names(block: dict) -> dict:
    """A media block with the names a server supplies cut short, so its
    metadata line stays small."""
    out = dict(block)
    if "mimeType" in out:
        out["mimeType"] = _clip_name(str(out["mimeType"]))
    resource = out.get("resource")
    if isinstance(resource, dict):
        out["resource"] = {**resource, **{key: _clip_name(str(resource[key]))
                                          for key in ("uri", "mimeType", "name") if key in resource}}
    return out


def _overflow(blocks: list[dict], budget: int, is_error: bool) -> int:
    """How many bytes ``blocks`` go over ``budget``; 0 when they fit."""
    return max(0, _tool_result_content_size(ToolResult(blocks, is_error=is_error)) - budget)


def _dehydrate_capped_result(result: ToolResult, budget: int, marker: str) -> ToolResult:
    """``result`` packed into ``budget`` bytes of final text: js's media
    notices first, then the media blocks whose metadata lines still fit with
    room left for a notice of the ones that do not, then the rest of the text
    clipped to the room left. Every byte, separators and an error prefix
    included, is inside the budget."""
    is_error = result.is_error
    media = [_with_short_names(block) for block in result.blocks if _media_payload(block) is not None]
    notices = [str(block.get("text", "")) for block in result.blocks if _is_media_notice(block)]
    text = ToolResult([block for block in result.blocks
                       if _media_payload(block) is None and not _is_media_notice(block)],
                      is_error=is_error).dehydrated()
    kept: list[dict] = []
    if notices:
        joined = "\n".join(notices)
        cap = budget
        while cap > 0:
            notice = {"type": "text", "text": cap_text(joined, cap, "]"), "media_notice": _MEDIA_NOTICE}
            over = _overflow([notice], budget, is_error)
            if not over:
                kept.append(notice)
                break
            cap -= over
    dropped: list[dict] = []
    if not _overflow([*kept, *media], budget, is_error):
        kept.extend(media)
    else:
        # Some media will not fit: room for the bare marker that says so is
        # kept free first, and media fill what is left.
        bare = {"type": "text", "text": '[media dropped]', "media_notice": _MEDIA_NOTICE}
        for block in media:
            if not _overflow([*kept, block, bare], budget, is_error):
                kept.append(block)
            else:
                dropped.append(block)
    if dropped:
        # A media block with no room still says so: its notice, cut to the
        # room there is, at the least a bare marker.
        joined = "\n".join(_media_notice(*_media_names(block), f'no room in a {budget}-byte result')
                           for block in dropped)
        cap = budget - _tool_result_content_size(ToolResult(kept, is_error=is_error)) - (1 if kept else 0)
        for wording in (joined, '[media dropped]'):
            notice = {"type": "text", "text": cap_text(wording, max(cap, 0), "]"), "media_notice": _MEDIA_NOTICE}
            if notice["text"] and not _overflow([*kept, notice], budget, is_error):
                kept.append(notice)
                break
    room = budget - _tool_result_content_size(ToolResult(kept, is_error=is_error)) - (1 if kept else 0)
    while room > 0:
        block = {"type": "text", "text": cap_text(text + marker, room, marker)}
        over = _overflow([block, *kept], budget, is_error) if block["text"] else room
        if not over:
            kept.insert(0, block)
            break
        room -= over
    if not kept:
        kept = [{"type": "text", "text": ""}]
    if _overflow(kept, budget, is_error):
        # A budget under the "ERROR: " an error result is persisted with: the
        # failure is said in what room there is, without the prefix.
        return ToolResult.text(cap_text("ERROR", budget, ""))
    return ToolResult(kept, is_error=is_error)


def _limit(setting: int | None) -> int | None:
    """A cap setting as the cap helpers take it: None (no cap) for a setting
    of 0 or less, the bytes otherwise. Inside the helpers 0 means zero."""
    return None if setting is None or setting <= 0 else setting


def _fit_images(result: ToolResult) -> ToolResult:
    """``result`` with each image block over imagefit's limits replaced by its
    fitted copy, followed by the note that says what changed."""
    blocks: list[dict] = []
    for block in result.blocks:
        blocks.append(block)
        mime = str(block.get("mimeType", ""))
        if block.get("type") != "image" or not mime.startswith("image/"):
            continue
        try:
            data = base64.b64decode(str(block.get("data", "")), validate=True)
        except (ValueError, TypeError):
            continue
        fitted = imagefit.fit(data, mime)
        if fitted is None or not fitted.changed:
            continue
        blocks[-1] = {**block, "data": base64.b64encode(fitted.data).decode("ascii"), "mimeType": fitted.mime}
        blocks.append({"type": "text", "media_notice": _MEDIA_NOTICE, "text": fitted.note})
    return result if blocks == result.blocks else replace(result, blocks=blocks)


def _cap_media(result: ToolResult, media_max: int | None) -> ToolResult:
    """``result`` with each media block over ``media_max`` bytes of data
    replaced by js's notice that says so. None = unlimited."""
    if media_max is None:
        return result
    blocks: list[dict] = []
    for block in result.blocks:
        payload = _media_payload(block)
        size = byte_size(payload) if payload is not None else 0
        if size > media_max:
            kind, mime = _media_names(block)
            blocks.append({"type": "text", "media_notice": _MEDIA_NOTICE, "text": _media_notice(
                kind, mime, f'{size} bytes is over TOOL_MEDIA_MAX ({media_max}); capture a smaller region or a lower resolution')})
        else:
            blocks.append(block)
    return result if blocks == result.blocks else replace(result, blocks=blocks)


def _image_marker_stub(result: Any) -> str | None:
    """The text a native read's image marker becomes, or None for any other result."""
    if not isinstance(result, str) or not result.startswith(IMAGE_RESULT_PREFIX):
        return None
    parts = result.split("\t", 3)
    return parts[3] if len(parts) == 4 else ""


def _cap_image_marker(result: str, cap_bytes: int | None, media_max: int | None) -> str:
    """A native read's image marker under the same rules as a media block:
    over ``media_max`` bytes of base64 data, or with no room for its text in
    ``cap_bytes``, it becomes js's notice, cut to ``cap_bytes``. None = no cap;
    0 means zero bytes."""
    parts = result.split("\t", 3)
    if len(parts) != 4:
        result = well_formed(result)
        return cap_text(result, cap_bytes, "]") if cap_bytes is not None else result
    _prefix, path, mime, stub = parts
    # The path stays as the filesystem names it (the file is read by it); the
    # text the model and the session see is made strictly UTF-8.
    mime, stub = well_formed(mime), well_formed(stub)
    result = f"{IMAGE_RESULT_PREFIX}{path}\t{mime}\t{stub}"
    try:
        size = 4 * math.ceil(os.path.getsize(path) / 3)
    except OSError:
        size = 0
    notice = None
    if media_max is not None and size > media_max:
        notice = _media_notice("image", mime, f'{size} bytes is over TOOL_MEDIA_MAX ({media_max}); capture a smaller region or a lower resolution')
    elif cap_bytes is not None and byte_size(tool_result_messages.provider_text(result)) > cap_bytes:
        notice = _media_notice("image", mime, f'no room in a {cap_bytes}-byte result')
    if notice is None:
        return result
    return cap_text(notice, cap_bytes, "]") if cap_bytes is not None else notice


def _well_formed_key(key: Any, taken: set) -> Any:
    """A dict key made encodable without merging it into another key: a lone
    surrogate is spelled `\\udcff`, and a clash with a key already present
    gets `~` appended until it is unique."""
    if not isinstance(key, str) or well_formed(key) is key:
        return key
    fixed = key.encode("utf-8", "backslashreplace").decode("utf-8")
    while fixed in taken:
        fixed += "~"
    return fixed


def _well_formed_value(value: Any) -> Any:
    """``value`` with every string in it made strictly UTF-8 encodable."""
    if isinstance(value, str):
        return well_formed(value)
    if isinstance(value, dict):
        taken = {k for k in value if not isinstance(k, str) or well_formed(k) is k}
        out: dict = {}
        for k, v in value.items():
            nk = _well_formed_key(k, taken)
            taken.add(nk)
            out[nk] = _well_formed_value(v)
        return out
    if isinstance(value, (list, tuple)):
        return type(value)(_well_formed_value(v) for v in value)
    return value


def _well_formed_result(result: ToolResult) -> ToolResult:
    """``result`` with lone surrogates in its text, structured and resource
    blocks replaced by `?`; unchanged (same object) when there are none."""
    blocks = [_well_formed_value(block) for block in result.blocks]
    if blocks == result.blocks:
        return result
    return replace(result, blocks=blocks)


def cap_result(result: Any, cap_bytes: int, inline_cap: int | None = None,
                context: ToolContext | None = None) -> Any:
    """Spill-then-clip a tool result.

    inline_cap (TOOL_RESULT_INLINE_MAX) sends anything larger to a
    file first, so what the model loses is only its position in the text, not
    the text. cap_bytes bounds the preview after that; a recovery notice alone
    may exceed it so the full result remains reachable.

    Original contract below: clip to the byte cap, leaving a visible marker when the slice
    actually shortens it — same wording the subagent layer uses (subagents.py) so a
    truncated leaf result never looks like the tool simply stopped early. A cap of
    0 or less means unlimited (matches the settings convention)."""
    if isinstance(result, ToolResult):
        result = _without_notice_marks(_well_formed_result(_text_blobs_as_text(result)))
        # TOOL_MEDIA_MAX of the context running the call.
        media_context = context if context is not None else T.STOCK_CONTEXT
        result = _cap_media(_fit_images(result), _limit(int(getattr(media_context, "max_tool_media_bytes", 0) or 0)))
        if cap_bytes > 0 and _tool_result_content_size(result) > cap_bytes:
            marker = f'\n[truncated: TOOL_RESULT_MAX ({cap_bytes}) reached]'
            return _dehydrate_capped_result(result, cap_bytes, marker)
        return result
    if not isinstance(result, str):
        return result
    if _image_marker_stub(result) is not None:
        media_context = context if context is not None else T.STOCK_CONTEXT
        return _cap_image_marker(result, _limit(cap_bytes),
                                 _limit(int(getattr(media_context, "max_tool_media_bytes", 0) or 0)))
    # A lone surrogate (surrogateescape read, json.loads) would crash whichever
    # strict encode meets it next; it reaches the model as `?`.
    is_content = isinstance(result, Output)
    result = well_formed(result)
    if is_content and not isinstance(result, Output):
        result = Output(result)
    if inline_cap is None:
        active_context = context if context is not None else T.STOCK_CONTEXT
        inline_cap = int(getattr(active_context, "max_tool_result_inline_bytes", 0) or 0)
    if inline_cap > 0:
        result = spill_oversized_result(result, inline_cap)
    if cap_bytes > 0:
        result = cap_result_text(result, cap_bytes, f'\n[truncated: TOOL_RESULT_MAX ({cap_bytes}) reached]')
    return Output(result) if is_content and not isinstance(result, Output) else result


def _fair_share_ceiling(sizes: list[int], budget: int) -> int:
    """Largest per-result allowance L where sum(min(size, L)) <= budget.

    Water-filling: small results are paid in full and never clipped, the budget
    left over is split evenly among whatever is still oversized. Returns -1 when
    everything already fits.
    """
    remaining = budget
    for i, size in enumerate(sorted(sizes)):
        share = remaining // (len(sizes) - i)
        if size > share:
            return share
        remaining -= size
    return -1


def cap_batch_results(results: list[Any], cap_bytes: int) -> list[Any]:
    """Clip one turn's batch of tool results down to an aggregate budget.

    The per-result cap (max_tool_result_bytes) can't stop N parallel calls from
    each returning a legal 256 KB and burying the turn under 2 MB, so the batch
    gets its own ceiling. Biggest results lose bytes first — a turn of one fat
    result and six small ones only clips the fat one. 0 or less = unlimited.
    """
    if cap_bytes <= 0:
        return results
    sizes = [_result_size(r) for r in results]
    if sum(sizes) <= cap_bytes:
        return results
    marker = f'\n[truncated: PARALLEL_RESULTS_MAX ({cap_bytes}) reached]'
    allowance = _fair_share_ceiling(sizes, cap_bytes)
    if allowance < 0:
        return results
    capped: list[Any] = []
    for result, size in zip(results, sizes, strict=True):
        if size <= allowance:
            capped.append(result)
        elif isinstance(result, ToolResult):
            capped.append(_dehydrate_capped_result(result, allowance, marker))
        elif _image_marker_stub(result) is not None:
            capped.append(_cap_image_marker(result, allowance, None))
        else:
            text = cap_result_text(result, allowance, marker)
            capped.append(Output(text) if isinstance(result, Output) and not isinstance(text, Output) else text)
    return capped
