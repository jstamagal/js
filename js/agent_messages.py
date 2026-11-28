"""Initialize a conversation from an agent's user/agent message files."""

from __future__ import annotations

import re
from collections.abc import Callable

from . import memory, messages as msgs, persona, runtime

# The control words of a user file. One of them as the last line of the file's
# expanded text makes the file a control step: nothing of it is sent.
PASS, LOOP, HALT = "PASS", "LOOP", "HALT"
_CONTROL_LINE = re.compile(rf"%%({PASS}|{LOOP}|{HALT})%%")
# A user file that names the PROMPT built-in, not escaped or in a code span,
# takes the run's prompt into itself.
_PROMPT_VARIABLE = re.compile(r"(?<![\\`])%%PROMPT%%")


def takes_prompt(spec) -> bool:
    """Whether a user file of ``spec`` takes the run's prompt (%%PROMPT%%).
    The prompt then reaches the model inside that file and is not sent again."""
    return any(exchange.user is not None and _PROMPT_VARIABLE.search(exchange.user)
               for exchange in spec.exchanges)


def control_word(text: str) -> str | None:
    """The control word that is the last line of ``text``, else None."""
    lines = text.rstrip().rsplit("\n", 1)
    match = _CONTROL_LINE.fullmatch(lines[-1].strip())
    return match[1] if match else None


async def initialize(cfg, spec, messages: list[dict], telemetry, *,
                     system: str | None = None, save: bool = True,
                     echo_user: Callable[[str], None] | None = None, prompt: str = "",
                     **turn_kwargs) -> str | None:
    """Append startup exchanges to a fresh conversation, persisting each step.

    Paired exchanges and assistant-only entries are synthetic. An unpaired user
    entry runs a normal model turn, shown through `echo_user` ahead of the
    reply the turn streams. Expansion happens when each entry is reached.
    Existing conversation history already contains its initialization.

    A user entry whose expanded text ends in a control word sends nothing, and
    its agent entry is not recorded. PASS goes on to the next exchange. LOOP
    and HALT end the exchanges there and are returned; the caller decides what
    ends with them.

    ``prompt`` is the PROMPT built-in of the user files: the run's `-p` text.
    """
    if messages:
        return None
    # Stamp what the turn runs with: the same override-or-cfg rule as
    # runtime._turn_request.
    model = turn_kwargs.get("model_override") or cfg.model
    provider_id = turn_kwargs.get("provider_id_override")
    if provider_id is None:
        provider_id = cfg.provider_id
    reasoning = turn_kwargs.get("reasoning_effort_override", cfg.reasoning_effort)
    stamp = memory.stamp_for(model, provider_id, reasoning)
    sink = telemetry.transcript_log
    for exchange in spec.exchanges:
        user_text = None
        if exchange.user is not None:
            user_text = persona.expand_agent_text(exchange.user, cfg, {"PROMPT": prompt})
            word = control_word(user_text)
            if word is not None:
                msgs.warn(msgs.EXCHANGE_CONTROL_WORD, number=exchange.number, word=word)
                if word == PASS:
                    continue
                return word
            message = {"role": "user", "content": user_text}
            messages.append(message)
            if save:
                memory.append_message(cfg.session_file, message)
            if sink is not None:
                sink.write_user(user_text)
        if exchange.agent is not None:
            text = persona.expand_agent_text(exchange.agent, cfg)
            message = {"role": "assistant", "content": text}
            messages.append(message)
            if save:
                memory.append_message(cfg.session_file, message, stamp=stamp)
            if sink is not None:
                sink.write_assistant(text)
        elif user_text is not None:
            if echo_user is not None:
                echo_user(user_text)
            try:
                await runtime.run_turn_async(
                    cfg, spec.system if system is None else system, messages, telemetry, **turn_kwargs,
                )
            finally:
                if save:
                    memory.persist_messages(cfg.session_file, messages, stamp=stamp)
    return None
