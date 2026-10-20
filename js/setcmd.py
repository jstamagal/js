"""Settings verbs for the command table in ``js.cli``.

`set`, `show` and `on` operate on a settings dict and an event-hook table,
using the registry in `js.settings`. The REPL's command table calls these;
config loading (jsrc, before the REPL exists) calls `apply_config_line`.

Callers own all I/O: every function returns a `CommandResult`; the REPL prints
its `lines`, config loading collects its `error`s as boot warnings.
"""

from __future__ import annotations

import copy
import fnmatch
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import events as _events
from . import messages as msgs
from . import settings as _s


@dataclass
class CommandResult:
    handled: bool = False          # a known verb was recognized
    changed: bool = False          # settings were mutated
    lines: list[str] = field(default_factory=list)  # human-readable output
    error: str | None = None       # a problem worth surfacing
    changed_keys: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _empty_display(spec: _s.SettingSpec) -> str:
    if spec.empty == _s.EMPTY_OFF:
        return "off"
    if spec.empty == _s.EMPTY_UNSET:
        return "<unset>"
    return "<none>"


def render_value(spec: _s.SettingSpec, value) -> str:
    """Render a knob's current value with honest empty states."""
    if value is None:
        return _empty_display(spec)
    if spec.type == "bool":
        return "on" if value else "off"
    if spec.secret:
        return "<set>" if value else _empty_display(spec)
    if isinstance(value, dict):
        if not value:
            return _empty_display(spec)
        return ", ".join(f"{k}={v}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        if not value:
            return _empty_display(spec)
        import json
        return json.dumps(value)
    return str(value)


def select(name: str | None) -> tuple[list[_s.SettingSpec], bool] | None:
    """The settings `set NAME` shows, and whether NAME named one setting.
    NAME is a setting (any case), a group (`/set compaction`), or a
    wildcard over the names (`/set *_FILE`). No NAME is every setting.
    None when NAME matches nothing."""
    if name is None:
        return list(_s.REGISTRY), False
    spec = _s.spec_for(name)
    if spec is not None:
        return [spec], True
    if any(char in name for char in "*?["):
        pattern = name.upper()
        found = [spec for spec in _s.REGISTRY if fnmatch.fnmatchcase(spec.key.upper(), pattern)]
    else:
        found = [spec for spec in _s.REGISTRY if spec.section.lower() == name.lower()]
    return (found, False) if found else None


def show_lines(settings: dict, key: str | None = None) -> CommandResult:
    """`set [NAME]`: the settings NAME selects (`select`) and their values. A
    setting the store does not hold shows its js/jsrc value, the value that runs."""
    return show_lines_effective(settings, None, key)


# ---------------------------------------------------------------------------
# Effective ("live") view
#
# `show_lines` renders the settings STORE — the values in the jsrc-derived dict.
# But a flag (`--model`), a saved login, and env-read sampling (JS_TEMPERATURE ...) feed
# the next turn WITHOUT ever landing in the store, so the store lies about what
# runs. The REPL overlays those live sources onto the display via a per-knob
# ``LiveValue`` map computed at the call site (cli.py), keeping `show_lines`
# itself pure for config-file processing.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LiveValue:
    """One knob's effective value plus where it actually comes from."""

    display: str   # already rendered + masked (secrets stay <set>)
    source: str    # e.g. "--model flag", "login testes", "env JS_TEMPERATURE"


def _annotate(line: str, live: LiveValue | None) -> str:
    return line if live is None else msgs.SETTING_LIVE.text(line=line, source=live.source)


def show_lines_effective(
    settings: dict,
    overlay: Mapping[str, LiveValue] | None = None,
    key: str | None = None,
) -> CommandResult:
    """Like `show_lines`, but a setting present in ``overlay`` shows its
    effective value and a ``live: <source>`` tag instead of the store value."""
    overlay = overlay or {}
    selected = select(key)
    if selected is None:
        return CommandResult(handled=True, error=msgs.UNKNOWN_SETTING.text(key=key))
    specs, one = selected
    lines: list[str] = []
    width = max(len(spec.key) for spec in specs)
    for spec in specs:
        live = overlay.get(spec.key)
        display = live.display if live is not None else render_value(spec, _s.knob(settings, spec.key))
        name = spec.key.upper()
        if one:
            line = msgs.SETTING_IS.line(colour=False, key=name, value=display)
        else:
            line = msgs.SETTING_ROW.line(colour=False, key=name.ljust(width), value=display)
        lines.append(_annotate(line, live))
    return CommandResult(handled=True, lines=lines)


# ---------------------------------------------------------------------------
# set
# ---------------------------------------------------------------------------

def apply_set(settings: dict, key: str, raw: str) -> CommandResult:
    """Set ``key`` to ``raw`` in ``settings``, coercing per the registry."""
    spec = _s.spec_for(key)
    if spec is not None:
        value, error = _s.coerce_value(spec, raw)
        if error is not None:
            return CommandResult(handled=True, error=f"{spec.key}: {error}")
        _s.set_dotted(settings, spec.path, value)
        return CommandResult(
            handled=True,
            changed=True,
            lines=[msgs.SETTING_CHANGED.line(colour=False, key=spec.key.upper(),
                                             value=render_value(spec, value) if spec.secret else raw.strip())],
            changed_keys=[spec.key],
        )

    # A sub-key of a map setting (EXTRA_PARAMS.organization) is stored with
    # loose scalar coercion. Any other name that is not a setting is refused,
    # children of registered non-map settings included.
    key = _s.child_key(key)
    path = tuple(p for p in key.split(".") if p)
    prefix_spec = _s.parent_spec(key)
    if prefix_spec is not None and prefix_spec.type == "map":
        value = _s.coerce_extra_value(raw)
        _s.set_dotted(settings, path, value)
        return CommandResult(
            handled=True,
            changed=True,
            lines=[msgs.SETTING_CHANGED.line(colour=False, key=key, value=raw.strip())],
            changed_keys=[key],
        )

    return CommandResult(handled=True, error=msgs.UNKNOWN_SETTING.text(key=key))


def _delete_dotted(settings: dict, path: tuple[str, ...]) -> bool:
    """Remove ``path`` from ``settings`` if present. Returns True if it existed."""
    cursor = settings
    for part in path[:-1]:
        nxt = cursor.get(part) if isinstance(cursor, dict) else None
        if not isinstance(nxt, dict):
            return False
        cursor = nxt
    if isinstance(cursor, dict) and path and path[-1] in cursor:
        del cursor[path[-1]]
        return True
    return False


def apply_unset(settings: dict, key: str, baseline: dict | None = None) -> CommandResult:
    """`set -<key>` — clear a knob. With ``baseline`` (the layers below this
    one), the knob takes the baseline's value; a knob the baseline does not
    hold is removed from ``settings``."""
    spec = _s.spec_for(key)
    if spec is None:
        key = _s.child_key(key)
    path = spec.path if spec is not None else tuple(p for p in key.split(".") if p)
    if not path:
        return CommandResult(handled=True, error=msgs.UNKNOWN_SETTING.text(key=key))
    if spec is None:
        prefix_spec = _s.parent_spec(key)
        if prefix_spec is None or prefix_spec.type != "map":
            # Only a sub-key of a map setting is an unset target besides a
            # setting (apply_set refuses the rest for the same reason).
            return CommandResult(handled=True, error=msgs.UNKNOWN_SETTING.text(key=key))
    missing = object()
    before = _s.get_dotted(settings, path, missing)
    restored = _s.get_dotted(baseline, path, missing) if baseline is not None else missing
    _delete_dotted(settings, path)
    if restored is not missing:
        _s.set_dotted(settings, path, copy.deepcopy(restored))
    after = _s.get_dotted(settings, path, missing)
    changed = before is not after and before != after
    if spec is not None:
        key = spec.key
        display = render_value(spec, None if after is missing else after)
        name = key.upper()
    else:
        prefix_spec = _s.parent_spec(key)
        secret = prefix_spec is not None and prefix_spec.secret
        if after is missing:
            display = "<unset>"
        elif secret:
            display = "<set>"
        else:
            display = str(after)
        name = key
    entry = msgs.SETTING_CHANGED if changed else msgs.SETTING_IS
    return CommandResult(
        handled=True,
        changed=changed,
        lines=[entry.line(colour=False, key=name, value=display)],
        changed_keys=[key] if changed else [],
    )


def set_command(settings: dict, arg: str, baseline: dict | None = None) -> CommandResult:
    """`set` shows every value, `set NAME` shows what NAME selects
    (`select`), `set -NAME` clears one (back to its ``baseline`` value, see
    `apply_unset`), `set NAME value` sets one."""
    parts = arg.split(maxsplit=1)
    if not parts:
        return show_lines(settings)
    key = parts[0]
    if key.startswith("-") and len(key) > 1:
        return apply_unset(settings, key[1:], baseline)
    if len(parts) == 1:
        return show_lines(settings, key)
    return apply_set(settings, key, parts[1])


# ---------------------------------------------------------------------------
# on
# ---------------------------------------------------------------------------

def event_lines(hooks: _events.EventHooks) -> list[str]:
    """Every registered handler as the `on` line that registers it again."""
    registered = hooks.all()
    lines: list[str] = []
    for event in _events.CANONICAL_EVENT_NAMES:
        for hook in registered.get(event, ()):
            prefix = "^" if hook.suppress else ""
            lines.append(f"on {prefix}{hook.event} {hook.handler}")
    return lines


def on_command(hooks: _events.EventHooks, arg: str) -> CommandResult:
    """`on` lists handlers; `on [^]event handler` registers one."""
    parts = arg.split(maxsplit=1)
    if not parts:
        return CommandResult(handled=True, lines=event_lines(hooks) or [msgs.NO_EVENT_HANDLERS.text()])
    if len(parts) < 2:
        return CommandResult(handled=True, error=msgs.ON_NEEDS_TWO.text())
    handler = parts[1].strip()
    if handler.startswith("="):
        handler = handler[1:].lstrip()
    try:
        hook = hooks.add(parts[0], handler)
    except ValueError as e:
        return CommandResult(handled=True, error=str(e))
    prefix = "^" if hook.suppress else ""
    return CommandResult(handled=True, changed=True, lines=[f"on {prefix}{hook.event} = {hook.handler}"])


# ---------------------------------------------------------------------------
# load paths
# ---------------------------------------------------------------------------

def _strip_load_comment(raw: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    at_word_start = True
    seen_word = False
    for index, char in enumerate(raw):
        if escaped:
            escaped = False
            at_word_start = False
            seen_word = True
            continue
        if char == "\\" and not in_single:
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            at_word_start = False
            seen_word = True
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            at_word_start = False
            seen_word = True
            continue
        if not in_single and not in_double:
            if char == "#":
                if at_word_start and seen_word:
                    return raw[:index].rstrip()
                at_word_start = False
                seen_word = True
                continue
            if char.isspace():
                at_word_start = True
                continue
        at_word_start = False
        seen_word = True
    return raw


LOAD_VERBS = ("load", "source")
MAX_LOAD_DEPTH = 16


def load_path(arg: str, base: Path) -> tuple[Path | None, str | None]:
    """Resolve a `load` argument (one shell-quoted path, trailing `# comment`
    allowed) against ``base``. Returns ``(path, error)``."""
    try:
        parts = shlex.split(_strip_load_comment(arg))
    except ValueError as e:
        return None, str(e)
    if len(parts) != 1:
        return None, msgs.LOAD_NEEDS_ONE_PATH.text()
    path = Path(parts[0]).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False), None


# ---------------------------------------------------------------------------
# Line splitting and the config layer
# ---------------------------------------------------------------------------

def split_command(line: str) -> tuple[str, str] | None:
    """``(verb, arg)`` for a command line, or None for a blank/comment line.
    One leading ``/`` is stripped: it is required at the input line and
    optional everywhere else."""
    body = line.strip()
    if body.startswith("/"):
        body = body[1:].lstrip()
    if not body or body.startswith("#"):
        return None
    verb, _, arg = body.partition(" ")
    return verb.lower(), arg.strip()


def config_owns(verb: str) -> bool:
    """Verbs the settings layer applies while config loads: `set`, and a
    setting's short name (`model X` is `set model X`). Every other verb in a
    jsrc runs through the command table when the REPL starts."""
    return verb == "set" or verb in _s.SPEC_BY_ALIAS


def apply_config_line(settings: dict, line: str, baseline: dict | None = None) -> CommandResult:
    """Apply one jsrc line to ``settings`` at config load. Comments/blanks are
    no-ops; a verb the settings layer does not own returns ``handled=False``.
    `set -key` puts that knob back to its ``baseline`` value (see
    `apply_unset`). A bad `set` returns an ``error`` so the loader can surface
    it without aborting."""
    parsed = split_command(line)
    if parsed is None:
        return CommandResult(handled=True)
    verb, arg = parsed
    if not config_owns(verb):
        return CommandResult(handled=False)
    if verb != "set":
        arg = f"{verb} {arg}"
    parts = arg.split(maxsplit=1)
    if len(parts) == 1 and parts[0].startswith("-") and len(parts[0]) > 1:
        return apply_unset(settings, parts[0][1:], baseline)
    if len(parts) < 2:
        return CommandResult(handled=True, error=msgs.SET_NEEDS_TWO.text(line=line.strip()))
    return apply_set(settings, parts[0], parts[1])
