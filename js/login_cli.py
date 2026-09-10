"""Plain terminal login/logout CLI for js providers."""

from __future__ import annotations

import contextlib
import curses
import os
import sys
from getpass import getpass
from dataclasses import replace

import ai

from . import codex_auth, colors as C, model_client, providers, xai_auth
from .logins import (
    Login,
    LoginsCorruptError,
    cache_models,
    load_logins,
    load_model_cache,
    remove_login,
    save_login,
    test_login,
    test_login_with_metadata,
)

_API_SHAPES: list[tuple[str, str, str]] = [
    ("openai-completions", "openai", "OpenAI-compatible chat completions"),
    ("openai-responses", "openai", "OpenAI Responses-style endpoint"),
    ("anthropic-custom", "anthropic", "Anthropic-compatible endpoint"),
    ("cliproxyapi", "openai", "CLIProxyAPI / OpenAI-compatible proxy with optional headers"),
]
_API_SHAPE_IDS = {shape_id for shape_id, _sdk, _desc in _API_SHAPES}
_SECONDARY_TEST_PROMPT = "1+1="
_MODEL_LIST_LIMIT = 20


def _mask(value: str) -> str:
    # Revealing an 8-char prefix + 4-char suffix only hides something when the
    # string is long enough that the two slices can't cover the whole value
    # (8 + 4 = 12) — otherwise every character comes through around the fake
    # asterisks, which looks redacted but isn't.
    if len(value) <= 12:
        return "*" * len(value)
    return f"{value[:8]}*******{value[-4:]}"


def _use_terminal_colors() -> None:
    """Keep the terminal's own background instead of painting curses black.

    ``curses.wrapper`` calls ``start_color()``, and that binds color pair 0 to
    white-on-black — so every cleared line gets filled with black no matter what
    theme the terminal is running. ``use_default_colors()`` remaps the default
    fg/bg back to "whatever the terminal already had", which is what makes the
    menu sit in the surrounding colors rather than on a black rectangle.
    """
    with contextlib.suppress(curses.error):
        curses.use_default_colors()


def _curses_picker(
    stdscr: curses.window,
    rows: list[tuple[str, str]],
    title: str,
    *,
    preselected: set[int] | None = None,
    details: list[list[str]] | None = None,
) -> list[int] | None:
    """Searchable chooser/checklist; selections always use original row indices.

    / edits the filter, enter returns to navigation, escape clears an in-progress
    search. Checklist all/none act on matching rows, preserving hidden choices.
    """
    _use_terminal_colors()
    curses.curs_set(0)
    stdscr.keypad(True)
    multiple = preselected is not None
    selected = set(preselected or ())
    idx = 0
    query = ""
    searching = False
    while True:
        matches = [i for i, row in enumerate(rows) if query.casefold() in " ".join(row).casefold()]
        idx = max(0, min(idx, len(matches) - 1))
        stdscr.clear()
        h, w = stdscr.getmaxyx()

        def draw(y: int, text: str) -> None:
            if 0 <= y < h:
                with contextlib.suppress(curses.error):
                    stdscr.addstr(y, 0, text[:max(0, w - 1)])

        draw(0, title)
        draw(1, f"/{query}" if query or searching else "-" * min(w - 1, len(title)))
        detail = details[matches[idx]] if details and matches else []
        detail = detail[:max(0, h - 7)]
        page = max(1, h - 4 - len(detail) - int(multiple))
        start = max(0, idx - page + 1)
        for off, original in enumerate(matches[start:start + page]):
            label, annotation = rows[original]
            cursor = ">" if start + off == idx else " "
            box = ("[x] " if original in selected else "[ ] ") if multiple else ""
            tag = f"  ({annotation})" if annotation else ""
            draw(off + 2, f"{cursor} {box}{label}{tag}")
        if not matches:
            draw(2, "No matches")
        for off, line in enumerate(detail):
            draw(h - 2 - len(detail) + off, line)
        if multiple:
            draw(h - 2, f"{len(selected)}/{len(rows)} selected")
        help_text = "↑↓/jk move  pgup/pgdn page  / search  enter select  q/esc back"
        if multiple:
            help_text = "↑↓/jk move  / search  space toggle  a all  n none  enter save  q back"
        draw(h - 1, "Search: type to filter, enter to navigate, esc clear" if searching else help_text)
        stdscr.refresh()
        try:
            key = stdscr.getch()
        except KeyboardInterrupt:
            return None
        if searching:
            if key in (10, 13, curses.KEY_ENTER):
                searching = False
            elif key == 27:
                query = ""
                searching = False
            elif key == 3:
                return None
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                query = query[:-1]
            elif 32 <= key < 256 and chr(key).isprintable():
                query += chr(key)
            idx = 0
            continue
        if key == ord("/"):
            searching = True
            query = ""
        elif key in (curses.KEY_UP, ord("k")):
            idx = max(0, idx - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            idx = min(len(matches) - 1, idx + 1)
        elif key == curses.KEY_PPAGE:
            idx = max(0, idx - page)
        elif key == curses.KEY_NPAGE:
            idx = min(len(matches) - 1, idx + page)
        elif key == curses.KEY_HOME:
            idx = 0
        elif key == curses.KEY_END:
            idx = len(matches) - 1
        elif multiple and key == ord(" ") and matches:
            original = matches[idx]
            selected.discard(original) if original in selected else selected.add(original)
        elif multiple and key in (ord("a"), ord("A")):
            selected.update(matches)
        elif multiple and key in (ord("n"), ord("N")):
            selected.difference_update(matches)
        elif key in (10, 13, curses.KEY_ENTER):
            if multiple:
                return sorted(selected)
            if matches:
                return [matches[idx]]
        elif key in (ord("q"), 27, 3):
            return None


def _curses_menu(
    stdscr: curses.window, items: list[str], title: str, *, details: list[list[str]] | None = None,
) -> int | None:
    chosen = _curses_picker(stdscr, [(item, "") for item in items], title, details=details)
    return chosen[0] if chosen else None


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
    """Spacebar checklist with / filtering; None means cancel."""
    if not rows:
        return []
    return _curses_picker(stdscr, rows, title, preselected=preselected)


def _select_models_to_cache(
    provider_id: str,
    models: list[str],
    *,
    annotate_dialects: bool = True,
) -> list[str] | None:
    """Curate which fetched models to cache. None == user cancelled.

    Interactive: a spacebar checklist (freshly fetched models preselected, stale
    cached-only rows visible but unselected) plus a free-text line to add ids the
    endpoint omitted. Non-interactive (piped/no TTY): keep every fetched model,
    the prior behavior. Cached-model editing disables dialect annotation so it
    never refreshes the models.dev catalog over the network.
    """
    if not models:
        return models
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return models
    dialects = _dialect_map(provider_id) if annotate_dialects else {}
    cache = load_model_cache()
    cached = cache.get(provider_id, [])
    options = list(dict.fromkeys([*models, *cached]))
    rows = [(model_id, dialects.get(model_id, "")) for model_id in options]
    fetched = set(models)
    preselected = {idx for idx, model_id in enumerate(options) if model_id in fetched}
    title = f"select models to keep for {provider_id}  (cached for /model + --list-models)"
    sys.stdout.flush()
    chosen = curses.wrapper(_curses_multiselect, rows, title, preselected=preselected)
    if chosen is None:
        return None
    selected = [options[i] for i in chosen]
    extra = _input("add model ids the list missed (comma-separated, enter to skip)", default="")
    for raw in (extra or "").split(","):
        model_id = raw.strip()
        if model_id and model_id not in selected:
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
        # openai-completions / openai-responses live under <add custom provider>;
        # a top-level row is a duplicate path to the same shape.
        if provider.id in ("openai-completions", "openai-responses"):
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
    items = ["<add custom provider>", *[f"{pid:<28} {name} [{source}]" for pid, name, source in rows]]
    sys.stdout.flush()
    idx = curses.wrapper(_curses_menu, items, "select provider")
    if idx is None:
        return None
    if idx == 0:
        return "__custom__"
    return rows[idx - 1][0]


def _select_api_shape() -> tuple[str, str] | None:
    items = [f"{pid}  {desc}" for pid, _sdk, desc in _API_SHAPES]
    sys.stdout.flush()
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


def _ask_custom_provider() -> tuple[str, str, str, providers.ProviderDef] | None:
    provider_id = _input("custom provider id")
    if not provider_id:
        return None
    selected = _select_api_shape()
    if selected is None:
        return None
    shape_id, sdk_id = selected
    provider = providers.provider_for_login(shape_id)
    return provider_id, sdk_id, shape_id, provider


def _env_key_name(provider: providers.ProviderDef, env: dict[str, str]) -> str | None:
    for env_name in provider.api_key_env:
        if env.get(env_name):
            return env_name
    return None


def _collect_api_login(
    provider_id: str,
    sdk_provider_id: str | None,
    provider: providers.ProviderDef,
    *,
    shape_provider_id: str | None = None,
) -> Login | None:
    existing = load_logins().get(providers.normalize_provider_id(provider_id) or provider_id)
    env = os.environ
    headers: dict[str, str] = dict(existing.provider_headers) if existing else {}

    env_key_name = _env_key_name(provider, env)
    env_key = env.get(env_key_name) if env_key_name else None
    env_base_url = providers.first_env(provider.base_url_env, env)
    env_model = providers.first_env(provider.model_env, env)

    if env_model:
        print(f"*** Preferred model from env: {env_model}")

    base_url = env_base_url or (existing.provider_base_url if existing else None) or provider.default_base_url
    # Never auto-take an env key: the operator may keep decoys there, or want a
    # different one. Env keys are OFFERED below, never silently used.
    api_key = (existing.provider_api_key if existing else None) or provider.default_api_key
    effective_sdk = (existing.sdk_provider_id if existing and existing.sdk_provider_id else None) or sdk_provider_id or provider.effective_sdk_provider_id
    effective_shape = (
        shape_provider_id
        or (existing.shape_provider_id if existing and existing.shape_provider_id else None)
        or (provider.id if provider.id in _API_SHAPE_IDS else None)
    )

    entered_base_url = _input("Base URL", default=base_url or "")
    if entered_base_url is None:
        return None
    base_url = entered_base_url or None

    if env_key_name and env_key:
        answer = _input(f"Found ENV:{env_key_name} ({_mask(env_key)}); use it? [y/N]", default="n")
        if answer is None:
            return None
        if answer.strip().lower() in {"y", "yes"}:
            api_key = env_key

    keyless_ok = provider.local or not provider.established
    if provider.requires_api_key and not api_key:
        prompt = "Enter API Key (enter for none)" if keyless_ok else "Enter API Key"
        api_key = _input(prompt, secret=True)
        if not api_key:
            if not keyless_ok:
                print("login aborted: no API key given", file=sys.stderr)
                return None
            # Keyless local endpoint: the openai wire still demands SOME token.
            api_key = "x"
            print("*** No key given; storing placeholder 'x' (local endpoints ignore it)")
    elif api_key and existing is not None and api_key == existing.provider_api_key:
        entered = _input("Enter API Key [enter = keep saved]", secret=True)
        if entered:
            api_key = entered

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
        shape_provider_id=effective_shape,
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

def _run_secondary_test(login: Login, provider: providers.ProviderDef, model_id: str) -> bool:
    # Seeing a real answer back IS the confirmation; a further "hit enter to
    # add" after that just re-asked the same question the model number already
    # answered, so it's gone — a bad answer or an exception is still visible
    # right here, and Ctrl-C still works anywhere above this point.
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
    return True

def _post_fetch_confirmation(login: Login, provider: providers.ProviderDef, models: list[str]) -> bool | None:
    _display_models(models)
    if provider.models_list_validates_auth:
        # Listing already proved the credentials work — asking "add without a
        # test, or verify first?" here is a prompt with only one sane answer.
        return True
    choice = _secondary_test_choice(models, require_test=True)
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
    try:
        save_login(login)
        cache_models(login.provider_id, to_cache)
    except LoginsCorruptError as exc:
        print(f"{C.ORANGE}login not saved: {exc}{C.RESET}", file=sys.stderr)
        return 1
    who = f" ({login.codex_email})" if login.codex_email else ""
    print(f"{C.GREEN}*** Provider added: {login.provider_id}{who}{C.RESET}")
    print(f"cached {len(to_cache)} models")
    return 0


def _run_xai_login() -> int:
    try:
        login = xai_auth.login_browser()
        print("*** Fetching models...")
        models = test_login(login)
    except Exception as exc:  # noqa: BLE001
        print(f"{C.ORANGE}login failed: {type(exc).__name__}: {exc}{C.RESET}", file=sys.stderr)
        return 1

    to_cache = _select_models_to_cache(login.provider_id, models)
    if to_cache is None:
        return 0
    try:
        save_login(login)
        cache_models(login.provider_id, to_cache)
    except LoginsCorruptError as exc:
        print(f"{C.ORANGE}login not saved: {exc}{C.RESET}", file=sys.stderr)
        return 1
    who = f" ({login.xai_email})" if login.xai_email else ""
    print(f"{C.GREEN}*** Provider added: {login.provider_id}{who}{C.RESET}")
    print(f"cached {len(to_cache)} models")
    return 0


def _provider_details(login: Login, model_count: int) -> list[str]:
    provider = providers.get_provider(login.provider_id)
    return [
        f"{login.provider_id} — {provider.display_name if provider else login.provider_id} [saved]",
        f"Base URL: {login.provider_base_url or '(default)'}",
        f"API key: {_mask(login.provider_api_key) if login.provider_api_key else '(none)'}",
        f"{model_count} cached models",
        "Headers: " + (", ".join(f"{k}={_mask(v)}" for k, v in login.provider_headers.items()) or "(none)"),
    ]


def _edit_saved_provider(login: Login) -> None:
    """Save local edits without fetching models or changing OAuth metadata."""
    base = _input("Base URL (enter keeps saved; - clears)", default=login.provider_base_url or "")
    if base is None:
        return
    key = _input("API key (enter keeps saved; - clears)", default="", secret=True)
    if key is None:
        return
    while True:
        headers = _input("Headers k=v,k=v (enter keeps saved; - clears)", default="", secret=True)
        if headers is None:
            return
        if not headers or headers == "-":
            parsed = {} if headers == "-" else login.provider_headers
            break
        parts = [part.strip() for part in headers.split(",")]
        if all("=" in part and part.split("=", 1)[0].strip() for part in parts):
            parsed = {k.strip(): v.strip() for k, v in (part.split("=", 1) for part in parts)}
            break
        print("Headers must use k=v,k=v; nothing saved yet.")
    save_login(replace(
        login,
        provider_base_url=None if base == "-" else base or None,
        provider_api_key=None if key == "-" else key or login.provider_api_key,
        provider_headers=parsed,
    ))


def _manage_providers() -> int:
    while True:
        saved = load_logins()
        cached = load_model_cache()
        ids = sorted(saved)
        items = ["<add custom provider>"]
        items.extend(_provider_details(saved[pid], len(cached.get(pid, [])))[0] for pid in ids)
        items.append("<add registry provider>")
        details = [[], *[_provider_details(saved[pid], len(cached.get(pid, []))) for pid in ids], []]
        choice = curses.wrapper(_curses_menu, items, "manage providers", details=details)
        if choice is None:
            return 0
        if choice == 0 or choice == len(items) - 1:
            provider_id = "__custom__" if choice == 0 else _select_provider()
            if provider_id is not None:
                _run_login(provider_id)
            continue
        provider_id = ids[choice - 1]
        try:
            _manage_saved_provider(provider_id)
        except LoginsCorruptError as exc:
            print(f"Provider not changed: {exc}", file=sys.stderr)
            return 1


def _manage_models(login: Login) -> None:
    status = ""
    while True:
        cached = load_model_cache().get(login.provider_id, [])
        actions = ["Select / deselect cached models", "Add model ids", "Re-fetch live model list", "Back"]
        title = f"models for {login.provider_id}: {len(cached)} cached"
        choice = curses.wrapper(_curses_menu, actions, f"{title}  {status}")
        status = ""
        if choice is None or choice == 3:
            return
        if choice == 0:
            if not cached:
                status = "Cache empty; add ids or re-fetch first."
            else:
                _run_models_edit(login.provider_id)
        elif choice == 1:
            extra = _input("Add model ids (comma-separated)", default="")
            if extra:
                added = [model.strip() for model in extra.split(",") if model.strip()]
                cache_models(login.provider_id, list(dict.fromkeys([*cached, *added])))
        elif choice == 2:
            print("*** Fetching models...")
            try:
                models, metadata = test_login_with_metadata(login)
            except Exception as exc:  # noqa: BLE001 - keep the existing cache on a failed fetch
                status = f"Fetch failed ({type(exc).__name__}); cache unchanged."
                continue
            curated = _select_models_to_cache(login.provider_id, models, annotate_dialects=False)
            if curated is not None:
                cache_models(login.provider_id, curated, metadata=metadata)


def _manage_saved_provider(provider_id: str) -> None:
    while (login := load_logins().get(provider_id)) is not None:
        details = _provider_details(login, len(load_model_cache().get(provider_id, [])))
        actions = ["Update URL / API key / headers", "Models", "Back", "Remove provider"]
        choice = curses.wrapper(_curses_menu, actions, f"manage {provider_id}", details=[details] * len(actions))
        if choice is None or choice == 2:
            return
        if choice == 0:
            _edit_saved_provider(login)
        elif choice == 1:
            _manage_models(login)
        elif choice == 3:
            _run_logout(provider_id)
            return


def _run_login(provider_id: str | None = None) -> int:
    if provider_id is None:
        return _manage_providers()
    raw_provider_id = provider_id
    provider_id = providers.normalize_provider_id(provider_id) or provider_id

    if provider_id == "__custom__":
        custom = _ask_custom_provider()
        if custom is None:
            return 0
        provider_id, sdk_provider_id, shape_provider_id, provider = custom
    else:
        provider = providers.provider_for_login(provider_id)
        sdk_provider_id = provider.effective_sdk_provider_id
        shape_provider_id = provider.id if provider.id in _API_SHAPE_IDS else None

    if codex_auth.is_codex_provider(provider_id):
        return _run_codex_login(raw_provider_id)
    if xai_auth.is_xai_oauth_provider(provider_id):
        return _run_xai_login()

    login = _collect_api_login(provider_id, sdk_provider_id, provider, shape_provider_id=shape_provider_id)
    if login is None:
        return 0

    print("*** Fetching models...")
    try:
        models, model_metadata = test_login_with_metadata(login)
    except Exception as exc:  # noqa: BLE001
        print(f"{C.ORANGE}login failed: {type(exc).__name__}: {exc}{C.RESET}", file=sys.stderr)
        base = login.provider_base_url or ""
        if "404" in str(exc) and base and not base.rstrip("/").endswith("/v1"):
            print(
                f"{C.ORANGE}hint: {base} has no /v1 suffix — OpenAI-compatible servers "
                f"usually serve at {base.rstrip('/')}/v1{C.RESET}",
                file=sys.stderr,
            )
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
    try:
        save_login(login)
        cache_models(canonical_id, to_cache, metadata=model_metadata)
    except LoginsCorruptError as exc:
        print(f"{C.ORANGE}login not saved: {exc}{C.RESET}", file=sys.stderr)
        return 1
    print(f"{C.GREEN}*** Provider added.{C.RESET}")
    print(f"cached {len(to_cache)} models")
    return 0


def _normalize_cached_provider_id(provider_id: str, cache: dict[str, list[str]]) -> str:
    requested = provider_id.strip()
    lowered = requested.lower()
    for provider in providers.all_providers():
        if lowered == provider.id.lower() or lowered in {alias.lower() for alias in provider.aliases}:
            return provider.id
    for cached_id in cache:
        if lowered == cached_id.lower():
            return cached_id
    return requested


def _run_models_edit(provider_id: str) -> int:
    cache = load_model_cache()
    target = _normalize_cached_provider_id(provider_id, cache)
    cached = cache.get(target)
    if not cached:
        print(f"{C.ORANGE}no cached models for {target}{C.RESET}", file=sys.stderr)
        return 1

    curated = _select_models_to_cache(target, cached, annotate_dialects=False)
    if curated is None:
        print(f"{C.GREY}model cache edit cancelled; {target} unchanged{C.RESET}")
        return 0

    cache_models(target, curated)
    print(f"cached {len(curated)} models for {target}")
    return 0


def _run_logout(provider_id: str) -> int:
    target = providers.normalize_provider_id(provider_id) or provider_id
    try:
        removed = remove_login(target)
    except LoginsCorruptError as exc:
        print(f"{C.ORANGE}logout failed: {exc}{C.RESET}", file=sys.stderr)
        return 1
    if removed:
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
    if args[0] in ("--models-edit", "models-edit"):
        if len(args) < 2:
            print(f"{C.ORANGE}usage: js --models-edit <provider-id>{C.RESET}", file=sys.stderr)
            return 2
        return _run_models_edit(args[1])
    if len(args) == 1:
        return _run_login(args[0])
    print(
        f"{C.ORANGE}usage: js --login [<provider-id>] | js --logout <provider-id> | "
        f"js --models-edit <provider-id>{C.RESET}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
