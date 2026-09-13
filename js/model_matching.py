"""Match routed model names against dated models.dev catalog rows."""

from __future__ import annotations

import re
from typing import Any

_EFFORT = re.compile(r"-(off|low|med|medium|high|xhigh|max|minimal)$")
_VARIANTS = {
    "mini", "nano", "super", "ultra", "lite", "image", "tts", "audio",
    "embedding", "embed", "rerank", "safety", "guard", "realtime", "vision",
    "omni", "fast", "highspeed", "ultraspeed", "preview", "experimental",
    "exp", "next", "coder", "coding", "pro", "flash", "max",
}


def name_key(value: str) -> str:
    return value.rsplit("/", 1)[-1].lower().replace("_", "-").replace(".", "-")


def without_effort(value: str) -> str:
    return _EFFORT.sub("", value)


def family_candidates(request: str, rows: tuple[Any, ...]) -> list[Any]:
    """Keep family and variant identity before comparing release dates."""
    stem = name_key(without_effort(request)).replace("-latest", "")
    requested = set(stem.split("-"))
    matches = []
    for row in rows:
        if not row.release_date or not row.context_window:
            continue
        model = name_key(row.model_id)
        family = name_key(row.family or "")
        words = re.sub(r"\d+(?:-\d+)*", "", model)
        words = re.sub("-+", "-", words).strip("-")
        if not (
            family == stem or family == "gpt-" + stem
            or model == stem or model.startswith(stem + "-")
            or requested <= set(words.split("-"))
        ):
            continue
        if any(char.isdigit() for char in stem) and not (model == stem or model.startswith(stem + "-")):
            continue
        extra = (set(model.split("-")) & _VARIANTS) - requested
        if stem == "step":
            extra -= {"flash"}
        if extra:
            continue
        matches.append(row)
    return matches


def issuer_rank(row: Any) -> int:
    """Prefer the model maker's catalog entry over reseller limits."""
    model = name_key(row.model_id)
    family = (row.family or "").lower()
    provider = row.provider_id.lower()
    issuers = {
        "claude": {"anthropic"}, "gpt": {"openai"}, "grok": {"xai"},
        "gemini": {"google"}, "gemma": {"google"}, "qwen": {"alibaba"},
        "deepseek": {"deepseek"}, "minimax": {"minimax"}, "mimo": {"xiaomi"},
        "nemotron": {"nvidia"}, "step": {"stepfun"}, "laguna": {"poolside"},
        "glm": {"zhipuai", "zai"}, "kimi": {"moonshotai"}, "mistral": {"mistral"},
    }
    for prefix, providers in issuers.items():
        if family.startswith(prefix) or model.startswith(prefix):
            return int(provider in providers)
    return int(model.startswith(provider + "-"))


def match_routed(request: str, rows: tuple[Any, ...]) -> tuple[Any | None, str]:
    wanted = name_key(request)
    latest = "-latest" in wanted
    if latest:
        matches = family_candidates(wanted, rows)
        method = "family-release"
    else:
        matches = [row for row in rows if name_key(row.model_id) == wanted]
        method = "normalized"
        if not matches:
            wanted = name_key(without_effort(request))
            matches = [row for row in rows if name_key(row.model_id) == wanted]
            method = "effort"
    if not matches:
        return None, method
    if latest:
        newest = max(row.release_date for row in matches)
        matches = [row for row in matches if row.release_date == newest]
    issuer = max(issuer_rank(row) for row in matches)
    matches = [row for row in matches if issuer_rank(row) == issuer]
    windows = {row.context_window for row in matches}
    if len(windows) != 1:
        return None, method
    requested = set(name_key(without_effort(request)).replace("-latest", "").split("-"))
    return sorted(matches, key=lambda row: (
        -len(requested & set(name_key(row.model_id).split("-"))),
        len(row.model_id), row.provider_id, row.model_id,
    ))[0], method
