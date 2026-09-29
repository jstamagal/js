"""The conversation a turn sends the model, and the context budget it is held to.

`TurnConvo` is the turn's SDK message list, built from the history, and the
trace cursor over it: the request trace dumps the system prompt and tool
schemas once, then only the messages not yet sent.
"""

from __future__ import annotations

from typing import Any

from . import model_client


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
