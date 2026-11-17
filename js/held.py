"""Held write content: the text of a failed `write` call, kept on disk so the
model finishes the write by naming it (`from_held`) instead of sending it again.

A held file lives in `held_dir()`: the jail's ~/.js/tmp under `js -C` (the
model can read it there), else ~/.js/tmp. It is mode 0600 and named
`<session>-<n>-<basename>`; its id is `h<n>`, never reused within the
session: `.<session>.seq` keeps the last number handed out, so an id whose
content was written and deleted cannot later name other content.
The write that commits it deletes it. Every held file this process made is
deleted when the process exits, and any held file older than HELD_TTL
seconds is deleted when content is next held or loaded.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from json.decoder import JSONDecoder, scanstring
from pathlib import Path
from typing import Any

from . import jail, paths, settings as _settings, snippets

_LOCK = threading.Lock()
_MADE: set[Path] = set()
_DECODER = JSONDecoder(strict=False)
# The first `"key": "` of each string argument the salvage reads, in raw
# argument JSON. An escaped quote inside a string value cannot match.
_KEY_RE = re.compile(r'"(file_path|path|content|from_held)"\s*:\s*"')
_APPEND_RE = re.compile(r'"append"\s*:\s*true\b')
# A JSON string body through its closing quote.
_STRING_END_RE = re.compile(r'(?:[^"\\]|\\.)*"', re.S)


class HeldError(Exception):
    """A held id that names no held content of this session."""


@dataclass(frozen=True)
class Held:
    id: str
    path: Path
    text: str

    @property
    def size(self) -> int:
        return len(self.text.encode("utf-8"))


def held_dir() -> Path:
    path = jail.scratch_dir() / "held"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _session_key(context: Any) -> str:
    """The session this context writes to, as a file-name part without `-`:
    the session file's stem, or the context's own token when it has none."""
    session_file = None if context.config is None else context.config.session_file
    name = ""
    if session_file is not None and Path(session_file).name not in ("", os.devnull, "null"):
        name = Path(session_file).stem
    name = name or context.task_owner or "session"
    return "".join(ch if ch.isalnum() else "_" for ch in name)


def _sweep(directory: Path, context: Any) -> None:
    ttl = int(_settings.knob_attr(context, "held_ttl", "HELD_TTL") or 0)
    if ttl <= 0:
        return
    oldest = time.time() - ttl
    for entry in directory.iterdir():
        if entry.name.endswith(".seq"):
            continue
        try:
            if entry.is_file() and entry.stat().st_mtime < oldest:
                entry.unlink()
                _MADE.discard(entry)
        except OSError:
            continue


def _session_files(directory: Path, session: str) -> dict[int, Path]:
    out: dict[int, Path] = {}
    prefix = f"{session}-"
    for entry in directory.iterdir():
        if not entry.name.startswith(prefix):
            continue
        number, _, _rest = entry.name[len(prefix):].partition("-")
        if number.isdigit():
            out[int(number)] = entry
    return out


def _next_number(directory: Path, session: str, files: dict[int, Path]) -> int:
    """One more than any number this session has handed out. Caller holds _LOCK."""
    seq = directory / f".{session}.seq"
    try:
        last = int(seq.read_text(encoding="ascii").strip() or 0)
    except (OSError, ValueError):
        last = 0
    number = max(last, max(files, default=0)) + 1
    seq.write_text(str(number), encoding="ascii")
    return number


def _remove_made() -> None:
    for path in list(_MADE):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    _MADE.clear()


atexit.register(_remove_made)


def hold(context: Any, text: str, file_path: str | None) -> Held:
    """Keep ``text`` as held content of this context's session. Text the
    session already holds keeps its id. Raises OSError."""
    data = text.encode("utf-8", errors="replace")
    stored = data.decode("utf-8")
    with _LOCK:
        directory = held_dir()
        _sweep(directory, context)
        session = _session_key(context)
        files = _session_files(directory, session)
        for number, existing in sorted(files.items()):
            try:
                if existing.stat().st_size == len(data) and existing.read_bytes() == data:
                    return Held(f"h{number}", existing, stored)
            except OSError:
                continue
        number = _next_number(directory, session, files)
        base = re.sub(r"[^\w.-]", "_", Path(str(file_path or "")).name).lstrip(".") or "content"
        target = directory / f"{session}-{number}-{base}"
        temporary = directory / f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        _MADE.add(target)
    return Held(f"h{number}", target, stored)


def load(context: Any, held_id: str) -> Held:
    """The held content ``held_id`` names. Raises HeldError."""
    text = str(held_id).strip()
    number = text[1:] if text[:1].lower() == "h" else text
    with _LOCK:
        directory = held_dir()
        _sweep(directory, context)
        path = _session_files(directory, _session_key(context)).get(int(number)) if number.isdigit() else None
        if path is None:
            raise HeldError(f'no held content {held_id} in this session')
        try:
            content = path.read_bytes().decode("utf-8", errors="replace")
        except OSError as exc:
            raise HeldError(f'held content {held_id} cannot be read: {exc}') from exc
    return Held(f"h{int(number)}", path, content)


def release(held: Held) -> None:
    """Delete held content that a write committed."""
    with _LOCK:
        held.path.unlink(missing_ok=True)
        _MADE.discard(held.path)


def _line_count(text: str) -> int:
    return text.count("\n") + (0 if not text or text.endswith("\n") else 1)


def _absolute(context: Any, raw: str) -> Path:
    path = Path(os.path.expanduser(raw))
    return path if path.is_absolute() else Path(context.cwd) / path


def _resolve(context: Any, raw: str) -> Path | None:
    """``raw`` as the write tool resolves it, or None when the jail refuses it."""
    try:
        return context.resolve_path(raw, write=True)
    except (jail.JailError, OSError, ValueError):
        return None


def suggested_path(context: Any, raw: str) -> Path | None:
    """A path under the cwd for ``raw``, a path the jail refused: its last
    components, from the longest tail whose first directory exists in the cwd,
    else the file's directory and name (the name alone when that directory is
    the root, /tmp, /home or a home directory). None when the jail refuses
    that too."""
    cwd = Path(context.cwd)
    path = Path(os.path.abspath(_absolute(context, raw)))
    parts = path.parts[1:]
    if not parts:
        return None
    tail = next((parts[-k:] for k in range(len(parts) - 1, 1, -1) if (cwd / parts[-k]).is_dir()), None)
    if tail is None:
        parent = path.parent
        plain = (parent in (Path("/"), Path("/tmp"), Path("/home"), paths.user_home())
                 or parent.parent == Path("/home"))
        tail = parts[-1:] if plain or len(parts) < 2 else parts[-2:]
    candidate = cwd.joinpath(*tail)
    if candidate == path or _resolve(context, str(candidate)) is None:
        return None
    return candidate


def _orientation(context: Any, target_raw: str | None) -> list[str]:
    agent = context.agent_id or _settings.default_value("DEFAULT_AGENT")
    model = context.model or "?"
    active = jail.active()
    if active is None:
        lines = [f'You: agent {agent}, model {model}; cwd {context.cwd}; no jail.']
    else:
        writable = [str(active.root)]
        writable += [str(b.path) for b in active.binds(context.jail_bind) if b.rw]
        writable += ["/tmp", str(paths.home() / "tmp")]
        lines = ['You: agent {agent}, model {model}; cwd {cwd}; jail root {root} (writable: {writable}).'.format(
            agent=agent, model=model, cwd=context.cwd, root=active.root,
            writable=", ".join(dict.fromkeys(writable)))]
    if target_raw:
        resolved = _resolve(context, target_raw)
        if resolved is None:
            lines.append(f'Target {_absolute(context, target_raw)}: outside what the jail lets you write.')
        else:
            lines.append(f'Target {jail.shown(str(resolved))}: exists={str(resolved.exists()).lower()}, read this session={str(resolved in context.reads.paths()).lower()}.')
    return lines


def held_note(context: Any, held: Held, file_path: str | None, *, partial: bool = False) -> str:
    """The orientation block a failed write's ERROR ends with: where the
    content is held, the call that finishes the write, and where the model is."""
    raw = str(file_path or "")
    suggestion = suggested_path(context, raw) if raw and _resolve(context, raw) is None else None
    finish_path = str(suggestion) if suggestion is not None else raw or "<path>"
    shown_path = jail.shown(str(held.path))
    lines = held.text.splitlines() or [""]
    if partial:
        call = json.dumps({"file_path": finish_path, "from_held": held.id, "append": True,
                           "content": "<the rest>"}, ensure_ascii=False)
        head = snippets.render('held-content-partial', SIZE=f'{held.size:,}', LINES=f'{_line_count(held.text):,}', PATH=shown_path, ID=held.id, LAST_LINE=json.dumps(lines[-1][-120:], ensure_ascii=False), CALL=call)
    else:
        call = json.dumps({"file_path": finish_path, "from_held": held.id}, ensure_ascii=False)
        head = snippets.render('held-content', SIZE=f'{held.size:,}', LINES=f'{_line_count(held.text):,}', PATH=shown_path, ID=held.id, CALL=call)
    return "\n\n" + "\n".join([head, *_orientation(context, finish_path if raw else None)])


def hold_failed(context: Any, text: str | None, file_path: str | None, *, partial: bool = False) -> str:
    """Hold the content of a failed write and return its orientation block,
    or "" when the content is shorter than HELD_WRITE_MIN bytes or cannot be
    held."""
    if not isinstance(text, str):
        return ""
    minimum = int(_settings.knob_attr(context, "held_write_min", "HELD_WRITE_MIN") or 0)
    if len(text.encode("utf-8", errors="replace")) < minimum:
        return ""
    try:
        held = hold(context, text, file_path)
    except OSError:
        return ""
    return held_note(context, held, file_path, partial=partial)


def _decode_prefix(raw: str, start: int) -> str:
    """The JSON string starting at ``start`` (after its opening quote) as far
    as ``raw`` holds it: an escape the end cut in two is left out."""
    body = raw[start:]
    for cut in range(len(body), max(len(body) - 8, 0) - 1, -1):
        try:
            return _DECODER.decode('"' + body[:cut] + '"')
        except ValueError:
            continue
    return ""


def salvage_arguments(raw: str) -> tuple[dict[str, Any], bool]:
    """``file_path``, ``content``, ``from_held`` and ``append`` read out of
    write arguments that do not parse, and whether ``content`` was cut off
    before its closing quote."""
    found: dict[str, Any] = {}
    partial = False
    for match in _KEY_RE.finditer(raw):
        key = "file_path" if match.group(1) == "path" else match.group(1)
        if key in found:
            continue
        try:
            found[key] = scanstring(raw, match.end(), False)[0]
        except ValueError:
            if key == "content" and not _STRING_END_RE.match(raw, match.end()):
                found[key] = _decode_prefix(raw, match.end())
                partial = True
    found["append"] = bool(_APPEND_RE.search(raw))
    return found, partial


def hold_failed_call(context: Any, raw_args: str, parsed: object) -> str:
    """The orientation block for a `write` call refused before it ran: its
    arguments did not parse or validate, or write is not callable here. The
    content held is the parsed ``content``, or what the raw arguments carry
    of it when they do not parse; after the held text the call named, when it
    named one."""
    if context is None:
        return ""
    partial = False
    if isinstance(parsed, dict) and parsed:
        args = parsed
    else:
        args, partial = salvage_arguments(raw_args or "")
    file_path = args.get("file_path") or args.get("path")
    file_path = file_path if isinstance(file_path, str) else None
    return hold_failed_write(context, args.get("content"), file_path, from_held=args.get("from_held"),
                             append=bool(args.get("append")), partial=partial)


def hold_failed_write(context: Any, content: object, file_path: str | None, *, from_held: object = None,
                      append: bool = False, partial: bool = False) -> str:
    """The orientation block for a failed write of ``content`` to
    ``file_path``. With ``from_held`` the content is the held text, plus
    ``content`` after it with ``append``. A plain append holds nothing: the
    held text would replace the file it was to be added to."""
    if not from_held:
        return "" if append else hold_failed(context, content, file_path, partial=partial)
    try:
        prior = load(context, str(from_held))
    except HeldError:
        return ""
    if not append or not isinstance(content, str) or not content:
        return held_note(context, prior, file_path)
    return hold_failed(context, prior.text + content, file_path, partial=partial)
