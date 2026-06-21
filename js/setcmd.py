"""Shared command runner — the first brick of the js scripting language.

Lexes a line into words and dispatches the `set` and `show` verbs against a
settings dict, using the knob registry in `js.settings`. There is NO variable
expansion yet — that arrives with the full lexer/verb set later. Both config
loading (slashless `set` in a jsrc script) and the REPL (`/set`, `/show`) call
this one runner, so the harness has a single config mechanism.

Callers own all I/O: `run_repl_command` and `apply_config_line` return a
`CommandResult`; the REPL prints its `lines`, config loading collects its
`error`s as boot warnings.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import settings as _s


@dataclass
class CommandResult:
    handled: bool = False          # a known verb was recognized
    changed: bool = False          # settings were mutated
    lines: list[str] = field(default_factory=list)  # human-readable output
    error: str | None = None       # a problem worth surfacing


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
        spec = _s.SPEC_BY_KEY.get(key)
        if spec is None:
            return CommandResult(handled=True, error=f"unknown knob: {key}")
        value = _s.get_dotted(settings, spec.path)
        return CommandResult(handled=True, lines=[f"{spec.key} = {render_value(spec, value)}"])

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
# set
# ---------------------------------------------------------------------------

def _map_prefix_spec(key: str) -> _s.SettingSpec | None:
    for spec in _s.REGISTRY:
        if spec.type == "map" and key.startswith(spec.key + "."):
            return spec
    return None


def apply_set(settings: dict, key: str, raw: str) -> CommandResult:
    """Set ``key`` to ``raw`` in ``settings``, coercing per the registry."""
    spec = _s.SPEC_BY_KEY.get(key)
    if spec is not None:
        value, error = _s.coerce_value(spec, raw)
        if error is not None:
            return CommandResult(handled=True, error=f"{key}: {error}")
        _s.set_dotted(settings, spec.path, value)
        return CommandResult(handled=True, changed=True,
                             lines=[f"{key} = {render_value(spec, value)}"])

    # sub-keys of a map knob (wiki.aliases.creative) or other keys within a known
    # section (provider.extra.*) — stored with loose scalar coercion.
    path = tuple(p for p in key.split(".") if p)
    if _map_prefix_spec(key) is not None or (path and path[0] in _s.KNOWN_SECTIONS and len(path) > 1):
        value = _s.coerce_extra_value(raw)
        _s.set_dotted(settings, path, value)
        return CommandResult(handled=True, changed=True, lines=[f"{key} = {value}"])

    return CommandResult(handled=True, error=f"unknown knob: {key}")


# ---------------------------------------------------------------------------
# Line dispatch
# ---------------------------------------------------------------------------

def _normalize(line: str) -> str | None:
    """Strip a comment/blank line (-> None) and a single leading REPL slash."""
    body = line.strip()
    if not body or body.startswith("#"):
        return None
    if body.startswith("/"):
        body = body[1:].lstrip()
    return body


def run_repl_command(settings: dict, line: str) -> CommandResult:
    """Dispatch a REPL command line (`/set ...`, `/show ...`). Returns
    ``handled=False`` when the verb is not one this runner owns."""
    body = _normalize(line)
    if body is None:
        return CommandResult(handled=False)
    parts = body.split(maxsplit=2)
    verb = parts[0].lower()

    if verb == "set":
        if len(parts) == 1:
            return show_lines(settings)
        if len(parts) == 2:
            return show_lines(settings, parts[1])
        return apply_set(settings, parts[1], parts[2])

    if verb == "show":
        return show_lines(settings, parts[1] if len(parts) > 1 else None)

    return CommandResult(handled=False)


def apply_config_line(settings: dict, line: str) -> CommandResult:
    """Apply one line from a jsrc config script. Only `set <key> <value>` is
    valid; comments/blanks are no-ops. Anything else returns an ``error`` so the
    loader can surface it as a boot warning without aborting."""
    body = _normalize(line)
    if body is None:
        return CommandResult(handled=True)
    parts = body.split(maxsplit=2)
    verb = parts[0].lower()
    if verb != "set":
        return CommandResult(handled=True, error=f"unknown command: {verb}")
    if len(parts) < 3:
        return CommandResult(handled=True, error=f"set needs a key and value: {body!r}")
    return apply_set(settings, parts[1], parts[2])
