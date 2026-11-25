"""Provider/model route resolution.

ONE place turns a requested model id plus configured/env provider hints into a
fully-resolved route: the model id, the js provider id, base URL, api key,
headers, and the provider's transport/wire. This replaces the prefix/provider
parsing that used to be copy-pasted across ``config.from_env``,
the REPL's per-turn route (``ReplState.config_for_turn``), subagent selection, and compaction.

``model_client.resolve_model`` stays the final ``ai`` provider factory and
nothing more — all routing decisions live here.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from . import logins as _logins, messages as msgs, providers


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


@dataclass(frozen=True)
class Wire:
    """Which wire requests on one provider route go out on.

    ``provider_id`` is the id as ``providers.normalize_provider_id`` leaves it
    (None with no provider) and ``provider`` its definition, None when nothing
    registers it. ``sdk`` is the SDK provider id requests are built with: the
    provider's, else ``provider_id``, and openai with no provider. ``name`` is
    the wire in the ``-u`` wire names: openai (chat completions), responses,
    anthropic, or the SDK id. ``responses`` is the OpenAI Responses API: the
    custom_responses transport on the openai SDK.

    ``provider_name`` and ``sdk_provider_name`` are ``provider_id`` and
    ``sdk``, "" with no provider; ``explicit_provider`` is whether a provider id
    was given at all. ``chat_completions`` is OpenAI chat completions: the
    openai SDK off the Responses API, which is also every run with no provider.
    ``reasoning_content`` is whether an OpenAI chat-completions request goes
    through js's ``reasoning_content`` adapter: an operator-run endpoint, the
    custom_openai transport, or a base URL other than the provider's default.
    ``deepseek`` is whether the endpoint takes DeepSeek's extras, and
    ``minimax`` whether it is MiniMax's."""

    provider: providers.ProviderDef | None
    provider_id: str | None
    sdk: str
    transport: str | None
    name: str
    explicit_provider: bool
    provider_name: str
    sdk_provider_name: str
    anthropic: bool
    responses: bool
    chat_completions: bool
    reasoning_content: bool
    deepseek: bool
    minimax: bool


def resolve_wire(provider_id: str | None, *, base_url: str | None = None, model_id: str = "") -> Wire:
    """The `Wire` requests to ``model_id`` on ``provider_id`` at ``base_url``
    go out on. Only ``reasoning_content`` reads ``base_url``, and only
    ``deepseek`` reads ``model_id``."""
    provider = providers.get_provider(provider_id)
    canonical_id = None if provider_id is None else providers.normalize_provider_id(provider_id) or provider_id
    transport = provider.transport if provider is not None else None
    if provider_id is None:
        sdk = "openai"
    else:
        sdk = (provider.effective_sdk_provider_id if provider is not None else None) or canonical_id
    responses = sdk == "openai" and transport == "custom_responses"
    name = "responses" if responses else sdk
    provider_name = canonical_id or ""
    sdk_provider_name = sdk if provider_id is not None else ""
    explicit_provider = provider_id is not None
    operator_endpoint = provider is not None and (provider.local or not provider.established)
    return Wire(
        provider=provider,
        provider_id=canonical_id,
        sdk=sdk,
        transport=transport,
        name=name,
        explicit_provider=explicit_provider,
        provider_name=provider_name,
        sdk_provider_name=sdk_provider_name,
        anthropic=sdk_provider_name == "anthropic" or provider_name == "anthropic",
        responses=responses,
        chat_completions=sdk == "openai" and not responses,
        reasoning_content=(operator_endpoint or transport == "custom_openai"
                           or providers.is_custom_base_url(provider, base_url)),
        # Whether the ENDPOINT takes DeepSeek's extras, which is not whether the
        # model is a DeepSeek model: `nvidia/deepseek-ai/deepseek-v4-pro-0813`
        # is one hosted elsewhere, and DeepSeek's private extras there are a 400.
        # With no provider the model id is the only signal there is.
        deepseek=provider_name == "deepseek" or sdk_provider_name == "deepseek" or (
            not explicit_provider and "deepseek" in model_id.lower()),
        minimax=provider_name == "minimax",
    )


class ProviderNotLoggedInError(ValueError):
    """A model id names a known provider the operator has not logged into.

    Subclasses ``ValueError`` so the friendly one-line error handling that already
    wraps route/config resolution (one line, never a traceback) catches it
    everywhere those failures already surface.
    """


def not_logged_in_message(provider_id: str) -> str:
    return msgs.PROVIDER_NOT_LOGGED_IN.said(provider=provider_id)


def unconfigured_model_message(model: str) -> str:
    # A known-provider prefix (deferred here from config resolution) names exactly
    # which login is missing — say so, don't fall back to the generic hint.
    prefix_provider, _ = providers.parse_model_prefix(str(model))
    if prefix_provider is not None:
        return not_logged_in_message(prefix_provider)
    return msgs.MODEL_UNCONFIGURED.said(model=model)


def _saved_login(provider_id: str | None):
    """Return the saved Login for ``provider_id`` (normalized), or None.

    A missing, unreadable or malformed login store reads as no logins
    (``logins.load_logins``), so it means no login-driven override or
    credential fill.
    """
    normalized = providers.normalize_provider_id(provider_id)
    if not normalized:
        return None

    return _logins.load_logins().get(normalized)


def provider_extra_params(cfg) -> dict | None:
    """The operator's ``EXTRA_PARAMS`` settings table, as a plain dict."""
    extra = (getattr(cfg, "settings", {}) or {}).get("EXTRA_PARAMS")
    return dict(extra) if isinstance(extra, dict) else None


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
    login_lookup=None,
    prefix_allowed=None,
) -> ModelRoute:
    """Resolve ``requested_model`` into a full route.

    A ``provider/model`` prefix routes ONLY when the operator has authorized that
    provider — env keys alone never create a route (the login/model cache is the
    single source of truth for what is runnable):

    - ``prefix_login``: a saved ``js --login`` exists for the prefixed provider.
      This is authoritative: it overrides a pinned ``PROVIDER`` and carries the
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
    lookup_login = _saved_login if login_lookup is None else login_lookup
    source = os.environ if env is None else env
    configured_provider_id = providers.normalize_provider_id(configured_provider_id)

    parsed_provider_id, parsed_model = providers.parse_model_prefix(str(requested_model))
    prefix_login = lookup_login(parsed_provider_id) if parsed_provider_id is not None else None
    routes_by_prefix = parsed_provider_id is not None and (
        prefix_login is not None
        or parsed_provider_id == configured_provider_id
        or (prefix_overrides_provider and prefix_login is not None)
    )
    if prefix_allowed is not None:
        routes_by_prefix = prefix_allowed(parsed_provider_id, configured_provider_id, prefix_login)
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
            saved = lookup_login(provider_id)
        elif prefix_login is not None and provider_id == parsed_provider_id:
            # Pinned, switched or unpinned alike: the login is why this routed.
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
