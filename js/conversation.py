"""A conversation for programs that assemble js from its parts.

`Conversation` is the session lifecycle the REPL runs around each turn,
without the REPL: replay the session file, run the turn, save what it
added, then the between-turn compaction check. A program gives it a Config,
a system prompt and a tool registry; everything else is optional. `-p`
(`cli`), swarm agents (`swarm_run`) and subagents (`toolkit.subagents`) run
their turns through `Conversation.turn`.

    chat = Conversation(cfg, system, registry=tools)
    await chat.ask("what is in this directory?")
    await chat.compact("keep the API decisions")

The functions after the class are the turn bookkeeping `-p` and the REPL
share: the mode- and model-switch notes a user message carries, appending a
message to the session file, and the REPL's between-turn compaction trigger.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import (
    attach,
    compaction,
    compaction_settings,
    context_window,
    memory,
    messages as msgs,
    runtime,
    snippets,
    stream_transport,
)
from .config import Config
from .toolkit import ToolContext, ToolRegistry

if TYPE_CHECKING:
    from .repl_state import ReplState


class Conversation:
    """One session's history and the state that persists between its turns.

    The history is ``cfg.session_file``; with the default Config that is
    os.devnull, so nothing is kept. ``messages`` is the history its turns
    continue, the replay of ``cfg.session_file`` when None. ``context`` is
    the ToolContext every turn runs with, a new one in ``cwd`` when None; it
    holds the token counts and file snapshots that compaction and undo read
    across turns, so one Conversation keeps one. ``run_turn`` runs a turn and takes
    `runtime.run_turn_async`'s arguments; None is `runtime.run_turn_async`.
    Extra keywords to `ask` and `turn` go to it (``approve``, ``steer``,
    ``event_sink``...), over the Conversation's registry, context and
    checkpoint.
    """

    def __init__(
        self,
        cfg: Config,
        system: str,
        *,
        registry: ToolRegistry | None = None,
        cwd: Path | None = None,
        context: ToolContext | None = None,
        telemetry: runtime.Telemetry | None = None,
        on_notice: Callable[[str], None] = print,
        run_turn: Callable[..., Awaitable[None]] | None = None,
        messages: list[dict] | None = None,
    ) -> None:
        self.cfg = cfg
        self.system = system
        self.registry = registry
        self.telemetry = telemetry or runtime.Telemetry(None)
        self.on_notice = on_notice
        self.run_turn = run_turn
        self.context = context if context is not None else ToolContext(cwd=cwd or Path.cwd())
        self.auto = compaction.AutoCompactState()
        if Path(cfg.session_file) != Path("/dev/null"):
            Path(cfg.session_file).parent.mkdir(parents=True, exist_ok=True)
        self.messages = memory.load_replay_messages(cfg.session_file) if messages is None else messages

    @property
    def stamp(self) -> dict:
        """The stamp each assistant message this Conversation saves carries."""
        return memory.stamp_for(self.cfg.model, self.cfg.provider_id, self.cfg.reasoning_effort)

    async def ask(self, text: str, **kwargs: Any) -> str:
        """Run one turn on ``text``, then the between-turn check; returns the
        model's final answer."""
        await self.turn({"role": "user", "content": text}, **kwargs)
        await self.maybe_compact()
        return self.answer()

    async def turn(self, message: dict, *, started: Callable[[], None] | None = None, **kwargs: Any) -> None:
        """Append ``message`` and run one turn on it.

        ``started`` is called once ``message`` is in the history, before the
        turn runs. The turn is saved after each batch of tool results, and
        when it ends, fails or is cancelled. The runtime compacts mid-turn
        when one turn outgrows the window; between turns, `maybe_compact`
        decides.
        """
        self.messages.append(memory.note_time(message))
        run = runtime.run_turn_async if self.run_turn is None else self.run_turn
        try:
            if started is not None:
                started()
            await run(self.cfg, self.system, self.messages, self.telemetry, **{
                "tool_registry": self.registry, "tool_context": self.context, "checkpoint": self.save, **kwargs})
        finally:
            self.save()

    def answer(self) -> str:
        """The last assistant reply with text, or ""."""
        for message in reversed(self.messages):
            if message.get("role") == "assistant" and message.get("content"):
                return str(message["content"])
        return ""

    async def maybe_compact(self, *, emit: Callable[..., Any] | None = None, log_dir: Path | None = None) -> bool:
        """The between-turn check: compact when the history is full enough,
        as COMPACTION_AUTO says. ``emit`` and ``log_dir`` go to
        `compaction.compact_now`."""
        if not compaction_settings.get_bool(self.cfg, "COMPACTION_AUTO"):
            return False
        window = lambda: context_window.resolve_context_window(  # noqa: E731
            self.cfg.model, self.cfg.provider_id, self.cfg.provider_base_url)
        outcome = await compaction.maybe_auto_compact_async(
            self.cfg, self.auto, self.context, self.system, self.messages, window, emit=emit, log_dir=log_dir)
        for notice in outcome.notices:
            self.on_notice(str(notice))
        if outcome.result is not None:
            self.on_notice(str(outcome.result))
        return outcome.compacted

    async def compact(self, focus: str = "") -> str:
        """Summarize the history before the kept tail now; returns what happened."""
        return await compaction.compact_now(
            self.cfg, self.system, self.messages, focus=focus, forced=True, context=self.context)

    def save(self) -> None:
        """Append what the session file does not have yet."""
        memory.persist_messages(self.cfg.session_file, self.messages, stamp=self.stamp)


_MODE_PLACES = {"repl": 'in interactive chat', "-p": 'as a one-shot run'}


def _mode_switch_notice(started: str, previous: str, now: str) -> str:
    """The reminder for a turn in mode ``now`` after a turn in ``previous``, in
    a session whose first turn ran in ``started``."""
    if started == now:
        course = (f'started {_MODE_PLACES[started]}, moved {_MODE_PLACES[previous]}, and now continues {_MODE_PLACES[now]} again')
    else:
        course = f'started {_MODE_PLACES[started]} and now continues {_MODE_PLACES[now]}'
    return snippets.render("mode-switch", COURSE=course, PRESENCE=snippets.render(
        "mode-presence-chat" if now == "repl" else "mode-presence-prompt"))


def note_mode_switch(cfg: Config, message: dict, mode: str) -> dict:
    """Record that this turn runs in ``mode`` ("repl" or "-p"). When the last
    turn ran in the other mode, this turn's user message carries the
    mode-switch reminder, which names the mode of the session's first turn."""
    if cfg.session_file == Path(os.devnull):
        return message
    previous = memory.record_turn_mode(cfg.session_file, mode)
    if previous is None:
        return message
    started = memory.first_turn_mode(cfg.session_file)
    if started not in _MODE_PLACES:
        started = previous
    return attach.with_note(message, _mode_switch_notice(started, previous, mode))


def stamp_label(model: str | None, provider: str | None) -> str:
    return f"{provider}/{model}" if provider else str(model)


def note_model_switch(cfg: Config, message: dict) -> dict:
    """When the session's newest reply came from a model other than the one
    ``cfg`` runs this turn on, this turn's user message carries the
    model-switch reminder. A stamp without a provider compares on the model id."""
    if cfg.session_file == Path(os.devnull):
        return message
    stamp = memory.last_reply_stamp(cfg.session_file)
    if stamp is None:
        return message
    provider = stamp.get("provider") if isinstance(stamp.get("provider"), str) else None
    if stamp["model"] == cfg.model and (provider is None or provider == cfg.provider_id):
        return message
    return attach.with_note(message, snippets.render("model-changed",
        PREVIOUS=stamp_label(stamp["model"], provider),
        CURRENT=stamp_label(cfg.model, cfg.provider_id),
    ))


def append_turn(cfg: Config, message: dict) -> None:
    memory.append_message(cfg.session_file, message)


async def maybe_auto_compact_async(cfg: Config, state: ReplState) -> None:
    """The REPL's between-turn trigger: the policy lives in compaction, the
    printing lives here. The gate reads the live settings, so a /set made
    during the turn decides whether the turn's end compacts."""
    active_cfg = state.config_for_turn(cfg)
    if not compaction_settings.get_bool(active_cfg, "COMPACTION_AUTO"):
        return
    turn_status = runtime.T.STOCK_CONTEXT.turn_status
    turn_status.compacting = True
    try:
        with stream_transport.net_role(msgs.COMPACTING_ROLE.words()):
            outcome = await compaction.maybe_auto_compact_async(
                active_cfg,
                state.auto_compact,
                runtime.T.STOCK_CONTEXT,
                state.system,
                state.messages,
                lambda: context_window.resolve_context_window(
                    active_cfg.model, active_cfg.provider_id, active_cfg.provider_base_url
                ),
                **compaction.compact_hooks(state, active_cfg),
            )
    finally:
        turn_status.compacting = False
    for notice in outcome.notices:
        msgs.say_said(notice)
    if outcome.result is not None:
        msgs.say(msgs.COMPACTION_DONE, result=outcome.result)
