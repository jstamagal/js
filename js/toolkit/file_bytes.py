"""A file's bytes as the file tools read them: a regular file read whole or to a limit, its content hash, and its line endings detected and normalized."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def read_regular_bytes(path: Path, limit: int | None = None) -> bytes:
    """Read bytes from *path*, refusing anything that is not a regular file and
    never blocking on a FIFO/socket/device.

    O_NONBLOCK makes the open return immediately for a pipe with no writer (the
    kernel would otherwise park the caller in fifo_open->wait_for_partner);
    on a regular file the flag has no effect and the read proceeds normally.
    Raises OSError for a non-regular file or any read error so callers degrade
    exactly as they do for a plain OSError."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"not a regular file: {path}")
        chunks: list[bytes] = []
        read_so_far = 0
        while limit is None or read_so_far < limit:
            want = 65536 if limit is None else min(65536, limit - read_so_far)
            chunk = os.read(fd, want)
            if not chunk:
                break
            chunks.append(chunk)
            read_so_far += len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def detect_line_ending(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    return "\r" if "\r" in text and "\n" not in text else "\n"


def normalize_line_endings(text: str, target: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\n", target)
