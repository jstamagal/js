"""The conversation a turn sends the model, and the context budget it is held to.

`TurnConvo` is the turn's SDK message list, built from the history, and the
trace cursor over it: the request trace dumps the system prompt and tool
schemas once, then only the messages not yet sent.

`TurnBudget` brings the next request under the context window: before a
model call (`fit`), and after the provider refused a request as too long or
showed that it cut the input (`recover_overflow`). Every rewrite of the
history rebuilds the convo, drops the provider usage anchor and marks the
prompt cache as broken.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from . import compaction
from . import messages as msgs
from . import model_client
from . import stream_transport


def last_user_message_index(messages: list[dict]) -> int | None:
    """Index of the message that opened the current turn. A steered message or
    a resume nudge joined a turn already running, so it does not open one."""
    for idx in range(len(messages) - 1, -1, -1):
        message = messages[idx]
        if message.get("role") == "user" and not message.get("steered") and not message.get("resume_nudge"):
            return idx
    return None


class TurnConvo:
    """``ai`` is the SDK form of ``messages`` for one turn. A rewrite of the
    history replaces ``ai`` with a new list, so a flight record holding the
    old list keeps what was sent before the rewrite."""

    def __init__(self, system: str, messages: list[dict], *, provider_id: str | None, model: str):
        self.system = system
        self.messages = messages
        self._provider_id = provider_id
        self._model = model
        self.ai: list[Any] = []
        self.rebuild()

    def rebuild(self) -> None:
        """Build ``ai`` again from the history, and trace the next request
        from its first message with the tool schemas."""
        self.ai = model_client.history_to_ai_messages(
            self.system, self.messages, provider_id=self._provider_id, model_id=self._model,
        )
        self.sent = 0
        self.schemas = True

    def add(self, records: list[dict]) -> None:
        """Append history records already appended to ``messages``."""
        self.ai.extend(model_client.history_to_ai_messages(
            "", records, provider_id=self._provider_id, model_id=self._model,
        ))

    def traced(self) -> None:
        """The request trace has now shown every message and the schemas."""
        self.sent = len(self.ai)
        self.schemas = False


@dataclass
class _Check:
    """One budget check: what it measured, and what it has reclaimed so far."""

    phase: str
    force: bool
    status: Any
    trigger: dict
    flight_data: dict
    chars_per_token: float
    reclaimed: int = 0
    changed: bool = False
    summary_failed: bool = False

    def over_budget(self, more: int = 0) -> bool:
        """The provider-anchored count, minus what was removed in the currency
        the anchor was calibrated in, is still over the input limit."""
        if self.status.effective_input_limit is None:
            return False
        remaining = self.status.current_context_tokens - int((self.reclaimed + more) / self.chars_per_token)
        return remaining > self.status.effective_input_limit


class TurnBudget:
    """``cfg`` is the turn's Config with its model and provider in place: the
    compaction settings and the summary model come from it. ``resolve_window``
    returns the model's known context window, or None."""

    def __init__(self, cfg: Any, convo: TurnConvo, token_state: Any, context: Any, *,
                 telemetry: Any, turn_status: Any, emit: Callable[..., Any],
                 resolve_window: Callable[[], int | None], max_out: int | None):
        self._cfg = cfg
        self._convo = convo
        self._tokens = token_state
        self._context = context
        self._telemetry = telemetry
        self._status = turn_status
        self._emit = emit
        self._resolve_window = resolve_window
        self._max_out = max_out

    def context_window(self) -> int:
        """The window the budget holds requests to; 0 when none is known."""
        return compaction.configured_context_window(self._cfg, self._resolve_window)

    def provider_window(self) -> int | None:
        """The window the provider holds its input to, or None. A
        compact.context_window above the catalog's says the real window is
        larger than the catalog knows."""
        return max(
            self._resolve_window() or 0,
            compaction.get_int(self._cfg, "context_window", 0),
        ) or None

    async def fit(self, *, phase: str, specs: list[dict], force: bool = False,
                  overflow_round: int = 0) -> bool:
        """Bring the next request under budget. Escalates in three steps, each
        costlier than the last and each stopping as soon as the budget is met:
        clear old tool-result bodies, summarize the history before the current
        user message, then summarize the current turn itself keeping only its
        tail. ``force`` is a provider rejection: compact whatever the budget
        believes, keeping half as much tail for each ``overflow_round``.
        Returns True when the history changed."""
        cfg, context = self._cfg, self._context
        system, messages = self._convo.system, self._convo.messages
        if not force and not compaction.get_bool(cfg, "auto"):
            return False
        context_window = self.context_window()
        if context_window <= 0 and not force:
            return False
        ai_tools = model_client.tool_specs_to_ai_tools(specs) if specs else None
        reserved = context_window - compaction.effective_context_window(cfg, context_window)
        buffer_tokens = min(compaction.get_nonnegative_int(cfg, "buffer_tokens"), max(0, reserved))
        status = self._tokens.budget_status(
            system=system,
            messages=messages,
            tools=ai_tools,
            context_window=context_window if context_window > 0 else None,
            output_reserve_tokens=max(0, reserved - buffer_tokens),
            buffer_tokens=buffer_tokens,
        )
        context.context_tokens = status.current_context_tokens
        context.tokens_until_compaction = status.tokens_until_compaction
        self._telemetry.event(
            "context_budget",
            phase=phase,
            context_tokens=status.current_context_tokens,
            context_window=status.context_window,
            effective_input_limit=status.effective_input_limit,
            tokens_until_compaction=status.tokens_until_compaction,
            used_provider_usage=status.used_provider_usage,
        )
        if not (force or status.should_compact):
            return False
        check = _Check(
            phase=phase, force=force, status=status,
            trigger={"phase": phase, "context_tokens": status.current_context_tokens,
                     "context_window": context_window,
                     "effective_input_limit": status.effective_input_limit,
                     "forced_recovery": force},
            flight_data={"budget": asdict(status), "tools": specs,
                         "usage_anchor": vars(self._tokens).get("_anchor"),
                         "ai_messages": self._convo.ai},
            chars_per_token=self._tokens.calibrated_chars_per_token(
                system=system, messages=messages, tools=ai_tools,
            ),
        )

        # 1. Old tool-result bodies are the bulk of a long turn and cost no
        #    model call to drop. Rewriting them mid-history busts the prompt
        #    cache, so while the cache is warm a summary of the earlier turns
        #    goes first and clearing waits for step 3. A cold cache, or a
        #    provider that already refused the request (force), clears first.
        clearing_deferred = not (force or compaction.cache_expired(cfg, context))
        if clearing_deferred:
            self._telemetry.event("context_clearing_deferred", phase=phase,
                                  cache_age_s=time.time() - context.last_request_at)
        elif self._clear(check):
            return True
        # 2. Summarize everything before the current user message, which stays
        #    verbatim along with the turn's work so far.
        preserve_from = last_user_message_index(messages)
        if (preserve_from is not None and preserve_from > 0
                and compaction.prefix_worth_summarizing(messages, preserve_from)
                and await self._summarize(check, preserve_from, f"{phase} context budget")
                and (force or not check.over_budget())):
            return True
        # 3. Clearing deferred in step 1 runs now whatever the cache: a summary
        #    of the current turn rewrites the whole history too, and when
        #    summaries are paused or failing it is the only step left.
        if clearing_deferred and self._clear(check):
            return True
        # 4. The current turn alone is over budget: summarize it too, keeping
        #    its most recent tail so the model can carry on from the summary.
        #    A provider rejection (force) says the request did not fit no matter
        #    what the budget believed, so keep half as much tail each round.
        #    A summary that already failed in this check is not retried here.
        if check.summary_failed:
            return check.changed
        tail_tokens = compaction.get_int(cfg, "tail_tokens")
        if force:
            history_tokens = int(compaction.history_chars(messages) / check.chars_per_token)
            tail_tokens = min(tail_tokens, history_tokens) // 2 ** overflow_round
        keep_from = compaction.tail_start(messages, tail_tokens, check.chars_per_token)
        if keep_from > 0 and compaction.prefix_worth_summarizing(messages, keep_from):
            await self._summarize(check, None, f"{phase} context budget: current turn over budget",
                                  tail_tokens=tail_tokens)
        elif not check.changed:
            self._telemetry.event("context_compaction_skipped", phase=phase, reason="tail_fills_budget")
        return check.changed

    async def recover_overflow(self, error: BaseException, *, overflow_round: int,
                               tools: list[dict], specs: list[dict]) -> bool:
        """Shed history after the provider said, or showed, that the request
        overflowed: clear old tool results, else summarize. ``tools`` are the
        registry's specs for the flight record, ``specs`` the specs the
        request sends. True when the history changed and the request is worth
        sending again."""
        action, _cleared, _reclaimed = compaction.recover_overflow(
            self._convo.messages, overflow_round, cfg=self._cfg,
            system=self._convo.system, error=error,
            flight_data={"context_window": self.context_window(),
                         "max_output_tokens": self._max_out,
                         "usage_anchor": vars(self._tokens).get("_anchor"),
                         "tools": tools,
                         "ai_messages": self._convo.ai},
        )
        if action == "cleared":
            self._rewritten()
            return True
        return await self.fit(phase="overflow_recovery", specs=specs, force=True,
                              overflow_round=overflow_round)

    def _rewritten(self) -> None:
        self._tokens.reset()
        self._convo.rebuild()
        self._context.compacted_during_turn = True
        compaction.history_rewritten(self._context)

    def _clear(self, check: _Check) -> bool:
        """Clear old tool-result bodies; True when that brought the request
        under budget."""
        cleared, chars = compaction.clear_for_budget(
            self._convo.messages, cfg=self._cfg, system=self._convo.system, trigger=check.trigger,
            flight_data=check.flight_data, over_budget=check.over_budget,
        )
        if not cleared:
            return False
        check.reclaimed += chars
        check.changed = True
        self._rewritten()
        self._telemetry.event("context_results_cleared", phase=check.phase, cleared=cleared)
        return not (check.force or check.over_budget())

    async def _summarize(self, check: _Check, preserve_from: int | None, focus: str, *,
                         tail_tokens: int | None = None) -> bool:
        cfg, context, messages = self._cfg, self._context, self._convo.messages
        if compaction.auto_paused(cfg, context):
            self._telemetry.event("context_compaction_skipped", phase=check.phase,
                                  reason="paused_after_failures")
            return False
        before_chars = compaction.history_chars(messages)
        self._status.compacting = True
        try:
            with stream_transport.net_role("Compacting"):
                result = await compaction.compact_now(
                    cfg, self._convo.system, messages, focus=focus, forced=True,
                    preserve_from=preserve_from, trigger=check.trigger, flight_data=check.flight_data,
                    tail_tokens=tail_tokens, context=context, emit=self._emit,
                )
        except Exception as exc:  # noqa: BLE001
            msgs.warn(msgs.COMPACTION_FAILED, error=f"{type(exc).__name__}: {exc}")
            self._telemetry.event("context_compaction_failed", phase=check.phase,
                                  error=f"{type(exc).__name__}: {exc}")
            if (paused := compaction.record_auto_failure(cfg, context)) is not None:
                msgs.say_said(paused, file=sys.stderr, flush=True)
            check.summary_failed = True
            return False
        finally:
            self._status.compacting = False
        if not compaction.compacted(result):
            self._telemetry.event("context_compaction_skipped", phase=check.phase, reason=result)
            return False
        check.reclaimed += before_chars - compaction.history_chars(messages)
        check.changed = True
        self._rewritten()
        self._telemetry.event("context_compacted", phase=check.phase, result=result)
        return True
