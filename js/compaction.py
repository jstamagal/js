"""Context compaction — one module owns the "history is too big" decision and the deed.

Three triggers funnel through here so they can never drift on what "full"
means: the REPL between-turn trigger (``maybe_auto_compact``), the in-turn
budget check (the runtime measures, then calls ``compact_now``), and the
overflow recovery escalation (``recover_overflow``). The summarize pipeline
exists only as an async core; ``compact_now_sync`` is one ``asyncio.run``, so
there is exactly one implementation. This module also owns every read of the
``compact.*`` settings subtree.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any

import ai

from . import colors as C
from . import context_budget
from . import memory as M
from . import model_client
from . import model_metadata
from . import routing
from . import tools as T
from .capped_process import CappedProcessResult, _run_capped, truncation_marker
from .config import Config

# --------------------------------------------------------------------------
# compact.* settings readers — the one family
# --------------------------------------------------------------------------


def get(cfg: Config, key: str, default: Any = None) -> Any:
    settings = getattr(cfg, "settings", {}) or {}
    compact = settings.get("compact", {}) if isinstance(settings, dict) else {}
    return compact.get(key, default) if isinstance(compact, dict) else default


def get_bool(cfg: Config, key: str, default: bool) -> bool:
    value = get(cfg, key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def get_int(cfg: Config, key: str, default: int, *, max_value: int | None = None) -> int:
    raw = get(cfg, key, default)
    if isinstance(raw, bool):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    if max_value is not None:
        return min(value, max_value)
    return value


def get_nonnegative_int(cfg: Config, key: str, default: int, *, max_value: int | None = None) -> int:
    """Like get_int but 0 is a legal value, not a request for the default —
    a buffer of 0 is a real choice."""
    raw = get(cfg, key, default)
    if isinstance(raw, bool):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value < 0:
        return default
    if max_value is not None:
        return min(value, max_value)
    return value


def get_float(cfg: Config, key: str, default: float, *, max_value: float | None = None) -> float:
    raw = get(cfg, key, default)
    if isinstance(raw, bool):
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    if max_value is not None and value > max_value:
        return default
    return value


def get_model(cfg: Config) -> str:
    raw = get(cfg, "model", "same")
    if not isinstance(raw, str):
        return cfg.model
    model = raw.strip()
    if not model or model.lower() == "same":
        return cfg.model
    return model


def _pre_hook_command(cfg: Config) -> str:
    raw = get(cfg, "pre_hook")
    if not isinstance(raw, str):
        return ""
    return raw.strip()


# --------------------------------------------------------------------------
# Overflow classification and recovery escalation
# --------------------------------------------------------------------------

_CONTEXT_OVERFLOW_NEEDLES = (
    "context_length_exceeded",
    "context window",
    "context limit",
    "context length",
    "maximum context",
    "max context",
    "prompt too long",
    "prompt is too long",
    "input is too long",
    "input too long",
    "too many tokens",
    "token limit",
    "tokens exceed",
    "reduce the length",
    "request too large",
)


def is_context_overflow_error(exc: BaseException) -> bool:
    if not isinstance(exc, ai.ProviderAPIError):
        return False
    fields: list[str] = [str(exc)]
    for attr in ("code", "error_type", "status_code", "body"):
        value = getattr(exc, attr, None)
        if value is not None:
            fields.append(str(value))
    haystack = " ".join(fields).lower()
    if "413" in haystack:
        return True
    return any(needle in haystack for needle in _CONTEXT_OVERFLOW_NEEDLES)


# How many rounds of recovery a single turn may attempt after the provider
# rejects the request for size. One was never enough: the first compaction
# targets whatever window we *believe* we have, and if that belief is wrong
# the retry lands on the wall again.
MAX_OVERFLOW_ROUNDS = 3

MICROCOMPACT_CLEARED_MESSAGE = "[old tool result cleared]"


# Tool results are the bulk of a long session and the cheapest thing to drop:
# the assistant's own reasoning about them survives in its messages, and the
# file can always be read again. Clearing them needs no model call, so unlike a
# summary it cannot fail at the exact moment the context is too big to send.
def microcompact(
    messages: list[dict],
    *,
    keep_recent: int = 20,
    min_chars: int = 400,
) -> tuple[int, int]:
    """Blank the bodies of old tool results in place.

    Leaves the newest ``keep_recent`` tool results untouched (the model is
    usually still working with those) and ignores results already small enough
    that clearing them buys nothing. Returns (results_cleared, chars_reclaimed).
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
    for i in indexes:
        body = messages[i]["content"]
        if len(body) < min_chars:
            continue
        messages[i] = {**messages[i], "content": MICROCOMPACT_CLEARED_MESSAGE}
        cleared += 1
        reclaimed += len(body) - len(MICROCOMPACT_CLEARED_MESSAGE)
    return cleared, reclaimed


def recover_overflow(messages: list[dict], round_: int) -> tuple[str, int, int]:
    """One escalation step after the provider rejects a request for size.

    Returns ("cleared", n, reclaimed) when old tool bodies were blanked — the
    caller resets its budget bookkeeping and retries. Returns ("summarize", 0,
    0) when nothing was left to clear and the only way out is a paid
    compaction. Each round spares fewer recent results than the last.
    """
    cleared, reclaimed = microcompact(messages, keep_recent=max(0, 20 // round_))
    if cleared:
        return "cleared", cleared, reclaimed
    return "summarize", 0, 0


# --------------------------------------------------------------------------
# Post-compaction rehydration
# --------------------------------------------------------------------------

# After a summary compaction the model knows it edited a file but not what is
# in it, so its next move is to re-read everything it was just working on —
# one turn wasted, and the summary rarely carries enough detail to edit from.
# Re-attach the files it touched most recently, newest first, under a budget.
POST_COMPACT_MAX_FILES = 5
POST_COMPACT_TOKEN_BUDGET = 50_000
POST_COMPACT_MAX_TOKENS_PER_FILE = 5_000


def _post_compact_rehydration(
    context: Any,
    *,
    chars_per_token: float = 4.0,
    max_files: int = POST_COMPACT_MAX_FILES,
    token_budget: int = POST_COMPACT_TOKEN_BUDGET,
    per_file_tokens: int = POST_COMPACT_MAX_TOKENS_PER_FILE,
) -> dict | None:
    """Build one user message re-attaching recently read files, or None.

    Reads from disk rather than replaying the pre-compaction text, so the
    content is current — the model may have edited the file since. Files that
    vanished or grew past the per-file budget are named but not inlined, which
    is still useful: it tells the model the file exists and must be re-read.
    """
    paths = list(getattr(context, "read_paths", []) or [])
    if not paths:
        return None
    # read_paths is a set; order by mtime so "recent" means recent.
    def _mtime(p):
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
    paths.sort(key=_mtime, reverse=True)

    sections: list[str] = []
    spent = 0
    for path in paths[:max_files]:
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        est = max(1, int(len(body) / max(1.0, chars_per_token)))
        if est > per_file_tokens or spent + est > token_budget:
            sections.append(f"### {path}\n(too large to re-attach; read it if you need it)")
            continue
        spent += est
        sections.append(f"### {path}\n{body}")
    if not sections:
        return None
    return {
        "role": "user",
        "content": (
            "<post-compaction-files>\n"
            "Current contents of the files that were open before the summary "
            "above. These are read fresh from disk, so they already include "
            "your edits. Do not re-read them unless you change them.\n\n"
            + "\n\n".join(sections)
            + "\n</post-compaction-files>"
        ),
    }


# --------------------------------------------------------------------------
# Shared math: thresholds, effective window, token estimates
# --------------------------------------------------------------------------


def thresholds(cfg: Config) -> tuple[float, float, float]:
    notify_at = get_float(cfg, "notify_threshold", 0.50, max_value=1.0)
    trigger_at = get_float(cfg, "trigger_threshold", 0.80, max_value=1.0)
    force_at = get_float(cfg, "force_threshold", 0.90, max_value=1.0)
    if not (notify_at <= trigger_at <= force_at):
        return 0.50, 0.80, 0.90
    return notify_at, trigger_at, force_at


def effective_context_window(cfg: Config, context_window: int) -> int:
    """Window minus the room the next turn already owes: one full reply plus the
    compaction buffer. Both triggers measure against this so they agree on what
    'full' means. A fraction of the raw window does not survive a small model —
    0.8 of 32k leaves 6.4k, less than a single max-length reply, so compaction
    would arm only after the turn it needed to prevent."""
    if context_window <= 0:
        return 0
    max_out = cfg.max_output_tokens
    if max_out is None:
        max_out = model_metadata.resolve_max_output(cfg.model, cfg.provider_id)
    # Reserve what a reply plausibly costs, NOT what the model is willing to
    # emit. gpt-5.6-sol declares max_output 128000; holding all of that back
    # would hand a third of a 370k window to an outcome that essentially never
    # happens. Cap it the way Claude Code does (min(max_output, 20k)).
    reserve = min(
        max(0, int(max_out or 0)),
        get_nonnegative_int(cfg, "summary_reserve_tokens", 20_000),
    )
    buffer_tokens = get_nonnegative_int(cfg, "buffer_tokens", 4096)
    # Never let the reserve eat the whole window on a model with a huge declared
    # output cap; keep at least half the window addressable for input.
    return max(context_window // 2, context_window - reserve - buffer_tokens)


def is_max_output_incomplete(reason: object) -> bool:
    text = str(reason or "").lower()
    return text in {"max_output_tokens", "max_tokens", "length"} or "max_output" in text


def _message_text_for_estimate(message: dict) -> str:
    return json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str)


def _estimate_tokens(messages: list[dict], chars_per_token: float = 4.0) -> int:
    ratio = chars_per_token if chars_per_token and chars_per_token > 0 else 4.0
    return int(sum(len(_message_text_for_estimate(m)) for m in messages) / ratio)


def estimated_prompt_tokens(cfg: Config, system: str, messages: list[dict]) -> int:
    """Local estimate of what the next request will cost, in tokens.

    Only used when the provider reported nothing — which is exactly the case
    after a turn that failed for being too large. Without this the trigger
    reads 0, does nothing, and the history that caused the failure survives
    into the next attempt unchanged.
    """
    cpt = get_float(cfg, "chars_per_token", 4.0)
    try:
        return (
            context_budget.estimate_messages_tokens(messages or [], chars_per_token=cpt)
            + context_budget.estimate_system_tokens(system or "", chars_per_token=cpt)
        )
    except Exception:  # noqa: BLE001 - an estimate must never break the turn
        return 0


def _calibrated_chars_per_token(cfg: Config, system: str, messages: list[dict]) -> float:
    """Configured chars_per_token, corrected against real provider counts.

    tail_tokens and min_savings_tokens used the raw 4.0 estimate while the
    trigger that called them used provider tokens. On a tool-heavy session the
    estimator drifts far enough that "keep 16k of tail" kept something quite
    different from 16k.
    """
    configured = get_float(cfg, "chars_per_token", 4.0)
    try:
        # The tracker lives on the tool context (set in run_turn_async), not in
        # module scope — reading a global here would silently always miss.
        tracker = getattr(T.DEFAULT_CONTEXT, "context_budget_state", None)
        if not hasattr(tracker, "calibrated_chars_per_token"):
            return configured
        return tracker.calibrated_chars_per_token(messages=messages, system=system)
    except Exception:  # noqa: BLE001 - calibration must never break compaction
        return configured


# --------------------------------------------------------------------------
# The summarize pipeline
# --------------------------------------------------------------------------

_COMPACTION_HEADINGS = (
    "Goal",
    "Decisions and rationale",
    "Files and code",
    "Commands and outcomes",
    "Errors and fixes",
    "Pending and next step",
)

# How many times the summarizer may drop its oldest half and retry when the
# summarize call itself overflows.
_SUMMARY_PEEL_RETRIES = 3


def _run_pre_hook(cfg: Config) -> str:
    hook = _pre_hook_command(cfg)
    if not hook:
        return ""
    shell_path = (
        os.environ.get("COMSPEC", "cmd.exe")
        if sys.platform == "win32"
        else os.environ.get("SHELL", "/bin/sh")
    )
    shell_arg = "/C" if sys.platform == "win32" else "-c"
    cap = int(getattr(cfg, "max_bash_output_bytes", 256 * 1024))
    ceiling = int(getattr(cfg, "max_bash_output_ceiling", 0) or 0)
    if ceiling > 0:
        cap = min(cap, ceiling)
    try:
        result = _run_capped(
            [shell_path, shell_arg, hook],
            timeout=30,
            cwd=str(getattr(cfg, "project_dir", os.getcwd())),
            env=None,
            cap=cap,
        )
    except Exception as exc:  # noqa: BLE001
        return f"WARNING: compact pre_hook failed: {type(exc).__name__}: {exc}"
    if not isinstance(result, CappedProcessResult):
        result = CappedProcessResult(result[0], result[1], result[2])
    stdout = result.stdout.decode("utf-8", errors="replace")
    stderr = result.stderr.decode("utf-8", errors="replace")
    if result.stdout_truncated:
        stdout = stdout.rstrip("\n") + f"\n{truncation_marker(cap)}\n"
    if result.stderr_truncated:
        stderr = stderr.rstrip("\n") + f"\n{truncation_marker(cap)}\n"
    if result.returncode != 0:
        return f"WARNING: compact pre_hook exited {result.returncode}: {(stderr or stdout).strip()}"
    return stdout.strip()


def _summary_prompt(messages: list[dict], focus: str, guidance: str) -> str:
    headings = "\n".join(f"## {h}" for h in _COMPACTION_HEADINGS)
    payload = json.dumps(messages, ensure_ascii=False, indent=2, default=str)
    extra = ""
    if focus:
        extra += f"\nFocus: {focus.strip()}\n"
    if guidance:
        extra += f"\nPre-hook guidance/stdout:\n{guidance}\n"
    return (
        "Summarize this js session for loss-minimized context compaction. "
        "Use exactly these six markdown headings and keep concrete file paths, commands, decisions, errors, and next steps.\n\n"
        f"{headings}\n{extra}\nSession messages JSON:\n{payload}"
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
    # The thing being summarized is, by construction, the part of the history
    # that would not fit — so the summarize call can overflow too. Peel the
    # oldest half and retry rather than failing at the exact moment the only
    # way out of the wall is a summary. Matches the PTL retry loop in Claude
    # Code (compact.ts, MAX_PTL_RETRIES).
    head = list(messages)
    result = None
    for peel in range(_SUMMARY_PEEL_RETRIES + 1):
        try:
            result = await model_client.stream_model_async(
                model_id=route.model,
                provider_id=route.provider_id,
                provider_base_url=route.base_url,
                provider_api_key=route.api_key,
                messages=[ai.user_message(_summary_prompt(head, focus, guidance))],
                tools=None,
                max_output_tokens=get_int(cfg, "summary_max_tokens", 4096, max_value=8192),
                reasoning_effort=None,
                on_text=lambda _t: None,
                provider_headers=route.headers,
                provider_extra=routing.provider_extra_params(cfg),
            )
            break
        except ai.ProviderAPIError as exc:
            if not is_context_overflow_error(exc) or peel >= _SUMMARY_PEEL_RETRIES or len(head) <= 2:
                raise
            dropped = len(head) // 2
            head = head[dropped:]
            print(
                f"  {C.ORANGE}(summary too large; dropped the oldest {dropped} messages "
                f"and retrying, {peel + 1}/{_SUMMARY_PEEL_RETRIES}){C.RESET}",
                flush=True,
            )
    assert result is not None
    text = result.text.strip()
    if not text:
        text = "\n".join(f"## {h}\n(Not captured.)" for h in _COMPACTION_HEADINGS)
    return text


# --------------------------------------------------------------------------
# compact_now — the deed
# --------------------------------------------------------------------------


def _compaction_summary_message(summary: str) -> dict:
    return {"role": "user", "content": f"<compaction-summary>\n{summary}\n</compaction-summary>"}


def _safe_tail_start(messages: list[dict], tail_tokens: int, chars_per_token: float = 4.0) -> int:
    if not messages:
        return 0
    ratio = chars_per_token if chars_per_token and chars_per_token > 0 else 4.0
    budget_chars = int(tail_tokens * ratio)
    total = 0
    start = len(messages)
    for idx in range(len(messages) - 1, -1, -1):
        total += len(_message_text_for_estimate(messages[idx]))
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
    preserve_from: int | None = None,
) -> str:
    chars_per_token = _calibrated_chars_per_token(cfg, system, messages)
    tail_tokens = get_int(cfg, "tail_tokens", 16384)
    min_savings = get_int(cfg, "min_savings_tokens", 400)
    original_len = len(messages)
    keep_from = _safe_tail_start(messages, tail_tokens, chars_per_token)
    if preserve_from is not None:
        try:
            keep_from = min(keep_from, max(0, int(preserve_from)))
        except (TypeError, ValueError):
            pass
    original_est = _estimate_tokens(messages, chars_per_token)
    tail_est = _estimate_tokens(messages[keep_from:], chars_per_token)
    if not forced and original_est - tail_est < min_savings:
        return f"compact skipped: estimated savings {original_est - tail_est} tokens < {min_savings}"
    guidance = _run_pre_hook(cfg)
    compact_model = get_model(cfg)
    summary = await summarize(cfg, compact_model, messages[:keep_from], focus, guidance)
    M.append_compaction_mark(cfg.session_file, summary=summary, keep_from=keep_from, forced=forced)
    rehydrated = _post_compact_rehydration(T.DEFAULT_CONTEXT, chars_per_token=chars_per_token)
    messages[:] = [
        _compaction_summary_message(summary),
        *([rehydrated] if rehydrated else []),
        *messages[keep_from:],
    ]
    return f"compacted: kept tail from message {keep_from}/{original_len} using {compact_model}"


def compact_now_sync(
    cfg: Config,
    system: str,
    messages: list[dict],
    *,
    focus: str = "",
    forced: bool = False,
    preserve_from: int | None = None,
    loop_runner: asyncio.Runner | None = None,
) -> str:
    """Sync wrapper over :func:`compact_now` for callers off the event loop.

    The ONLY sync path — there is no second implementation to drift.
    """
    coro = compact_now(
        cfg, system, messages, focus=focus, forced=forced, preserve_from=preserve_from
    )
    if loop_runner is not None:
        return loop_runner.run(coro)
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# The REPL trigger: between-turn auto-compaction policy
# --------------------------------------------------------------------------


@dataclass
class AutoCompactState:
    consecutive: int = 0
    paused: bool = False
    notified: bool = False
    incomplete_consecutive: int = 0


@dataclass
class AutoCompactOutcome:
    compacted: bool = False
    forced: bool = False
    result: str | None = None
    notices: list[str] = field(default_factory=list)


def maybe_auto_compact(
    cfg: Config,
    ac: AutoCompactState,
    context: Any,
    system: str,
    messages: list[dict],
    resolve_window: Any,
) -> AutoCompactOutcome:
    """Run the between-turn trigger, compacting if it fires.

    ``resolve_window`` is a zero-arg callable returning the model's context
    window (or None); it is invoked at most once and only when the configured
    override is unset, so a turn that exits early never pays for the probe.
    Notices are returned, not printed — presentation belongs to the caller.
    """
    out = AutoCompactOutcome()
    output_limited = is_max_output_incomplete(getattr(context, "last_incomplete_reason", None))
    ac.incomplete_consecutive = ac.incomplete_consecutive + 1 if output_limited else 0
    incomplete_forced = ac.incomplete_consecutive >= 2
    prompt_tokens = int(getattr(context, "last_prompt_tokens", 0) or 0)
    if prompt_tokens <= 0:
        prompt_tokens = estimated_prompt_tokens(cfg, system, messages)
    if prompt_tokens <= 0 and not incomplete_forced:
        return out
    context_window = get_int(cfg, "context_window", 0)
    if context_window <= 0:
        context_window = int(resolve_window() or 0)
    if context_window <= 0:
        context_window = get_int(cfg, "context_window_fallback", 0)
    effective_window = effective_context_window(cfg, context_window)
    fullness = (prompt_tokens / effective_window) if effective_window > 0 else 0.0
    notify_at, trigger_at, force_at = thresholds(cfg)
    if fullness < trigger_at and not incomplete_forced:
        ac.consecutive = 0
        ac.paused = False
        if fullness < notify_at:
            ac.notified = False
        elif not ac.notified:
            out.notices.append(f"(context {fullness:.0%} full; auto-compaction armed)")
            ac.notified = True
        return out
    if ac.paused:
        return out
    if fullness >= notify_at and not ac.notified:
        out.notices.append(f"(context {fullness:.0%} full; auto-compaction armed)")
        ac.notified = True
    if incomplete_forced:
        out.notices.append("(response incomplete from max output tokens twice; auto-compacting)")
    out.forced = fullness >= force_at or incomplete_forced
    out.result = compact_now_sync(cfg, system, messages, forced=out.forced)
    out.compacted = True
    if incomplete_forced:
        ac.incomplete_consecutive = 0
    ac.consecutive += 1
    if ac.consecutive >= 2:
        ac.paused = True
        out.notices.append(
            "(auto-compaction paused after two consecutive turns; "
            "resumes when context drops below trigger)"
        )
    return out
