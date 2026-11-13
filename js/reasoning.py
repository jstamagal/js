"""The effort setting: one word, sent as typed, translated where js knows the model.

`EFFORT` (`-r`) takes any word and every request carries it. When the model
name matches a family below and the word is on the effort ladder

    none < minimal < low < medium < high < xhigh < max

(``off`` is ``none``), the word is snapped to the nearest stop that family
serves. Any other word, or a model no table names, gets the word verbatim and
the endpoint answers for it. The banner shows what was sent.

Sending the word as typed is deliberate, not a bug: custom chat templates
define their own levels, so js never filters or maps an unknown word. An
endpoint that rejects the word (HTTP 400 for `off`, `default`, ...) is the
expected result.

The supported sets below are ground-truthed by live probe (2026-06-30) against
the endpoints js targets, not vendor docs:

    mimo  (xiaomi direct)      low, medium, high                 (others 400/500)
    kimi  (moonshot/opencode)  minimal, low, medium, high        (none/xhigh/max 400)
    glm   (zhipu/opencode-go)  none, low, medium, high, xhigh, max
    deepseek                   low, medium, high, xhigh, max     (none/minimal 400)
    codex (gpt-5.x Responses)  minimal, low, medium, high, xhigh
"""

from __future__ import annotations

from . import settings as _settings

import re
from dataclasses import dataclass

EFFORT_LADDER: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_RANK: dict[str, int] = {name: i for i, name in enumerate(EFFORT_LADDER)}

CODEX_EFFORTS: frozenset[str] = frozenset({"minimal", "low", "medium", "high", "xhigh"})

# Ordered most-specific-first so e.g. "kimi" wins before a looser match.
_FAMILY_EFFORTS: tuple[tuple[tuple[str, ...], frozenset[str]], ...] = (
    (("mimo",), frozenset({"low", "medium", "high"})),
    (("kimi",), frozenset({"minimal", "low", "medium", "high"})),
    (("glm",), frozenset({"none", "low", "medium", "high", "xhigh", "max"})),
    (("deepseek",), frozenset({"low", "medium", "high", "xhigh", "max"})),
    # MiniMax text-generation guide: MiniMax-M3.1-Flash takes low..max and
    # always thinks; "none" is a 400.
    (("minimax-m3.1-flash",), frozenset({"low", "medium", "high", "xhigh", "max"})),
)


# Model families whose endpoint rejects a replayed ``reasoning`` field on the
# OpenAI chat-completions wire ("Extra inputs are not permitted"). Probed
# 2026-06-30: glm (zhipu backend, incl. via the opencode-go gateway) rejects it,
# which breaks resume/model-switch; kimi and mimo on the same wire accept it, and
# DeepSeek uses its own provider (field ``reasoning_content``, required) and never
# reaches this path. Keyed by model family, not provider, because one gateway
# fronts both rejecting and accepting backends.
_REPLAY_REJECTED: tuple[str, ...] = ("glm",)


def rejects_reasoning_replay(model_name: str) -> bool:
    """True when replaying a ``reasoning`` field to this model 400s the request."""
    name = model_name.lower()
    return any(needle in name for needle in _REPLAY_REJECTED)


def supported_efforts(model_name: str) -> frozenset[str] | None:
    """Effort stops a model serves, or ``None`` to leave the knob untouched."""
    name = model_name.lower()
    for needles, allowed in _FAMILY_EFFORTS:
        if any(needle in name for needle in needles):
            return allowed
    return None


def snap_effort(effort: str | None, allowed: frozenset[str] | None) -> str | None:
    """Snap ``effort`` to the nearest stop in ``allowed`` on the effort ladder.

    ``None`` allowed (passthrough) or an already-served stop returns ``effort``
    unchanged. For an unserved stop, pick the ladder neighbour with the smallest
    distance; ties go to the gentler (lower) stop, so ``minimal`` lands on
    ``none`` when both are one step away.
    """
    if effort is None or allowed is None or effort in allowed:
        return effort
    target = _RANK.get(effort)
    if target is None or not allowed:
        return effort
    return min(allowed, key=lambda stop: (abs(_RANK[stop] - target), _RANK[stop]))


def ladder_stop(effort: str | None) -> str | None:
    """The ladder stop ``effort`` names (``off`` is ``none``), or None for a
    word that is not on the ladder."""
    if effort is None:
        return None
    word = effort.strip().lower()
    word = "none" if word == "off" else word
    return word if word in _RANK else None


def translate_effort(effort: str, allowed: frozenset[str] | None) -> str:
    """The word to send for ``effort`` to a model whose family serves
    ``allowed``: a ladder word snapped to its nearest served stop, any other
    word (or any word when ``allowed`` is None) as typed."""
    stop = ladder_stop(effort)
    if allowed is None or stop is None:
        return effort
    return snap_effort(stop, allowed) or effort


# --- The Anthropic Messages wire -------------------------------------------
#
# The anthropic SDK provider (api.anthropic.com, and every provider or login
# whose sdk is anthropic) carries the effort as ``output_config.effort``.
# Every Claude model takes ``thinking`` as ``{"type": "adaptive"}`` with that
# effort, and ``off`` as thinking disabled (Anthropic model docs, 2026-09).
# Every other model on the wire gets the effort word as typed. ``thinking``
# carries a token budget only when `THINKING_BUDGET` sets one.

# claude-opus-5-5, claude-fable-5-2, claude-6, claude-sonnet-6.1-high: the
# version after `claude-` and an optional family word.
_CLAUDE_VERSION = re.compile(r"claude-(?:[a-z]+-)?(\d+)(?:[-.](\d{1,2}))?(?!\d)")
_ADAPTIVE_4_6_EFFORTS = frozenset({"low", "medium", "high", "max"})
_ADAPTIVE_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


@dataclass(frozen=True)
class AnthropicThinking:
    """The thinking fields of one Anthropic Messages request."""

    thinking: dict | None       # the request's ``thinking`` object, or None to omit it
    effort: str | None          # ``output_config.effort``, or None to omit it


def _claude_version(name: str) -> tuple[int, int] | None:
    match = _CLAUDE_VERSION.search(name)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2) or 0)


def anthropic_adaptive(model_name: str) -> bool:
    """True when the model takes adaptive thinking with an effort level:
    every Claude model, whatever provider prefix it carries."""
    name = model_name.lower()
    return "claude" in name or "fable" in name or "mythos" in name


def _anthropic_rejects_disabled(name: str) -> bool:
    """Fable, Mythos, and Claude 5.5 and later always think: ``disabled`` is a 400."""
    if "fable" in name or "mythos" in name:
        return True
    version = _claude_version(name)
    return version is not None and version >= (5, 5)


def anthropic_thinking(model_name: str, effort: str | None, budget: int | None = None,
                       *, settings: dict | None = None) -> AnthropicThinking | None:
    """The thinking fields for ``effort`` on this model, or None when no
    effort is set. ``budget`` (`THINKING_BUDGET`) makes ``thinking`` a
    budget of that many tokens."""
    if effort is None:
        return None
    name = model_name.lower()
    budgeted = {"type": "enabled", "budget_tokens": budget} if budget else None
    if not anthropic_adaptive(name):
        return AnthropicThinking(thinking=budgeted, effort=effort)
    if ladder_stop(effort) == "none":
        if _anthropic_rejects_disabled(name):
            return AnthropicThinking(thinking=None, effort=_settings.knob(settings, "ANTHROPIC_REQUIRED_EFFORT"))
        return AnthropicThinking(thinking={"type": "disabled"}, effort=None)
    allowed = _ADAPTIVE_4_6_EFFORTS if _claude_version(name) == (4, 6) else _ADAPTIVE_EFFORTS
    thinking = budgeted or {"type": "adaptive", "display": _settings.knob(settings, "ANTHROPIC_THINKING_DISPLAY")}
    return AnthropicThinking(thinking=thinking, effort=translate_effort(effort, allowed))


# --- MiniMax's own Anthropic-compatible endpoint ----------------------------
#
# The `minimax` provider (platform.minimax.io/docs/api-reference/text-anthropic-api):
# MiniMax-M3.1-Flash always thinks, with depth in ``output_config.effort``
# (low..max) and ``thinking: disabled`` or effort ``none`` a 400. MiniMax-M3
# does not think unless ``thinking`` is ``{"type": "adaptive"}``, and
# ``disabled`` keeps it off. Other models get the effort word as typed.

_MINIMAX_M3 = re.compile(r"minimax-m3(?![.\d])")


def minimax_thinking(model_name: str, effort: str | None,
                     budget: int | None = None) -> AnthropicThinking | None:
    """The thinking fields for ``effort`` on the minimax provider, or None
    when no effort is set. ``budget`` (`THINKING_BUDGET`) makes ``thinking``
    a budget of that many tokens."""
    if effort is None:
        return None
    name = model_name.lower()
    budgeted = {"type": "enabled", "budget_tokens": budget} if budget else None
    allowed = supported_efforts(name)
    if allowed is not None:
        return AnthropicThinking(thinking=budgeted or {"type": "adaptive"},
                                 effort=translate_effort(effort, allowed))
    if _MINIMAX_M3.search(name):
        if ladder_stop(effort) == "none":
            return AnthropicThinking(thinking={"type": "disabled"}, effort=None)
        return AnthropicThinking(thinking=budgeted or {"type": "adaptive"}, effort=effort)
    return AnthropicThinking(thinking=budgeted, effort=effort)


# --- Signed reasoning replay -----------------------------------------------
#
# Anthropic thinking blocks carry a signature, and Codex reasoning items carry
# encrypted content. Both are valid only for the model that produced them, so a
# history record keeps them with the provider and model they came from and they
# are replayed only to that same provider and model. Anywhere else the record's
# plain ``reasoning_content`` follows the rules above.

def reasoning_origin(provider_id: str | None, model_id: str | None) -> dict:
    """The origin a signed reasoning record is stored under."""
    return {"provider": provider_id, "model": model_id}


def replays_signed_reasoning(origin: object, provider_id: str | None, model_id: str | None) -> bool:
    """True when signed reasoning from ``origin`` may be sent to this provider and model."""
    return (
        isinstance(origin, dict)
        and model_id is not None
        and origin.get("provider") == provider_id
        and origin.get("model") == model_id
    )
