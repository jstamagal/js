"""jev: typed questions to TypeSafe's Jev (System One) about a state.

One request carries the state and every question. A noul question answers
0-1, a choice question picks one option with a probability for each, and a
score question places the state on levels given lowest first.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .core import Tool, ToolContext
from .descriptions import load_description

API_KEY_ENV = "TYPESAFE_API_KEY"
KINDS = ("noul", "choice", "score")


def _client(model: str, api_key: str | None = None) -> Any:
    from typesafe_sdk import TypeSafeClient

    return TypeSafeClient(model=model, api_key=api_key)


def _state(raw: str) -> Any:
    """A JSON object or array is sent as JSON; anything else as text."""
    text = raw.strip()
    if text[:1] in ("{", "["):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
    return raw


def _options(raw: list[Any]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for item in raw:
        name, sep, desc = str(item).partition("=")
        out[name.strip()] = desc.strip() if sep else None
    return out


def _question(item: Any) -> dict[str, Any] | str:
    """The wire question for one tool-call item, or an error line."""
    if not isinstance(item, dict) or not str(item.get("question") or "").strip():
        return "each question needs a non-empty `question`"
    kind = item.get("kind") or "noul"
    if kind not in KINDS:
        return f"kind must be one of {', '.join(KINDS)}, got {kind!r}"
    asked: dict[str, Any] = {"type": kind, "instructions": item["question"]}
    if kind == "noul":
        return asked
    options = item.get("options") or []
    if not isinstance(options, list) or len(options) < 2:
        return f"a {kind} question needs at least two options"
    criteria = _options(options) if kind == "choice" else [str(o) for o in options]
    if kind == "choice" and len(criteria) != len(options):
        return "choice option names must be unique"
    asked["criteria"] = criteria
    return asked


def _field(answer: Any, name: str) -> Any:
    return answer.get(name) if isinstance(answer, dict) else getattr(answer, name, None)


def _format(answer: Any) -> str:
    kind = _field(answer, "type")
    if kind == "noul":
        return f"noul {float(_field(answer, 'noul')):.2f}"
    probabilities = _field(answer, "probabilities") or {}
    ranked = sorted(probabilities.items(), key=lambda kv: -kv[1])
    dist = " ".join(f"{k}={v:.2f}" for k, v in ranked)
    confidence = float(_field(answer, "confidence") or 0)
    if kind == "choice":
        return f"choice {_field(answer, 'choice')} conf={confidence:.2f} | {dist}"
    if kind == "score":
        top = len(_field(answer, "legend") or {}) - 1
        return f"score {float(_field(answer, 'score')):.2f}/{top} conf={confidence:.2f} | {dist}"
    return str(answer)


def jev(
    state: str,
    questions: list[dict[str, Any]],
    check_order: bool = False,
    context: ToolContext | None = None,
) -> str:
    assert context is not None
    if not isinstance(state, str) or not state.strip():
        return "ERROR: state is required"
    if not isinstance(questions, list) or not questions:
        return "ERROR: questions needs at least one question"
    asked: dict[str, dict[str, Any]] = {}
    for n, item in enumerate(questions, 1):
        built = _question(item)
        if isinstance(built, str):
            return f"ERROR: question {n}: {built}"
        asked[f"q{n}"] = built
        if check_order and built["type"] == "choice":
            reversed_criteria = dict(reversed(list(built["criteria"].items())))
            asked[f"q{n}_reversed"] = dict(built, criteria=reversed_criteria)
    configured_key = context.typesafe_api_key.strip()
    if not (configured_key or os.environ.get(API_KEY_ENV, "").strip()):
        return f"ERROR: {API_KEY_ENV} is not set"
    model = getattr(context, "jev_model", None) or "jev-latest"
    try:
        with (_client(model, api_key=configured_key) if configured_key else _client(model)) as client:
            response = client.system_one(_state(state), asked)
    except ImportError:
        return "ERROR: typesafe-sdk is not installed; run `just sync`"
    except Exception as exc:  # noqa: BLE001 - the SDK's errors reach the model as text
        return f"ERROR: {type(exc).__name__}: {exc}"
    answers = getattr(response, "answers", None) or {}
    lines: list[str] = []
    for n in range(1, len(questions) + 1):
        key = f"q{n}"
        if key not in answers:
            lines.append(f"{n}. (no answer)")
            continue
        lines.append(f"{n}. {_format(answers[key])}")
        flipped = answers.get(f"{key}_reversed")
        if flipped is not None:
            stable = _field(flipped, "choice") == _field(answers[key], "choice")
            verdict = "stable" if stable else "FLIPPED: order-sensitive, do not trust"
            lines.append(f"   reversed: {_format(flipped)}; order check: {verdict}")
    return "\n".join(lines)


def tools() -> tuple[Tool, ...]:
    return (
        Tool(
            "jev",
            load_description("jev"),
            jev,
            {
                "state": {
                    "type": "string",
                    "description": "What the questions are about. A JSON object or array is sent as JSON.",
                },
                "questions": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"},
                            "kind": {"type": "string", "enum": list(KINDS), "default": "noul"},
                            "options": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "choice: `name` or `name=description`; score: levels, lowest first.",
                            },
                        },
                        "required": ["question"],
                        "additionalProperties": False,
                    },
                },
                "check_order": {
                    "type": "boolean",
                    "default": False,
                    "description": "Ask each choice again with its options reversed and report a flip.",
                },
            },
            required=("state", "questions"),
            read_only=True,
        ),
    )
