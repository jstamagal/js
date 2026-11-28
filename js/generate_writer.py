"""A single tool-free writer call routed independently of the media model."""
from __future__ import annotations

import json
import mimetypes
from pathlib import Path

import ai

from . import config, generate_route, imagefit, model_client, paths, persona, retry, turn_settings
from .cli_options import RunOptions, invocation


def pictures(a):
    """The input pictures in the order the media model numbers them, as (label, source):
    an edit's base image then its references; a video's first and last frame, its
    references, then its keyframes by time."""
    sources = []
    if a.kind == "image":
        sources = ([a.image] if a.image else []) + list(a.ref or [])
    elif a.kind == "video":
        keyed = sorted((float(spec.partition("=")[0]), spec.partition("=")[2]) for spec in a.keyframe or [])
        sources = [s for s in (a.first_frame, a.last_frame) if s] + list(a.ref or []) + [path for _, path in keyed]
    return [(f"<Picture {n}>", source) for n, source in enumerate(sources, 1)]


def attachments(a, vision):
    """What the writer is told about each input picture, and the pictures themselves:
    a local image file is attached when the writer's model takes images."""
    listed, parts = [], []
    marks = [(name, source) for name in ("mask", "markup") if (source := getattr(a, name, None))]
    for label, source in pictures(a) + marks:
        path = Path(source).expanduser()
        mime = mimetypes.guess_type(path.name)[0] or ""
        attached = vision and mime.startswith("image/") and path.is_file()
        if attached:
            data = path.read_bytes()
            fitted = imagefit.fit(data, mime)
            if fitted is not None:
                data, mime = fitted.data, fitted.mime
            parts += [f"{label}: {path.name}", ai.types.messages.FilePart(data=data, media_type=mime)]
        listed.append({"label": label, "file": str(source), "attached": bool(attached)})
    return listed, parts


def setup(a, values, target):
    h3 = a.kind == "image" and "h3-image" in target.lower()
    name = a.agent or ("h3-image" if h3 else "media-writer")
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
    listed, parts = attachments(a, config.vision_enabled_for_model(route.model, values))
    if listed:
        brief["pictures"] = listed
    contract = ("Return only the final media prompt. Preserve quoted text exactly. For speech, return only "
                "delivery instructions; the script is sent separately, verbatim. Lyrics are supplied separately.")
    result = model_client.stream_model(
        model_id=route.model, provider_id=route.provider_id, provider_base_url=route.base_url,
        provider_api_key=route.api_key, provider_headers=route.headers,
        messages=[ai.system_message(system + "\n\n" + contract),
                  ai.user_message(json.dumps(brief, ensure_ascii=False), *parts)],
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
