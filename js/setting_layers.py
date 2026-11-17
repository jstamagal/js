"""The config layers over `defaults/jsrc`, and their collection
(`collect_settings`).

A config file is a *script*: each non-comment line is a command (see
`js.setcmd`). The conventional filenames follow the `rc` lineage (`.ircrc`,
`bitchtearc`): global `jsrc`, project `.js/jsrc`, local `.js/jsrc.local`.
There is no TOML — `js --migrate-config` converts a legacy `config.toml` once.

Precedence, lowest to highest:
    defaults/jsrc < ~/.js/jsrc < project .js/jsrc
        < project .js/jsrc.local < env vars < --extra CLI flag
"""

from __future__ import annotations

import copy
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import (
    fset,
    messages as msgs,
    output,
    paths as _paths,
    setcmd,  # lazy: setcmd imports this module
)
from .setting_values import coerce_value, from_line_literal
from .settings import (
    REGISTRY,
    SettingSpec,
    child_key,
    package_settings,
    parent_spec,
    seed_defaults,
    set_dotted,
    spec_for,
)

# ---------------------------------------------------------------------------
# CLI --extra one-shots
# ---------------------------------------------------------------------------

def _parse_dotted_key(key: str) -> tuple[str, ...]:
    parts = tuple(p for p in key.split(".") if p)
    if not parts:
        raise ValueError(msgs.SETTING_EMPTY_KEY.format(key=key))
    return parts


def coerce_extra_value(raw: str) -> Any:
    """Coerce a CLI ``--extra KEY=VALUE`` right-hand side: int, then float, then
    bool/null tokens, else string."""
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    lowered = raw.strip().lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "none"}:
        return None
    return raw


def parse_extra_arg(arg: str) -> tuple[tuple[str, ...], Any]:
    """Parse one ``--extra KEY=VALUE`` argument into (path, value)."""
    if "=" not in arg:
        raise ValueError(msgs.EXTRA_NOT_KEY_VALUE.said(arg=arg))
    raw_key, raw_value = arg.split("=", 1)
    key = raw_key.strip()
    if not key:
        raise ValueError(msgs.EXTRA_EMPTY_KEY.said(arg=arg))
    if raw_value == "":
        raise ValueError(msgs.EXTRA_EMPTY_VALUE.said(arg=arg))
    spec = spec_for(key)
    if spec is not None:
        value, error = coerce_value(spec, raw_value)
        if error is not None:
            raise ValueError(msgs.EXTRA_BAD_VALUE.said(key=spec.key, error=error))
        return spec.path, value
    key = child_key(key)
    path = _parse_dotted_key(key)
    prefix_spec = parent_spec(key)
    if prefix_spec is None or prefix_spec.type != "map":
        raise ValueError(msgs.EXTRA_UNKNOWN_SETTING.said(key=key))
    return path, coerce_extra_value(raw_value)


def apply_cli_extras(settings: dict, extras: list[str]) -> dict:
    for arg in extras:
        path, value = parse_extra_arg(arg)
        set_dotted(settings, path, value)
    return settings


# ---------------------------------------------------------------------------
# Env layer
# ---------------------------------------------------------------------------

def canonical_env_name(key: str) -> str:
    """The env var for a setting: ``NAME`` -> ``JS_NAME`` (a dotted display
    setting ``a.b`` -> ``JS_A_B``)."""
    return "JS_" + key.upper().replace(".", "_")


def env_names_for(spec: SettingSpec) -> tuple[str, ...]:
    """Env vars that feed ``spec``, highest precedence first: ``JS_<KEY>``,
    then ``spec.env``, if any."""
    canon = canonical_env_name(spec.key)
    if spec.env and spec.env != canon:
        return (canon, spec.env)
    return (canon,)


def apply_env_overrides(settings: dict, env: dict[str, str] | None = None) -> dict:
    """Overlay env vars onto ``settings``: each setting's ``JS_<KEY>``, or
    its ``spec.env`` name when ``JS_<KEY>`` is not set."""
    source = env if env is not None else os.environ
    endpoint = _env_endpoint(source)
    for spec in REGISTRY:
        if endpoint is not None and spec.key in endpoint:
            if endpoint[spec.key] is not _SKIP_ENV:
                set_dotted(settings, spec.path, endpoint[spec.key])
            continue
        missing = object()
        value = env_value(spec, source, missing, warn=True)
        if value is not missing:
            set_dotted(settings, spec.path, value)
    return settings


_SKIP_ENV = object()


def _env_endpoint(env: Mapping[str, str]) -> dict[str, Any] | None:
    """MODEL, PROVIDER, BASE_URL and API_KEY from a ``JS_BASE_URL`` written in
    the ``-u`` form; None for a plain base URL, or one js cannot read (named)."""
    from . import endpoint_uri  # lazy: endpoint_uri imports this module

    try:
        spec = endpoint_uri.from_env(env)
    except endpoint_uri.EndpointSpecError as exc:
        output.notice(msgs.ENV_SETTING_IGNORED, name="JS_BASE_URL", error=exc)
        return {"BASE_URL": _SKIP_ENV}
    if spec is None:
        return None
    return {"MODEL": spec.model, "PROVIDER": spec.provider_id,
            "BASE_URL": spec.base_url, "API_KEY": spec.api_key}


def env_value(spec: SettingSpec, env: Mapping[str, str], missing: Any = None, *, warn: bool = False) -> Any:
    """The value the environment gives ``spec``: its first env name, JS_<KEY>
    then the older one, that holds a value `set` takes. An invalid one is
    passed over (and named, with ``warn``) and the next name is tried.
    ``missing`` when none gives one."""
    for name in env_names_for(spec):
        if name not in env:
            continue
        value, error = coerce_value(spec, env[name])
        if error is not None:
            # garbage in the env: skip rather than clobber a working value,
            # but say so — a silently dropped JS_BASE_URL costs an evening
            if warn:
                output.notice(msgs.ENV_SETTING_IGNORED, name=name, error=error)
            continue
        return value
    return missing


# ---------------------------------------------------------------------------
# Collect: defaults/jsrc < jsrc files < env < CLI extras
# ---------------------------------------------------------------------------


# A line of a jsrc file the settings layer does not own: (file, line number, text).
JsrcCommand = tuple[Path, int, str]


def apply_jsrc(path: Path, settings: dict, stack: list[Path], warnings: list[str],
                lines: list[str] | None = None, commands: list[JsrcCommand] | None = None) -> str | None:
    """Apply the settings lines of the jsrc at ``path`` (or ``lines``, read
    as that file) onto ``settings``, the files it loads with it, in order. A
    loaded file stops at its first bad setting, as `/load` stops it, and so
    does every loaded file containing it; the top-level jsrc goes on. Every
    other line is the REPL's, which runs the whole file again in one pass
    (`repl_commands.run_rc_commands`); ``commands`` collects those lines.
    Returns why a loaded file stopped, or None."""

    if lines is None:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            return msgs.LOAD_UNREADABLE.said(path=path, error=f"{type(exc).__name__}: {exc}")
    loaded = bool(stack)
    stack.append(path)
    try:
        for lineno, raw in enumerate(lines, 1):
            # $NAMEs expand once, before the line is read as any command.
            raw = setcmd.expand_line(raw, settings)
            parsed = setcmd.split_command(raw)
            if parsed is not None and parsed[0] == "fset":
                name, value = setcmd.first_word(parsed[1])
                if name.upper() == "THEME" and value:
                    from . import fset

                    theme = from_line_literal(value)
                    if theme and fset.theme_path(theme) is None:
                        raise FileNotFoundError(f"{path}:{lineno}: {fset.theme_error(theme)}")
            error = None
            if parsed is not None and parsed[0] in setcmd.LOAD_VERBS:
                # A loaded file's lines sit in the jsrc layer too, resolved
                # against the containing file, at the point the load stands.
                target, error = setcmd.load_path(parsed[1], path.parent)
                if error is None and len(stack) >= setcmd.MAX_LOAD_DEPTH:
                    error = msgs.LOAD_TOO_DEEP.said()
                elif error is None and target in stack:
                    error = msgs.LOAD_CYCLE.said(path=target)
                elif error is None and not target.is_file():
                    error = msgs.LOAD_NOT_FOUND.said(path=target)
                if error is None:
                    error = apply_jsrc(target, settings, stack, warnings, commands=commands)
            else:
                result = setcmd.apply_config_line(settings, raw, baseline=package_settings())
                if result.handled and result.error:
                    error = msgs.SCRIPT_LINE_FAILED.said(path=path, lineno=lineno, error=result.error)
                    warnings.append(error)
                elif not result.handled and commands is not None:
                    commands.append((path, lineno, raw))
            if loaded and error is not None:
                return error
    finally:
        stack.pop()
    return None


def load_jsrc_files(paths: list[Path], settings: dict, commands: list[JsrcCommand] | None = None) -> list[str]:
    """Apply each existing jsrc script's settings onto ``settings`` in order;
    ``commands`` collects every other line. Returns human-readable warnings
    for bad lines. A named missing theme raises FileNotFoundError with the
    directories searched."""
    warnings: list[str] = []
    for path in paths:
        if path.exists():
            apply_jsrc(path.resolve(strict=False), settings, [], warnings, commands=commands)
    return warnings


def jsrc_commands(paths: list[Path]) -> list[JsrcCommand]:
    """The lines of the jsrc files ``paths`` the settings layer does not own
    (`on`, `alias`, any other command), in the order config load meets them
    inside the files it loads. The /fset entries in force stay as they are."""

    commands: list[JsrcCommand] = []
    with fset.held():
        load_jsrc_files(paths, seed_defaults(), commands)
    return commands


# The layers each list of jsrc files was last collected with, to collect it
# again (`collect_again`) or to lay over a jsrc layer the REPL ran
# (`over_jsrc_layer`).
_COLLECTED: dict[tuple[Path, ...], tuple[dict[str, str] | None, list[str] | None]] = {}


def collect_settings(
    config_paths: list[Path] | None = None,
    env: dict[str, str] | None = None,
    extras: list[str] | None = None,
    *,
    reset_formats=None,
) -> dict:
    """Run precedence: defaults/jsrc < jsrc files (in order) < env < CLI extras.

    ``config_paths`` defaults to ``~/.js/jsrc``. ``js.config.from_env``
    passes the global, project, and project-local files explicitly.
    """
    settings = seed_defaults()

    # The /fset entries the jsrc files set are rebuilt with the settings.
    (fset.reset if reset_formats is None else reset_formats)()

    paths = config_paths if config_paths is not None else [_paths.global_config_file()]
    load_jsrc_files(paths, settings)

    apply_env_overrides(settings, env=env)
    if extras:
        apply_cli_extras(settings, extras)
    _COLLECTED[tuple(paths)] = (env, list(extras) if extras else None)
    return settings


def collect_again(paths: list[Path]) -> dict:
    """The settings ``paths`` give under the env and CLI layers they were last
    collected with (`collect_settings`); the jsrc layer alone when they were
    never collected."""
    if tuple(paths) not in _COLLECTED:
        settings = seed_defaults()
        load_jsrc_files(paths, settings)
        return settings
    env, extras = _COLLECTED[tuple(paths)]
    return collect_settings(paths, env=env, extras=extras)


def over_jsrc_layer(paths: list[Path], layer: dict) -> dict:
    """``layer``, the settings the jsrc files ``paths`` set, under the env and
    CLI layers they were last collected with."""
    settings = copy.deepcopy(layer)
    env, extras = _COLLECTED.get(tuple(paths), ({}, None))
    apply_env_overrides(settings, env=env)
    if extras:
        apply_cli_extras(settings, extras)
    return settings
