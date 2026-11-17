"""Plain terminal login/logout CLI for js providers."""

from __future__ import annotations

import curses
import os
import re
import sys
from collections.abc import Callable
from dataclasses import replace
from getpass import getpass
from types import SimpleNamespace

import ai

from . import (
    config,
    curses_menu,
    login_defaults,
    messages as msgs,
    model_client,
    paranoid,
    providers,
    retry,
    turn_settings,
)
from .logins import (
    Login,
    LoginsCorruptError,
    cache_models,
    load_logins,
    load_model_cache,
    remove_login,
    rename_login,
    save_login,
    test_login_with_metadata,
    update_login,
)

_API_SHAPES: list[tuple[str, str, str]] = [
    ("openai-completions", "openai", msgs.LOGIN_SHAPE_CHAT_COMPLETIONS.format()),
    ("openai-responses", "openai", msgs.LOGIN_SHAPE_RESPONSES.format()),
    ("anthropic-custom", "anthropic", msgs.LOGIN_SHAPE_ANTHROPIC.format()),
    ("cliproxyapi", "openai", msgs.LOGIN_SHAPE_PROXY.format()),
]
_API_SHAPE_IDS = {shape_id for shape_id, _sdk, _desc in _API_SHAPES}
_SECONDARY_TEST_PROMPT = '1+1='
_MODEL_LIST_LIMIT = 20


def _mask(value: str) -> str:
    # PARANOID_MODE off shows a key in full.
    if not paranoid.masks(paranoid.mode()):
        return value
    # Revealing an 8-char prefix + 4-char suffix only hides something when the
    # string is long enough that the two slices can't cover the whole value
    # (8 + 4 = 12) — otherwise every character comes through around the fake
    # asterisks, which looks redacted but isn't.
    if len(value) <= 12:
        return "*" * len(value)
    return f"{value[:8]}*******{value[-4:]}"


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
        import modelsdotdev

        from . import model_metadata

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


def _select_models_to_cache(
    provider_id: str,
    models: list[str],
    *,
    annotate_dialects: bool = True,
    saved: Login | None = None,
    pending: list[Login] | None = None,
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
    if not _interactive():
        return models
    dialects = _dialect_map(provider_id) if annotate_dialects else {}
    cache = load_model_cache()
    cached = cache.get(provider_id, [])
    options = list(dict.fromkeys([*models, *cached]))
    rows = [(model_id, _model_annotation(saved, model_id, dialects.get(model_id, ""))) for model_id in options]
    fetched = set(models)
    preselected = {idx for idx, model_id in enumerate(options) if model_id in fetched}
    title = msgs.LOGIN_PICK_MODELS.text(provider=provider_id)
    sys.stdout.flush()
    on_edit = _model_defaults_editor(saved.provider_id, pending, dialects) if saved is not None else None
    chosen = curses.wrapper(curses_menu.multiselect, rows, title, preselected=preselected, on_edit=on_edit)
    if chosen is None:
        return None
    selected = [options[i] for i in chosen]
    extra = _input(msgs.LOGIN_ASK_EXTRA_MODELS.text(), default="")
    for raw in (extra or "").split(","):
        model_id = raw.strip()
        if model_id and model_id not in selected:
            selected.append(model_id)
    return selected


def _interactive() -> bool:
    """curses needs a terminal on both ends; a piped stdin or stdout makes
    `curses.wrapper` die with `nocbreak() returned ERR`."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


class NoTerminalError(RuntimeError):
    """A picker was needed but stdin/stdout is not a terminal."""


def _menu(*args, **kwargs):
    """`curses.wrapper` for a picker; NoTerminalError instead of a curses crash
    when there is no terminal."""
    if not _interactive():
        raise NoTerminalError(msgs.LOGIN_PICKER_NEEDS_AN_INTERACTIVE_TERMINAL_STDIN_AND_STDOUT.format())
    return curses.wrapper(*args, **kwargs)


def _login_provider_rows(*, hidden=("openai-completions", "openai-responses")) -> list[tuple[str, str, str]]:
    saved = load_logins()
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    for provider_id, _login in sorted(saved.items()):
        provider = providers.get_provider(provider_id)
        rows.append((provider_id, provider.display_name if provider else provider_id, msgs.LOGIN_SOURCE_SAVED.words()))
        seen.add(provider_id)

    env_rows: list[tuple[str, str, str]] = []
    registry_rows: list[tuple[str, str, str]] = []
    for provider in providers.login_providers():
        if provider.id in seen:
            continue
        # openai-completions / openai-responses live under <add custom provider>;
        # a top-level row is a duplicate path to the same shape.
        if provider.id in hidden:
            continue
        env_configured = providers.first_env(provider.api_key_env + provider.base_url_env + provider.model_env) is not None
        target = env_rows if env_configured else registry_rows
        target.append((provider.id, provider.display_name, msgs.LOGIN_SOURCE_ENVIRONMENT.words() if env_configured else msgs.LOGIN_SOURCE_REGISTRY.words()))
        seen.add(provider.id)

    env_rows.sort(key=lambda row: row[1].lower())
    registry_rows.sort(key=lambda row: row[1].lower())
    return rows + env_rows + registry_rows


def _select_provider() -> str | None:
    rows = _login_provider_rows()
    items = [msgs.LOGIN_ADD_CUSTOM.text(), *[msgs.LOGIN_PROVIDER_ROW.format(pid=pid, name=name, source=source) for pid, name, source in rows]]
    sys.stdout.flush()
    idx = _menu(curses_menu.menu, items, msgs.LOGIN_PICK_PROVIDER.text(), typeahead=True)
    if idx is None:
        return None
    if idx == 0:
        return "__custom__"
    return rows[idx - 1][0]


def _select_api_shape() -> tuple[str, str] | None:
    items = [msgs.LOGIN_API_SHAPE_ROW.format(pid=pid, desc=desc) for pid, _sdk, desc in _API_SHAPES]
    sys.stdout.flush()
    idx = _menu(curses_menu.menu, items, msgs.LOGIN_PICK_SHAPE.text())
    if idx is None:
        return None
    pid, sdk, _desc = _API_SHAPES[idx]
    return pid, sdk


def _input(prompt: str, *, default: str | None = None, secret: bool = False, read=None, read_secret=None, write=None) -> str | None:
    label = prompt if default in (None, "") else f"{prompt} [{default}]"
    try:
        hidden = secret and paranoid.masks(paranoid.mode())
        reader = (getpass if read_secret is None else read_secret) if hidden else (input if read is None else read)
        value = reader(f"{label}: ")
    except (EOFError, KeyboardInterrupt):
        (print if write is None else write)()
        return None
    stripped = value.strip()
    if stripped:
        return stripped
    return default


def _ask_custom_provider() -> tuple[str, str, str, providers.ProviderDef] | None:
    provider_id = _input(msgs.LOGIN_ASK_CUSTOM_ID.text())
    if not provider_id:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", provider_id):
        msgs.warn(msgs.LOGIN_FAILED, error="Provider names must use letters, digits, '.', '_' or '-'.")
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


def _accept_env_key(answer: str) -> bool:
    return answer.strip().lower() in {"y", "yes"}

def _collect_api_login(
    provider_id: str,
    sdk_provider_id: str | None,
    provider: providers.ProviderDef,
    *,
    shape_provider_id: str | None = None,
    accept_env_key: Callable[[str], bool] | None = None,
    env_key_default: str = "n",
    env_key_question: msgs.Message = msgs.LOGIN_ASK_ENV_KEY,
) -> Login | None:
    existing = load_logins().get(providers.normalize_provider_id(provider_id) or provider_id)
    env = os.environ
    headers: dict[str, str] = dict(existing.provider_headers) if existing else {}

    env_key_name = _env_key_name(provider, env)
    env_key = env.get(env_key_name) if env_key_name else None
    env_base_url = providers.first_env(provider.base_url_env, env)
    env_model = providers.first_env(provider.model_env, env)

    if env_model:
        msgs.say(msgs.LOGIN_ENV_MODEL, model=env_model)

    base_url = env_base_url or (existing.provider_base_url if existing else None) or provider.default_base_url
    # Never auto-take an env key: the operator may keep decoys there, or want a
    # different one. Env keys are OFFERED below, never silently used.
    api_key = (existing.provider_api_key if existing else None) or provider.default_api_key
    # The sdk the operator just picked wins over the saved one, the same order
    # as effective_shape below; otherwise a re-added shape keeps the old transport.
    effective_sdk = sdk_provider_id or (existing.sdk_provider_id if existing and existing.sdk_provider_id else None) or provider.effective_sdk_provider_id
    effective_shape = (
        shape_provider_id
        or (existing.shape_provider_id if existing and existing.shape_provider_id else None)
        or (provider.id if provider.id in _API_SHAPE_IDS else None)
    )

    entered_base_url = _input(msgs.LOGIN_ASK_BASE_URL.text(), default=base_url or "")
    if entered_base_url is None:
        return None
    base_url = entered_base_url or None

    if env_key_name and env_key:
        answer = _input(env_key_question.text(name=env_key_name, key=_mask(env_key)), default=env_key_default)
        if answer is None:
            return None
        if (accept_env_key or _accept_env_key)(answer):
            api_key = env_key

    keyless_ok = provider.local or not provider.established
    if provider.requires_api_key and not api_key:
        prompt = msgs.LOGIN_ASK_KEY_OPTIONAL if keyless_ok else msgs.LOGIN_ASK_KEY
        # An empty default keeps a bare Enter an answer; only cancellation in
        # _input yields None.
        api_key = _input(prompt.text(), default="", secret=True)
        if api_key is None:
            return None
        if not api_key:
            if not keyless_ok:
                msgs.warn(msgs.LOGIN_NO_KEY)
                return None
            # Keyless local endpoint: the openai wire still demands SOME token.
            api_key = config.run_settings()["LOCAL_API_KEY_PLACEHOLDER"]
            msgs.say(msgs.LOGIN_PLACEHOLDER_KEY, key=api_key)
    elif api_key and existing is not None and api_key == existing.provider_api_key:
        # An empty default keeps a bare Enter an answer; only cancellation in
        # _input yields None.
        entered = _input(msgs.LOGIN_ASK_KEY_KEEP.text(), default="", secret=True)
        if entered is None:
            return None
        if entered:
            api_key = entered

    if provider.transport == "cliproxyapi":
        raw_headers = _input(
            msgs.LOGIN_ASK_HEADERS.text(),
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
        msgs.say(msgs.LOGIN_MODEL_ROW, index=idx, model=model_id)
    if len(models) > len(shown):
        msgs.say(msgs.MORE_ROWS, count=len(models) - len(shown))


def _test_answer(choice: str, models: list[str]) -> str | bool | None:
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
    return None


def _secondary_test_choice(models: list[str]) -> str | None | bool:
    if not models:
        return True
    # Enter always means "add it, I don't care to test"; a model number means
    # "test that one first". The heads-up says the listing didn't prove the
    # key can generate — it never forces a test.
    msgs.say(msgs.LOGIN_LISTING_NOT_PROOF)
    prompt = msgs.LOGIN_ASK_TEST.text()
    while True:
        try:
            choice = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        result = _test_answer(choice, models)
        if result is not None:
            return result
        msgs.say(msgs.LOGIN_TEST_CHOICES)

def _run_secondary_test(login: Login, provider: providers.ProviderDef, model_id: str, *, settings=None, say=None) -> bool:
    # Seeing a real answer back IS the confirmation; a further "hit enter to
    # add" after that just re-asked the same question the model number already
    # answered, so it's gone — a bad answer or an exception is still visible
    # right here, and Ctrl-C still works anywhere above this point.
    announce = msgs.say if say is None else say
    announce(msgs.LOGIN_TEST_USER, prompt=_SECONDARY_TEST_PROMPT)
    chunks: list[str] = []

    def on_text(chunk: str) -> None:
        chunks.append(chunk)

    try:
        settings = config.run_settings() if settings is None else settings
        run = SimpleNamespace(**turn_settings.project(settings))
        result = model_client.stream_model(
            model_id=model_id,
            provider_id=login.provider_id,
            provider_base_url=login.provider_base_url,
            provider_api_key=login.provider_api_key,
            provider_headers=model_client.turn_headers(login.provider_headers, run),
            messages=[ai.user_message(_SECONDARY_TEST_PROMPT)],
            tools=None,
            max_output_tokens=None,
            reasoning_effort=provider.default_effort(settings),
            on_text=on_text,
            retry_budget=retry.Budget.from_settings(None),
            stream_limits=retry.StreamLimits.from_settings(None),
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001
        announce(msgs.LOGIN_TEST_FAILED, error=f"{type(exc).__name__}: {exc}")
        return False

    answer = result.text.strip() or "".join(chunks).strip()
    announce(msgs.LOGIN_TEST_ANSWER, answer=answer)
    return True

def _post_fetch_confirmation(login: Login, provider: providers.ProviderDef, models: list[str]) -> bool | None:
    _display_models(models)
    if provider.models_list_validates_auth:
        # Listing already proved the credentials work — asking "add without a
        # test, or verify first?" here is a prompt with only one sane answer.
        return True
    choice = _secondary_test_choice(models)
    if choice is True:
        return True
    if choice is False or choice is None:
        return None
    return _run_secondary_test(login, provider, choice)


def _keep_list(provider_id: str, login: Login, models: list[str]) -> tuple[list[str] | None, Login]:
    """A login flow's keep-list for ``login``, not saved yet: the models to
    cache (None when cancelled) and the login with what `e` edited in its
    model defaults, starting from the ones a login saved before has."""
    previous = load_logins().get(provider_id)
    if login.model_defaults is None and previous is not None:
        login = replace(login, model_defaults=dict(previous.model_defaults or {}))
    pending = [login]
    to_cache = _select_models_to_cache(provider_id, models, saved=login, pending=pending)
    return to_cache, pending[0]


def _provider_details(login: Login, model_count: int) -> list[str]:
    provider = providers.get_provider(login.provider_id)
    return [
        msgs.LOGIN_SAVED_PROVIDER_DETAIL.format(provider_id=login.provider_id, value=provider.display_name if provider else login.provider_id),
        msgs.LOGIN_DETAIL_BASE_URL.text(url=login.provider_base_url or msgs.BUILTIN_VALUE.text()),
        msgs.LOGIN_DETAIL_KEY.text(key=_mask(login.provider_api_key) if login.provider_api_key else msgs.NONE_VALUE.text()),
        msgs.LOGIN_DETAIL_CACHED.text(models=msgs.plural(model_count, "cached model")),
        msgs.LOGIN_DETAIL_HEADERS.text(
            headers=", ".join(f"{k}={_mask(v)}" for k, v in login.provider_headers.items()) or msgs.NONE_VALUE.text()),
        msgs.LOGIN_DETAIL_DEFAULTS.text(defaults=login_defaults.render(login.defaults) or msgs.NONE_VALUE.text()),
    ]


def _edit_defaults(login: Login) -> None:
    """Ask for the server defaults and save them."""
    while True:
        raw = _input(msgs.LOGIN_EDIT_DEFAULTS.text(names=", ".join(login_defaults.KEYS)),
                     default=login_defaults.render(login.defaults))
        if raw is None:
            return
        if raw == "-":
            values: dict = {}
            break
        try:
            values = login_defaults.parse(raw)
            break
        except ValueError as exc:
            msgs.say(msgs.LOGIN_BAD_DEFAULTS, error=exc)
    update_login(login.provider_id, lambda current: replace(current, defaults=values))


def _model_annotation(login: Login | None, model_id: str, tag: str = "") -> str:
    """A keep-list row's annotation: its dialect tag and the model's defaults."""
    values = login_defaults.render(((login.model_defaults if login else None) or {}).get(model_id))
    return "  ".join(part for part in (tag, values) if part)


def _model_defaults_editor(provider_id: str, pending: list[Login] | None = None,
                           tags: dict[str, str] | None = None,
                           ) -> Callable[[curses.window, str], str | None]:
    """The keep-list's `e` action: ask for the model's defaults on the bottom
    line, save them to the saved login, and return the row's new annotation
    (None leaves it as it was). With ``pending`` (one login not saved yet,
    during js --login) the edit goes into that login instead. ``tags`` holds
    each row's dialect tag, kept in front of the defaults."""

    def edit(stdscr: curses.window, model_id: str) -> str | None:
        login = pending[0] if pending else load_logins().get(provider_id)
        if login is None:
            return None
        current = login_defaults.render((login.model_defaults or {}).get(model_id))
        raw = curses_menu.line_prompt(stdscr, msgs.LOGIN_EDIT_MODEL_DEFAULTS.text(model=model_id), current)
        if raw is None:
            return None
        try:
            values = {} if raw.strip() == "-" else login_defaults.parse(raw)
        except ValueError:
            return None
        def change(current):
            model_defaults = {k: v for k, v in (current.model_defaults or {}).items() if k != model_id}
            if values:
                model_defaults[model_id] = values
            return replace(current, model_defaults=model_defaults)

        if pending:
            updated = change(login)
            pending[0] = updated
        else:
            updated = update_login(provider_id, change)
        return _model_annotation(updated, model_id, (tags or {}).get(model_id, ""))

    return edit


def _edit_saved_provider(login: Login) -> None:
    """Save local edits without fetching models."""
    base = _input(msgs.LOGIN_EDIT_BASE_URL.text(), default=login.provider_base_url or "")
    if base is None:
        return
    key = _input(msgs.LOGIN_EDIT_KEY.text(), default="", secret=True)
    if key is None:
        return
    while True:
        headers = _input(msgs.LOGIN_EDIT_HEADERS.text(), default="", secret=True)
        if headers is None:
            return
        if not headers or headers == "-":
            parsed = {} if headers == "-" else login.provider_headers
            break
        parts = [part.strip() for part in headers.split(",")]
        if all("=" in part and part.split("=", 1)[0].strip() for part in parts):
            parsed = {k.strip(): v.strip() for k, v in (part.split("=", 1) for part in parts)}
            break
        msgs.say(msgs.LOGIN_BAD_HEADERS)
    changes = {}
    url = None if base == "-" else base or None
    if url != login.provider_base_url:
        changes["provider_base_url"] = url
    if key:
        changes["provider_api_key"] = None if key == "-" else key
    if headers:
        changes["provider_headers"] = parsed
    update_login(login.provider_id, lambda current: replace(current, **changes))


def _rename_saved_provider(provider_id: str) -> str:
    name = _input(msgs.LOGIN_RENAME.text(), default=provider_id)
    if name and rename_login(provider_id, name):
        return providers.normalize_provider_id(name) or name
    return provider_id


def _manage_providers() -> int:
    while True:
        saved = load_logins()
        cached = load_model_cache()
        ids = sorted(saved)
        items = [msgs.LOGIN_ADD_CUSTOM.text()]
        items.extend(_provider_details(saved[pid], len(cached.get(pid, [])))[0] for pid in ids)
        items.append(msgs.LOGIN_ADD_REGISTRY.text())
        details = [[], *[_provider_details(saved[pid], len(cached.get(pid, []))) for pid in ids], []]
        requested = []

        def action(key, index):
            if 1 <= index <= len(ids):
                requested.append(key)
                return True
            return False

        choice = _menu(curses_menu.menu, items, msgs.LOGIN_MANAGE_PROVIDERS.text(), details=details,
                       on_action=action)
        if choice is None:
            return 0
        if choice == 0 or choice == len(items) - 1:
            provider_id = "__custom__" if choice == 0 else _select_provider()
            if provider_id is not None:
                _run_login(provider_id)
            continue
        provider_id = ids[choice - 1]
        try:
            if requested == ["r"]:
                _rename_saved_provider(provider_id)
            elif requested == ["d"]:
                _run_logout(provider_id)
            else:
                _manage_saved_provider(provider_id)
        except (LoginsCorruptError, ValueError) as exc:
            msgs.warn(msgs.LOGIN_PROVIDER_NOT_CHANGED, error=exc)
            return 1


def _manage_models(login: Login) -> None:
    status = ""
    while True:
        cached = load_model_cache().get(login.provider_id, [])
        actions = [msgs.LOGIN_MODELS_SELECT.text(), msgs.LOGIN_MODELS_ADD.text(),
                   msgs.LOGIN_MODELS_REFETCH.text(), msgs.LOGIN_BACK.text()]
        title = msgs.LOGIN_MODELS_TITLE.text(provider=login.provider_id, count=len(cached))
        choice = _menu(curses_menu.menu, actions, f"{title}  {status}")
        status = ""
        if choice is None or choice == 3:
            return
        if choice == 0:
            if not cached:
                status = msgs.LOGIN_CACHE_EMPTY.text()
            else:
                _run_models_edit(login.provider_id)
        elif choice == 1:
            extra = _input(msgs.LOGIN_ASK_MODEL_IDS.text(), default="")
            if extra:
                added = [model.strip() for model in extra.split(",") if model.strip()]
                cache_models(login.provider_id, list(dict.fromkeys([*cached, *added])))
        elif choice == 2:
            msgs.say(msgs.LOGIN_FETCHING)
            try:
                models, metadata = test_login_with_metadata(login)
            except Exception as exc:  # noqa: BLE001 - keep the existing cache on a failed fetch
                status = msgs.LOGIN_REFETCH_FAILED.text(error=type(exc).__name__)
                continue
            # Read again: an `e` in an earlier picker saved new defaults.
            saved = load_logins().get(login.provider_id) or login
            curated = _select_models_to_cache(login.provider_id, models, annotate_dialects=False,
                                              saved=saved)
            if curated is not None:
                cache_models(login.provider_id, curated, metadata=metadata)


def _manage_saved_provider(provider_id: str) -> None:
    while (login := load_logins().get(provider_id)) is not None:
        details = _provider_details(login, len(load_model_cache().get(provider_id, [])))
        actions = [msgs.LOGIN_UPDATE.text(), msgs.LOGIN_MODELS.text(), msgs.LOGIN_DEFAULTS.text(),
                   msgs.LOGIN_BACK.text(), msgs.LOGIN_REMOVE.text()]
        requested = []

        def action(key, _index):
            requested.append(key)
            return True

        choice = _menu(curses_menu.menu, actions, msgs.LOGIN_MANAGE_ONE.text(provider=provider_id),
                       details=[details] * len(actions), on_action=action)
        if requested == ["r"]:
            provider_id = _rename_saved_provider(provider_id)
            continue
        if requested == ["d"]:
            _run_logout(provider_id)
            return
        if choice is None or choice == 3:
            return
        if choice == 0:
            _edit_saved_provider(login)
        elif choice == 1:
            _manage_models(login)
        elif choice == 2:
            _edit_defaults(login)
        elif choice == 4:
            _run_logout(provider_id)
            return


def _run_login(provider_id: str | None = None) -> int:
    if provider_id is None:
        return _manage_providers()
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

    login = _collect_api_login(provider_id, sdk_provider_id, provider, shape_provider_id=shape_provider_id)
    if login is None:
        return 0

    msgs.say(msgs.LOGIN_FETCHING)
    try:
        models, model_metadata = test_login_with_metadata(login)
    except Exception as exc:  # noqa: BLE001
        # A server that is down now is still saved; its models come later
        # through Models > Re-fetch.
        msgs.warn(msgs.LOGIN_MODELS_UNREACHABLE, error=f"{type(exc).__name__}: {exc}")
        try:
            save_login(login)
        except LoginsCorruptError as save_exc:
            msgs.warn(msgs.LOGIN_NOT_SAVED, error=save_exc)
            return 1
        canonical_id = providers.normalize_provider_id(provider_id) or provider_id
        msgs.say(msgs.PROVIDER_ADDED, provider=canonical_id, models=msgs.plural(0, "model"))
        return 0

    confirmed = _post_fetch_confirmation(login, provider, models)
    if confirmed is None:
        return 0
    if confirmed is False:
        return 1

    canonical_id = providers.normalize_provider_id(provider_id) or provider_id
    to_cache, login = _keep_list(canonical_id, login, models)
    if to_cache is None:
        return 0
    try:
        save_login(login)
        cache_models(canonical_id, to_cache, metadata=model_metadata)
    except LoginsCorruptError as exc:
        msgs.warn(msgs.LOGIN_NOT_SAVED, error=exc)
        return 1
    msgs.say(msgs.PROVIDER_ADDED, provider=canonical_id, models=msgs.plural(len(to_cache), "model"))
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
        msgs.warn(msgs.LOGIN_NO_CACHED_MODELS, provider=target)
        return 1

    curated = _select_models_to_cache(target, cached, annotate_dialects=False,
                                      saved=load_logins().get(target))
    if curated is None:
        msgs.say(msgs.LOGIN_CACHE_EDIT_CANCELLED, provider=target)
        return 0

    cache_models(target, curated)
    msgs.say(msgs.LOGIN_CACHED, models=msgs.plural(len(curated), "model"), provider=target)
    return 0


def _run_logout(provider_id: str) -> int:
    target = providers.normalize_provider_id(provider_id) or provider_id
    try:
        removed = remove_login(target)
    except LoginsCorruptError as exc:
        msgs.warn(msgs.LOGOUT_FAILED, error=exc)
        return 1
    if removed:
        msgs.say(msgs.LOGGED_OUT, provider=target)
        return 0
    msgs.warn(msgs.NOT_LOGGED_IN, provider=target)
    return 1


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except NoTerminalError as exc:
        msgs.warn(msgs.LOGIN_FAILED, error=exc)
        return 2


def _main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        return _run_login(None)
    if args[0] in ("--login", "login"):
        provider_id = args[1] if len(args) > 1 else None
        return _run_login(provider_id)
    if args[0] in ("--logout", "logout"):
        if len(args) < 2:
            msgs.warn(msgs.USAGE, usage="js --logout <provider-id>")
            return 2
        return _run_logout(args[1])
    if args[0] in ("--models-edit", "models-edit"):
        if len(args) < 2:
            msgs.warn(msgs.USAGE, usage="js --models-edit <provider-id>")
            return 2
        return _run_models_edit(args[1])
    if len(args) == 1:
        return _run_login(args[0])
    msgs.warn(msgs.USAGE, usage="js --login [<provider-id>] | js --logout <provider-id> | js --models-edit <provider-id>")
    return 2


if __name__ == "__main__":
    sys.exit(main())
