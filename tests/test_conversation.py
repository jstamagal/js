from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import ai
import ai.models

from js import model_client
from js.config import Config
from js.conversation import Conversation


class _Echo(ai.models.Provider):
    """Answers with every user line it was sent, so a reply shows the history.
    Asked for a summary, it answers with a short one."""

    provider_class_id: str = "test-conversation-echo"
    name: str = "echo"
    default_base_url: str = "http://echo.invalid"

    async def stream(self, model, messages, **kwargs):
        said = [part.text for message in messages if message.role == "user"
                for part in message.parts if getattr(part, "text", None)]
        reply = "short summary" if "<conversation>" in " ".join(said) else " | ".join(said)
        async with ai.models.Stream.replay_message(ai.assistant_message(reply)) as stream:
            async for event in stream:
                yield event


def _echo_model():
    return model_client.using_model_factory(lambda model_id, **_route: ai.Model(id=model_id, provider=_Echo()))


def _cfg(tmp_path: Path, **settings) -> Config:
    return Config(model="echo", trace=False, session_file=tmp_path / "sessions" / "chat.jsonl",
                  settings={"COMPACTION_AUTO": False, **settings})


def _ask(chat: Conversation, text: str) -> str:
    async def run() -> str:
        with _echo_model():
            return await chat.ask(text, suppress_output=True)
    return asyncio.run(run())


def test_a_new_conversation_on_the_same_session_file_replays_its_history(tmp_path):
    first = Conversation(_cfg(tmp_path), "system")
    assert _ask(first, "my number is 7311") == "my number is 7311"

    second = Conversation(_cfg(tmp_path), "system")

    assert "my number is 7311" in _ask(second, "what is my number?")


def test_compact_replaces_the_old_history_with_a_summary_and_keeps_the_session(tmp_path):
    cfg = _cfg(tmp_path, COMPACTION_KEEP_RECENT_TOKENS=1, COMPACTION_MIN_SAVINGS_TOKENS=1)
    chat = Conversation(cfg, "system")
    for text in ("one " * 200, "two " * 200, "three"):
        _ask(chat, text)
    before = len(chat.messages)

    async def compact() -> str:
        with _echo_model():
            return await chat.compact()
    asyncio.run(compact())

    assert len(chat.messages) < before
    assert "compaction-summary" in str(chat.messages[0]["content"])
    assert len(Conversation(replace(cfg), "system").messages) == len(chat.messages)


def test_a_failed_turn_is_still_saved(tmp_path):
    chat = Conversation(_cfg(tmp_path), "system")

    async def run() -> None:
        with model_client.using_model_factory(lambda model_id, **_route: (_ for _ in ()).throw(RuntimeError("down"))):
            await chat.ask("remember this", suppress_output=True)

    try:
        asyncio.run(run())
    except Exception:  # noqa: BLE001 - the failure itself is not under test
        pass

    replayed = Conversation(_cfg(tmp_path), "system").messages
    assert any(message.get("content") == "remember this" for message in replayed)
