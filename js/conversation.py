"""A conversation for programs that assemble js from its parts.

`Conversation` is the session lifecycle the REPL runs around each turn,
without the REPL: replay the session file, run the turn, save what it
added, then the between-turn compaction check. A program gives it a Config,
a system prompt and a tool registry; everything else is optional. Swarm
agents (`swarm_run`) and subagents (`toolkit.subagents`) run their turns through
`Conversation.turn`.

    chat = Conversation(cfg, system, registry=tools)
    await chat.ask("what is in this directory?")
    await chat.compact("keep the API decisions")
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from . import compaction, memory, runtime
from .config import Config
from .toolkit import ToolContext, ToolRegistry


class Conversation:
    """One session's history and the state that persists between its turns.

    The history is ``cfg.session_file``; with the default Config that is
    os.devnull, so nothing is kept. ``context`` is the ToolContext every turn
    runs with, a new one in ``cwd`` when None; it holds the token counts and
    file snapshots that compaction and undo read across turns, so one
    Conversation keeps one. ``run_turn`` runs a turn and takes
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
        self.messages = memory.load_replay_messages(cfg.session_file)

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

    async def turn(self, message: dict, **kwargs: Any) -> None:
        """Append ``message`` and run one turn on it.

        The turn is saved after each batch of tool results, and when it ends,
        fails or is cancelled. The runtime compacts mid-turn when one turn
        outgrows the window; between turns, `maybe_compact` decides.
        """
        self.messages.append(memory.note_time(message))
        run = runtime.run_turn_async if self.run_turn is None else self.run_turn
        try:
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
        if not compaction.get_bool(self.cfg, "COMPACTION_AUTO"):
            return False
        window = lambda: runtime._resolve_context_window(  # noqa: E731
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
