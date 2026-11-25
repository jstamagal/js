"""Filename conventions shared by agent discovery and prompt loading."""

import re
from collections.abc import Iterable
from pathlib import Path

AGENT_MANIFEST = "agent.yaml"
PROMPT_KIND = "prompt"
ENV_KIND = "env"
MESSAGE_FILE = re.compile(r"(?:(\d+)[-_])?(user|agent)")


def is_message_file(path: Path) -> bool:
    return MESSAGE_FILE.fullmatch(path.stem) is not None


def is_zero_file(path: Path) -> bool:
    stem = path.stem
    return stem == "00" or stem.startswith("00-") or stem.startswith("00_")


def _ends_in_keyword(path: Path, keyword: str) -> bool:
    stem = path.stem
    return stem == keyword or stem.endswith(f"-{keyword}") or stem.endswith(f"_{keyword}")


def is_benchmark_file(path: Path) -> bool:
    """A NN-benchmark.md (or bare benchmark.md) file: a --bench turn, never part
    of the system prompt."""
    return _ends_in_keyword(path, "benchmark")


def system_kind(path: Path) -> str | None:
    """`prompt` or `env` for a file that is system-prompt text, else None."""
    for kind in (PROMPT_KIND, ENV_KIND):
        if _ends_in_keyword(path, kind):
            return kind
    return None


def _is_agent_filename(path: Path) -> bool:
    """Whether the filename identifies an agent source, including legacy manifests."""
    if path.name == AGENT_MANIFEST or (path.suffix == ".yaml" and is_zero_file(path)):
        return True
    return path.suffix == ".md" and (
        system_kind(path) is not None or is_message_file(path) or is_benchmark_file(path) or is_zero_file(path)
    )


def has_agent_source(entries: Iterable[Path]) -> bool:
    return any(_is_agent_filename(p) and (p.is_symlink() or p.is_file()) for p in entries)
