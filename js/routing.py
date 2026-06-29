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
    discover_env: bool = True,
    use_saved_login: bool = True,
) -> ModelRoute:
    """Resolve ``requested_model`` into a full route.

    - A ``provider/model`` prefix selects that provider, but only when no
      provider is pinned or the prefix names the same one (so a gateway id like
      ``anthropic/claude-...`` under provider ``omp`` does not hijack routing).
    - ``prefix_overrides_provider``: let a known prefix route even when a different
      provider is pinned (for explicit agent/subagent `model:` choices); the
      pinned provider's base/key/headers are then dropped for the new provider.
    - ``discover_env``: when no provider is set, sniff provider-specific env vars
      (e.g. ``DEEPSEEK_API_KEY``) so a fresh shell works without ``js --login``.
    - ``use_saved_login``: fill base/key/headers from a saved login when the
      caller has not supplied them.
    - ``explicit_model``: when False and no prefix routed, the provider's default
      model may fill in.
    """
    source = os.environ if env is None else env
    configured_provider_id = providers.normalize_provider_id(configured_provider_id)

    parsed_provider_id, parsed_model = providers.parse_model_prefix(str(requested_model))
    # A model-id prefix that names a provider the operator has explicitly logged
    # into is an authoritative routing signal: it overrides a pinned provider
    # (e.g. a stale `provider.id` in jsrc) the same way ``prefix_overrides_provider``
    # does. It does NOT hijack a gateway id like ``anthropic/claude-...`` under a
    # pinned ``omp`` — there is no saved ``anthropic`` login, so the prefix yields.
    prefix_login = _saved_login(parsed_provider_id) if parsed_provider_id is not None else None
    routes_by_prefix = parsed_provider_id is not None and (
        prefix_overrides_provider
        or configured_provider_id is None
        or parsed_provider_id == configured_provider_id
        or prefix_login is not None
    )
    if routes_by_prefix:
        model = parsed_model or str(requested_model)
        provider_id = parsed_provider_id
    else:
        model = str(requested_model)
        provider_id = configured_provider_id

    if provider_id is None and discover_env:
        discovered = providers.discover_env_provider(source)
        if discovered is not None:
            provider_id = discovered.id

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
