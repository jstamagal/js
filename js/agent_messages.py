"""Initialize a conversation from an agent's user/agent message files."""

from __future__ import annotations

from collections.abc import Callable

from . import memory, persona, runtime


async def initialize(cfg, spec, messages: list[dict], telemetry, *,
                     system: str | None = None, save: bool = True,
                     echo_user: Callable[[str], None] | None = None, **turn_kwargs) -> None:
    """Append startup exchanges to a fresh conversation, persisting each step.

    Paired exchanges and assistant-only entries are synthetic. An unpaired user
    entry runs a normal model turn, shown through `echo_user` ahead of the
    reply the turn streams. Expansion happens when each entry is reached.
    Existing conversation history already contains its initialization.
    """
    if messages:
        return
    reasoning = turn_kwargs.get("reasoning_effort_override", cfg.reasoning_effort)
    stamp = memory.stamp_for(cfg.model, cfg.provider_id, reasoning)
    sink = telemetry.transcript_log
    for exchange in spec.exchanges:
        if exchange.user is not None:
            text = persona.expand_agent_text(exchange.user, cfg)
            message = {"role": "user", "content": text}
            messages.append(message)
            if save:
                memory.append_message(cfg.session_file, message)
            if sink is not None:
                sink.write_user(text)
        if exchange.agent is not None:
            text = persona.expand_agent_text(exchange.agent, cfg)
            message = {"role": "assistant", "content": text}
            messages.append(message)
            if save:
                memory.append_message(cfg.session_file, message, stamp=stamp)
            if sink is not None:
                sink.write_assistant(text)
        elif exchange.user is not None:
            if echo_user is not None:
                echo_user(text)
            try:
                await runtime.run_turn_async(
                    cfg, spec.system if system is None else system, messages, telemetry, **turn_kwargs,
                )
            finally:
                if save:
                    memory.persist_messages(cfg.session_file, messages, stamp=stamp)
