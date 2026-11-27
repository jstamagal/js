"""Encode the selected operation as the endpoint's native HTTP request."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

from .generate_files import Asset, InlineImage


@dataclass
class Request:
    url: str
    method: str = "POST"
    fields: dict = field(default_factory=dict)
    files: list = field(default_factory=list)
    query: dict = field(default_factory=dict)
    api_key_header: str = "Authorization"

    def preview(self):
        return {"method": self.method, "url": self.url, "fields": printable(self.fields),
                "files": [[key, asset.source] for key, asset in self.files], "query": self.query}


def printable(value):
    if isinstance(value, Asset):
        return value.source
    if isinstance(value, dict):
        return {k: printable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [printable(v) for v in value]
    return value


def add(fields, **kwargs):
    fields.update((k, v) for k, v in kwargs.items() if v is not None)


def merge(fields, extra, reserved=()):
    duplicate = (fields.keys() | set(reserved)) & extra.keys()
    if duplicate:
        raise ValueError("duplicate request field: " + ", ".join(sorted(duplicate)))
    fields.update(extra)


def images(req, a, fmt, grok):
    body = req.fields
    add(body, n=a.count, size=a.size, quality=a.quality, background=a.background,
        resolution=a.resolution, aspect_ratio=a.aspect_ratio)
    if not grok and (a.format or a.output and a.output != "-" and fmt != "png"):
        body["output_format"] = "jpeg" if fmt == "jpg" else fmt
    if a.op == "t2i":
        return "/images/generations"
    sources = [Asset(i) for i in ([a.image] if a.image else []) + (a.ref or [])]
    if grok:
        refs = [{"url": s, "type": "image_url"} for s in sources]
        if len(refs) == 1:
            body["image"] = refs[0]
        else:
            body["images"] = refs
        if a.mask or a.markup:
            raise ValueError("Grok JSON edits accept reference images; mask and markup require the GPT Image edit route")
    elif a.markup:
        if a.mask:
            raise ValueError("--markup and --mask are mutually exclusive")
        body["images"] = [{"image_url": s} for s in sources]
        body["markup"] = {"image_url": Asset(a.markup)}
    else:
        req.files.extend(("image[]", s) for s in sources)
        if a.mask:
            req.files.append(("mask", Asset(a.mask)))
    return "/images/edits"


def video(req, a):
    body = req.fields
    add(body, duration=a.duration, resolution=a.resolution, aspect_ratio=a.aspect_ratio, generate_audio=a.audio)
    for key, source in [("image", a.first_frame), ("last_frame", a.last_frame), ("video", a.video)]:
        if source:
            body[key] = {"url": Asset(source)}
    if a.ref:
        body["reference_images"] = [{"url": Asset(s)} for s in a.ref]
    for spec in a.keyframe or []:
        sec, sep, path = spec.partition("=")
        if not sep or not path or not math.isfinite(float(sec)) or float(sec) < 0:
            raise ValueError("--keyframe expects a nonnegative SEC=FILE")
        body.setdefault("keyframes", []).append({"timestamp_s": float(sec), "image": {"url": Asset(path)}})
    return {"v2v": "/videos/edits", "extend-video": "/videos/extensions"}.get(a.op, "/videos/generations")


def eleven(req, a, model, fmt):
    req.api_key_header = "xi-api-key"
    req.fields = {"model_id": model}
    body = req.fields
    if a.list_voices:
        req.fields.clear()
        req.method = "GET"
        return "/voices"
    formats = {"mp3": "mp3_44100_128", "wav": "wav_44100", "pcm": "pcm_44100"}
    req.query["output_format"] = formats.get(fmt, fmt)
    if a.op == "tts":
        if a.enhance:
            raise ValueError("ElevenLabs speech takes delivery cues in the script; omit --enhance")
        if not a.voice or a.instructions:
            raise ValueError("ElevenLabs speech needs --voice ID and takes delivery cues in the script")
        add(body, text=a.prompt, language_code=a.language)
        return "/text-to-speech/" + quote(a.voice, safe="")
    if a.op == "sfx":
        add(body, text=a.prompt, duration_seconds=a.duration, loop=a.loop or None)
        return "/sound-generation"
    if a.composition_plan:
        if a.prompt or a.lyrics or a.instrumental or a.duration or a.enhance:
            raise ValueError("--composition-plan supplies the complete song, including lyrics, style and timing")
        body["composition_plan"] = json.loads(a.composition_plan.read_text())
    elif a.lyrics:
        if a.instrumental:
            raise ValueError("--instrumental and --lyrics are mutually exclusive")
        body["composition_plan"] = {
            "positive_global_styles": [a.prompt] if a.prompt else [], "negative_global_styles": [],
            "sections": [{"section_name": "Song", "positive_local_styles": [], "negative_local_styles": [],
                          "duration_ms": round((a.duration or 30) * 1000), "lines": a.lyrics.splitlines()}]}
    else:
        add(body, prompt=a.prompt, force_instrumental=a.instrumental or None,
            music_length_ms=round(a.duration * 1000) if a.duration else None)
    return "/music"


def google_route(a, route, base):
    if a.kind != "image":
        return False
    if urlsplit(base).hostname == "generativelanguage.googleapis.com" or base.rstrip("/").endswith("/v1beta"):
        return True
    name = route.model.rsplit("/", 1)[-1]
    return route.provider_id in {"cpa", "cliproxyapi"} and name.startswith("gemini-") and "image" in name


def google_image(a, route, base, fmt):
    if a.resume or a.mask or a.markup or a.size or a.quality or a.background:
        raise ValueError("native Google image requests take --aspect-ratio and --resolution; "
                         "use --param-json for additional native fields")
    base = base.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    if not base.endswith("/v1beta"):
        base += "/v1beta"
    model = route.model
    url = base + "/models/" + quote(model, safe="/")
    if model.rsplit("/", 1)[-1].startswith("imagen-"):
        if a.op != "t2i":
            raise ValueError("Imagen predict takes --t2i")
        params = {}
        add(params, sampleCount=a.count, aspectRatio=a.aspect_ratio, sampleImageSize=a.resolution)
        if a.format:
            params["outputOptions"] = {"mimeType": "image/" + ("jpeg" if fmt == "jpg" else fmt)}
        req = Request(url + ":predict", fields={"instances": [{"prompt": a.prompt}], "parameters": params})
    else:
        parts = [InlineImage(s) for s in ([a.image] if a.image else []) + (a.ref or [])]
        parts.append({"text": a.prompt})
        generation = {"responseModalities": ["IMAGE"]}
        image_config = {}
        add(image_config, imageSize=a.resolution, aspectRatio=a.aspect_ratio)
        add(generation, candidateCount=a.count, imageConfig=image_config or None)
        req = Request(url + ":generateContent", fields={"contents": [{"role": "user", "parts": parts}],
                                                       "generationConfig": generation})
    if urlsplit(base).hostname == "generativelanguage.googleapis.com":
        req.api_key_header = "x-goog-api-key"
    merge(req.fields, a.params)
    return req



def prepare(a, route, fmt):
    base = route.base_url
    if not base:
        base = {"openai": "https://api.openai.com/v1", "mistral": "https://api.mistral.ai/v1",
                "elevenlabs": "https://api.elevenlabs.io/v1"}.get(route.provider_id)
    if not base:
        raise ValueError("no media endpoint resolved; use a saved provider/model or -u URL")
    if google_route(a, route, base):
        return google_image(a, route, base, fmt)
    model = route.model
    host = urlsplit(base).hostname
    if urlsplit(base).path in {"", "/"}:
        base = base.rstrip("/") + "/v1"
    req = Request(base.rstrip("/"), fields={"model": model})
    add(req.fields, prompt=a.prompt)
    grok = route.provider_id == "xai" or host == "api.x.ai" or model.rsplit("/", 1)[-1].startswith("grok-")
    is_eleven = route.provider_id == "elevenlabs" or host == "api.elevenlabs.io"
    if a.resume:
        if a.kind != "video":
            raise ValueError("--resume on this endpoint takes a video operation")
        req.method = "GET"
        req.fields.clear()
        path = "/videos/" + quote(a.resume, safe="")
    elif a.kind == "image":
        path = images(req, a, fmt, grok)
    elif a.kind == "video":
        path = video(req, a)
    elif a.kind in {"music", "sfx"} or (is_eleven and a.op == "tts"):
        path = eleven(req, a, model, fmt)
    elif a.op == "stt":
        path = "/audio/transcriptions"
        req.files = [("file", Asset(a.audio_file))]
        add(req.fields, language=a.language, diarize=a.diarize or None,
            timestamp_granularities="segment" if a.diarize or a.timestamps else None)
    elif a.list_voices:
        path = "/tts/voices" if grok else "/audio/voices"
        req.method = "GET"
        req.fields.clear()
    elif host == "api.x.ai" or route.provider_id == "xai":
        if a.instructions or a.enhance:
            raise ValueError("native Grok TTS takes delivery cues in the spoken script; omit --instructions and --enhance")
        path = "/tts"
        req.fields = {"text": a.prompt, "output_format": {"codec": fmt}}
        add(req.fields, voice_id=a.voice, language=a.language)
    else:
        path = "/audio/speech"
        req.fields = {"model": model, "input": a.prompt, "response_format": fmt}
        add(req.fields, voice=a.voice, instructions=a.instructions, language=a.language)
    req.url += path
    merge(req.fields, a.params, [k for k, _ in req.files] + list(req.query))
    return req
