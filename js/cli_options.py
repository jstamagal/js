"""Invocation-local prompt and tool overrides shared by CLI modes."""
from __future__ import annotations

import contextlib
import contextvars
import os
from dataclasses import dataclass
from pathlib import Path

from . import messages as msgs
from .toolkit import policy


@dataclass(frozen=True)
class RunOptions:
    system_prompt_file: str | None = None
    system_prompt: str | None = None
    append_system_prompt: tuple[str, ...] = ()
    # --you-are: None when absent, the TEXT after it (or '') when given.
    you_are: str | None = None
    no_env: bool = False
    chat: bool = False
    # The agent named on the command line with --agent; --chat takes its persona from it.
    agent: str | None = None
    no_agents_md: bool = False
    no_skills: bool = False
    no_mcp: bool = False
    no_tools: bool = False
    tools: str | None = None

    @property
    def replaces_prompt(self) -> bool:
        return self.system_prompt_file is not None or self.system_prompt is not None or self.you_are is not None

    @property
    def changes_prompt(self) -> bool:
        return self.replaces_prompt or bool(self.append_system_prompt)

    @property
    def zero_tools(self) -> bool:
        """The run starts from no tools; only --tools adds any."""
        return self.no_tools or self.chat or self.you_are is not None

    @property
    def skips_env(self) -> bool:
        """No `*-env.md` file loads and envctx prints nothing."""
        return self.no_env or self.chat or self.you_are is not None or self.system_prompt_file is not None

    @property
    def skips_agents_md(self) -> bool:
        return (self.no_agents_md or self.chat or self.you_are is not None
                or self.system_prompt_file is not None)

    @property
    def skips_skills(self) -> bool:
        return self.no_skills or self.zero_tools

    @property
    def skips_mcp(self) -> bool:
        return self.no_mcp or self.zero_tools

    def selectors(self, original: tuple[str, ...]) -> tuple[str, ...]:
        if self.tools is not None:
            entries = tool_list(self.tools)
        elif self.zero_tools:
            entries = ()
        else:
            entries = original
        if self.no_skills:
            entries = ('skill:ban', *entries)
        return entries

    def unmatched_tools(self, tools) -> tuple[str, ...]:
        """The --tools entries, other than tags, that name no tool in `tools`."""
        if self.tools is None:
            return ()
        rules = policy.expand(tool_list(self.tools), policy.load_tools_config(), '--tools')
        return tuple(rule.entry.text for rule in rules
                     if not rule.via and rule.entry.noun != 'tag'
                     and not any(rule.matches(tool) for tool in tools))

    def prompt_text(self) -> str:
        if self.system_prompt_file is not None:
            return Path(self.system_prompt_file).expanduser().read_text(encoding='utf-8')
        return self.system_prompt or ''


def tool_list(text: str) -> tuple[str, ...]:
    """`--tools shell,fetch:lazy,web*`: tool names, globs or tags, each eager
    unless it says `:eager` or `:lazy`."""
    entries = []
    for item in text.split(','):
        item = item.strip()
        noun, sep, modifier = item.partition(':')
        if noun.strip().lower() == 'tag':
            entries.append(item)
            continue
        if not noun.strip() or (sep and modifier.strip() not in ('eager', 'lazy')):
            raise ValueError(msgs.TOOLS_LIST_ENTRY.text(entry=item))
        entries.append(item if sep else f'{item}:eager')
    return policy.parse_entries(entries, '--tools')


_current: contextvars.ContextVar[RunOptions] = contextvars.ContextVar('js_run_options', default=RunOptions())


def current() -> RunOptions:
    return _current.get()


@contextlib.contextmanager
def invocation(options: RunOptions):
    token = _current.set(options)
    previous = os.environ.get('JS_NOENV')
    if options.skips_env:
        os.environ['JS_NOENV'] = '1'
    try:
        yield
    finally:
        _current.reset(token)
        if previous is None:
            os.environ.pop('JS_NOENV', None)
        else:
            os.environ['JS_NOENV'] = previous
