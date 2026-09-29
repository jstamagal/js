"""Settings verbs for the command table in ``js.cli``.

`set`, `show` and `on` operate on a settings dict and an event-hook table,
using the registry in `js.settings`. The REPL's command table calls these;
config loading (jsrc, before the REPL exists) calls `apply_config_line`.

Callers own all I/O: every function returns a `CommandResult`; the REPL prints
its `lines`, config loading collects its `error`s as boot warnings.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import events as _events
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


def show_lines(settings: dict, key: str | None = None) -> CommandResult:
    """`show [key]` — every knob and its current value, or just one."""
    if key is not None:
        spec = _s.spec_for(key)
        if spec is None:
            return CommandResult(handled=True, error=f"unknown knob: {key}")
        value = _s.get_dotted(settings, spec.path)
        return CommandResult(
            handled=True,
            lines=[
                f"{spec.key} = {render_value(spec, value)}",
                f"  {spec.doc}",
            ],
        )

    lines: list[str] = []
    current_section: str | None = None
    for spec in _s.REGISTRY:
        if spec.section != current_section:
            if current_section is not None:
                lines.append("")
            lines.append(f"[{spec.section}]")
            current_section = spec.section
        value = _s.get_dotted(settings, spec.path)
        lines.append(f"  {spec.key} = {render_value(spec, value)}")
    return CommandResult(handled=True, lines=lines)


# ---------------------------------------------------------------------------
# Effective ("live") view
#
# `show_lines` renders the settings STORE — the values in the jsrc-derived dict.
# But a flag (`--model`), a saved login, and env-read sampling (JS_TEMP ...) feed
# the next turn WITHOUT ever landing in the store, so the store lies about what
# runs. The REPL overlays those live sources onto the display via a per-knob
# ``LiveValue`` map computed at the call site (cli.py), keeping `show_lines`
# itself pure for config-file processing.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LiveValue:
    """One knob's effective value plus where it actually comes from."""

    display: str   # already rendered + masked (secrets stay <set>)
    source: str    # e.g. "--model flag", "login testes", "env JS_TEMP"


def _annotate(line: str, live: LiveValue | None) -> str:
    return line if live is None else f"{line}  (live: {live.source})"


def show_lines_effective(
    settings: dict,
    overlay: Mapping[str, LiveValue] | None = None,
    key: str | None = None,
) -> CommandResult:
    """Like `show_lines`, but a knob present in ``overlay`` shows its effective
    value and a ``(live: <source>)`` tag instead of the store value."""
    overlay = overlay or {}
    if key is not None:
        spec = _s.spec_for(key)
        if spec is None:
            return CommandResult(handled=True, error=f"unknown knob: {key}")
        live = overlay.get(spec.key)
        display = live.display if live is not None else render_value(spec, _s.get_dotted(settings, spec.path))
        return CommandResult(
            handled=True,
            lines=[
                _annotate(f"{spec.key} = {display}", live),
                f"  {spec.doc}",
            ],
        )

    lines: list[str] = []
    current_section: str | None = None
    for spec in _s.REGISTRY:
        if spec.section != current_section:
            if current_section is not None:
                lines.append("")
            lines.append(f"[{spec.section}]")
            current_section = spec.section
        live = overlay.get(spec.key)
        display = live.display if live is not None else render_value(spec, _s.get_dotted(settings, spec.path))
        lines.append(_annotate(f"  {spec.key} = {display}", live))
    return CommandResult(handled=True, lines=lines)


# ---------------------------------------------------------------------------
# set
# ---------------------------------------------------------------------------

def _prefix_spec(key: str) -> _s.SettingSpec | None:
    for spec in _s.REGISTRY:
        if key.startswith(spec.key + "."):
            return spec
    return None


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
            lines=[f"{spec.key} = {render_value(spec, value)}"],
            changed_keys=[spec.key],
        )

    # sub-keys of a map knob (wiki.aliases.creative) or other keys within a known
    # section — stored with loose scalar coercion. Children of registered
    # non-map knobs are rejected so structured settings keep their validated shape.
    path = tuple(p for p in key.split(".") if p)
    prefix_spec = _prefix_spec(key)
    if prefix_spec is not None and prefix_spec.type != "map":
        return CommandResult(handled=True, error=f"unknown knob: {key}")
    if prefix_spec is not None or (path and path[0] in _s.KNOWN_SECTIONS and len(path) > 1):
        value = _s.coerce_extra_value(raw)
        _s.set_dotted(settings, path, value)
        return CommandResult(
            handled=True,
            changed=True,
            lines=[f"{key} = {value}"],
            changed_keys=[key],
        )

    return CommandResult(handled=True, error=f"unknown knob: {key}")


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


def apply_unset(settings: dict, key: str) -> CommandResult:
    """`set -<key>` — clear a knob back to its default/unset state."""
    spec = _s.spec_for(key)
    path = spec.path if spec is not None else tuple(p for p in key.split(".") if p)
    if not path:
        return CommandResult(handled=True, error=f"unknown knob: {key}")
    if spec is None:
        prefix_spec = _prefix_spec(key)
        if prefix_spec is None and not (path[0] in _s.KNOWN_SECTIONS and len(path) > 1):
            return CommandResult(handled=True, error=f"unknown knob: {key}")
    existed = _delete_dotted(settings, path)
    display = render_value(spec, None) if spec is not None else "<unset>"
    note = "" if existed else "  (already unset)"
    if spec is not None:
        key = spec.key
    return CommandResult(
        handled=True,
        changed=existed,
        lines=[f"{key} = {display}{note}"],
        changed_keys=[key] if existed else [],
    )


def set_command(settings: dict, arg: str) -> CommandResult:
    """`set` shows every value, `set key` shows one, `set -key` clears one,
    `set key value` sets one."""
    parts = arg.split(maxsplit=1)
    if not parts:
        return show_lines(settings)
    key = parts[0]
    if key.startswith("-") and len(key) > 1:
        return apply_unset(settings, key[1:])
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
        return CommandResult(handled=True, lines=event_lines(hooks) or ["(no event handlers)"])
    if len(parts) < 2:
        return CommandResult(handled=True, error="on needs an event and handler")
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
        return None, "load needs exactly one path"
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


def apply_config_line(settings: dict, line: str) -> CommandResult:
    """Apply one jsrc line to ``settings`` at config load. Comments/blanks are
    no-ops; a verb the settings layer does not own returns ``handled=False``.
    `set -key` clears that knob. A bad `set` returns an ``error`` so the loader
    can surface it without aborting."""
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
        return apply_unset(settings, parts[0][1:])
    if len(parts) < 2:
        return CommandResult(handled=True, error=f"set needs a key and value: {line.strip()!r}")
    return apply_set(settings, parts[0], parts[1])
