"""Standalone command line for media generation."""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import httpx

from . import (
    config,
    generate_files,
    generate_request,
    generate_route,
    generate_space,
    generate_writer,
    messages as msgs,
)
from .generate_http import execute

KINDS = dict(t2i="image", i2i="image", t2v="video", i2v="video", r2v="video",
             v2v="video", tts="tts", stt="stt", music="music", sfx="sfx")
KINDS["extend-video"] = "video"
VIDEO = {op for op, kind in KINDS.items() if kind == "video"}
IMAGES = {"t2i", "i2i"}


def parser():
    p = msgs.ArgumentParser(prog="js generate", allow_abbrev=False,
                                description=msgs.GENERATE_DESCRIPTION.words())
    ops = p.add_mutually_exclusive_group()
    for op in KINDS:
        ops.add_argument("--" + op, dest="op", action="store_const", const=op, help=msgs.GENERATE_OPERATIONS[op].words())
    route = p.add_argument_group("model and endpoint")
    route.add_argument("-m", "--model", help=msgs.GENERATE_MODEL.words())
    route.add_argument("-u", "--url", help=msgs.GENERATE_URL.words())
    route.add_argument("--space", help=msgs.GENERATE_SPACE.words())
    route.add_argument("--api-name", help=msgs.GENERATE_API_NAME.words())
    route.add_argument("--param-json", action="append", default=[], metavar="KEY=JSON",
                       help=msgs.GENERATE_PARAM_JSON.words())
    prompt = p.add_mutually_exclusive_group()
    prompt.add_argument("-p", "--prompt", help=msgs.GENERATE_PROMPT.words())
    prompt.add_argument("--prompt-file", type=Path)
    p.add_argument("-o", "--output", help=msgs.GENERATE_OUTPUT.words())
    p.add_argument("--force", action="store_true", help=msgs.GENERATE_FORCE.words())
    p.add_argument("--format", help=msgs.GENERATE_FORMAT.words())
    p.add_argument("--dry-run", action="store_true", help=msgs.GENERATE_DRY_RUN.words())
    writer = p.add_argument_group("optional prompt writer")
    writer.add_argument("--enhance", action="store_true")
    writer.add_argument("-a", "--agent", help=msgs.GENERATE_AGENT.words())
    writer.add_argument("--enhance-model", help=msgs.GENERATE_ENHANCE_MODEL.words())
    writer.add_argument("--enhance-url", help=msgs.GENERATE_ENHANCE_URL.words())
    allowed = {}
    def option(group, ops, *flags, **kwargs):
        item = group.add_argument(*flags, **kwargs)
        allowed[item.dest] = ops
    img = p.add_argument_group("images and frames")
    option(img, {"i2i"}, "--image", help=msgs.GENERATE_IMAGE.words())
    option(img, {"i2i", "r2v", "i2v"}, "--ref", action="append", help=msgs.GENERATE_REF.words())
    option(img, {"i2i"}, "--mask")
    option(img, {"i2i"}, "--markup")
    option(img, IMAGES, "-n", "--count", type=int)
    for flag in ["--size", "--quality", "--background"]:
        option(img, IMAGES, flag)
    for flag in ["--aspect-ratio", "--resolution"]:
        option(img, IMAGES | VIDEO, flag)
    vid = p.add_argument_group("video jobs")
    option(vid, {"v2v", "extend-video"}, "--video")
    for flag in ["--first-frame", "--last-frame"]:
        option(vid, {"i2v", "r2v"}, flag)
    option(vid, {"i2v", "r2v"}, "--keyframe", action="append", metavar="SEC=FILE")
    option(vid, VIDEO, "--audio", action=argparse.BooleanOptionalAction, default=None)
    option(vid, VIDEO | {"music", "sfx"}, "--duration", type=float, metavar="SEC")
    option(vid, VIDEO | IMAGES, "--submit-only", action="store_true", help=msgs.GENERATE_SUBMIT_ONLY.words())
    option(vid, VIDEO | IMAGES, "--resume", metavar="ID", help=msgs.GENERATE_RESUME.words())
    snd = p.add_argument_group("speech, music and effects")
    for flag in ["--audio-file", "--timestamps", "--diarize"]:
        option(snd, {"stt"}, flag, **({} if flag == "--audio-file" else {"action": "store_true"}))
    option(snd, {"tts"}, "--list-voices", action="store_true")
    option(snd, {"tts"}, "--voice")
    option(snd, {"tts"}, "--instructions")
    option(snd, {"tts", "stt"}, "--language")
    option(snd, {"music"}, "--composition-plan", type=Path)
    option(snd, {"music"}, "--lyrics")
    option(snd, {"music"}, "--instrumental", action="store_true")
    option(snd, {"sfx"}, "--loop", action="store_true")
    return p, allowed


def read_args(argv):
    p, allowed = parser()
    a = p.parse_args(argv)
    if not a.op:
        if a.list_voices:
            a.op = "tts"
        elif a.resume:
            a.op = "t2v"
        else:
            p.error("choose an operation such as --t2i or --t2v")
    a.kind = KINDS[a.op]
    for field, ops in allowed.items():
        value = getattr(a, field)
        if value is not None and value is not False and a.op not in ops:
            p.error(f"--{field.replace('_', '-')} does not apply to --{a.op}")
    if a.audio is False and a.op not in VIDEO:
        p.error("--no-audio requires a video operation")
    if a.count is not None and a.count < 1:
        p.error("--count must be positive")
    if a.duration is not None and (not math.isfinite(a.duration) or a.duration <= 0):
        p.error("--duration must be a positive number")
    a.enhance = bool(a.enhance or a.enhance_url or a.enhance_model or a.agent)
    a.params = {}
    for raw in a.param_json:
        key, sep, value = raw.partition("=")
        if not key or not sep or key in a.params:
            p.error(f"expected a unique KEY=JSON for --param-json: {key!r}")
        a.params[key] = json.loads(value)
    if "n" in a.params and a.kind == "image":
        n = a.params.pop("n")
        if a.count is not None or type(n) is not int or n < 1:
            p.error("image count needs one positive integer from --count or --param-json n=...")
        a.count = n
    passive = a.resume or a.list_voices
    if passive and (a.enhance or a.prompt is not None or a.prompt_file or a.params or
                    any(getattr(a, k) is not None and getattr(a, k) is not False
                        for k in allowed if k not in {"resume", "list_voices"})):
        p.error("resume and voice listings take routing and output options only")
    if a.prompt_file:
        a.prompt = a.prompt_file.read_bytes().decode()
    elif a.prompt == "-":
        a.prompt = sys.stdin.read()
    if a.op == "stt" and (a.prompt is not None or a.enhance):
        p.error("--stt uses --audio-file")
    if not passive:
        if not (a.prompt or "").strip() and a.op != "stt" and not (a.lyrics or a.composition_plan):
            p.error("supply -p TEXT, -p - or --prompt-file")
        required = {"i2i": (a.image or a.ref, "--image or --ref"), "i2v": (a.first_frame, "--first-frame"),
                    "r2v": (a.ref, "--ref"), "stt": (a.audio_file, "--audio-file"),
                    "v2v": (a.video, "--video"), "extend-video": (a.video, "--video")}
        if a.op in required and not required[a.op][0]:
            p.error(f"--{a.op} needs {required[a.op][1]}")
    return a


def media_route(a, values):
    key = a.kind.upper() + "_MODEL"
    return generate_route.resolve(url=a.url, explicit_model=a.model, default_model=values.get(key),
                                  values=values, missing=f"set JS_{key} or pass -m MODEL", media=True)


def run(a):
    values = config.run_settings(sources=config.InvocationSources())
    model = a.model or values.get(a.kind.upper() + "_MODEL") or ""
    if not a.space and not a.url and model.startswith("hf.space/"):
        a.space = model.removeprefix("hf.space/")
    if a.api_name and not a.space:
        raise ValueError("--api-name requires a Space")
    if a.space and (a.url or (a.model and a.model != "hf.space/" + a.space)):
        raise ValueError("--space selects the media model and endpoint")
    if a.submit_only and not (a.space or a.kind == "video"):
        raise ValueError("--submit-only requires a Space or video endpoint")
    if a.submit_only and (a.output or a.force or a.format):
        raise ValueError("select output options when retrieving the job with --resume")
    fmt, outputs = generate_files.destinations(a)
    if a.space:
        target = "hf.space/" + a.space
        req = generate_space.prepare(a, fmt)
    else:
        route = media_route(a, values)
        target = route.model
        req = generate_request.prepare(a, route, fmt)
    if a.dry_run:
        preview = {"model": target, **req.preview(), "outputs": [str(p) for p in outputs]}
        if a.enhance:
            preview["writer"] = generate_writer.describe(a, values, target)
        print(json.dumps(preview, indent=2, ensure_ascii=False))
        return
    if a.enhance:
        generate_writer.enhance(a, target, values)
        req = generate_space.prepare(a, fmt) if a.space else generate_request.prepare(a, route, fmt)
    with httpx.Client(timeout=httpx.Timeout(600, connect=15)) as client:
        results = (generate_space.execute(client, req, a) if a.space else
                   execute(client, req, a, route))
        if not a.submit_only:
            generate_files.save(outputs, results, fmt, a)


def main(argv):
    try:
        run(read_args(argv))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"js generate: {exc}", file=sys.stderr)
        return 1
    return 0
