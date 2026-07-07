"""Provider/model route resolution.

ONE place turns a requested model id plus configured/env provider hints into a
fully-resolved route: the model id, the js provider id, base URL, api key,
headers, and the provider's transport/wire. This replaces the prefix/provider
parsing that used to be copy-pasted across ``config.from_env``,
``cli._cfg_for_active_model``, subagent selection, and compaction.

``model_client.resolve_model`` stays the final ``ai`` provider factory and
nothing more — all routing decisions live here.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from . import providers


@dataclass(frozen=True)
class ModelRoute:
    """A fully-resolved model route. ``transport`` is the provider's wire shape
    (``ProviderDef.transport``) or ``None`` when the id routes through the SDK's
    default gateway with no explicit provider."""

    model: str
    provider_id: str | None
    base_url: str | None
    api_key: str | None
    headers: dict[str, str] = field(default_factory=dict)
    transport: str | None = None


class ProviderNotLoggedInError(ValueError):
    """A model id names a known provider the operator has not logged into.

    Subclasses ``ValueError`` so the friendly one-line error handling that already
    wraps route/config resolution (``error: {e}``, never a traceback) catches it
    everywhere those failures already surface.
    """


def not_logged_in_message(provider_id: str) -> str:
    return (
        f"provider {provider_id!r} is not logged in; run `js --login {provider_id}` "
        "(js --list-models shows what's runnable)"
    )


def unconfigured_model_message(model: str) -> str:
    # A known-provider prefix (deferred here from config resolution) names exactly
    # which login is missing — say so, don't fall back to the generic hint.
    prefix_provider, _ = providers.parse_model_prefix(str(model))
    if prefix_provider is not None:
        return not_logged_in_message(prefix_provider)
    return (
        f"model {model!r} has no provider configured and no login; set provider.id "
        "(or JS_PROVIDER), run `js --login`, or prefix a logged-in provider "
        "(js --list-models shows what's runnable)"
    )


def _saved_login(provider_id: str | None):
    """Return the saved Login for ``provider_id`` (normalized), or None.

    Resilient by design: a missing/unreadable login store must never block
    routing — it just means no login-driven override or credential fill.
    """
    normalized = providers.normalize_provider_id(provider_id)
    if not normalized:
        return None
    try:
        from . import logins as _logins

        return _logins.load_logins().get(normalized)
    except Exception:  # noqa: BLE001
        return None


def resolve_model_route(
    requested_model: str,
    *,
    configured_provider_id: str | None = None,
    configured_base_url: str | None = None,
    configured_api_key: str | None = None,
    configured_headers: Mapping[str, str] | None = None,
    env: Mapping[str, str] | None = None,
    explicit_model: bool = False,
    prefix_overrides_provider: bool = False,
    use_saved_login: bool = True,
) -> ModelRoute:
    """Resolve ``requested_model`` into a full route.

    A ``provider/model`` prefix routes ONLY when the operator has authorized that
    provider — env keys alone never create a route (the login/model cache is the
    single source of truth for what is runnable):

    - ``prefix_login``: a saved ``js --login`` exists for the prefixed provider.
      This is authoritative: it overrides a pinned ``provider.id`` and carries the
      login's base/key.
    - the prefix names the explicitly pinned ``configured_provider_id``.
    - ``prefix_overrides_provider`` (invocation-explicit ``-m``/``JS_MODEL`` and
      agent/subagent ``model:`` choices) makes a known prefix authoritative: a
      saved-login prefix routes past a stale pin, while an unlogged different
      known provider raises instead of riding the stale pin.

    A prefix naming a KNOWN provider (builtin or catalog) with no login and no
    explicit pin raises :class:`ProviderNotLoggedInError` instead of silently
    riding the pinned/gateway provider on that vendor's env keys. An UNKNOWN first
    segment is not a prefix at all — parse leaves the slashy id whole (OpenRouter /
    Hugging Face style model names).

    - ``use_saved_login``: fill base/key/headers from a saved login when the
      caller has not supplied them.
    - ``explicit_model``: when False and no prefix routed, the provider's default
      model may fill in.
    """
    source = os.environ if env is None else env
    configured_provider_id = providers.normalize_provider_id(configured_provider_id)

    parsed_provider_id, parsed_model = providers.parse_model_prefix(str(requested_model))
    prefix_login = _saved_login(parsed_provider_id) if parsed_provider_id is not None else None
    routes_by_prefix = parsed_provider_id is not None and (
        prefix_login is not None
        or parsed_provider_id == configured_provider_id
        or (prefix_overrides_provider and prefix_login is not None)
    )
    if routes_by_prefix:
        model = parsed_model or str(requested_model)
        provider_id = parsed_provider_id
    else:
        if (
            prefix_overrides_provider
            and parsed_provider_id is not None
            and parsed_provider_id != configured_provider_id
        ):
            raise ProviderNotLoggedInError(not_logged_in_message(parsed_provider_id))
        # A known-provider prefix that carries no login and does not name the
        # pinned provider is legitimate only as a gateway id UNDER an explicit pin
        # (`anthropic/claude-...` on a pinned `omp` yields to omp). With nothing
        # pinned it is simply a provider the operator never logged into.
        if parsed_provider_id is not None and configured_provider_id is None:
            raise ProviderNotLoggedInError(not_logged_in_message(parsed_provider_id))
        model = str(requested_model)
        provider_id = configured_provider_id

    # When a prefix switched to a DIFFERENT provider than the one configured, the
    # configured base/key/headers belong to the old provider — start fresh so the
    # new provider's saved login / env / defaults fill them in.
    switched = routes_by_prefix and configured_provider_id is not None and parsed_provider_id != configured_provider_id
    base_url = None if switched else configured_base_url
    api_key = None if switched else configured_api_key
    headers: dict[str, str] = {} if switched else dict(configured_headers or {})

    # Fill missing base/key/headers from a saved login. Normally gated by
    # ``use_saved_login``, but a prefix that ROUTED because of its saved login
    # must also carry that login's credentials even when the caller (the live
    # REPL state path) otherwise opts out — the route only exists because of it.
    saved = None
    if provider_id is not None:
        if use_saved_login:
            saved = _saved_login(provider_id)
        elif switched and prefix_login is not None and provider_id == parsed_provider_id:
            saved = prefix_login
    if saved is not None:
        base_url = base_url or saved.provider_base_url
        api_key = api_key or saved.provider_api_key
        if not headers:
            headers = dict(saved.provider_headers)

    provider_def = providers.get_provider(provider_id)
    transport = provider_def.transport if provider_def is not None else None
    if provider_def is not None:
        base_url = providers.provider_base_url(provider_def, base_url, source)
        api_key = providers.provider_api_key(provider_def, api_key, source)
        if not headers and provider_def.headers:
            headers = dict(provider_def.headers)
        if not explicit_model and parsed_provider_id is None:
            model = providers.provider_model(provider_def, None, source) or model

    return ModelRoute(
        model=model,
        provider_id=provider_id,
        base_url=base_url,
        api_key=api_key,
        headers=headers,
        transport=transport,
    )
