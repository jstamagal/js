"""What `just install` asks the operator: the keys js uses and the default model.

Each key in `KEYS` that is in neither the environment nor ~/.js/.env is asked
for in the terminal; an answer is appended to ~/.js/.env, which is kept at mode
600, and Enter skips the key. When the `model.id` js starts with has no
provider to run on, the operator picks a saved login and one of its models, or
adds a provider through the login flow, and the pick becomes `model.id` in
~/.js/jsrc. A rerun asks only about what is still missing. Without a terminal
nothing is asked: the missing keys and an unrouted model are named instead.

    uv run python -m js.install
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping, MutableMapping
from getpass import getpass
from pathlib import Path
from typing import Any

from . import dotenv, home, logins, paths, providers, routing
from . import messages as msgs
from . import settings as settings_mod

# The keys js reads from the environment, and what each one is for.
KEYS: tuple[tuple[str, str], ...] = (
    ("TYPESAFE_API_KEY", "session tags"),
    ("TAVILY_API_KEY", "tavily_search"),
    ("EXA_API_KEY", "exa_search"),
    ("SERPER_API_KEY", "serper_search"),
    ("CONTEXT7_API_KEY", "docs_search, which also runs without one at a lower rate limit"),
)

Ask = Callable[[str], "str | None"]


def missing_keys(environ: Mapping[str, str], env_file: Path) -> list[tuple[str, str]]:
    """The `KEYS` entries set neither in ``environ`` nor in ``env_file``."""
    try:
        saved = dotenv.parse(env_file.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        saved = {}
    return [(name, use) for name, use in KEYS
            if not environ.get(name, "").strip() and not saved.get(name, "").strip()]


def _env_value(value: str) -> str:
    """``value`` as `dotenv.parse` reads it back: quoted when bare text would
    lose a space, an inline comment or its quotes."""
    if value and not any(ch.isspace() or ch in "#'\"" for ch in value):
        return value
    return f'"{value}"' if '"' not in value else f"'{value}'"


def save_key(env_file: Path, name: str, value: str) -> None:
    """Append `name=value` to ``env_file``, creating it, and leave it mode 600."""
    env_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        before = env_file.read_bytes()
    except FileNotFoundError:
        before = b""
    fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        if before and not before.endswith(b"\n"):
            handle.write("\n")
        handle.write(f"{name}={_env_value(value)}\n")


def ask_keys(environ: Mapping[str, str], env_file: Path, ask: Ask) -> list[str]:
    """Ask for each missing key and save the answers. An empty answer skips a
    key; None, the terminal closing, stops asking. Returns the names saved."""
    saved: list[str] = []
    for name, use in missing_keys(environ, env_file):
        answer = ask(msgs.INSTALL_ASK_KEY.text(name=name, use=use))
        if answer is None:
            break
        value = answer.strip()
        if not value:
            continue
        save_key(env_file, name, value)
        msgs.say(msgs.INSTALL_KEY_SAVED, name=name, path=env_file)
        saved.append(name)
    return saved


def default_model(environ: Mapping[str, str]) -> tuple[str | None, bool]:
    """The `model.id` js starts with outside a project (~/.js/jsrc under the
    environment), and whether it routes to a provider."""
    settings = settings_mod.collect_settings([paths.global_config_file()], env=dict(environ))

    def knob(key: str) -> Any:
        return settings_mod.knob(settings, key)

    model_id = knob("model.id")
    if not model_id:
        return None, False
    try:
        route = routing.resolve_model_route(
            str(model_id),
            configured_provider_id=knob("provider.id"),
            configured_base_url=knob("provider.base_url"),
            configured_api_key=knob("provider.api_key"),
            env=environ,
        )
    except routing.ProviderNotLoggedInError:
        return str(model_id), False
    return str(model_id), bool(route.provider_id or route.base_url)


def _say_unrouted(model_id: str | None) -> None:
    if model_id:
        msgs.say(msgs.INSTALL_MODEL_UNROUTED, model=model_id)
    else:
        msgs.say(msgs.INSTALL_MODEL_UNSET)


def ask_model(environ: Mapping[str, str], ask: Ask, *, pick: Callable[[], dict | None],
              add_provider: Callable[[], Any]) -> str | None:
    """When the default model does not route: offer the saved logins to pick
    a model from, and the login flow to add a provider, until a model is
    picked or the operator skips. Returns the `model.id` saved, or None."""
    model_id, routes = default_model(environ)
    if routes:
        return None
    _say_unrouted(model_id)
    while True:
        saved = logins.load_logins()
        if saved:
            cache = logins.load_model_cache()
            rows = ", ".join(msgs.INSTALL_LOGIN_ROW.text(provider=provider_id,
                                                         models=msgs.plural(len(cache.get(provider_id, [])), "model"))
                             for provider_id in sorted(saved))
            msgs.say(msgs.INSTALL_LOGINS, logins=rows)
            answer = ask(msgs.INSTALL_ASK_MODEL.text())
        else:
            answer = ask(msgs.INSTALL_ASK_PROVIDER.text())
        choice = (answer or "").strip().lower()
        if not choice:
            return None
        if choice == "a":
            add_provider()
        elif choice == "p" and saved:
            selected = pick()
            if selected:
                chosen = providers.qualified_model_id(selected.get("provider_id"), selected["model"])
                settings_mod.write_model_id(paths.global_config_file(), chosen)
                msgs.say(msgs.MODEL_SAVED_AS_DEFAULT, model=chosen)
                return chosen


def _asker(read: Callable[[str], str]) -> Ask:
    def ask(prompt: str) -> str | None:
        try:
            return read(f"{prompt}: ")
        except (EOFError, KeyboardInterrupt):
            print()
            return None
    return ask


def _pick() -> dict | None:
    from . import picker

    return picker.pick_model()


def _add_provider() -> None:
    from . import login_cli

    login_cli.main([])


def main(argv: list[str] | None = None, environ: MutableMapping[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    home.migrate_once()
    paths.ensure_home()
    settings_mod.ensure_user_jsrc(paths.global_config_file())
    env_file = paths.global_env_file()
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        missing = missing_keys(env, env_file)
        if missing:
            msgs.say(msgs.INSTALL_KEYS_MISSING, names=", ".join(name for name, _use in missing), path=env_file)
        model_id, routes = default_model(env)
        if not routes:
            _say_unrouted(model_id)
        return 0
    ask_keys(env, env_file, _asker(getpass))
    ask_model(env, _asker(input), pick=_pick, add_provider=_add_provider)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
