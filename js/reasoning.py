"""The reasoning knob: one js effort dial, mapped to what each model accepts.

js exposes a single thinking-effort knob with a seven-stop ladder:

    none < minimal < low < medium < high < xhigh < max

No endpoint accepts every stop, and the stops a model serves are an API
contract that models.dev does not encode — gateways and direct vendor
endpoints disagree (opencode-go's gateway takes ``max`` on glm; Xiaomi's
direct endpoint 400s on anything outside ``low|medium|high``). So rather than
forward a stop a model rejects, treat the request as a *dial* and snap it to
the nearest stop that endpoint actually serves.

The supported sets below are ground-truthed by live probe (2026-06-30) against
the endpoints js targets, not vendor docs:

    mimo  (xiaomi direct)      low, medium, high                 (others 400/500)
    kimi  (moonshot/opencode)  minimal, low, medium, high        (none/xhigh/max 400)
    glm   (zhipu/opencode-go)  none, low, medium, high, xhigh, max
    deepseek                   low, medium, high, xhigh, max     (none/minimal 400)
    codex (gpt-5.x Responses)  minimal, low, medium, high, xhigh

A model family absent from the table is left untouched (passthrough): the
server either self-normalizes (deepseek-direct, glm) or ignores the knob
(minimax/kimi-instruct), so the endpoint stays the single source of truth.
"""

from __future__ import annotations

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


# --- The Anthropic Messages wire -------------------------------------------
#
# The anthropic SDK provider (api.anthropic.com, and every provider or login
# whose sdk is anthropic) takes thinking in one of two request shapes, chosen by
# model (Anthropic model docs, 2026-09):
#
#   adaptive   Claude 4.6 and later, Fable, Mythos. ``thinking`` is
#              ``{"type": "adaptive"}`` and depth is ``output_config.effort``;
#              ``budget_tokens`` is a 400 on 4.7 and later.
#   budget     Every other model on the wire: Claude 4.5 and earlier, and the
#              non-Claude models behind Anthropic-compatible endpoints (MiniMax,
#              opencode-go). ``thinking`` is ``{"type": "enabled",
#              "budget_tokens": N}`` with N below ``max_tokens``.

ANTHROPIC_THINKING_BUDGETS: dict[str, int] = {
    "minimal": 1024,
    "low": 2048,
    "medium": 8192,
    "high": 16384,
    "xhigh": 24576,
    "max": 32000,
}
# The API's smallest budget, and the tokens a budget always leaves for the answer.
ANTHROPIC_MIN_BUDGET = 1024
ANTHROPIC_ANSWER_ROOM = 1024
# max_tokens above the budget when the model's output cap is unknown.
ANTHROPIC_UNKNOWN_CAP_ANSWER = 8192

_CLAUDE_VERSION = re.compile(r"claude-(opus|sonnet|haiku)-(\d+)(?:[-.](\d{1,2}))?(?!\d)")
_ADAPTIVE_4_6_EFFORTS = frozenset({"low", "medium", "high", "max"})
_ADAPTIVE_EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


@dataclass(frozen=True)
class AnthropicThinking:
    """The thinking fields of one Anthropic Messages request."""

    thinking: dict | None       # the request's ``thinking`` object, or None to omit it
    effort: str | None          # ``output_config.effort``, or None to omit it
    max_tokens: int | None      # ``max_tokens`` the request needs, or None to keep the caller's


def _claude_version(name: str) -> tuple[int, int] | None:
    match = _CLAUDE_VERSION.search(name)
    if match is None:
        return None
    return int(match.group(2)), int(match.group(3) or 0)


def anthropic_adaptive(model_name: str) -> bool:
    """True when the model takes adaptive thinking with an effort level."""
    name = model_name.lower()
    if "fable" in name or "mythos" in name:
        return True
    version = _claude_version(name)
    return version is not None and version >= (4, 6)


def _anthropic_rejects_disabled(name: str) -> bool:
    """Fable, Mythos, and Claude 5.5 and later always think: ``disabled`` is a 400."""
    if "fable" in name or "mythos" in name:
        return True
    version = _claude_version(name)
    return version is not None and version >= (5, 5)


def anthropic_thinking(model_name: str, effort: str | None,
                       max_output_tokens: int | None,
                       budget: int | None = None) -> AnthropicThinking | None:
    """The thinking fields for ``effort`` on this model, or None to send none.

    ``budget`` (``model.thinking_budget``) replaces the effort's budget on the
    models that take one."""
    if effort is None or effort not in _RANK:
        return None
    name = model_name.lower()
    if anthropic_adaptive(name):
        if effort == "none":
            if _anthropic_rejects_disabled(name):
                return AnthropicThinking(thinking=None, effort="low", max_tokens=None)
            return AnthropicThinking(thinking={"type": "disabled"}, effort=None, max_tokens=None)
        version = _claude_version(name)
        allowed = _ADAPTIVE_4_6_EFFORTS if version == (4, 6) else _ADAPTIVE_EFFORTS
        return AnthropicThinking(
            thinking={"type": "adaptive", "display": "summarized"},
            effort=snap_effort(effort, allowed),
            max_tokens=None,
        )
    if effort == "none":
        return None
    budget = max(budget, ANTHROPIC_MIN_BUDGET) if budget else ANTHROPIC_THINKING_BUDGETS[effort]
    if max_output_tokens is None:
        max_tokens = budget + ANTHROPIC_UNKNOWN_CAP_ANSWER
    else:
        max_tokens = max_output_tokens
        budget = min(budget, max_output_tokens - ANTHROPIC_ANSWER_ROOM)
        if budget < ANTHROPIC_MIN_BUDGET:
            return None
    return AnthropicThinking(
        thinking={"type": "enabled", "budget_tokens": budget},
        effort=None,
        max_tokens=max_tokens,
    )


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
