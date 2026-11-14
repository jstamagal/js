"""Per-model tool surfaces: agent.yaml `models:` entries and the TOOL_PROFILES setting.

A profile is one mapping:

    match: qwen              # string or list; case-insensitive substrings of
                             # the model id or the provider id
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
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import messages as msgs
from . import output
from . import settings as _settings
from .toolkit import policy

MODES = ("replace", "prepend")
KEYS = ("match", "mode", "tools", "core", "prompt", "sampling", "reasoning")

# The tools.yaml tag a `replace` profile keeps; without that tag, CORE_ENTRIES.
# `tool_discovery` is on the surface whenever skills, lazy tools or MCP
# servers are, so keeping `skill` keeps it too.
CORE_TAG = "core"
CORE_ENTRIES = ("skill:eager",)


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
        haystacks = [str(model or "").lower()]
        if provider_id:
            haystacks.append(str(provider_id).lower())
        return any(needle in haystack for needle in self.match for haystack in haystacks)

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


def parse_profile(raw: Any, where: str) -> ModelProfile:
    from .config import validate_agent_id
    from .persona import _coerce_reasoning_effort, _coerce_sampling

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
        sampling=_coerce_sampling(where, raw.get("sampling")),
        reasoning=_coerce_reasoning_effort(where, raw.get("reasoning")),
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
