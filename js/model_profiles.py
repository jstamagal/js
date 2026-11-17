"""Per-model tool surfaces: agent.yaml `models:` entries and the TOOL_PROFILES setting.

A profile is one mapping:

    match: [sol, terra]      # string or list; model family names, matched as
                             # whole words of the model id or the provider id
    mode: replace            # replace (the default) or prepend
    tools: [shell:eager]     # chain entries, as in agent.yaml `tools:`
    core: true               # replace keeps the core entries unless false
    prompt: small-coder      # another agent whose prompt is the persona
    sampling: {temperature: 0.2}
    reasoning: low

The first agent `models:` entry that matches the run's model wins; with none,
the first matching TOOL_PROFILES entry. `replace` puts the profile's entries
and the core entries in place of the agent's chain; `prepend` puts the
profile's entries ahead of it, so under first-match-wins they decide every
tool they name. A profile without `tools:` leaves the chain as it is.

A `match` word is a whole word of the id: `sol` matches `cpa/sol:latest` and
`gpt-6.1-sol-medium`, not `console`. A word may be followed by a version
number, so `qwen` matches `Qwen3.6-35B-A3B`. A multi-word entry
(`claude-opus`) matches those words in a row.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import messages as msgs, output, setting_values, settings as _settings
from .config import validate_agent_id
from .sampling import SAMPLING_FIELDS
from .toolkit import policy

MODES = ("replace", "prepend")
KEYS = ("match", "mode", "tools", "core", "prompt", "sampling", "reasoning")

# The tools.yaml tag a `replace` profile keeps; without that tag, CORE_ENTRIES.
# `tool_discovery` is on the surface whenever skills, lazy tools or MCP
# servers are, so keeping `skill` keeps it too.
CORE_TAG = "core"
CORE_ENTRIES = ("skill:eager",)


def _words(text: Any) -> list[str]:
    return [word for word in re.split(r"[^a-z0-9]+", str(text or "").lower()) if word]


def _word_matches(needle: str, word: str) -> bool:
    """``word`` is ``needle``, or ``needle`` followed by a version number."""
    return word == needle or (word.startswith(needle) and word[len(needle)].isdigit())


def _has_run(words: list[str], needles: list[str]) -> bool:
    if not needles:
        return False
    last = len(words) - len(needles)
    return any(
        all(_word_matches(needle, words[start + offset]) for offset, needle in enumerate(needles))
        for start in range(last + 1)
    )


@dataclass(frozen=True)
class ModelProfile:
    match: tuple[str, ...]
    tools: tuple[str, ...] | None = None
    mode: str = "replace"
    core: bool = True
    prompt: str = ""
    sampling: Mapping[str, Any] = field(default_factory=dict)
    reasoning: str | None = None

    @property
    def label(self) -> str:
        return ", ".join(self.match)

    def matches(self, model: str, provider_id: str | None) -> bool:
        haystacks = [_words(model)]
        if provider_id:
            haystacks.append(_words(provider_id))
        return any(_has_run(words, _words(needle)) for needle in self.match for words in haystacks)

    def chain(self, agent_chain: Sequence[str], config: policy.ToolsConfig) -> tuple[str, ...]:
        """The tool chain this profile makes of the agent's own."""
        if self.tools is None:
            return tuple(agent_chain)
        if self.mode == "prepend":
            return (*self.tools, *agent_chain)
        if not self.core:
            return self.tools
        core = (f"tag:{CORE_TAG}",) if CORE_TAG in config.tags else CORE_ENTRIES
        return (*self.tools, *core)


# Sampling settings an agent may set under `sampling:` in agent.yaml: the
# sampling settings' own names, in lower case.
_SAMPLING_KEYS = SAMPLING_FIELDS


# The agent.yaml value of `model:` or `reasoning:` that takes the value from
# the parent (a subagent) or the run's settings (a main agent), as leaving the
# key out does.
INHERIT = "inherit"


def coerce_reasoning_effort(path: Path, raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip().lower() == INHERIT:
        return None
    # PyYAML's YAML 1.1 resolver parses an unquoted `off` as False.
    if raw is False:
        return "off"
    if not isinstance(raw, str):
        raise ValueError(msgs.AGENT_MANIFEST_REASONING_IN_MUST_BE_A_STRING.format(path=path))
    spec = _settings.SPEC_BY_KEY["EFFORT"]
    value, error = setting_values.coerce_value(spec, raw)
    if error:
        raise ValueError(msgs.AGENT_MANIFEST_REASONING_IN.format(path=path, error=error))
    return value


def coerce_sampling(path: Path, raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(msgs.AGENT_MANIFEST_SAMPLING_FRONTMATTER_IN_MUST_BE_A_MAPPING.format(path=path))
    out: dict[str, Any] = {}
    for key, val in raw.items():
        if key not in _SAMPLING_KEYS:
            raise ValueError(
                msgs.AGENT_MANIFEST_UNKNOWN_SAMPLING_KEY_IN_ALLOWED.format(key=key, path=path, value=', '.join(_SAMPLING_KEYS))
            )
        if isinstance(val, list):
            text = json.dumps(val)
        elif isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ValueError(msgs.AGENT_MANIFEST_SAMPLING_IN_MUST_BE_A_NUMBER.format(key=key, path=path))
        else:
            text = str(val)
        value, error = setting_values.coerce_value(_settings.SPEC_BY_KEY[key.upper()], text)
        if error is not None:
            raise ValueError(msgs.AGENT_MANIFEST_SAMPLING_IN.format(key=key, path=path, error=error))
        out[key] = value
    return out


def parse_profile(raw: Any, where: str) -> ModelProfile:
    if not isinstance(raw, dict):
        raise ValueError(msgs.MODEL_PROFILE_NOT_A_MAPPING.format(where=where))
    unknown = sorted(str(key) for key in raw if key not in KEYS)
    if unknown:
        raise ValueError(msgs.MODEL_PROFILE_UNKNOWN_KEYS.format(
            where=where, keys=", ".join(unknown), allowed=", ".join(KEYS)))
    match = raw.get("match")
    needles = [match] if isinstance(match, str) else match
    if (not isinstance(needles, list) or not needles
            or any(not isinstance(item, str) or not item.strip() for item in needles)):
        raise ValueError(msgs.MODEL_PROFILE_MATCH.format(where=where))
    mode = raw.get("mode", "replace")
    if mode not in MODES:
        raise ValueError(msgs.MODEL_PROFILE_MODE.format(where=where, modes=", ".join(MODES)))
    core = raw.get("core", True)
    if not isinstance(core, bool):
        raise ValueError(msgs.MODEL_PROFILE_CORE.format(where=where))
    prompt = raw.get("prompt") or ""
    if prompt:
        try:
            validate_agent_id(prompt if isinstance(prompt, str) else "")
        except ValueError as exc:
            raise ValueError(msgs.MODEL_PROFILE_PROMPT.format(where=where, error=exc)) from exc
    return ModelProfile(
        match=tuple(item.strip().lower() for item in needles),
        tools=policy.parse_entries(raw["tools"], f"{where} tools") if "tools" in raw else None,
        mode=mode,
        core=core,
        prompt=prompt,
        sampling=coerce_sampling(where, raw.get("sampling")),
        reasoning=coerce_reasoning_effort(where, raw.get("reasoning")),
    )


def parse_profiles(raw: Any, where: str) -> tuple[ModelProfile, ...]:
    """A `models:` list or a TOOL_PROFILES value; None is no profiles."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(msgs.MODEL_PROFILES_NOT_A_LIST.format(where=where))
    return tuple(parse_profile(item, f"{where}[{index}]") for index, item in enumerate(raw))


def setting_profiles(store: Mapping[str, Any] | None) -> tuple[ModelProfile, ...]:
    """The TOOL_PROFILES setting's profiles."""
    return parse_profiles(_settings.knob(dict(store or {}), "TOOL_PROFILES"), "TOOL_PROFILES")


def choose(
    agent_profiles: Iterable[ModelProfile],
    store: Mapping[str, Any] | None,
    model: str,
    provider_id: str | None,
) -> ModelProfile | None:
    """The first agent profile matching the model or provider; with none, the
    first matching TOOL_PROFILES profile."""
    for profile in (*agent_profiles, *setting_profiles(store)):
        if profile.matches(model, provider_id):
            return profile
    return None


_warned_empty: set[tuple[str, ...]] = set()


def warn_empty(profile: ModelProfile) -> None:
    """One notice per process for a profile that would leave no tools."""
    if profile.match in _warned_empty:
        return
    _warned_empty.add(profile.match)
    output.notice(msgs.TOOL_PROFILE_LEAVES_NO_TOOLS, match=profile.label)
