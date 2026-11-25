"""Local media inputs, output naming and atomic publication."""
from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx


@dataclass(frozen=True)
class Asset:
    source: str

    def __post_init__(self):
        if not self.remote and not Path(self.source).is_file():
            raise ValueError(f"input file does not exist: {self.source}")

    @property
    def remote(self):
        return self.source.startswith(("http://", "https://"))

    def upload(self, client):
        name = Path(urlsplit(self.source).path if self.remote else self.source).name
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if self.remote:
            r = client.get(self.source, follow_redirects=True)
            r.raise_for_status()
            return name, r.content, r.headers.get("content-type", mime)
        return name, Path(self.source).read_bytes(), mime

    def url(self, client):
        if self.remote:
            return self.source
        _, data, mime = self.upload(client)
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


@dataclass(frozen=True)
class InlineImage(Asset):
    """A Gemini inlineData part."""


def destinations(a):
    text = a.op == "stt" or a.list_voices
    default = {"image": "png", "video": "mp4", "tts": "mp3", "music": "mp3", "sfx": "mp3",
               "stt": "json" if a.timestamps or a.diarize else "txt"}[a.kind]
    if a.list_voices:
        default = "json"
    suffix = Path(a.output or "").suffix.lstrip(".").lower()
    fmt = (a.format or suffix or default).lower()
    ext = fmt.split("_")[0] if a.kind in {"music", "sfx", "tts"} else fmt
    if not ext.isalnum():
        raise ValueError(f"invalid output format {fmt!r}")
    alias = {"jpg": "jpeg", "wave": "wav", "text": "txt"}
    if suffix and alias.get(suffix, suffix) != alias.get(ext, ext):
        raise ValueError("--format and output filename extension disagree")
    if text and fmt not in ({"json"} if a.list_voices or a.timestamps or a.diarize else {"txt", "text", "json"}):
        raise ValueError("use JSON to retain transcript metadata or voice listings, or txt for plain transcription")
    if a.kind == "video" and fmt != "mp4":
        raise ValueError("video output requires mp4")
    path = Path(a.output or ("-" if text else "out." + ext))
    count = a.count or 1
    if count > 1 and path == Path("-"):
        raise ValueError("-o - takes one artifact")
    outputs = ([path] if count == 1 else
               [path.with_name(f"{path.stem}-{i:03}{path.suffix}") for i in range(1, count + 1)])
    if a.submit_only:
        return fmt, []
    for dest in outputs:
        if dest == Path("-"):
            continue
        if not dest.parent.is_dir():
            raise ValueError(f"output directory does not exist: {dest.parent}")
        if dest.is_dir() or (os.path.lexists(dest) and not a.force):
            raise ValueError(f"output exists: {dest} (use --force to replace a file)")
    return fmt, outputs


def image_bytes(data, fmt):
    fmt = {"jpg": "JPEG"}.get(fmt, fmt.upper())
    # Native encodings pass through, including on installations without Pillow.
    magic = {"PNG": b"\x89PNG\r\n\x1a\n", "JPEG": b"\xff\xd8\xff", "GIF": b"GIF8"}
    if (fmt in magic and data.startswith(magic[fmt])) or (
            fmt == "WEBP" and data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        return data
    from PIL import Image
    out = io.BytesIO()
    with Image.open(io.BytesIO(data)) as img:
        if img.format == fmt:
            return data
        img = img.convert("RGB") if fmt == "JPEG" else img
        img.save(out, format=fmt)
    return out.getvalue()


def save(outputs, results, fmt, a):
    if len(results) != len(outputs):
        raise ValueError(f"endpoint returned {len(results)} artifacts for {len(outputs)} outputs")
    staged = []
    linked = []
    try:
        for path, data in zip(outputs, results, strict=True):
            if not data:
                raise ValueError("endpoint returned an empty artifact")
            if a.kind == "image":
                data = image_bytes(data, fmt)
            if path == Path("-"):
                sys.stdout.buffer.write(data)
                return
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
                staged.append((Path(f.name), path))
                f.write(data)
        # A concurrent run cannot replace a file when --force was absent.
        for tmp, path in staged:
            if a.force:
                os.replace(tmp, path)
            else:
                os.link(tmp, path)
                linked.append((tmp, path))
    except OSError:
        for tmp, path in linked:
            if path.exists() and path.samefile(tmp):
                path.unlink()
        raise
    finally:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)
    for path in outputs:
        print(path)


def pretty(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def checked(response):
    if response.is_error:
        raise ValueError(f"endpoint returned HTTP {response.status_code}: {response.text[:2000]}")
    response.raise_for_status()
    return response


def download(client, url, base, headers):
    dest = httpx.URL(base).join(url)
    origin = httpx.URL(base)
    same_origin = (dest.scheme, dest.host, dest.port) == (origin.scheme, origin.host, origin.port)
    # Follow redirects explicitly so saved-login custom headers stay on their origin.
    for _ in range(10):
        response = client.get(dest, headers=headers if same_origin else {})
        if not response.is_redirect:
            return checked(response).content
        dest = dest.join(response.headers["location"])
        same_origin = (dest.scheme, dest.host, dest.port) == (origin.scheme, origin.host, origin.port)
    raise ValueError("too many artifact redirects")
