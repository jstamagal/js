"""Initialize a conversation from an agent's user/agent message files."""

from __future__ import annotations

from . import memory, persona, runtime


async def initialize(cfg, spec, messages: list[dict], telemetry, *,
                     system: str | None = None, save: bool = True, **turn_kwargs) -> None:
    """Append startup exchanges to a fresh conversation, persisting each step.

    Paired exchanges and assistant-only entries are synthetic. An unpaired user
    entry runs a normal model turn. Expansion happens when each entry is reached.
    Existing conversation history already contains its initialization.
    """
    if messages:
        return
    stamp = memory.stamp_for(cfg.model, cfg.provider_id, cfg.reasoning_effort)
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
            reply_start = len(messages)
            try:
                await runtime.run_turn_async(
                    cfg, spec.system if system is None else system, messages, telemetry, **turn_kwargs,
                )
            finally:
                if save:
                    memory.persist_messages(cfg.session_file, messages, stamp=stamp)
                if sink is not None and turn_kwargs.get("suppress_output"):
                    for message in messages[reply_start:]:
                        if message.get("role") == "assistant" and message.get("content") and not message.get("tool_calls"):
                            sink.write_assistant(message["content"])
