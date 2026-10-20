"""Settings verbs for the command table in ``js.cli``.

`set`, `show` and `on` operate on a settings dict and an event-hook table,
using the registry in `js.settings`; `fset` on the entries /fset owns: the
registry's `command="fset"` settings and `js.fset`. The REPL's command table calls these;
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
from . import fset as _fset
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
        return msgs.SETTING_VALUE_OFF.words()
    if spec.empty == _s.EMPTY_UNSET:
        return msgs.SETTING_VALUE_UNSET.words()
    return msgs.SETTING_VALUE_NONE.words()


def masks_secrets(settings: dict | None) -> bool:
    """True when ``settings``' PARANOID_MODE hides secret values."""
    from . import paranoid

    # The store alone: `js/jsrc` itself is applied through `apply_set`.
    return paranoid.masks(paranoid.read(_s.get_dotted(settings or {}, ("PARANOID_MODE",))))


def masked_store(settings: dict, *, masked: bool) -> dict:
    """A copy of ``settings`` for printing or a snapshot: each secret
    setting's value rendered as `render_value` shows it when ``masked``."""
    out = copy.deepcopy(settings)
    if not masked:
        return out
    for spec in _s.REGISTRY:
        if not spec.secret:
            continue
        missing = object()
        value = _s.get_dotted(out, spec.path, missing)
        if value is not missing:
            _s.set_dotted(out, spec.path, render_value(spec, value, masked=True))
    return out


def render_value(spec: _s.SettingSpec, value, *, masked: bool = True) -> str:
    """Render a setting's current value with honest empty states. A secret
    shows as <set> when ``masked``."""
    if value is None:
        return _empty_display(spec)
    if spec.type == "bool":
        return msgs.SETTING_VALUE_ON.words() if value else msgs.SETTING_VALUE_OFF.words()
    if spec.secret and masked:
        return msgs.SETTING_VALUE_SECRET.words() if value else _empty_display(spec)
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


def _set_specs() -> list[_s.SettingSpec]:
    """The settings `set` owns; the rest are /fset's."""
    return [spec for spec in _s.REGISTRY if spec.command == "set"]


def select(name: str | None) -> tuple[list[_s.SettingSpec], bool] | None:
    """The settings `set NAME` shows, and whether NAME named one setting.
    NAME is a setting (any case), a group (`/set compaction`), or a
    wildcard over the names (`/set *_FILE`). No NAME is every setting.
    None when NAME matches nothing. A group or a wildcard lists only the
    settings `set` owns."""
    if name is None:
        return _set_specs(), False
    spec = _s.spec_for(name)
    if spec is not None:
        return [spec], True
    if any(char in name for char in "*?["):
        pattern = name.upper()
        found = [spec for spec in _set_specs() if fnmatch.fnmatchcase(spec.key.upper(), pattern)]
    else:
        found = [spec for spec in _set_specs() if spec.section.lower() == name.lower()]
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
# runs. The REPL overlays those live sources onto the display via a per-setting
# ``LiveValue`` map computed at the call site (cli.py), keeping `show_lines`
# itself pure for config-file processing.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LiveValue:
    """One setting's effective value plus where it actually comes from."""

    display: str   # already rendered; a secret masked per PARANOID_MODE
    source: str    # e.g. "--model flag", "login testes", "env JS_TEMPERATURE"


def _annotate(line: str, live: LiveValue | None) -> str:
    return line if live is None else msgs.line_for(msgs.SETTING_LIVE, line=line, source=live.source)


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
        return CommandResult(handled=True, error=msgs.UNKNOWN_SETTING.said(key=key))
    specs, one = selected
    if one and specs[0].command != "set":
        return CommandResult(handled=True, error=msgs.SETTING_IS_FSET.said(key=specs[0].key))
    lines: list[str] = []
    width = max(len(spec.key) for spec in specs)
    masked = masks_secrets(settings)
    for spec in specs:
        live = overlay.get(spec.key)
        display = live.display if live is not None else render_value(
            spec, _s.knob(settings, spec.key), masked=masked)
        name = spec.key.upper()
        if one:
            line = msgs.line_for(msgs.SETTING_IS, key=name, value=display)
        else:
            line = msgs.line_for(msgs.SETTING_ROW, key=name.ljust(width), value=display)
        lines.append(_annotate(line, live))
    return CommandResult(handled=True, lines=lines)


# ---------------------------------------------------------------------------
# set
# ---------------------------------------------------------------------------

def apply_set(settings: dict, key: str, raw: str, *, verb: str = "set") -> CommandResult:
    """Set ``key`` to ``raw`` in ``settings``, coercing per the registry.
    ``verb`` is the command doing it; a setting another verb owns is refused."""
    spec = _s.spec_for(key)
    if spec is not None and spec.command != verb:
        return CommandResult(handled=True, error=msgs.SETTING_IS_FSET.said(key=spec.key))
    if spec is not None:
        value, error = _s.coerce_value(spec, raw)
        if error is not None:
            return CommandResult(handled=True, error=msgs.FSET_REFUSED.said(name=spec.key, error=error))
        _s.set_dotted(settings, spec.path, value)
        if spec.key == "BANNER":
            # The confirmation already speaks under the new banner.
            msgs.set_banner(str(value))
        return CommandResult(
            handled=True,
            changed=True,
            lines=[msgs.line_for(msgs.SETTING_CHANGED, key=spec.key.upper(),
                                 value=render_value(spec, value, masked=masks_secrets(settings))
                                 if spec.secret else raw.strip())],
            changed_keys=[spec.key],
        )

    # A sub-key of a map setting (EXTRA_PARAMS.organization) is stored with
    # loose scalar coercion, or as the string a JSON string literal spells. Any other name that is not a setting is refused,
    # children of registered non-map settings included.
    key = _s.child_key(key)
    path = tuple(p for p in key.split(".") if p)
    prefix_spec = _s.parent_spec(key)
    if prefix_spec is not None and prefix_spec.type == "map":
        # A JSON string literal is the string it spells, as for any string
        # setting (`from_line_literal`); anything else is read loosely.
        text = raw.strip()
        decoded = _s.from_line_literal(text)
        value = decoded if decoded != text else _s.coerce_extra_value(raw)
        _s.set_dotted(settings, path, value)
        return CommandResult(
            handled=True,
            changed=True,
            lines=[msgs.line_for(
                msgs.SETTING_CHANGED, key=key,
                value=msgs.SETTING_VALUE_SECRET.words() if prefix_spec is not None and prefix_spec.secret and masks_secrets(settings)
                else raw.strip())],
            changed_keys=[key],
        )

    return CommandResult(handled=True, error=msgs.UNKNOWN_SETTING.said(key=key))


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


def apply_unset(settings: dict, key: str, baseline: dict | None = None, *, verb: str = "set") -> CommandResult:
    """`set -<key>` — clear a setting. With ``baseline`` (the layers below this
    one), the setting takes the baseline's value; a setting the baseline does not
    hold is removed from ``settings``. ``verb`` is the command doing it."""
    spec = _s.spec_for(key)
    if spec is not None and spec.command != verb:
        return CommandResult(handled=True, error=msgs.SETTING_IS_FSET.said(key=spec.key))
    if spec is None:
        key = _s.child_key(key)
    path = spec.path if spec is not None else tuple(p for p in key.split(".") if p)
    if not path:
        return CommandResult(handled=True, error=msgs.UNKNOWN_SETTING.said(key=key))
    if spec is None:
        prefix_spec = _s.parent_spec(key)
        if prefix_spec is None or prefix_spec.type != "map":
            # Only a sub-key of a map setting is an unset target besides a
            # setting (apply_set refuses the rest for the same reason).
            return CommandResult(handled=True, error=msgs.UNKNOWN_SETTING.said(key=key))
    missing = object()
    before = _s.get_dotted(settings, path, missing)
    restored = _s.get_dotted(baseline, path, missing) if baseline is not None else missing
    _delete_dotted(settings, path)
    if restored is not missing:
        _s.set_dotted(settings, path, copy.deepcopy(restored))
    after = _s.get_dotted(settings, path, missing)
    if spec is not None and spec.key == "BANNER":
        msgs.set_banner(str(_s.knob(settings, "BANNER")))
    changed = before is not after and before != after
    if spec is not None:
        key = spec.key
        display = render_value(spec, None if after is missing else after, masked=masks_secrets(settings))
        name = key.upper()
    else:
        prefix_spec = _s.parent_spec(key)
        secret = prefix_spec is not None and prefix_spec.secret
        if after is missing:
            display = msgs.SETTING_VALUE_UNSET.words()
        elif secret and masks_secrets(settings):
            display = msgs.SETTING_VALUE_SECRET.words()
        else:
            display = str(after)
        name = key
    entry = msgs.SETTING_CHANGED if changed else msgs.SETTING_IS
    return CommandResult(
        handled=True,
        changed=changed,
        lines=[msgs.line_for(entry, key=name, value=display)],
        changed_keys=[key] if changed else [],
    )


def set_command(settings: dict, arg: str, baseline: dict | None = None) -> CommandResult:
    """`set` shows every value, `set NAME` shows what NAME selects
    (`select`), `set -NAME` clears one (back to its ``baseline`` value, see
    `apply_unset`), `set NAME value` sets one."""
    key, value = first_word(arg)
    if not key:
        return show_lines(settings)
    if key.startswith("-") and len(key) > 1:
        return apply_unset(settings, key[1:], baseline)
    if not value:
        return show_lines(settings, key)
    return apply_set(settings, key, value)


# ---------------------------------------------------------------------------
# fset
# ---------------------------------------------------------------------------

def _fset_specs() -> list[_s.SettingSpec]:
    return [spec for spec in _s.REGISTRY if spec.command == "fset"]


def fset_rows(settings: dict, pattern: str | None = None) -> list[str]:
    """`NAME value` for every /fset entry, or those ``pattern`` matches
    (`*` and `?` wildcards; a bare name matches itself)."""
    rows: list[str] = []
    for spec in _fset_specs():
        if pattern is None or fnmatch.fnmatchcase(spec.key, pattern):
            value = render_value(spec, _s.knob(settings, spec.key))
            rows.append(msgs.line_for(msgs.FSET_VALUE, name=spec.key, value=value))
    for name in _fset.names():
        if pattern is None or fnmatch.fnmatchcase(name, pattern):
            value = _fset.lookup(name)
            if value is None and name in msgs.ENTRIES:
                value = msgs.ENTRIES[name].format()
            rows.append(msgs.line_for(msgs.FSET_VALUE, name=name, value=value) if value
                        else msgs.line_for(msgs.FSET_EMPTY, name=name))
    return rows


def fset_command(settings: dict, arg: str, baseline: dict | None = None) -> CommandResult:
    """`fset` lists every entry, `fset NAME` (or a `*` pattern) shows them,
    `fset -NAME` clears one, `fset NAME value` sets one. A display level is a
    setting in ``settings``; any other entry is `js.fset`'s."""
    name, value = first_word(arg)
    name = name.upper()
    if not name:
        return CommandResult(handled=True, lines=fset_rows(settings))
    spec = _s.spec_for(name.lstrip("-"))
    if spec is not None and spec.command == "fset":
        if name.startswith("-"):
            return apply_unset(settings, spec.key, baseline, verb="fset")
        if not value:
            return CommandResult(handled=True, lines=fset_rows(settings, spec.key))
        result = apply_set(settings, spec.key, value, verb="fset")
        if result.error is None:
            result.lines = fset_rows(settings, spec.key)
        return result
    if name.startswith("-") and len(name) > 1:
        if not _fset.is_name(name[1:]):
            return CommandResult(handled=True, error=msgs.FSET_UNKNOWN.said(name=name[1:]))
        changed = _fset.unset_entry(name[1:])
        return CommandResult(handled=True, changed=changed, lines=fset_rows(settings, name[1:]))
    if not value:
        rows = fset_rows(settings, name)
        if not rows:
            return CommandResult(handled=True, error=msgs.FSET_UNKNOWN.said(name=name))
        return CommandResult(handled=True, lines=rows)
    if not _fset.is_name(name):
        return CommandResult(handled=True, error=msgs.FSET_UNKNOWN.said(name=name))
    error = _fset.set_entry(name, _s.from_line_literal(value))
    if error is not None:
        return CommandResult(handled=True, error=msgs.FSET_REFUSED.said(name=name, error=error))
    return CommandResult(handled=True, changed=True, lines=fset_rows(settings, name))


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
    event, handler = first_word(arg)
    if not event:
        return CommandResult(handled=True, lines=event_lines(hooks) or [msgs.line_for(msgs.NO_EVENT_HANDLERS)])
    if not handler:
        return CommandResult(handled=True, error=msgs.ON_NEEDS_TWO.said())
    if handler.startswith("="):
        handler = handler[1:].lstrip()
    try:
        hook = hooks.add(event, handler)
    except ValueError as e:
        return CommandResult(handled=True, error=str(e))
    prefix = "^" if hook.suppress else ""
    return CommandResult(handled=True, changed=True, lines=[msgs.EVENT_HANDLER_REGISTERED.words(prefix=prefix, event=hook.event, handler=hook.handler)])


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
        return None, msgs.LOAD_NEEDS_ONE_PATH.said()
    path = Path(parts[0]).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False), None


# ---------------------------------------------------------------------------
# Line splitting and the config layer
# ---------------------------------------------------------------------------

def first_word(text: str) -> tuple[str, str]:
    """The first word of ``text`` and the rest after it, stripped, split at
    any run of whitespace: how `set`, `fset`, `on` and every command line,
    typed or in a jsrc, are read."""
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return "", ""
    return parts[0], parts[1].strip() if len(parts) > 1 else ""


def split_command(line: str) -> tuple[str, str] | None:
    """``(verb, arg)`` for a command line, or None for a blank/comment line.
    One leading ``/`` is stripped: it is required at the input line and
    optional everywhere else."""
    body = line.strip()
    if body.startswith("/"):
        body = body[1:].lstrip()
    if not body or body.startswith("#"):
        return None
    verb, arg = first_word(body)
    return verb.lower(), arg


# Verbs whose text keeps its `$NAME`s for later: an alias or handler body is
# expanded when it runs.
_EXPANDED_LATER = ("alias", "on")


def expand_line(line: str, settings: dict | None = None) -> str:
    """A script, jsrc, alias or handler line with its `$NAME`s expanded once
    (`js.fset.expand_names`). An `alias` or `on` line, and an `fset` line for
    a format or a colour, keep theirs: they are read when the text runs or is
    drawn (`js.fset.read_when_drawn`)."""
    parsed = split_command(line)
    if parsed is None or parsed[0] in _EXPANDED_LATER:
        return line
    verb, arg = parsed
    name = first_word(arg)[0].lstrip("-").upper()
    if verb == "fset" and _fset.read_when_drawn(name):
        return line
    return _fset.expand_names(line, settings)


def config_owns(verb: str) -> bool:
    """Verbs the settings layer applies while config loads: `set`, `fset`,
    and a setting's short name (`model X` is `set model X`). Every other verb
    in a jsrc runs through the command table when the REPL starts."""
    return verb in ("set", "fset") or verb in _s.SPEC_BY_ALIAS


def apply_config_line(settings: dict, line: str, baseline: dict | None = None) -> CommandResult:
    """Apply one jsrc line to ``settings`` at config load. Comments/blanks are
    no-ops; a verb the settings layer does not own returns ``handled=False``.
    `set -key` puts that setting back to its ``baseline`` value (see
    `apply_unset`). A bad `set` returns an ``error`` so the loader can surface
    it without aborting."""
    parsed = split_command(line)
    if parsed is None:
        return CommandResult(handled=True)
    verb, arg = parsed
    if not config_owns(verb):
        return CommandResult(handled=False)
    if verb == "fset":
        if not first_word(arg)[1] and not arg.startswith("-"):
            return CommandResult(handled=True, error=msgs.SET_NEEDS_TWO.said(line=line.strip()))
        return fset_command(settings, arg, baseline)
    if verb != "set":
        arg = f"{verb} {arg}"
    key, value = first_word(arg)
    if not value and key.startswith("-") and len(key) > 1:
        return apply_unset(settings, key[1:], baseline)
    if not value:
        return CommandResult(handled=True, error=msgs.SET_NEEDS_TWO.said(line=line.strip()))
    return apply_set(settings, key, value)
