"""Plain terminal login/logout CLI for js providers."""

from __future__ import annotations

import curses
import os
import sys
from getpass import getpass

import ai

from . import codex_auth, colors as C, model_client, providers
from .logins import Login, cache_models, load_logins, remove_login, save_login, test_login

_API_SHAPES: list[tuple[str, str, str]] = [
    ("openai-completions", "openai", "OpenAI-compatible chat completions"),
    ("openai-responses", "openai", "OpenAI Responses-style endpoint"),
    ("anthropic-custom", "anthropic", "Anthropic-compatible endpoint"),
    ("cliproxyapi", "openai", "CLIProxyAPI / OpenAI-compatible proxy with optional headers"),
]
_SECONDARY_TEST_PROMPT = "1+1="
_MODEL_LIST_LIMIT = 20


def _mask(value: str) -> str:
    if len(value) <= 10:
        return "*" * len(value)
    return f"{value[:8]}*******{value[-4:]}"


def _curses_menu(stdscr: curses.window, items: list[str], title: str) -> int | None:
    curses.curs_set(0)
    stdscr.keypad(True)
    idx = 0
    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        stdscr.addstr(0, 0, title[: w - 1])
        stdscr.addstr(1, 0, "-" * min(w - 1, max(1, len(title))))
        start = max(0, idx - max(0, h - 6))
        visible = items[start : start + max(1, h - 4)]
        for off, item in enumerate(visible):
            i = start + off
            line = f"> {item}" if i == idx else f"  {item}"
            stdscr.addstr(off + 3, 0, line[: w - 1])
        stdscr.addstr(h - 1, 0, "↑↓/j/k move  enter select  q/esc cancel"[: w - 1])
        stdscr.refresh()
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            idx = max(0, idx - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            idx = min(len(items) - 1, idx + 1)
        elif key in (ord("\n"), ord("\r"), curses.KEY_ENTER):
            return idx
        elif key in (ord("q"), 27, 3):
            return None


_NPM_DIALECT = {
    "@ai-sdk/anthropic": "anthropic",
    "@ai-sdk/google": "google",
}


def _dialect_map(provider_id: str) -> dict[str, str]:
    """Map model id -> wire dialect tag from models.dev ``provider_config.npm``.

    A multi-endpoint gateway (opencode-go) lists its anthropic-endpoint models
    on the openai endpoint too, so the tag tells which models actually belong to
    *this* login's wire. Best-effort: an empty/failed lookup just yields no tags.
    """
    try:
        from . import model_metadata
        import modelsdotdev

        model_metadata.ensure_fresh_catalog()
    except Exception:  # noqa: BLE001 - annotation is cosmetic; never block login
        return {}
    canonical = providers.normalize_provider_id(provider_id) or provider_id
    candidates = {canonical}
    if canonical.startswith("opencode"):
        candidates |= {"opencode", "opencode-go"}
    out: dict[str, str] = {}
    for model in modelsdotdev.iter_models():
        if model.provider_id not in candidates:
            continue
        config = getattr(model, "provider_config", None)
        npm = getattr(config, "npm", None) if config is not None else None
        tag = _NPM_DIALECT.get(npm, "openai") if npm else "openai"
        # Prefer an explicit non-openai dialect if any entry carries one.
        if model.id not in out or (tag != "openai" and out[model.id] == "openai"):
            out[model.id] = tag
    return out


def _curses_multiselect(
    stdscr: curses.window,
    rows: list[tuple[str, str]],
    title: str,
    *,
    preselected: set[int],
) -> list[int] | None:
    """Spacebar checklist. Returns selected indices, or None on cancel.

    ``rows`` are ``(label, annotation)``; annotation is shown dimmed in parens.
    """
    curses.curs_set(0)
    stdscr.keypad(True)
    n = len(rows)
    if n == 0:
        return []
    idx = 0
    selected = set(preselected)
    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        stdscr.addstr(0, 0, title[: w - 1])
        stdscr.addstr(1, 0, "-" * min(w - 1, max(1, len(title))))
        start = max(0, idx - max(0, h - 7))
        visible = rows[start : start + max(1, h - 5)]
        for off, (label, annotation) in enumerate(visible):
            i = start + off
            box = "[x]" if i in selected else "[ ]"
            cursor = ">" if i == idx else " "
            tag = f"  ({annotation})" if annotation else ""
            stdscr.addstr(off + 3, 0, f"{cursor} {box} {label}{tag}"[: w - 1])
        stdscr.addstr(h - 2, 0, f"{len(selected)}/{n} selected"[: w - 1])
        stdscr.addstr(
            h - 1, 0,
            "↑↓/jk move  space toggle  a all  n none  enter confirm  q cancel"[: w - 1],
        )
        stdscr.refresh()
        key = stdscr.getch()
        if key in (curses.KEY_UP, ord("k")):
            idx = max(0, idx - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            idx = min(n - 1, idx + 1)
        elif key == ord(" "):
            selected.discard(idx) if idx in selected else selected.add(idx)
        elif key in (ord("a"), ord("A")):
            selected = set(range(n))
        elif key in (ord("n"), ord("N")):
            selected = set()
        elif key in (ord("\n"), ord("\r"), curses.KEY_ENTER):
            return sorted(selected)
        elif key in (ord("q"), 27, 3):
            return None


def _select_models_to_cache(provider_id: str, models: list[str]) -> list[str] | None:
    """Curate which fetched models to cache. None == user cancelled.

    Interactive: a spacebar checklist (all preselected, so plain Enter keeps the
    lot) plus a free-text line to add ids the endpoint omitted. Non-interactive
    (piped/no TTY): keep every fetched model, the prior behavior.
    """
    if not models:
        return models
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return models
    dialects = _dialect_map(provider_id)
    rows = [(model_id, dialects.get(model_id, "")) for model_id in models]
    title = f"select models to keep for {provider_id}  (cached for /model + --list-models)"
    chosen = curses.wrapper(_curses_multiselect, rows, title, preselected=set(range(len(models))))
    if chosen is None:
        return None
    selected = [models[i] for i in chosen]
    known = set(models)
    extra = _input("add model ids the list missed (comma-separated, enter to skip)", default="")
    for raw in (extra or "").split(","):
        model_id = raw.strip()
        if model_id and model_id not in known and model_id not in selected:
            selected.append(model_id)
    return selected


def _login_provider_rows() -> list[tuple[str, str, str]]:
    saved = load_logins()
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    for provider_id, _login in sorted(saved.items()):
        provider = providers.get_provider(provider_id)
        rows.append((provider_id, provider.display_name if provider else provider_id, "saved"))
        seen.add(provider_id)

    env_rows: list[tuple[str, str, str]] = []
    registry_rows: list[tuple[str, str, str]] = []
    for provider in providers.login_providers():
        if provider.id in seen:
            continue
        env_configured = providers.first_env(provider.api_key_env + provider.base_url_env + provider.model_env) is not None
        target = env_rows if env_configured else registry_rows
        target.append((provider.id, provider.display_name, "env" if env_configured else "registry"))
        seen.add(provider.id)

    env_rows.sort(key=lambda row: row[1].lower())
    registry_rows.sort(key=lambda row: row[1].lower())
    return rows + env_rows + registry_rows


def _select_provider() -> str | None:
    rows = _login_provider_rows()
    items = [f"{pid:<28} {name} [{source}]" for pid, name, source in rows]
    items.append("<add custom provider>")
    idx = curses.wrapper(_curses_menu, items, "select provider")
    if idx is None:
        return None
    if idx == len(items) - 1:
        return "__custom__"
    return rows[idx][0]


def _select_api_shape() -> tuple[str, str] | None:
    items = [f"{pid}  {desc}" for pid, _sdk, desc in _API_SHAPES]
    idx = curses.wrapper(_curses_menu, items, "select API shape")
    if idx is None:
        return None
    pid, sdk, _desc = _API_SHAPES[idx]
    return pid, sdk


def _input(prompt: str, *, default: str | None = None, secret: bool = False) -> str | None:
    label = prompt if default in (None, "") else f"{prompt} [{default}]"
    try:
        value = getpass(f"{label}: ") if secret else input(f"{label}: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    stripped = value.strip()
    if stripped:
        return stripped
    return default


def _ask_custom_provider() -> tuple[str, str, providers.ProviderDef] | None:
    provider_id = _input("custom provider id")
    if not provider_id:
        return None
    selected = _select_api_shape()
    if selected is None:
        return None
    shape_id, sdk_id = selected
    provider = providers.provider_for_login(shape_id)
    return provider_id, sdk_id, provider


def _env_key_name(provider: providers.ProviderDef, env: dict[str, str]) -> str | None:
    for env_name in provider.api_key_env:
        if env.get(env_name):
            return env_name
    return None


def _collect_api_login(provider_id: str, sdk_provider_id: str | None, provider: providers.ProviderDef) -> Login | None:
    existing = load_logins().get(providers.normalize_provider_id(provider_id) or provider_id)
    env = os.environ
    headers: dict[str, str] = dict(existing.provider_headers) if existing else {}

    env_key_name = _env_key_name(provider, env)
    env_key = env.get(env_key_name) if env_key_name else None
    env_base_url = providers.first_env(provider.base_url_env, env)
    env_model = providers.first_env(provider.model_env, env)

    if env_key_name and env_key:
        print(f"*** {provider.display_name} API key found in {env_key_name}: {_mask(env_key)}")

    if env_model:
        print(f"*** Preferred model from env: {env_model}")

    base_url = env_base_url or (existing.provider_base_url if existing else None) or provider.default_base_url
    api_key = env_key or (existing.provider_api_key if existing else None) or provider.default_api_key
    effective_sdk = (existing.sdk_provider_id if existing and existing.sdk_provider_id else None) or sdk_provider_id or provider.effective_sdk_provider_id

    if provider.login_base_url_field:
        base_url = _input("Base URL", default=base_url)
        if base_url is None:
            return None

    if provider.requires_api_key and not api_key:
        if provider.api_key_env:
            print(f"*** Did not find existing ENV:{provider.api_key_env[0]}")
        api_key = _input("Enter API Key", secret=True)
        if not api_key:
            return None
    elif existing is not None and existing.provider_api_key and not env_key:
        print(f"*** Using saved login for {provider_id}")

    if provider.transport == "cliproxyapi":
        raw_headers = _input(
            "Headers k=v,k=v (optional)",
            default=",".join(f"{k}={v}" for k, v in headers.items()),
        )
        if raw_headers is None:
            return None
        headers = {}
        if raw_headers:
            for part in raw_headers.split(","):
                if "=" not in part:
                    continue
                key, value = part.split("=", 1)
                if key.strip():
                    headers[key.strip()] = value.strip()

    return Login(
        provider_id=provider_id,
        sdk_provider_id=effective_sdk,
        provider_base_url=base_url or None,
        provider_api_key=api_key or None,
        provider_headers=headers,
    )


def _display_models(models: list[str]) -> None:
    shown = models[:_MODEL_LIST_LIMIT]
    for idx, model_id in enumerate(shown, 1):
        print(f"[{idx}] {model_id}")
    if len(models) > len(shown):
        print(f"... {len(models) - len(shown)} more cached models")


def _secondary_test_choice(models: list[str], *, require_test: bool) -> str | None | bool:
    if not models:
        return True
    # Enter always means "add it, I don't care to test"; a model number means
    # "test that one first". require_test only adds a heads-up that the listing
    # didn't prove the key can generate — it never forces a test.
    if require_test:
        print("*** Model listing alone does not prove these credentials can generate.")
    prompt = "[enter] add without a test, or a model number to verify (q cancel): "
    while True:
        try:
            choice = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if not choice:
            return True
        if choice.lower() in {"q", "quit", "exit"}:
            return False
        if choice.lower() == "t":
            return models[0]
        if choice in models:
            return choice
        if choice.isdigit():
            index = int(choice) - 1
            if 0 <= index < min(len(models), _MODEL_LIST_LIMIT):
                return models[index]
        print("*** enter to add, q to cancel, or a model number / exact model id")

def _run_secondary_test(login: Login, provider: providers.ProviderDef, model_id: str) -> bool | None:
    print(f"*** [user] {_SECONDARY_TEST_PROMPT}")
    chunks: list[str] = []

    def on_text(chunk: str) -> None:
        chunks.append(chunk)

    try:
        result = model_client.stream_model(
            model_id=model_id,
            provider_id=login.provider_id,
            provider_base_url=login.provider_base_url,
            provider_api_key=login.provider_api_key,
            provider_headers=login.provider_headers,
            messages=[ai.user_message(_SECONDARY_TEST_PROMPT)],
            tools=None,
            max_output_tokens=None,
            reasoning_effort=provider.reasoning_effort,
            on_text=on_text,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"{C.ORANGE}*** secondary test failed: {type(exc).__name__}: {exc}{C.RESET}")
        return False

    answer = result.text.strip() or "".join(chunks).strip()
    print(f"*** [assistant] {answer}")
    try:
        confirm = input("*** hit enter to add...[enter] ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return True if confirm == "" else None

def _post_fetch_confirmation(login: Login, provider: providers.ProviderDef, models: list[str]) -> bool | None:
    _display_models(models)
    choice = _secondary_test_choice(
        models,
        require_test=not provider.models_list_validates_auth,
    )
    if choice is True:
        return True
    if choice is False or choice is None:
        return None
    return _run_secondary_test(login, provider, choice)


def _run_codex_login(provider_id: str) -> int:
    try:
        if provider_id == codex_auth.CODEX_DEVICE_PROVIDER_ID:
            login = codex_auth.login_device()
        else:
            login = codex_auth.login_browser()
        print("*** Fetching models...")
        models = test_login(login)
    except Exception as exc:  # noqa: BLE001
        print(f"{C.ORANGE}login failed: {type(exc).__name__}: {exc}{C.RESET}", file=sys.stderr)
        return 1

    to_cache = _select_models_to_cache(login.provider_id, models)
    if to_cache is None:
        return 0
    save_login(login)
    cache_models(login.provider_id, to_cache)
    who = f" ({login.codex_email})" if login.codex_email else ""
    print(f"{C.GREEN}*** Provider added: {login.provider_id}{who}{C.RESET}")
    print(f"cached {len(to_cache)} models")
    return 0


def _run_login(provider_id: str | None = None) -> int:
    if provider_id is None:
        provider_id = _select_provider()
        if provider_id is None:
            return 0
    raw_provider_id = provider_id
    provider_id = providers.normalize_provider_id(provider_id) or provider_id

    if provider_id == "__custom__":
        custom = _ask_custom_provider()
        if custom is None:
            return 0
        provider_id, sdk_provider_id, provider = custom
    else:
        provider = providers.provider_for_login(provider_id)
        sdk_provider_id = provider.effective_sdk_provider_id

    if codex_auth.is_codex_provider(provider_id):
        return _run_codex_login(raw_provider_id)

    login = _collect_api_login(provider_id, sdk_provider_id, provider)
    if login is None:
        return 0

    print("*** Fetching models...")
    try:
        models = test_login(login)
    except Exception as exc:  # noqa: BLE001
        print(f"{C.ORANGE}login failed: {type(exc).__name__}: {exc}{C.RESET}", file=sys.stderr)
        return 1

    confirmed = _post_fetch_confirmation(login, provider, models)
    if confirmed is None:
        return 0
    if confirmed is False:
        return 1

    canonical_id = providers.normalize_provider_id(provider_id) or provider_id
    to_cache = _select_models_to_cache(canonical_id, models)
    if to_cache is None:
        return 0
    save_login(login)
    cache_models(canonical_id, to_cache)
    print(f"{C.GREEN}*** Provider added.{C.RESET}")
    print(f"cached {len(to_cache)} models")
    return 0


def _run_logout(provider_id: str) -> int:
    target = providers.normalize_provider_id(provider_id) or provider_id
    if remove_login(target):
        print(f"{C.GREY}logged out of {target}{C.RESET}")
        return 0
    print(f"{C.ORANGE}not logged in to {target}{C.RESET}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        return _run_login(None)
    if args[0] in ("--login", "login"):
        provider_id = args[1] if len(args) > 1 else None
        return _run_login(provider_id)
    if args[0] in ("--logout", "logout"):
        if len(args) < 2:
            print(f"{C.ORANGE}usage: js --logout <provider-id>{C.RESET}", file=sys.stderr)
            return 2
        return _run_logout(args[1])
    if len(args) == 1:
        return _run_login(args[0])
    print(f"{C.ORANGE}usage: js --login [<provider-id>] | js --logout <provider-id>{C.RESET}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
