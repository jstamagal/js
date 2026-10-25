"""The bytes of the images and other media a session sends, stored once by content.

A history message lists its media parts, ``{"type": "media", "sha256", "mime",
"size"}``, under ``MEDIA_KEY``; the provider gets the bytes. The bytes live in
``paths.media_dir() / <sha256>``. The session file stays small, and every
later request, in this run or after a resume, sends the same bytes in the same
place, so a provider's prompt cache keeps matching past the image.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any

from . import paths

MEDIA_PART = "media"
# The key of a history message's list of media parts.
MEDIA_KEY = "media"
_DIGEST = re.compile(r"[0-9a-f]{64}")


def store(data: bytes, mime: str) -> dict:
    """Keep ``data`` in the store and return the media part naming it."""
    digest = hashlib.sha256(data).hexdigest()
    path = paths.media_dir() / digest
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(f".{digest}.{os.getpid()}.partial")
        partial.write_bytes(data)
        os.replace(partial, path)
    return {"type": MEDIA_PART, "sha256": digest, "mime": mime, "size": len(data)}


def is_part(value: Any) -> bool:
    """Whether ``value`` is a media part."""
    return (isinstance(value, dict) and value.get("type") == MEDIA_PART
            and isinstance(value.get("sha256"), str) and isinstance(value.get("mime"), str))


def load(part: dict) -> bytes | None:
    """The bytes ``part`` names; None when the store no longer has them."""
    digest = part["sha256"]
    if not _DIGEST.fullmatch(digest):
        return None
    try:
        return (paths.media_dir() / digest).read_bytes()
    except OSError:
        return None
