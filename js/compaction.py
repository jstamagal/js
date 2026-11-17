"""Context compaction — one module owns the "history is too big" decision and the deed.

Three triggers funnel through here so they can never drift on what "full"
means: the REPL between-turn trigger (``maybe_auto_compact_async``), the in-turn
budget check (the runtime measures, then calls ``compact_now``), and the
overflow recovery escalation (``microcompact.recover_overflow``). The summarize pipeline
exists only as an async core; ``compact_now_sync`` is one ``asyncio.run``, so
there is exactly one implementation. This module also owns every read of the
``compact.*`` settings subtree.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import ai

from . import (
    compaction_flight,
    compaction_settings,
    context_window,
    jail,
    media,
    memory as M,
    messages as msgs,
    microcompact,
    model_client,
    owning_loop,
    provider_errors,
    retry,
    routing,
    settings as _settings,
    snippets,
    toolkit as T,
    usage as usage_mod,
)
from .capped_process import CappedProcessResult, _run_capped, truncation_marker
from .compaction_flight import (
    ACTIVE_FLIGHT,
    NOTICE as NOTICE,
    CompactionFlight,
    flight_log_dir as flight_log_dir,
    printing as printing,
)
from .config import Config

if TYPE_CHECKING:
    from .repl_state import ReplState


def _pre_hook_command(cfg: Config) -> str:
    raw = compaction_settings.get(cfg, "COMPACTION_HINT_COMMAND")
    if not isinstance(raw, str):
        return ""
    return raw.strip()


# --------------------------------------------------------------------------
# The prompt cache and the summary breaker
# --------------------------------------------------------------------------

# A response whose cache read falls by more than this fraction of the previous
# comparable response's, and by at least CACHE_BREAK_MIN_TOKENS, is a cache
# break. The token floor keeps small prompts from reporting noise.
CACHE_BREAK_FRACTION = 0.05
CACHE_BREAK_MIN_TOKENS = 2000


def cache_expired(cfg: Config, context: Any, *, now: float | None = None) -> bool:
    """Whether the prompt cache of ``context``'s conversation has outlived
    ``COMPACTION_CACHE_TTL``. No request yet counts as expired."""
    ttl = compaction_settings.get_nonnegative_int(cfg, "COMPACTION_CACHE_TTL")
    last = context.last_request_at
    if ttl == 0 or last is None:
        return True
    return (time.time() if now is None else now) - last >= ttl


def note_response(context: Any, *, model_key: str, cache_read: int | None, now: float) -> str | None:
    """Record a finished main-conversation response on ``context``.

    Returns the cache-break line when this response read noticeably less from
    the cache than the previous comparable one: same model, no history rewrite
    in between. Otherwise None. A response without usage (``cache_read`` None)
    records only the request time."""
    if cache_read is None:
        context.last_request_at = now
        return None
    previous = context.cache_read_baseline
    comparable = previous is not None and context.cache_read_model == model_key
    last = context.last_request_at
    context.cache_read_baseline = cache_read
    context.cache_read_model = model_key
    context.last_request_at = now
    if not comparable:
        return None
    drop = previous - cache_read
    if drop <= previous * CACHE_BREAK_FRACTION or drop < CACHE_BREAK_MIN_TOKENS:
        return None
    idle = int(now - last) if last is not None else 0
    return msgs.NET_CACHE_BREAK.said(before=previous, after=cache_read, drop=drop / previous, idle=idle)


def history_rewritten(context: Any) -> None:
    """A deliberate rewrite shrinks the next cache read; it is not a break."""
    context.cache_read_baseline = None


def auto_paused(cfg: Config, context: Any) -> bool:
    """Whether failed summaries have paused automatic compaction on ``context``."""
    return context.summary_failures >= compaction_settings.get_int(cfg, "COMPACTION_FAILURES_MAX")


def record_auto_failure(cfg: Config, context: Any) -> msgs.Said | None:
    """Count one failed automatic summary. Returns the pause notice on the
    failure that reaches ``COMPACTION_FAILURES_MAX``, else None."""
    context.summary_failures += 1
    limit = compaction_settings.get_int(cfg, "COMPACTION_FAILURES_MAX")
    if context.summary_failures == limit:
        return msgs.AUTO_COMPACT_BREAKER.said(failures=limit)
    return None


# --------------------------------------------------------------------------
# Post-compaction rehydration
# --------------------------------------------------------------------------

# After a summary compaction the model knows it edited a file but not what is
# in it, so its next move is to re-read everything it was just working on —
# one turn wasted, and the summary rarely carries enough detail to edit from.
# Re-attach the files it touched most recently, newest first, under a budget
# (compact.rehydrate_*).


def _post_compact_rehydration(
    context: Any,
    *,
    chars_per_token: float = 4.0,
    max_files: int | None = None,
    token_budget: int | None = None,
    per_file_tokens: int | None = None,
) -> dict | None:
    """Build one user message re-attaching recently read files, or None.

    Reads from disk rather than replaying the pre-compaction text, so the
    content is current — the model may have edited the file since. Files that
    are unavailable, non-text, or exceed a reattachment budget are named with
    omission notices rather than inlined. Jail-denied paths remain omitted.
    A budget left None is its compact.rehydrate_* value in js/jsrc.
    """
    if max_files is None:
        max_files = _settings.default_value("COMPACTION_REATTACH_FILES_MAX")
    if token_budget is None:
        token_budget = _settings.default_value("COMPACTION_REATTACH_TOKENS_MAX")
    if per_file_tokens is None:
        per_file_tokens = _settings.default_value("COMPACTION_REATTACH_FILE_TOKENS_MAX")
    paths = []
    for path in context.reads.paths():
        try:
            paths.append(context.resolve_path(path))
        except (OSError, RuntimeError, jail.JailError):
            continue
    if not paths:
        return None
    # The ledger's paths come unordered; order by mtime so "recent" means recent.
    def _mtime(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    paths.sort(key=_mtime, reverse=True)

    from .toolkit.fs import _detect_visual_mime, _read_regular_bytes

    sections: list[str] = []
    spent = 0
    ratio = max(1.0, chars_per_token)
    # The estimate floors character/ratio; UTF-8 needs at most four bytes per character.
    character_limit = max(0, math.ceil((per_file_tokens + 1) * ratio) - 1)
    byte_limit = character_limit * 4
    for path in paths[:max_files]:
        try:
            header = _read_regular_bytes(path, 4096)
            if _detect_visual_mime(path, header) or b"\x00" in header:
                sections.append(snippets.render('compaction-file-nontext', PATH=path))
                continue
            if path.stat().st_size > byte_limit:
                sections.append(snippets.render('compaction-file-too-large', PATH=path))
                continue
            data = _read_regular_bytes(path, byte_limit + 1)
            if len(data) > byte_limit:
                sections.append(snippets.render('compaction-file-too-large', PATH=path))
                continue
            if b"\x00" in data:
                sections.append(snippets.render('compaction-file-nontext', PATH=path))
                continue
            body = data.decode("utf-8")
        except UnicodeError:
            sections.append(snippets.render('compaction-file-nontext', PATH=path))
            continue
        except OSError:
            sections.append(snippets.render('compaction-file-unavailable', PATH=path))
            continue
        est = max(1, int(len(body) / ratio))
        if est > per_file_tokens:
            sections.append(snippets.render('compaction-file-too-large', PATH=path))
            continue
        if spent + est > token_budget:
            sections.append(snippets.render('compaction-file-budget', PATH=path))
            continue
        spent += est
        sections.append(f'### {path}\n{body}')
    if not sections:
        return None
    return {
        "role": "user",
        "content": (
            snippets.render("compaction-rehydrate", FILES="\n\n".join(sections))
        ),
    }


def is_max_output_incomplete(reason: object) -> bool:
    text = str(reason or "").lower()
    return text in {"max_output_tokens", "max_tokens", "length"} or "max_output" in text


# --------------------------------------------------------------------------
# The summarize pipeline
# --------------------------------------------------------------------------

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


def _run_pre_hook(cfg: Config) -> str:
    """The compact pre-hook's stdout, the guidance for the summarizer. A hook
    that fails or times out warns the user and gives no guidance: its error
    text is not instructions for the model."""
    hook = _pre_hook_command(cfg)
    if not hook:
        return ""
    shell_path = compaction_settings.get(cfg, "COMPACTION_HINT_SHELL_PROGRAM") or os.environ.get("SHELL", "/bin/sh")
    cap = int(_settings.knob_attr(cfg, "max_bash_output_bytes", "SHELL_OUTPUT_MAX"))
    try:
        result = _run_capped(
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


async def summarize(cfg: Config, model: str, messages: list[dict], focus: str, guidance: str) -> str:
    route = routing.resolve_model_route(
        model,
        configured_provider_id=cfg.provider_id,
        configured_base_url=cfg.provider_base_url,
        configured_api_key=cfg.provider_api_key,
        configured_headers=getattr(cfg, "provider_headers", None),
        explicit_model=True,
    )
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
                max_output_tokens=compaction_settings.get_int(cfg, "COMPACTION_SUMMARY_TOKENS_MAX", max_value=compaction_settings.get_int(cfg, "COMPACTION_SUMMARY_REQUEST_TOKENS_MAX")),
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
            # The previous summary travels with the older half only, so each
            # request carries at most one summary's worth of extra text.
            left = await summarize_chunk(head[:middle], depth + 1, previous)
            right = await summarize_chunk(head[middle:], depth + 1, "")
            return left + "\n\n" + right
        usage_mod.record(getattr(result, "usage", None), model=route.model, provider_id=route.provider_id,
                         session_file=getattr(cfg, "session_file", None))
        if (flight := ACTIVE_FLIGHT.get()) is not None:
            flight.record("summary_response", text=result.text,
                          usage=getattr(result, "usage", None),
                          finish_reason=getattr(result, "finish_reason", None),
                          incomplete_reason=getattr(result, "incomplete_reason", None),
                          provider_metadata=getattr(result, "provider_metadata", None))
        incomplete = (getattr(result, "incomplete_reason", None)
                      or provider_errors.incomplete_reason_from_metadata(getattr(result, "provider_metadata", None)))
        finish = str(getattr(result, "finish_reason", ""))
        if incomplete or finish.startswith("incomplete") or is_max_output_incomplete(finish):
            raise ValueError(msgs.SUMMARY_RESPONSE_INCOMPLETE.words(value=incomplete or finish))
        text = result.text.strip()
        if not text or getattr(result, "tool_calls", None):
            raise ValueError(msgs.SUMMARY_RESPONSE_NO_TEXT.words())
        return text

    return await summarize_chunk(rest, 0, previous)


# --------------------------------------------------------------------------
# compact_now — the deed
# --------------------------------------------------------------------------


def compacted(result: str) -> bool:
    """Whether a compact_now result is a compaction rather than a skip."""
    return isinstance(result, msgs.Said) and result.message == msgs.COMPACTED


def tail_start(messages: list[dict], tail_tokens: int, chars_per_token: float) -> int:
    """Index where a summary with no preserved prefix would keep the history from."""
    return _safe_tail_start(messages, tail_tokens, chars_per_token)


def _safe_tail_start(messages: list[dict], tail_tokens: int, chars_per_token: float = 4.0) -> int:
    if not messages:
        return 0
    ratio = chars_per_token if chars_per_token and chars_per_token > 0 else 4.0
    budget_chars = int(tail_tokens * ratio)
    total = 0
    start = len(messages)
    for idx in range(len(messages) - 1, -1, -1):
        size = len(context_window.message_text_for_estimate(messages[idx]))
        if start < len(messages) and total + size > budget_chars:
            break
        total += size
        start = idx
        if total >= budget_chars:
            break
    # Back up so an assistant tool_calls message is never separated from the
    # tool result messages that immediately answer it.
    while start > 0:
        prev = messages[start - 1]
        if prev.get("role") == "assistant" and prev.get("tool_calls"):
            start -= 1
            continue
        if messages[start].get("role") == "tool":
            start -= 1
            continue
        break
    return max(0, start)


async def compact_now(
    cfg: Config,
    system: str,
    messages: list[dict],
    *,
    focus: str = "",
    forced: bool = False,
    model: str | None = None,
    preserve_from: int | None = None,
    trigger: dict | None = None,
    flight_data: dict | None = None,
    tail_tokens: int | None = None,
    context: Any = None,
    emit: Callable[..., Any] | None = None,
    log_dir: Path | None = None,
) -> str:
    """Summarize ``messages`` before the kept tail, in place. ``emit(event,
    **payload)`` hears pre_compact when the summary is about to be written,
    post_compact once it replaced the history, and each flight notice as a
    ``compaction_notice`` event. The flight log goes to ``log_dir``; None
    writes none."""
    flight = CompactionFlight(
        cfg, system, messages, trigger=trigger or {"phase": "manual"},
        forced=forced, focus=focus, preserve_from=preserve_from, details=flight_data,
        directory=log_dir, emit=emit,
    )
    with microcompact.flight_recording(flight, system, messages):
        chars_per_token = context_window.calibrated_chars_per_token(cfg, system, messages, context)
        if tail_tokens is None:
            tail_tokens = compaction_settings.get_int(cfg, "COMPACTION_KEEP_RECENT_TOKENS")
        min_savings = compaction_settings.get_int(cfg, "COMPACTION_MIN_SAVINGS_TOKENS")
        original_len = len(messages)
        keep_from = _safe_tail_start(messages, tail_tokens, chars_per_token)
        if preserve_from is not None:
            keep_from = min(keep_from, max(0, int(preserve_from)))
        original_est = context_window.estimate_tokens(messages, chars_per_token)
        tail_est = context_window.estimate_tokens(messages[keep_from:], chars_per_token)
        flight.record("decision", chars_per_token=chars_per_token, tail_tokens=tail_tokens,
                      min_savings=min_savings, keep_from=keep_from,
                      original_estimate=original_est, tail_estimate=tail_est)
        if keep_from <= 0 or not microcompact.prefix_worth_summarizing(messages, keep_from):
            result = msgs.COMPACT_SKIPPED_NO_PREFIX.said()
            flight.finish("skipped", system, messages, result=result)
            return result
        if not forced and original_est - tail_est < min_savings:
            result = msgs.COMPACT_SKIPPED_SAVINGS.said(savings=original_est - tail_est, required=min_savings)
            flight.finish("skipped", system, messages, result=result)
            return result
        compact_model = model or compaction_settings.get_model(cfg)
        flight.notice("start", msgs.COMPACTION_FLIGHT_START.words(compact_model=compact_model, value=trigger or 'manual', original_len=original_len))
        phase = (trigger or {}).get("phase", "manual")
        if emit is not None:
            emit("pre_compact", phase=phase, forced=forced, focus=focus, model=compact_model,
                 messages=original_len, keep_from=keep_from)
        # The hook is a blocking subprocess: off the event loop.
        guidance = await asyncio.to_thread(_run_pre_hook, cfg)
        flight.record("summary_input", model=compact_model, guidance=guidance,
                      messages=messages[:keep_from])
        summary = await summarize(cfg, compact_model, messages[:keep_from], focus, guidance)
        recorded_trigger = {**(trigger or {"phase": "manual"}), "attempt_id": flight.id,
                            **({"flight_path": str(flight.path)} if flight.path else {})}
        rehydrated = _post_compact_rehydration(
            context or T.STOCK_CONTEXT,
            chars_per_token=chars_per_token,
            max_files=compaction_settings.get_nonnegative_int(cfg, "COMPACTION_REATTACH_FILES_MAX"),
            token_budget=compaction_settings.get_nonnegative_int(cfg, "COMPACTION_REATTACH_TOKENS_MAX"),
            per_file_tokens=compaction_settings.get_nonnegative_int(cfg, "COMPACTION_REATTACH_FILE_TOKENS_MAX"),
        )
        marker, after = M.compaction_mark(messages, keep_from, summary=summary, forced=forced,
                                          trigger=recorded_trigger, rehydrated=rehydrated)
        required_savings = 1 if forced else min_savings
        if original_est - context_window.estimate_tokens(after, chars_per_token) < required_savings and rehydrated:
            rehydrated = None
            marker, after = M.compaction_mark(messages, keep_from, summary=summary, forced=forced,
                                              trigger=recorded_trigger)
        savings = original_est - context_window.estimate_tokens(after, chars_per_token)
        if savings < required_savings:
            result = msgs.COMPACT_SKIPPED_SAVINGS.said(savings=savings, required=required_savings)
            # A summary was paid for and thrown away. Count it like a failed
            # summary, or the next turn pays for the same one again, and the
            # next, until the breaker would have stopped it.
            if trigger and trigger.get("phase") == "between-turn":
                owner = context or T.STOCK_CONTEXT
                if (paused := record_auto_failure(cfg, owner)) is not None:
                    compaction_flight.notice(paused.message, **paused.fields)
            flight.finish("skipped", system, messages, result=result)
            return result
        flight.record("commit_pending", summary=summary, keep_from=keep_from, rehydrated=rehydrated)
        M.persist_messages(cfg.session_file, messages, stamp=microcompact.cfg_stamp(cfg))
        M.append_mark(cfg.session_file, marker)
        messages[:] = after
        owner = context or T.STOCK_CONTEXT
        tracker = owner.context_budget_state
        if tracker is not None:
            tracker.reset()
        owner.summary_failures = 0
        history_rewritten(owner)
        result = msgs.COMPACTED.said(keep_from=keep_from, total=original_len, model=compact_model)
        flight.finish("success", system, messages, result=result, keep_from=keep_from)
        if emit is not None:
            emit("post_compact", phase=phase, forced=forced, model=compact_model,
                 messages=len(messages), summarized=keep_from)
        return result


def compact_now_sync(
    cfg: Config,
    system: str,
    messages: list[dict],
    *,
    focus: str = "",
    forced: bool = False,
    model: str | None = None,
    preserve_from: int | None = None,
    trigger: dict | None = None,
    flight_data: dict | None = None,
    context: Any = None,
    emit: Callable[..., Any] | None = None,
    log_dir: Path | None = None,
) -> str:
    """Sync wrapper over :func:`compact_now` for callers off the event loop.

    The ONLY sync path — there is no second implementation to drift.
    """
    coro = compact_now(
        cfg, system, messages, focus=focus, forced=forced, model=model,
        preserve_from=preserve_from, trigger=trigger, flight_data=flight_data,
        context=context, emit=emit, log_dir=log_dir,
    )
    return owning_loop.run_owning_loop(coro)


# --------------------------------------------------------------------------
# The REPL trigger: between-turn auto-compaction policy
# --------------------------------------------------------------------------


@dataclass
class AutoCompactState:
    consecutive: int = 0
    paused: bool = False
    notified: bool = False


@dataclass
class AutoCompactOutcome:
    compacted: bool = False
    forced: bool = False
    result: str | None = None
    notices: list[msgs.Said] = field(default_factory=list)  # for the screen


async def maybe_auto_compact_async(
    cfg: Config,
    ac: AutoCompactState,
    context: Any,
    system: str,
    messages: list[dict],
    resolve_window: Any,
    emit: Callable[..., Any] | None = None,
    log_dir: Path | None = None,
) -> AutoCompactOutcome:
    """Run the between-turn trigger, compacting if it fires. ``emit`` and
    ``log_dir`` go to compact_now.

    ``resolve_window`` is a zero-arg callable returning the model's context
    window (or None); it is invoked at most once and only when the configured
    override is unset, so a turn that exits early never pays for the probe.
    Notices are returned, not printed — presentation belongs to the caller.
    """
    out = AutoCompactOutcome()
    tracker = context.context_budget_state
    if tracker is not None:
        registry = context.tool_registry
        tools = model_client.tool_specs_to_ai_tools(registry.openai_specs()) if registry else None
        prompt_tokens, _, _ = tracker.current_context_tokens(system=system, messages=messages, tools=tools)
    else:
        prompt_tokens = context.last_prompt_tokens + context.last_output_tokens
        if prompt_tokens <= 0:
            prompt_tokens = context_window.estimated_prompt_tokens(cfg, system, messages)
    if prompt_tokens <= 0:
        return out
    window = context_window.configured_context_window(cfg, resolve_window)
    effective_window = context_window.effective_context_window(cfg, window)
    fullness = (prompt_tokens / effective_window) if effective_window > 0 else 0.0
    notify_at, trigger_at, force_at = context_window.thresholds(cfg)
    if fullness < trigger_at:
        ac.consecutive = 0
        ac.paused = False
        if fullness < notify_at:
            ac.notified = False
        elif not ac.notified:
            out.notices.append(msgs.AUTO_COMPACT_ARMED.said(fullness=fullness))
            ac.notified = True
        return out
    if ac.paused or auto_paused(cfg, context):
        return out
    if fullness >= notify_at and not ac.notified:
        out.notices.append(msgs.AUTO_COMPACT_ARMED.said(fullness=fullness))
        ac.notified = True
    out.forced = fullness >= force_at
    try:
        out.result = await _between_turn_compact(cfg, ac, context, system, messages, out.forced,
                                                 prompt_tokens, window, effective_window, emit,
                                                 log_dir)
    except snippets.SnippetError:
        raise
    except Exception as exc:  # noqa: BLE001 - a failed summary leaves the history as it was
        out.notices.append(msgs.COMPACTION_FAILED.said(error=f"{type(exc).__name__}: {exc}"))
        if (paused := record_auto_failure(cfg, context)) is not None:
            out.notices.append(paused)
        return out
    out.compacted = compacted(out.result)
    if not out.compacted:
        return out
    if tracker is not None:
        tracker.reset()
    context.last_prompt_tokens = 0
    context.last_output_tokens = 0
    ac.consecutive += 1
    if ac.consecutive >= compaction_settings.get_int(cfg, "COMPACTION_CONSECUTIVE_MAX"):
        ac.paused = True
        out.notices.append(msgs.AUTO_COMPACT_PAUSED.said())
    return out


async def _between_turn_compact(cfg: Config, ac: AutoCompactState, context: Any, system: str,
                                messages: list[dict], forced: bool, prompt_tokens: int,
                                context_window: int, effective_window: int,
                                emit: Callable[..., Any] | None = None,
                                log_dir: Path | None = None) -> str:
    return await compact_now(
        cfg, system, messages, forced=forced, context=context, emit=emit, log_dir=log_dir,
        trigger={"phase": "between-turn", "context_tokens": prompt_tokens,
                 "context_window": context_window, "effective_input_limit": effective_window},
        flight_data={"auto_state": dict(vars(ac)), "last_prompt_tokens": context.last_prompt_tokens,
                     "last_cached_tokens": context.last_cached_tokens,
                     "last_incomplete_reason": context.last_incomplete_reason,
                     "usage_anchor": vars(context.context_budget_state).get("_anchor")
                         if context.context_budget_state is not None else None,
                     "tools": context.tool_registry.openai_specs() if context.tool_registry else []},
    )


def maybe_auto_compact(
    cfg: Config, ac: AutoCompactState, context: Any, system: str,
    messages: list[dict], resolve_window: Any,
) -> AutoCompactOutcome:
    """Between-turn compaction for callers outside an event loop."""
    return asyncio.run(maybe_auto_compact_async(cfg, ac, context, system, messages, resolve_window))


def compact_hooks(state: ReplState, cfg: Config) -> dict:
    """The ``emit`` and ``log_dir`` keywords a compaction call takes: notices
    print, other events go to the state's `on` handler table when it has one,
    and the flight log goes to js's flight log directory."""
    hook_table = state.events
    return {"emit": printing(None if hook_table is None else hook_table.emit),
            "log_dir": flight_log_dir(cfg)}


def track_compact_skips(emit: Callable[..., Any]) -> tuple[Callable[..., Any], set[str]]:
    skipped: set[str] = set()

    def tracked(event: str, **payload: Any) -> Any:
        result = emit(event, **payload)
        if event == "compaction_notice" and payload.get("stage") == "skipped":
            skipped.add(payload.get("detail", ""))
        return result

    return tracked, skipped
