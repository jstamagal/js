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

import shlex
from dataclasses import dataclass, field, replace
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


@dataclass(frozen=True)
class CommandContext:
    cwd: Path = field(default_factory=Path.cwd)
    events: _events.EventHooks | None = None
    max_load_depth: int = 16
    _load_stack: tuple[Path, ...] = ()


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
        return CommandResult(
            handled=True,
            changed=True,
            lines=[f"{key} = {render_value(spec, value)}"],
            changed_keys=[key],
        )

    # sub-keys of a map knob (wiki.aliases.creative) or other keys within a known
    # section (provider.extra.*) — stored with loose scalar coercion.
    path = tuple(p for p in key.split(".") if p)
    if _map_prefix_spec(key) is not None or (path and path[0] in _s.KNOWN_SECTIONS and len(path) > 1):
        value = _s.coerce_extra_value(raw)
        _s.set_dotted(settings, path, value)
        return CommandResult(
            handled=True,
            changed=True,
            lines=[f"{key} = {value}"],
            changed_keys=[key],
        )

    return CommandResult(handled=True, error=f"unknown knob: {key}")


# ---------------------------------------------------------------------------
# on / load
# ---------------------------------------------------------------------------

def _show_event_lines(context: CommandContext | None) -> CommandResult:
    if context is None or context.events is None:
        return CommandResult(handled=True, lines=["(no event handlers)"])
    hooks = context.events.all()
    if not hooks:
        return CommandResult(handled=True, lines=["(no event handlers)"])
    lines: list[str] = []
    for event in _events.CANONICAL_EVENT_NAMES:
        for hook in hooks.get(event, ()):
            prefix = "^" if hook.suppress else ""
            lines.append(f"on {prefix}{hook.event} = {hook.handler}")
    return CommandResult(handled=True, lines=lines)


def apply_on(context: CommandContext | None, event_token: str, handler: str) -> CommandResult:
    if context is None or context.events is None:
        return CommandResult(handled=True, error="on needs an event context")
    try:
        hook = context.events.add(event_token, handler)
    except ValueError as e:
        return CommandResult(handled=True, error=str(e))
    prefix = "^" if hook.suppress else ""
    return CommandResult(
        handled=True,
        changed=True,
        lines=[f"on {prefix}{hook.event} = {hook.handler}"],
    )


def _split_load_arg(raw: str) -> tuple[str | None, str | None]:
    try:
        parts = shlex.split(raw)
    except ValueError as e:
        return None, str(e)
    if len(parts) != 1:
        return None, "load needs exactly one path"
    return parts[0], None


def _resolve_load_path(raw_path: str, context: CommandContext) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = context.cwd / path
    return path.resolve(strict=False)


def run_script_file(settings: dict, raw_path: str, context: CommandContext | None) -> CommandResult:
    """Load one ircII-style script file.

    Script files use slashless commands. For this foundation pass, the accepted
    verbs are ``set``, ``show``, ``on``, and nested ``load``.
    """
    if context is None:
        context = CommandContext()
    path = _resolve_load_path(raw_path, context)
    if len(context._load_stack) >= context.max_load_depth:
        return CommandResult(handled=True, error="load nesting too deep")
    if path in context._load_stack:
        return CommandResult(handled=True, error=f"load cycle: {path}")
    if not path.is_file():
        return CommandResult(handled=True, error=f"script not found: {path}")

    child = replace(context, cwd=path.parent, _load_stack=(*context._load_stack, path))
    lines: list[str] = []
    changed_keys: list[str] = []
    changed = False
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        result = apply_script_line(settings, raw, context=child)
        if result.error:
            return CommandResult(
                handled=True,
                changed=changed,
                lines=lines,
                error=f"{path}:{lineno}: {result.error}",
                changed_keys=changed_keys,
            )
        changed = changed or result.changed
        changed_keys.extend(result.changed_keys)
        lines.extend(result.lines)
    lines.append(f"loaded {path}")
    return CommandResult(handled=True, changed=changed, lines=lines, changed_keys=changed_keys)


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


def is_repl_command(line: str, *commands: str) -> bool:
    body = line.strip().lower()
    return any(body == cmd.lower() or body.startswith(cmd.lower() + " ") for cmd in commands)


def run_repl_command(settings: dict, line: str, *, context: CommandContext | None = None) -> CommandResult:
    """Dispatch a REPL command line (`/set ...`, `/show ...`, `/on ...`,
    `/load ...`). Returns
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

    if verb == "on":
        if len(parts) == 1:
            return _show_event_lines(context)
        if len(parts) < 3:
            return CommandResult(handled=True, error="on needs an event and handler")
        return apply_on(context, parts[1], parts[2])

    if verb == "load":
        if len(parts) < 2:
            return CommandResult(handled=True, error="load needs a path")
        raw_path, error = _split_load_arg(body[len(parts[0]):].strip())
        if error:
            return CommandResult(handled=True, error=error)
        return run_script_file(settings, raw_path or "", context)

    return CommandResult(handled=False)


def _handler_verb(line: str) -> str:
    body = _normalize(line)
    if body is None:
        return "<blank>"
    return body.split(maxsplit=1)[0].lower()


@dataclass(frozen=True)
class EventCommandDispatcher:
    """Run event handler text through the slash/setcmd command surface."""

    settings: dict
    cwd: Path = field(default_factory=Path.cwd)
    events: _events.EventHooks | None = None
    max_load_depth: int = 16

    def __call__(
        self,
        hook: _events.EventHook,
        emission: _events.EventEmission,
    ) -> _events.EventHandlerResult:
        context = CommandContext(
            cwd=self.cwd,
            events=self.events,
            max_load_depth=self.max_load_depth,
        )
        result = run_repl_command(self.settings, hook.handler, context=context)
        if not result.handled:
            return _events.EventHandlerResult(
                hook=hook,
                error=f"unsupported event handler command: {_handler_verb(hook.handler)}",
            )
        return _events.EventHandlerResult(
            hook=hook,
            lines=list(result.lines),
            error=result.error,
            changed=result.changed,
            changed_keys=list(result.changed_keys),
        )


def apply_script_line(settings: dict, line: str, *, context: CommandContext | None = None) -> CommandResult:
    """Apply one line from a loaded ircII-style script. Comments/blanks are
    no-ops; commands are slashless, though a leading slash is tolerated at the
    normalization layer."""
    body = _normalize(line)
    if body is None:
        return CommandResult(handled=True)
    parts = body.split(maxsplit=2)
    verb = parts[0].lower()
    if verb in {"set", "show", "on", "load"}:
        return run_repl_command(settings, body, context=context)
    return CommandResult(handled=True, error=f"unknown command: {verb}")


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
