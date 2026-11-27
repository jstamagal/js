"""The summary a compaction replaces history with: the operator's hint command, the prompt, and the summarizer's call.

The summary request is the conversation's last request extended to the
current history, with the summary instructions as one more user message
(`replay_messages`). That request starts with the prefix the provider just
cached, so the summary reads it from the cache, and the summarizer sees every
tool result whole. Without such a request (another summarizer model, a history
rewritten since, ``COMPACTION_REUSE_CACHE`` off), or when it does not fit, the
history goes out as a plain-text transcript in a request of its own."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from typing import Any

import ai

from . import (
    compaction_flight,
    compaction_settings,
    media,
    messages as msgs,
    model_client,
    model_metadata,
    provider_errors,
    retry,
    routing,
    settings as _settings,
    snippets,
    usage as usage_mod,
)
from .capped_process import CappedProcessResult, run_capped, truncation_marker
from .compaction_flight import ACTIVE_FLIGHT
from .config import Config


def _pre_hook_command(cfg: Config) -> str:
    raw = compaction_settings.get(cfg, "COMPACTION_HINT_COMMAND")
    if not isinstance(raw, str):
        return ""
    return raw.strip()


_COMPACTION_HEADINGS = (
    'Goal',
    'Decisions and rationale',
    'Files and code',
    'Commands and outcomes',
    'Errors and fixes',
    'Pending and next step',
)


# Bound recursive partitions when the summary request itself overflows.
_SUMMARY_SPLIT_DEPTH = 3


def run_pre_hook(cfg: Config) -> str:
    """The compact pre-hook's stdout, the guidance for the summarizer. A hook
    that fails or times out warns the user and gives no guidance: its error
    text is not instructions for the model."""
    hook = _pre_hook_command(cfg)
    if not hook:
        return ""
    shell_path = compaction_settings.get(cfg, "COMPACTION_HINT_SHELL_PROGRAM") or os.environ.get("SHELL", "/bin/sh")
    cap = int(_settings.knob_attr(cfg, "max_bash_output_bytes", "SHELL_OUTPUT_MAX"))
    try:
        result = run_capped(
            [shell_path, "-c", hook],
            timeout=compaction_settings.get_int(cfg, "COMPACTION_HINT_TIMEOUT"),
            cwd=str(getattr(cfg, "project_dir", os.getcwd())),
            env=None,
            cap=cap,
        )
    except Exception as exc:  # noqa: BLE001
        compaction_flight.notice(msgs.FAILED_WARN, error=f"COMPACTION_HINT_COMMAND failed: {type(exc).__name__}: {exc}")
        return ""
    if not isinstance(result, CappedProcessResult):
        result = CappedProcessResult(result[0], result[1], result[2])
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    if result.stdout_truncated:
        stdout = stdout.rstrip("\n") + f"\n{truncation_marker(cap)}\n"
    if result.stderr_truncated:
        stderr = stderr.rstrip("\n") + f"\n{truncation_marker(cap)}\n"
    if result.returncode != 0:
        compaction_flight.notice(msgs.FAILED_WARN,
                  error=f"COMPACTION_HINT_COMMAND exited {result.returncode}: {(stderr or stdout).strip()}")
        return ""
    return stdout.strip()


_SUMMARY_OPEN = "<compaction-summary>"


_FILES_OPEN = "<post-compaction-files>"


def _clip(text: str, limit: int) -> str:
    """``text`` cut to its head and tail when it is longer than ``limit``."""
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit // 2
    return f'{text[:head]}\n[... {len(text) - limit} chars clipped ...]\n{text[len(text) - (limit - head):]}'


def _part_text(part: Any) -> str:
    if isinstance(part, str):
        return part
    text = part.get("text") if isinstance(part, dict) else getattr(part, "text", None)
    if isinstance(text, str):
        return text
    kind = part.get("type") if isinstance(part, dict) else type(part).__name__
    return '[{kind_or_non_text} part]'.format(kind_or_non_text=kind or 'non-text')


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, (list, tuple)):
        return "\n".join(text for text in (_part_text(p) for p in content) if text)
    return _part_text(content)


def serialize_conversation(messages: list[dict], *, tool_result_chars: int) -> str:
    """The history as plain text for a summary request.

    Tool results, tool-call arguments and reasoning longer than
    ``tool_result_chars`` keep their head and tail. A re-attached files message
    is reduced to the paths it carried."""
    parts: list[str] = []
    for message in messages:
        role = message.get("role")
        text = _content_text(message.get("content"))
        if role == "user":
            if text.startswith(_FILES_OPEN):
                paths = re.findall(r"^### (.+)$", text, re.MULTILINE)
                parts.append('[Files re-attached after the previous summary]: ' + ", ".join(paths))
            elif text:
                parts.append(f'[User]: {text}')
        elif role == "assistant":
            reasoning = message.get("reasoning_content")
            if reasoning:
                parts.append(f'[Assistant thinking]: {_clip(str(reasoning), tool_result_chars)}')
            if text:
                parts.append(f'[Assistant]: {text}')
            calls = []
            for call in message.get("tool_calls") or []:
                fn = call.get("function") or {}
                calls.append('{fn_get_name}({value_1})'.format(fn_get_name=fn.get('name', ''), value_1=_clip(str(fn.get('arguments', '')), tool_result_chars)))
            if calls:
                parts.append('[Assistant tool calls]: ' + "; ".join(calls))
        elif role == "tool":
            if text.startswith(media.IMAGE_RESULT_PREFIX):
                text = '[image result]'
            parts.append('[Tool result {message_get_name}]: {clip_text_tool_result_chars}'.format(message_get_name=message.get('name', ''), clip_text_tool_result_chars=_clip(text, tool_result_chars)))
        elif role == "system" and text:
            parts.append(f'[System]: {text}')
    return "\n\n".join(parts)


def _split_previous_summary(messages: list[dict]) -> tuple[str, list[dict]]:
    """(previous summary text, the messages without it)."""
    previous: list[str] = []
    rest: list[dict] = []
    for message in messages:
        content = message.get("content")
        if message.get("role") == "user" and isinstance(content, str) and content.startswith(_SUMMARY_OPEN):
            body = content[len(_SUMMARY_OPEN):]
            previous.append(body.removesuffix("</compaction-summary>").strip())
        else:
            rest.append(message)
    return "\n\n".join(previous), rest


def _summary_prompt(conversation: str, previous: str, focus: str, guidance: str, *,
                    tool_result_chars: int) -> str:
    headings = "\n".join(f'## {h}' for h in _COMPACTION_HEADINGS)
    body = f'<conversation>\n{conversation}\n</conversation>\n'
    if previous:
        body += f'\n<previous-summary>\n{previous}\n</previous-summary>\n'
        task = (
            snippets.render('compaction-update')
        )
    else:
        task = snippets.render('compaction-start')
    clipped = (snippets.render('compaction-clipping', CHARACTERS=tool_result_chars) if tool_result_chars > 0 else "")
    extra = ""
    if focus:
        extra += f'\nFocus: {focus.strip()}\n'
    if guidance:
        extra += f'\nPre-hook guidance/stdout:\n{guidance}\n'
    return (
        snippets.render('compaction-summary', CLIPPED=clipped, BODY=body, TASK=task, HEADINGS=headings, EXTRA=extra)
    )


def replay_messages(cached: Any, history: Sequence[dict]) -> list | None:
    """The SDK messages of ``cached`` (a `cache_keepalive.CachedRequest`)
    followed by those of the records ``history`` has added since, or None
    when ``history`` no longer starts with the records that request was built
    from."""
    if cached is None or cached.render is None:
        return None
    sent = cached.history
    if len(history) < len(sent) or any(a != b for a, b in zip(sent, history, strict=False)):
        return None
    added = list(history[len(sent):])
    return [*cached.messages, *(cached.render(added) if added else [])]


def _replay_prompt(history: Sequence[dict], focus: str, guidance: str) -> str:
    headings = "\n".join(f'## {h}' for h in _COMPACTION_HEADINGS)
    has_previous = any(
        m.get("role") == "user" and isinstance(m.get("content"), str) and m["content"].startswith(_SUMMARY_OPEN)
        for m in history
    )
    task = snippets.render('compaction-replay-update' if has_previous else 'compaction-start')
    extra = ""
    if focus:
        extra += f'\nFocus: {focus.strip()}\n'
    if guidance:
        extra += f'\nPre-hook guidance/stdout:\n{guidance}\n'
    return snippets.render('compaction-replay', TASK=task, HEADINGS=headings, EXTRA=extra)


def _summary_text(result: Any) -> str | None:
    """The summary in ``result``, or None when the reply is empty or calls a
    tool; raises ValueError when it is cut short."""
    incomplete = (getattr(result, "incomplete_reason", None)
                  or provider_errors.incomplete_reason_from_metadata(getattr(result, "provider_metadata", None)))
    finish = str(getattr(result, "finish_reason", ""))
    if incomplete or finish.startswith("incomplete") or provider_errors.is_max_output_incomplete(finish):
        raise ValueError(msgs.SUMMARY_RESPONSE_INCOMPLETE.words(value=incomplete or finish))
    text = result.text.strip()
    if not text or getattr(result, "tool_calls", None):
        return None
    return text


def _record_response(result: Any, *, model: str, provider_id: str | None, cfg: Config) -> None:
    usage_mod.record(getattr(result, "usage", None), model=model, provider_id=provider_id,
                     session_file=getattr(cfg, "session_file", None))
    if (flight := ACTIVE_FLIGHT.get()) is not None:
        flight.record("summary_response", text=result.text,
                      usage=getattr(result, "usage", None),
                      finish_reason=getattr(result, "finish_reason", None),
                      incomplete_reason=getattr(result, "incomplete_reason", None),
                      provider_metadata=getattr(result, "provider_metadata", None))


def _max_output(cfg: Config, model: str, provider_id: str | None) -> int:
    """The output cap a summary request to ``model`` sends:
    COMPACTION_SUMMARY_TOKENS_MAX, held to the model's own output limit where
    that is known."""
    cap = compaction_settings.get_int(
        cfg, "COMPACTION_SUMMARY_TOKENS_MAX",
        max_value=compaction_settings.get_int(cfg, "COMPACTION_SUMMARY_REQUEST_TOKENS_MAX"))
    known = model_metadata.resolve_max_output(model, provider_id)
    return min(cap, known) if known else cap


async def _summarize_replayed(cfg: Config, cached: Any, replayed: list, history: Sequence[dict],
                              focus: str, guidance: str) -> str | None:
    """The summary from ``replayed`` plus the instructions, sent as ``cached``
    went out but for its output cap; None when the provider refuses the
    request or its reply is not a checkpoint under the six headings."""
    prompt = _replay_prompt(history, focus, guidance)
    if (flight := ACTIVE_FLIGHT.get()) is not None:
        flight.record("summary_replay", model=cached.model, replayed_messages=len(replayed), prompt=prompt)
    try:
        result = await retry.call(lambda: model_client.stream_model_async(
            model_id=cached.model,
            provider_id=cached.provider_id,
            provider_base_url=cached.base_url,
            provider_api_key=cached.api_key,
            messages=[*replayed, ai.user_message(prompt)],
            tools=list(cached.tools) if cached.tools else None,
            max_output_tokens=_max_output(cfg, cached.model, cached.provider_id),
            reasoning_effort=cached.effort,
            thinking_budget=cached.thinking_budget,
            on_text=lambda text: ACTIVE_FLIGHT.get().record("summary_chunk", text=text) if ACTIVE_FLIGHT.get() else None,
            provider_headers=cached.headers,
            provider_extra=cached.extra,
            sampling=cached.sampling,
            cache_key=cached.cache_key,
            settings=cached.settings,
            trace_request=ACTIVE_FLIGHT.get() is not None,
            trace_sink=ACTIVE_FLIGHT.get(),
            stream_limits=cached.stream_limits,
        ), retry.Budget.from_settings(getattr(cfg, "settings", None)))
    except ai.ProviderAPIError as exc:
        # Too long, or a shape this endpoint does not take (two user messages
        # in a row): the transcript request is one user message.
        if (flight := ACTIVE_FLIGHT.get()) is not None:
            flight.record("summary_replay_refused", error=str(exc),
                          overflow=provider_errors.is_context_overflow_error(exc))
        return None
    _record_response(result, model=cached.model, provider_id=cached.provider_id, cfg=cfg)
    text = _summary_text(result)
    if text is None or not all(f"## {heading}" in text for heading in _COMPACTION_HEADINGS):
        # The conversation's own system prompt is in force here, so a reply
        # can be the agent carrying on; only a checkpoint replaces history.
        return None
    return text


def _replayable(cfg: Config, cached: Any, route: Any) -> bool:
    """Whether ``cached`` can carry the summary: reuse is on, the summarizer
    is the model the request went to, at the same provider, endpoint and
    key and under the same headers, and the output cap leaves room above a
    fixed thinking budget."""
    if cached is None or not compaction_settings.get_bool(cfg, "COMPACTION_REUSE_CACHE"):
        return False
    if ((route.model, route.provider_id, route.base_url, route.api_key)
            != (cached.model, cached.provider_id, cached.base_url, cached.api_key)):
        return False
    if model_client.turn_headers(route.headers, cfg) != dict(cached.headers or {}):
        return False
    return (cached.thinking_budget is None
            or _max_output(cfg, cached.model, cached.provider_id) > cached.thinking_budget)


async def summarize(cfg: Config, model: str, messages: list[dict], focus: str, guidance: str, *,
                    cached: Any = None, history: Sequence[dict] | None = None) -> str:
    """The summary of ``messages``, the history before the kept tail.

    ``cached`` is the conversation's last request
    (`cache_keepalive.CachedRequest`) and ``history`` the whole current
    history; with both, and `replay_messages` able to extend that request to
    ``history``, the summary request replays it."""

    route = routing.resolve_model_route(
        model,
        configured_provider_id=cfg.provider_id,
        configured_base_url=cfg.provider_base_url,
        configured_api_key=cfg.provider_api_key,
        configured_headers=getattr(cfg, "provider_headers", None),
        explicit_model=True,
    )
    if history is not None and _replayable(cfg, cached, route):
        replayed = replay_messages(cached, history)
        if replayed is not None:
            summary = await _summarize_replayed(cfg, cached, replayed, history, focus, guidance)
            if summary is not None:
                return summary
    previous, rest = _split_previous_summary(list(messages))
    tool_result_chars = compaction_settings.get_nonnegative_int(cfg, "COMPACTION_SUMMARY_RESULT_CHARS_MAX")
    settings = getattr(cfg, "settings", None)
    limits = retry.StreamLimits.from_settings(settings)
    budget = retry.Budget.from_settings(settings)

    async def summarize_chunk(head: list[dict], depth: int, previous: str) -> str:
        prompt = _summary_prompt(serialize_conversation(head, tool_result_chars=tool_result_chars),
                                 previous, focus, guidance, tool_result_chars=tool_result_chars)
        try:
            result = await retry.call(lambda: model_client.stream_model_async(
                model_id=route.model,
                provider_id=route.provider_id,
                provider_base_url=route.base_url,
                provider_api_key=route.api_key,
                messages=[ai.user_message(prompt)],
                tools=None,
                max_output_tokens=_max_output(cfg, route.model, route.provider_id),
                reasoning_effort=None,
                on_text=lambda text: ACTIVE_FLIGHT.get().record("summary_chunk", text=text) if ACTIVE_FLIGHT.get() else None,
                provider_headers=model_client.turn_headers(route.headers, cfg),
                provider_extra=routing.provider_extra_params(cfg),
                trace_request=ACTIVE_FLIGHT.get() is not None,
                trace_sink=ACTIVE_FLIGHT.get(),
                stream_limits=limits,
                settings=settings,
            ), budget)
        except ai.ProviderAPIError as exc:
            if not provider_errors.is_context_overflow_error(exc) or depth >= _SUMMARY_SPLIT_DEPTH or len(head) < 2:
                raise
            middle = len(head) // 2
            if (flight := ACTIVE_FLIGHT.get()) is not None:
                flight.record("summary_overflow", depth=depth, split_at=middle, error=str(exc), messages=head)
            compaction_flight.notice(msgs.SUMMARY_SPLIT, depth=depth + 1, stderr=False, flush=True)
            # The older half is summarized first and its summary is the
            # previous summary the newer half updates, so the result is one
            # summary under one set of headings.
            older = await summarize_chunk(head[:middle], depth + 1, previous)
            try:
                return await summarize_chunk(head[middle:], depth + 1, older)
            except ai.ProviderAPIError as again:
                if not provider_errors.is_context_overflow_error(again):
                    raise
                # The newer half has no room for the older half's summary
                # beside it: it is summarized alone and the two are joined.
                return older + "\n\n" + await summarize_chunk(head[middle:], depth + 1, "")
        _record_response(result, model=route.model, provider_id=route.provider_id, cfg=cfg)
        text = _summary_text(result)
        if text is None:
            raise ValueError(msgs.SUMMARY_RESPONSE_NO_TEXT.words())
        return text

    return await summarize_chunk(rest, 0, previous)
