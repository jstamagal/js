"""`/save`: the changed settings and /fset entries written into the user
jsrc, in place (`save_settings_to_jsrc`)."""

from __future__ import annotations

import fcntl
import os
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import fset, messages as msgs, setcmd
from .setting_layers import apply_jsrc
from .setting_values import config_value_text, jsrc_text
from .settings import (
    REGISTRY,
    SPEC_BY_KEY,
    SettingSpec,
    knob,
    seed_defaults,
    spec_for,
)


def _config_line_value(spec: SettingSpec, value: Any) -> str:
    """Render ``value`` as the right-hand side of a `set <key> <value>` line,
    so a saved line reloads to the same value (`jsrc_text`)."""
    return jsrc_text(spec.key, config_value_text(value))


def _set_line(spec: SettingSpec, value: Any) -> str:
    """`set <key> <value>`, or `set -<key>` when ``value`` is None; `fset`
    for an entry /fset owns. An empty value is written `""`."""
    if value is None:
        return f"set -{spec.key}"
    return f"set {spec.key} {_config_line_value(spec, value)}"


# How `_jsrc_key` names an `fset` line for an entry `js.fset` holds.
FSET_KEY = "fset:"



def _jsrc_key(line: str) -> tuple[str | None, bool]:
    """The setting a jsrc line sets, and whether the line is live. A commented
    `#set <setting> ...` line names its setting too, as not live; any other
    comment is prose and names nothing."""
    body = line.strip()
    live = not body.startswith("#")
    if not live and not body.startswith(("#set ", "#fset ")):
        return None, False
    parsed = setcmd.split_command(body if live else body[1:])
    if parsed is None or not setcmd.config_owns(parsed[0]):
        return None, False
    verb, arg = parsed
    name = setcmd.first_word(arg)[0] if verb in ("set", "fset") and arg else verb
    spec = spec_for(name.lstrip("-"))
    if spec is None and verb == "fset" and name:
        return FSET_KEY + name.lstrip("-"), live
    return (spec.key if spec else None), live


def _edited_jsrc_lines(lines: list[str], wanted: dict[str, str | None], extra_lines: list[str],
                       unsets: dict[str, str]) -> tuple[list[str], int]:
    """``lines`` of a jsrc edited so each setting in ``wanted`` gets the line
    it maps to, or its stock value for None, with ``extra_lines`` not in it
    yet appended; and how many lines changed.

    A setting's last live line is rewritten, else its `#set` line
    uncommented. When a `load` comes after that line, the loaded file could
    set the setting back, so the line goes at the end instead; the earlier
    lines stay, since a `load` between them may read them. A setting back at
    its stock value has its live lines with no `load` after them commented
    out, and its line in ``unsets`` (`set -NAME`) appended when one with a
    `load` after it is left. Every other line is left alone."""
    lines = list(lines)
    keyed = [_jsrc_key(line) for line in lines]
    loads = [i for i, line in enumerate(lines)
             if (parsed := setcmd.split_command(line)) is not None and parsed[0] in setcmd.LOAD_VERBS]

    def before_a_load(index: int) -> bool:
        return bool(loads) and loads[-1] > index

    live: dict[str, list[int]] = {}
    commented: dict[str, list[int]] = {}
    for index, (key, is_live) in enumerate(keyed):
        if key is not None:
            (live if is_live else commented).setdefault(key, []).append(index)
    changed = 0
    tail: list[str] = []
    for key, line in wanted.items():
        last = live[key][-1] if key in live else None
        if line is None:
            for index in live.get(key, []):
                if not before_a_load(index):
                    lines[index] = "#" + lines[index]
                    changed += 1
            if last is not None and before_a_load(last) and key in unsets:
                tail.append(unsets[key])
            continue
        if last is not None and not before_a_load(last):
            if lines[last].strip() != line:
                lines[last] = line
                changed += 1
        elif last is None and key in commented and not before_a_load(commented[key][0]):
            lines[commented[key][0]] = line
            changed += 1
        else:
            tail.append(line)
    present = {line.strip() for line in lines}
    tail += [line for line in extra_lines if line.strip() not in present]
    return lines + tail, changed + len(tail)


@contextmanager
def _locked_jsrc(path: Path) -> Iterator[None]:
    """Hold the config directory across a jsrc read, edit and write."""
    fd = os.open(path.resolve(strict=False).parent, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)



def _serialized(lines: list[str]) -> str:
    return "\n".join(lines) + "\n"


def _write_lines(path: Path, lines: list[str]) -> None:
    path = path.resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    os.close(fd)
    staging = Path(name)
    try:
        staging.write_text(_serialized(lines), encoding="utf-8")
        if path.exists():
            staging.chmod(path.stat().st_mode & 0o7777)
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def _reloaded(path: Path, text: str) -> tuple[dict, dict[str, str]]:
    """The settings and /fset entries a fresh start would load from ``text``
    as the jsrc at ``path``, read the way startup reads a file, the files it
    loads with it. The live ones, and the banner the messages speak behind,
    are left as they are."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    loaded = seed_defaults()
    banner = msgs.BANNER
    try:
        with fset.held():
            apply_jsrc(path.resolve(strict=False), loaded, [], [], lines=lines)
            entries = fset.overrides()
    finally:
        msgs.set_banner(banner)
    return loaded, entries


def _at_stock(spec: SettingSpec, value: Any) -> bool:
    return value == spec.default or (value in (None, "") and spec.default in (None, ""))


def changed_keys(before: dict, after: dict) -> list[str]:
    """The registered settings whose value differs between two stores."""
    return [spec.key for spec in REGISTRY if knob(before, spec.key) != knob(after, spec.key)]


def changed_fset_names(before: Mapping[str, str]) -> list[str]:
    """The /fset entries that differ from ``before`` (`fset.overrides()` then)."""
    now = fset.overrides()
    return sorted(name for name in set(now) | set(before) if now.get(name) != before.get(name))


def save_settings_to_jsrc(
    path: Path,
    settings: dict,
    *,
    keys: Iterable[str],
    fset_names: Iterable[str] = (),
    extra_lines: list[str] | None = None,
) -> int:
    """Write the settings named in ``keys`` and the /fset entries named in
    ``fset_names`` into the jsrc at ``path``, editing it in place
    (`_edited_jsrc_lines`); every other line is left as it is. A setting or
    entry back at js's own default loses its line. The edited file is then
    loaded in memory the way a fresh start loads it, files it loads
    included; each named setting or entry that comes back different from the
    live one gets its line, or its clearing line, appended, which nothing
    after it can set back. ``extra_lines`` are `on` and `alias` lines. With
    nothing to write, no file is made. No copy of the file is made. Returns
    how many lines changed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked_jsrc(path):
        return _save_settings_to_jsrc(path, settings, list(keys), list(fset_names), extra_lines or [])


def _save_settings_to_jsrc(path: Path, settings: dict, keys: list[str], fset_names: list[str],
                           extra_lines: list[str]) -> int:
    wanted: dict[str, str | None] = {}
    unsets: dict[str, str] = {}
    for key in keys:
        spec = SPEC_BY_KEY[key]
        value = knob(settings, key)
        unsets[key] = _set_line(spec, None)
        wanted[key] = None if _at_stock(spec, value) else _set_line(spec, value)
    entries = fset.overrides()
    for name in fset_names:
        unsets[FSET_KEY + name] = f"fset -{name}"
        wanted[FSET_KEY + name] = (f"fset {name} {jsrc_text(name, entries[name])}"
                                   if name in entries else None)
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines, changed = _edited_jsrc_lines(existing, wanted, extra_lines, unsets)

    loaded, loaded_entries = _reloaded(path, _serialized(lines))
    tail: list[str] = []
    for key in keys:
        spec = SPEC_BY_KEY[key]
        value, back = knob(settings, key), knob(loaded, key)
        if not (back == value or (_at_stock(spec, value) and _at_stock(spec, back))):
            tail.append(wanted[key] or unsets[key])
    for name in fset_names:
        if loaded_entries.get(name) != entries.get(name):
            tail.append(wanted[FSET_KEY + name] or unsets[FSET_KEY + name])
    if changed or tail:
        _write_lines(path, lines + tail)
    return changed + len(tail)
