"""The session file's record of a turn's tool surface.

A lazy registry changes its surface as the model loads tools. `SurfaceJournal`
appends each new surface to the session file under a scope (agent and working
directory), and restores the last one saved under the same scope when the
next turn starts. A session file of os.devnull is never read or written.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import memory


class SurfaceJournal:
    def __init__(self, session_file: Path | str | None, *, agent_id: str, cwd: Path, registry: Any):
        if session_file is not None and Path(session_file).resolve() == Path(os.devnull):
            session_file = None
        self._file = session_file
        self._scope = {"version": 1, "agent_id": agent_id, "cwd": str(Path(cwd).resolve())}
        self._registry = registry
        self._prior = memory.load_tool_surface(session_file) if session_file is not None else None
        self._last: dict | None = None

    async def restore(self) -> None:
        """Restore the surface saved under this scope, then journal every
        change the registry reports."""
        prior = self._prior
        if prior is not None and all(prior.get(k) == v for k, v in self._scope.items()):
            await self._registry.restore(prior)
        self._last = self._registry.snapshot()
        self._registry.on_change = self.on_change

    def on_change(self, state: dict) -> None:
        """Append ``state`` when it differs from the last surface seen."""
        if state == self._last:
            return
        if self._file is not None:
            memory.append_tool_surface(self._file, {**self._scope, **state})
        self._last = state
