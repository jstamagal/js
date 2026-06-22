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


def resolve_model_route(
    requested_model: str,
    *,
    configured_provider_id: str | None = None,
    configured_base_url: str | None = None,
    configured_api_key: str | None = None,
    configured_headers: Mapping[str, str] | None = None,
    env: Mapping[str, str] | None = None,
    explicit_model: bool = False,
    discover_env: bool = True,
    use_saved_login: bool = True,
) -> ModelRoute:
    """Resolve ``requested_model`` into a full route.

    - A ``provider/model`` prefix selects that provider, but only when no
      provider is pinned or the prefix names the same one (so a gateway id like
      ``anthropic/claude-...`` under provider ``omp`` does not hijack routing).
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
    if parsed_provider_id is not None and (
        configured_provider_id is None or parsed_provider_id == configured_provider_id
    ):
        model = parsed_model or str(requested_model)
        provider_id = parsed_provider_id
    else:
        model = str(requested_model)
        provider_id = configured_provider_id

    if provider_id is None and discover_env:
        discovered = providers.discover_env_provider(source)
        if discovered is not None:
            provider_id = discovered.id

    base_url = configured_base_url
    api_key = configured_api_key
    headers: dict[str, str] = dict(configured_headers or {})

    if use_saved_login and provider_id is not None:
        try:
            from . import logins as _logins

            saved = _logins.load_logins().get(provider_id)
        except Exception:  # noqa: BLE001
            saved = None
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
