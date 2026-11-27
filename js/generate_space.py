"""Call a Gradio Space's named-argument API and retrieve its event by ID."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote, urlsplit

from .generate_files import Asset, checked, download
from .generate_request import Request, add, merge

TURBO_SPACE = "minimaxai/minimax-h3-turbo-lora"


def function_name(a):
    default = "output_video" if a.space.lower() == TURBO_SPACE else "generate"
    return (a.api_name or default).lstrip("/")


def turbo_inputs(fields, extra):
    # The published workflow exposes its nine UI inputs as in_0 through in_8.
    inputs = {"prompt": None, "image_path": None, "last_image_path": None, "canvas": "960x544 · 16:9 fast",
              "duration": 5, "steps": 6, "seed": 42, "upsample": False, "lora": "larry"}
    names = {key: f"in_{i}" for i, key in enumerate(inputs)}
    body = {names[k]: v for k, v in inputs.items()}
    provided = {names.get(k, k): v for k, v in fields.items()}
    for key, value in extra.items():
        merge(provided, {names.get(key, key): value})
    body.update(provided)
    return body


class SpaceRequest(Request):
    def preview(self):
        info = super().preview()
        info["api_path"] = info.pop("url")
        return info


def prepare(a, fmt):
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", a.space) or any(s in {".", ".."} for s in a.space.split("/")):
        raise ValueError("--space expects owner/name")
    if a.kind not in {"image", "video"}:
        raise ValueError("--space supports image and video generation")
    name = function_name(a)
    if not re.fullmatch(r"[\w-]+", name):
        raise ValueError("invalid Space --api-name")
    root = "/gradio_api"
    if a.resume:
        return SpaceRequest(root + "/call/" + name + "/" + quote(a.resume, safe=""), method="GET")
    req = SpaceRequest(root + "/call/v2/" + name)
    # The H3 image Space accepts an ordered Gradio gallery. Its video Space
    # names the two pinned file inputs image_path and last_image_path.
    add(req.fields, prompt=a.prompt, aspect=a.aspect_ratio, resolution=a.resolution, duration=a.duration,
        quality=a.quality, size=a.size, background=a.background, generate_audio=a.audio, n=a.count)
    sources = ([a.image] if a.image else []) + (a.ref or [])
    if sources:
        req.fields["images"] = [{"image": Asset(s)} for s in sources]
    for key, source in [("image_path", a.first_frame), ("last_image_path", a.last_frame),
                        ("video", a.video), ("mask", a.mask), ("markup", a.markup)]:
        if source:
            req.fields[key] = Asset(source)
    if a.keyframe:
        raise ValueError("use --param-json for the Space's own keyframe input")
    if a.space.lower() == TURBO_SPACE and name == "output_video":
        if a.op not in {"t2v", "i2v"}:
            raise ValueError("H3 Turbo video takes --t2v or --i2v")
        req.fields = turbo_inputs(req.fields, a.params)
    else:
        merge(req.fields, a.params)
    return req


def token_headers():
    token = os.environ.get("HF_TOKEN")
    if not token:
        cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "huggingface"
        hf_home = Path(os.environ.get("HF_HOME", cache))
        token_path = Path(os.environ.get("HF_TOKEN_PATH", hf_home / "token"))
        if token_path.is_file():
            token = token_path.read_text().strip()
    return {"Authorization": "Bearer " + token} if token else {}


def upload(value, root, headers, client):
    if isinstance(value, Asset):
        if value.remote:
            path = value.source
        else:
            r = client.post(root + "/upload", headers=headers, files={"files": value.upload(client)})
            path = checked(r).json()[0]
        return {"path": path, "meta": {"_type": "gradio.FileData"}}
    if isinstance(value, list):
        return [upload(v, root, headers, client) for v in value]
    if isinstance(value, dict):
        return {k: upload(v, root, headers, client) for k, v in value.items()}
    return value


def artifacts(items):
    if isinstance(items, list):
        for item in items:
            yield from artifacts(item)
    if isinstance(items, dict):
        if items.get("url"):
            yield items["url"]
        else:
            for key in ["video", "image"]:
                if key in items:
                    yield from artifacts(items[key])


def execute(client, req, a):
    headers = token_headers()
    meta = checked(client.get("https://huggingface.co/api/spaces/" + a.space, headers=headers)).json()
    host = meta.get("host") or ""
    parsed = urlsplit(host)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".hf.space"):
        raise ValueError(f"HF returned no running Space host for {a.space}")
    root = host.rstrip("/") + "/gradio_api"
    name = function_name(a)
    job = a.resume
    if not job:
        payload = upload(req.fields, root, headers, client)
        res = checked(client.post(root + "/call/v2/" + name, json=payload, headers=headers))
        job = res.json().get("event_id")
        if not isinstance(job, str) or not job:
            raise ValueError("Space did not return an event_id")
        if a.submit_only:
            print(job, flush=True)
            return []
        print(f"event_id={job}", file=sys.stderr, flush=True)
    url = root + "/call/" + name + "/" + quote(job, safe="")
    with client.stream("GET", url, headers=headers) as response:
        if not response.is_success:
            response.read()
            checked(response)
        event = ""
        for line in response.iter_lines():
            if line.startswith("event:"):
                event = line.partition(":")[2].strip()
            elif line.startswith("data:"):
                data = line.partition(":")[2].strip()
                if event == "error":
                    raise ValueError(f"Space event {job} failed: {data}")
                if event == "complete":
                    urls = list(artifacts(json.loads(data)))
                    if not urls:
                        raise ValueError(f"Space event {job} returned no downloadable media")
                    return [download(client, u, response, headers) for u in urls]
    raise ValueError(f"Space stream ended before completion; reconnect with --resume {job}")
