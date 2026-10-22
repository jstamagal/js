"""Invocation-local prompt and tool overrides shared by CLI modes."""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
from fnmatch import fnmatchcase
from dataclasses import dataclass
from pathlib import Path

from .toolkit import policy


@dataclass(frozen=True)
class RunOptions:
    system_prompt_file: str | None = None
    system_prompt: str | None = None
    append_system_prompt: tuple[str, ...] = ()
    you_are: bool = False
    noenv: bool = False
    chat: bool = False
    no_agents_md: bool = False
    no_skills: bool = False
    no_mcp: bool = False
    with_tools: str | None = None
    without_tools: str | None = None
    tools: str | None = None
    remove_if_fail: str | None = None

    @property
    def replaces_prompt(self) -> bool:
        return self.system_prompt_file is not None or self.system_prompt is not None or self.you_are

    @property
    def changes_prompt(self) -> bool:
        return self.replaces_prompt or bool(self.append_system_prompt)

    def selectors(self, original: tuple[str, ...]) -> tuple[str, ...]:
        entries = () if self.chat else original
        if self.tools is not None:
            entries = policy.parse_entries(json.loads(self.tools), '--tools')
        if self.with_tools is not None:
            entries = policy.parse_entries([
                item.strip() if ':' in item else f'{item.strip()}:eager'
                for item in self.with_tools.split(',')
            ], '--with-tools')
        if self.no_skills:
            entries = ('skill:ban', *entries)
        if self.without_tools is not None:
            names = [item.strip() for item in self.without_tools.split(',')]
            if any(not name or ':' in name for name in names):
                raise ValueError('--without-tools requires comma-separated tool names or globs')
            config = policy.load_tools_config()
            rules = policy.expand(entries, config, '--without-tools')
            # Flatten tags so explicit exclusions precede even named tag members.
            entries = tuple(rule.entry.text for rule in rules
                            if not any(any(char in name for char in '*?[')
                                       and fnmatchcase(rule.entry.noun, name.lower()) for name in names))
            entries = tuple(f'{name}:ban' for name in names) + entries
        return entries

    def prompt_text(self) -> str:
        if self.system_prompt_file is not None:
            return Path(self.system_prompt_file).expanduser().read_text(encoding='utf-8')
        return self.system_prompt or ''


_current: contextvars.ContextVar[RunOptions] = contextvars.ContextVar('js_run_options', default=RunOptions())


def current() -> RunOptions:
    return _current.get()


@contextlib.contextmanager
def invocation(options: RunOptions):
    from .model_health import HealthSession, RemovalPolicy, _session
    health = HealthSession(RemovalPolicy.parse(options.remove_if_fail)) if options.remove_if_fail is not None else None
    health_token = _session.set(health)
    token = _current.set(options)
    previous = os.environ.get('JS_NOENV')
    if options.noenv or options.chat:
        os.environ['JS_NOENV'] = '1'
    try:
        yield
    finally:
        _session.reset(health_token)
        if health is not None:
            health.finish()
        _current.reset(token)
        if previous is None:
            os.environ.pop('JS_NOENV', None)
        else:
            os.environ['JS_NOENV'] = previous
