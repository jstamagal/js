"""Room made without a summary: old tool results cleared in place (microcompaction), to recover from an overflow or to fit a turn's budget."""

from __future__ import annotations

import asyncio
import traceback
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import (
    compaction_flight,
    compaction_settings,
    history_record,
    media,
    memory as M,
    messages as msgs,
    provider_errors,
    settings as _settings,
)
from .compaction_flight import ACTIVE_FLIGHT, CompactionFlight
from .config import Config

# How many rounds of recovery a single turn may attempt after the provider
# rejects the request for size. One was never enough: the first compaction
# targets whatever window we *believe* we have, and if that belief is wrong
# the retry lands on the wall again.
MAX_OVERFLOW_ROUNDS = _settings.default_value("COMPACTION_OVERFLOW_ROUNDS_MAX")


MICROCOMPACT_CLEARED_MESSAGE = '[old tool result cleared]'


# Tool results shorter than this are not worth clearing.
_MICROCOMPACT_MIN_CHARS = 400


# Tool results are the bulk of a long session and the cheapest thing to drop:
# the assistant's own reasoning about them survives in its messages, and the
# file can always be read again. Clearing them needs no model call, so unlike a
# summary it cannot fail at the exact moment the context is too big to send.
def microcompact(
    messages: list[dict],
    *,
    keep_recent: int = 20,
    min_chars: int = _MICROCOMPACT_MIN_CHARS,
) -> tuple[int, int]:
    """Blank the bodies of old tool results in place.

    Leaves the newest ``keep_recent`` tool results untouched (the model is
    usually still working with those) and ignores results already small enough
    that clearing them buys nothing; a result carrying media (`js.media`) is
    always worth clearing, and loses its media too. Signed reasoning after the
    first cleared result is dropped (`history_record.drop_signed_reasoning`). Returns
    (results_cleared, chars_reclaimed).
    """
    indexes = [
        i for i, m in enumerate(messages)
        if isinstance(m, dict)
        and m.get("role") == "tool"
        and isinstance(m.get("content"), str)
        and m["content"] != MICROCOMPACT_CLEARED_MESSAGE
    ]
    if keep_recent > 0:
        indexes = indexes[:-keep_recent] if keep_recent < len(indexes) else []
    cleared = 0
    reclaimed = 0
    first: int | None = None
    for i in indexes:
        body = messages[i]["content"]
        has_media = bool(messages[i].get(media.MEDIA_KEY))
        if len(body) < min_chars and not has_media:
            continue
        cleared_record = {**messages[i], "content": MICROCOMPACT_CLEARED_MESSAGE}
        cleared_record.pop(media.MEDIA_KEY, None)
        messages[i] = cleared_record
        cleared += 1
        reclaimed += max(0, len(body) - len(MICROCOMPACT_CLEARED_MESSAGE))
        first = i if first is None else first
    if first is not None:
        history_record.drop_signed_reasoning(messages, first + 1)
    return cleared, reclaimed


def _clearing_flight(messages: list[dict], *, cfg: Config, system: str, trigger: dict,
                     flight_data: dict, log_dir: Path | None,
                     emit: Callable[..., Any] | None) -> CompactionFlight:
    try:
        return CompactionFlight(
            cfg, system, messages, trigger=trigger,
            forced=True, focus="clear old tool-result bodies", preserve_from=None,
            details=flight_data, directory=log_dir, emit=emit, operation="tool-result-clearing",
        )
    except OSError as exc:
        compaction_flight.notice(msgs.CLEARING_FLIGHT_FAILED, emit=emit, error=exc)
        raise


def cfg_stamp(cfg: Config) -> dict:
    """The stamp of the conversation's model, for assistant messages written here."""
    return M.stamp_for(getattr(cfg, "model", None), getattr(cfg, "provider_id", None),
                       getattr(cfg, "reasoning_effort", None))


@contextmanager
def flight_recording(flight: CompactionFlight, system: str, messages: list[dict]):
    token = ACTIVE_FLIGHT.set(flight)
    failure = None
    try:
        yield
    except BaseException as exc:
        failure = exc
        phase = "cancelled" if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)) else "failure"
        try:
            flight.finish(phase, system, messages, error=f"{type(exc).__name__}: {exc}",
                          traceback=traceback.format_exc())
        except Exception as recording_error:
            exc.add_note(f"Compaction flight recording failed: {recording_error}")
        raise
    finally:
        ACTIVE_FLIGHT.reset(token)
        try:
            flight.close()
        except Exception as close_error:
            if failure is None:
                raise
            failure.add_note(f"Compaction flight close failed: {close_error}")


def _record_cleared(flight: CompactionFlight, messages: list[dict], *, keep_recent: int) -> tuple[int, int]:
    before = messages.copy()
    cleared, reclaimed = microcompact(messages, keep_recent=keep_recent)
    changed = [
        {"message_index": index, "tool_call_id": old.get("tool_call_id"),
         "tool_name": old.get("name"), "original_chars": len(old["content"]),
         "replacement_chars": len(new["content"])}
        for index, (old, new) in enumerate(zip(before, messages, strict=True))
        if old != new and old.get("role") == "tool"
    ]
    flight.record("cleared_results", results=changed, cleared=cleared,
                  reclaimed_chars=reclaimed, keep_recent=keep_recent, min_chars=_MICROCOMPACT_MIN_CHARS)
    if cleared:
        stamp = cfg_stamp(flight.cfg)
        M.persist_messages(flight.cfg.session_file, before, stamp=stamp)
        M.persist_messages(flight.cfg.session_file, messages, stamp=stamp)
    return cleared, reclaimed


def recover_overflow(
    messages: list[dict], round_: int, *, cfg: Config, system: str,
    error: BaseException, flight_data: dict,
    log_dir: Path | None = None, emit: Callable[..., Any] | None = None,
) -> tuple[str, int, int]:
    """Record and announce tool-result clearing after a provider overflow.
    ``log_dir`` is where the flight log goes (None: no log); notices go to
    ``emit``."""
    keep_recent = max(0, compaction_settings.get_nonnegative_int(cfg, "COMPACTION_OVERFLOW_KEEP_TOOL_RESULTS") // round_)
    flight = _clearing_flight(
        messages, cfg=cfg, system=system,
        trigger={"phase": "overflow_recovery", "round": round_,
                 "error": f"{type(error).__name__}: {error}",
                 "provider_error": provider_errors.provider_error_fields(error)},
        flight_data=flight_data, log_dir=log_dir, emit=emit,
    )
    with flight_recording(flight, system, messages):
        flight.notice("start", msgs.CLEARING_OVERFLOW_START.words(round_=round_, keep_recent=keep_recent))
        cleared, reclaimed = _record_cleared(flight, messages, keep_recent=keep_recent)
        result = (msgs.CLEARING_OVERFLOW_RESULT.words(cleared=cleared, reclaimed=reclaimed)
                  if cleared else msgs.CLEARING_NO_RESULTS.words())
        flight.finish("success" if cleared else "skipped", system, messages, result=result)
        return ("cleared", cleared, reclaimed) if cleared else ("summarize", 0, 0)


def clear_for_budget(
    messages: list[dict], *, cfg: Config, system: str, trigger: dict, flight_data: dict,
    over_budget: Any, log_dir: Path | None = None, emit: Callable[..., Any] | None = None,
) -> tuple[int, int]:
    """Clear old tool-result bodies until ``over_budget(reclaimed_chars)`` is false.

    Starts with ``COMPACTION_KEEP_TOOL_RESULTS`` intact results and halves that
    each round down to one. An explicit zero clears all eligible results.
    Returns (results_cleared, chars_reclaimed). ``log_dir`` and ``emit`` are
    as for :func:`recover_overflow`."""
    keep_recent = compaction_settings.get_nonnegative_int(cfg, "COMPACTION_KEEP_TOOL_RESULTS")
    flight = _clearing_flight(messages, cfg=cfg, system=system, trigger=trigger, flight_data=flight_data,
                              log_dir=log_dir, emit=emit)
    with flight_recording(flight, system, messages):
        flight.notice("start", msgs.CLEARING_BUDGET_START.words(value=trigger.get('phase'), keep_recent=keep_recent))
        total = 0
        reclaimed = 0
        while True:
            cleared, chars = _record_cleared(flight, messages, keep_recent=keep_recent)
            total += cleared
            reclaimed += chars
            still_over = over_budget(reclaimed)
            if not still_over or keep_recent <= 1:
                break
            keep_recent = max(1, keep_recent // 2)
        result = (msgs.CLEARING_BUDGET_RESULT.words(total=total, reclaimed=reclaimed)
                  + (msgs.CLEARING_STILL_OVER_BUDGET.words() if still_over else ""))
        flight.finish("success" if total else "skipped", system, messages, result=result)
        return total, reclaimed


def prefix_worth_summarizing(messages: list[dict], preserve_from: int) -> bool:
    """Whether the prefix contains new history beyond a previous summary and files."""
    for message in messages[:preserve_from]:
        content = message.get("content", "")
        if message.get("role") == "user" and isinstance(content, str) and (
            content.startswith("<compaction-summary>") or content.startswith("<post-compaction-files>")
        ):
            continue
        return True
    return False
