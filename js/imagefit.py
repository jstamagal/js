"""Images fitted before they are sent, the way Claude Code fits them.

An image over 2000 px on a side is scaled to fit 2000 x 2000, keeping its
aspect ratio. An image over 3.75 MiB (5 MiB of base64) is re-encoded smaller:
a PNG first as a palette PNG, then any image as JPEG at falling quality, and
last at 1000 px wide. The image on disk is never changed. Every change says so
in a note the model and the user see.
"""

from __future__ import annotations

import hashlib
import io
import mimetypes
import warnings
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from . import paths
from .prompt_text import load_catalog

_PROMPTS = load_catalog('imagefit')

MAX_EDGE = 2000
MAX_BYTES = 5 * 1024 * 1024 * 3 // 4
_LAST_RESORT_WIDTH = 1000
_JPEG_QUALITIES = (80, 60, 40, 20)
_FORMATS = {"image/png": "PNG", "image/jpeg": "JPEG", "image/gif": "GIF", "image/webp": "WEBP"}


@dataclass(frozen=True)
class Fitted:
    data: bytes
    mime: str
    width: int
    height: int
    original_width: int
    original_height: int
    # How the bytes were re-encoded to fit MAX_BYTES; empty when they were not.
    encoding: str

    @property
    def changed(self) -> bool:
        return bool(self.encoding) or (self.width, self.height) != (self.original_width, self.original_height)

    @property
    def note(self) -> str:
        """What the model is told about the change; empty when there is none."""
        parts = []
        if (self.width, self.height) != (self.original_width, self.original_height):
            parts.append(_PROMPTS['image_resized'].format(
                original_width=self.original_width, original_height=self.original_height,
                width=self.width, height=self.height,
                scale=f"{self.original_width / self.width:.2f}"))
        if self.encoding:
            parts.append(_PROMPTS['image_reencoded'].format(encoding=self.encoding))
        return _PROMPTS['image_note'].format(parts=" ".join(parts)) if parts else ""


def fit(data: bytes, mime: str) -> Fitted | None:
    """``data`` as it is sent: unchanged when it already fits. None when the
    image cannot be decoded (or Pillow is missing); the caller sends it as is."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                return _fit(image, data, mime)
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def _fit(image, data: bytes, mime: str) -> Fitted:
    width, height = image.size
    if mime not in _FORMATS:
        mime = next((known for known, name in _FORMATS.items() if name == image.format), "image/png")
    fmt = _FORMATS[mime]

    def result(out: bytes, out_mime: str, w: int, h: int, encoding: str = "") -> Fitted:
        return Fitted(out, out_mime, w, h, width, height, encoding)

    if len(data) <= MAX_BYTES and width <= MAX_EDGE and height <= MAX_EDGE:
        return result(data, mime, width, height)

    if width <= MAX_EDGE and height <= MAX_EDGE:
        # Within the edge, too many bytes: smaller encodings at full size first.
        smaller = _smaller_encoding(image, mime)
        if smaller is not None:
            return result(smaller[0], smaller[1], width, height, smaller[2])

    w, h = width, height
    if w > MAX_EDGE:
        h, w = round(h * MAX_EDGE / w), MAX_EDGE
    if h > MAX_EDGE:
        w, h = round(w * MAX_EDGE / h), MAX_EDGE
    w, h = max(1, w), max(1, h)
    resized = image.resize((w, h), _lanczos())
    out = _encode(resized, fmt)
    if len(out) <= MAX_BYTES:
        return result(out, mime, w, h)
    smaller = _smaller_encoding(resized, mime)
    if smaller is not None:
        return result(smaller[0], smaller[1], w, h, smaller[2])
    w2 = min(w, _LAST_RESORT_WIDTH)
    h2 = max(1, round(h * w2 / max(w, 1)))
    out = _encode(image.resize((w2, h2), _lanczos()), "JPEG", quality=20)
    return result(out, "image/jpeg", w2, h2, _PROMPTS['encoding_jpeg'].format(quality=20))


def _smaller_encoding(image, mime: str) -> tuple[bytes, str, str] | None:
    """The first of a palette PNG (for a PNG) and JPEG at falling quality
    that fits MAX_BYTES, with its mime and how it was encoded; None when none
    does."""
    if mime == "image/png":
        out = _encode(image.convert("RGBA").quantize(256), "PNG", optimize=True)
        if len(out) <= MAX_BYTES:
            return out, "image/png", _PROMPTS['encoding_palette_png']
    for quality in _JPEG_QUALITIES:
        out = _encode(image, "JPEG", quality=quality)
        if len(out) <= MAX_BYTES:
            return out, "image/jpeg", _PROMPTS['encoding_jpeg'].format(quality=quality)
    return None


def _encode(image, fmt: str, **options) -> bytes:
    if fmt == "JPEG" and image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    if fmt in ("JPEG", "WEBP"):
        options.setdefault("quality", 80)
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **options)
    return buffer.getvalue()


def _lanczos():
    from PIL import Image
    return Image.Resampling.LANCZOS


def stored(fitted: Fitted) -> Path:
    """A file holding ``fitted``'s bytes, named by their content, for a
    marker that names the image by path."""
    suffix = mimetypes.guess_extension(fitted.mime) or ".img"
    path = paths.tmp_dir() / f"image-{hashlib.sha256(fitted.data).hexdigest()[:24]}{suffix}"
    if not path.exists():
        partial = path.with_name(f".{path.name}.{uuid4().hex}.partial")
        try:
            partial.write_bytes(fitted.data)
            partial.replace(path)
        finally:
            partial.unlink(missing_ok=True)
    return path
