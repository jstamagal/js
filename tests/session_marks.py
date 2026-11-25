"""Write session marks the way a run leaves them, for tests that start from
a session already compacted."""

from __future__ import annotations

from pathlib import Path

from js import memory


def append_compaction_mark(memory_file: Path, *, summary: str, keep_from: int, forced: bool = False,
                           trigger: dict | None = None, rehydrated: dict | None = None) -> None:
    """Append the mark a compaction to ``summary`` keeping messages from
    ``keep_from`` on would write."""
    marker, _history = memory.compaction_mark([], keep_from, summary=summary, forced=forced,
                                              trigger=trigger, rehydrated=rehydrated)
    memory.append_mark(memory_file, marker)
