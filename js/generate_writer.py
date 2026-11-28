"""A single tool-free writer call routed independently of the media model."""
from __future__ import annotations

import json
import re

import ai

from . import config, generate_route, model_client, paths, persona, retry, turn_settings
from .cli_options import RunOptions, invocation

# The stock writer for each media kind and model family: the first row whose
# kind matches and whose pattern is found in the target's last path segment.
# Music and sound effects go out in ElevenLabs's request shape whatever the
# target, so those kinds have one writer each.
WRITERS = [
    ("image", r"h3-image", "h3-image"),
    ("image", r"gpt-image", "gpt-image"),
    ("image", r"^grok-", "grok-image"),
    ("image", r"^gemini-.*image|nano-banana", "gemini-image"),
    ("image", r"^imagen-", "imagen-image"),
    ("video", r"^grok-", "grok-video"),
    ("video", r"hailuo|minimax-h3", "h3-video"),
    ("tts", r"^gpt-.*tts", "openai-speech"),
    ("music", r"", "elevenlabs-music"),
    ("sfx", r"", "elevenlabs-sfx"),
]


def stock_writer(kind, target):
    name = target.lower().rsplit("/", 1)[-1]
    return next((agent for k, pattern, agent in WRITERS if k == kind and re.search(pattern, name)),
                "media-writer")


def setup(a, values, target):
    name = a.agent or stock_writer(a.kind, target)
    spec = persona.load_agent_prompt_spec(name, paths.agent_roots(), env=False)
    route = generate_route.resolve(url=a.enhance_url, explicit_model=a.enhance_model,
                                   default_model=spec.model or values.get("MODEL"), values=values,
                                   missing="supply --enhance-model or a model in --enhance-url")
    return name, spec, route


def describe(a, values, target):
    name, _, route = setup(a, values, target)
    return {"agent": name, "model": route.model, "provider": route.provider_id, "url": route.base_url}


def enhance(a, target, values):
    name, spec, route = setup(a, values, target)
    options = RunOptions(agent=name, no_env=True, no_agents_md=True, no_tools=True)
    with invocation(options):
        cfg = config.Config(model=route.model, agent_id=name, provider_id=route.provider_id,
                            provider_base_url=route.base_url, settings=values, prompt_roots=paths.agent_roots(),
                            **turn_settings.project(values))
        spec = persona.for_model(spec, cfg)
        system = persona.expand_agent_text(spec.system, cfg)
    brief = {"operation": a.op, "target": target, "prompt": a.prompt}
    for field in ["image", "ref", "mask", "markup", "duration", "aspect_ratio", "size", "resolution",
                  "first_frame", "last_frame", "keyframe", "video", "language", "instructions", "lyrics"]:
        value = getattr(a, field)
        if value is not None:
            brief[field] = value
    if a.audio is not None:
        brief["audio"] = a.audio
    for field in ["instrumental", "loop"]:
        if getattr(a, field):
            brief[field] = True
    contract = ("Return only the final media prompt. Preserve quoted text exactly. For speech, return only "
                "delivery instructions; the script is sent separately, verbatim. Lyrics are supplied separately.")
    result = model_client.stream_model(
        model_id=route.model, provider_id=route.provider_id, provider_base_url=route.base_url,
        provider_api_key=route.api_key, provider_headers=route.headers,
        messages=[ai.system_message(system + "\n\n" + contract),
                  ai.user_message(json.dumps(brief, ensure_ascii=False))],
        tools=None, max_output_tokens=spec.max_output_tokens or values.get("OUTPUT_TOKENS_MAX"),
        reasoning_effort=spec.reasoning_effort or values.get("EFFORT"), on_text=lambda _: None,
        retry_budget=retry.Budget.from_settings(values), stream_limits=retry.StreamLimits.from_settings(values),
        settings=values)
    if result.finish_reason != "stop" or not result.text.strip():
        raise ValueError(f"prompt writer returned no complete prompt ({result.finish_reason})")
    if a.op == "tts":
        a.instructions = result.text.strip()
    else:
        a.prompt = result.text.strip()
