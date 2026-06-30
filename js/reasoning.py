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
