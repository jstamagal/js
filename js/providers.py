"""Provider registry and runtime resolution for js.

This is the one place that knows user-facing provider ids, aliases, default
endpoints, environment variables, API shapes, append-only behavior, and model
prefix parsing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from collections.abc import Mapping

import modelsdotdev
from ai.providers.base import _PROVIDER_REGISTRY

from . import codex_auth

Transport = str

_OPENCODE_GO_ANTHROPIC_BASE_URL = "https://opencode.ai/zen/go"
_MODELSDEV_TRANSPORTS: dict[str, Transport] = {
    "@ai-sdk/anthropic": "anthropic",
    "@ai-sdk/openai": "openai",
    "@ai-sdk/openai-compatible": "openai_compatible",
    "@ai-sdk/gateway": "gateway",
    "vercel": "gateway",
}


@dataclass(frozen=True)
class ProviderDef:
    id: str
    display_name: str
    transport: Transport
    sdk_provider_id: str | None = None
    default_base_url: str | None = None
    default_api_key: str | None = None
    default_model: str | None = None
    api_key_env: tuple[str, ...] = ()
    base_url_env: tuple[str, ...] = ()
    model_env: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    requires_api_key: bool = True
    established: bool = True
    append_only: bool = False
    reasoning_effort: str | None = None
    models_list_validates_auth: bool = True
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def effective_sdk_provider_id(self) -> str | None:
        return self.sdk_provider_id or self.id

    @property
    def login_base_url_field(self) -> bool:
        return not self.established or self.transport in {"custom_openai", "custom_responses", "custom_anthropic", "cliproxyapi"}


def _p(
    id: str,
    display_name: str,
    transport: Transport,
    *,
    sdk: str | None = None,
    base: str | None = None,
    key: str | None = None,
    model: str | None = None,
    key_env: tuple[str, ...] = (),
    base_env: tuple[str, ...] = (),
    model_env: tuple[str, ...] = (),
    aliases: tuple[str, ...] = (),
    requires_api_key: bool = True,
    established: bool = True,
    append_only: bool = False,
    reasoning_effort: str | None = None,
    models_list_validates_auth: bool = True,
    headers: Mapping[str, str] | None = None,
) -> ProviderDef:
    return ProviderDef(
        id=id,
        display_name=display_name,
        transport=transport,
        sdk_provider_id=sdk,
        default_base_url=base,
        default_api_key=key,
        default_model=model,
        api_key_env=key_env,
        base_url_env=base_env,
        model_env=model_env,
        aliases=aliases,
        requires_api_key=requires_api_key,
        established=established,
        append_only=append_only,
        reasoning_effort=reasoning_effort,
        models_list_validates_auth=models_list_validates_auth,
        headers=headers or {},
    )


_OPENCODE_GO_OPENAI_BASE_URL = "https://opencode.ai/zen/go/v1"
_BUILTINS: tuple[ProviderDef, ...] = (
    _p(
        "deepseek",
        "DeepSeek",
        "deepseek",
        sdk="deepseek",
        base="https://api.deepseek.com",
        model="deepseek-v4-flash",
        key_env=("DEEPSEEK_API_KEY",),
        base_env=("DEEPSEEK_BASE_URL",),
        model_env=("DEEPSEEK_MODEL",),
        append_only=True,
        reasoning_effort="xhigh",
    ),
    _p(
        "ollama",
        "Ollama Local",
        "ollama",
        sdk="openai",
        base="http://127.0.0.1:11434/v1",
        key="ollama",
        key_env=("OLLAMA_API_KEY", "OLLAMA_LOCAL_API_KEY"),
        base_env=("OLLAMA_BASE_URL", "OLLAMA_LOCAL_BASE_URL"),
        model_env=("OLLAMA_MODEL", "OLLAMA_LOCAL_MODEL"),
        aliases=("ollama-local",),
        requires_api_key=False,
    ),
    _p(
        "ollama-cloud",
        "Ollama Cloud",
        "ollama",
        sdk="openai",
        base="https://ollama.com/v1",
        key_env=("OLLAMA_CLOUD_API_KEY",),
        base_env=("OLLAMA_CLOUD_BASE_URL",),
        model_env=("OLLAMA_CLOUD_MODEL",),
    ),
    _p(
        "llama.cpp",
        "llama.cpp",
        "llama.cpp",
        sdk="openai",
        base="http://127.0.0.1:8080/v1",
        key="x",
        key_env=("LLAMACPP_API_KEY", "LLAMA_CPP_API_KEY"),
        base_env=("LLAMACPP_BASE_URL", "LLAMA_CPP_BASE_URL"),
        model_env=("LLAMACPP_MODEL", "LLAMA_CPP_MODEL"),
        aliases=("llamacpp", "llama-cpp"),
        requires_api_key=False,
    ),
    _p(
        "opencode-go",
        "opencode-go OpenAI-compatible",
        "openai_compatible",
        sdk="openai",
        base=_OPENCODE_GO_OPENAI_BASE_URL,
        key_env=("OPENCODE_GO_API_KEY",),
        base_env=("OPENCODE_GO_BASE_URL",),
        model_env=("OPENCODE_GO_MODEL",),
        models_list_validates_auth=False,
    ),
    _p(
        "opencode-go-anthropic",
        "opencode-go Anthropic-compatible",
        "anthropic",
        sdk="anthropic",
        base=_OPENCODE_GO_ANTHROPIC_BASE_URL,
        model="qwen3.7-plus",
        key_env=("OPENCODE_GO_API_KEY",),
        base_env=("OPENCODE_GO_ANTHROPIC_BASE_URL",),
        model_env=("OPENCODE_GO_ANTHROPIC_MODEL",),
        models_list_validates_auth=False,
    ),
    _p(
        "mimo",
        "Xiaomi MiMo API",
        "openai_compatible",
        sdk="openai",
        base="https://api.xiaomimimo.com/v1",
        model="mimo-v2.5-pro",
        key_env=("XIAOMI_API_KEY", "MIMO_API_KEY"),
        base_env=("XIAOMI_API_BASE_URL", "MIMO_API_BASE_URL"),
        model_env=("XIAOMI_API_MODEL", "MIMO_API_MODEL"),
        aliases=("xiaomi", "xiaomi-api"),
        append_only=True,
    ),
    _p(
        "mimo-token-plan",
        "Xiaomi MiMo Token Plan",
        "openai_compatible",
        sdk="openai",
        base="https://token-plan-sgp.xiaomimimo.com/v1",
        model="mimo-v2.5",
        key_env=("XIAOMI_TP_KEY", "MIMO_TP_KEY"),
        base_env=("XIAOMI_TP_BASE_URL", "MIMO_TP_BASE_URL"),
        model_env=("XIAOMI_TP_MODEL", "MIMO_TP_MODEL"),
        aliases=("mimo-tp", "xiaomi-token-plan"),
        append_only=True,
    ),
    _p(
        "minimax",
        "MiniMax",
        "anthropic",
        sdk="anthropic",
        base="https://api.minimax.io/anthropic/v1",
        key_env=("MINIMAX_API_KEY",),
        base_env=("MINIMAX_BASE_URL",),
        model_env=("MINIMAX_MODEL",),
    ),
    _p(
        "openai-codex",
        "OpenAI Codex OAuth",
        "codex_oauth",
        sdk=codex_auth.CODEX_PROVIDER_ID,
        base=codex_auth.DEFAULT_CODEX_BASE_URL,
        aliases=(codex_auth.CODEX_DEVICE_PROVIDER_ID, "codex"),
    ),
    _p(
        "openai",
        "OpenAI API",
        "openai",
        sdk="openai",
        key_env=("OPENAI_API_KEY",),
        base_env=("OPENAI_BASE_URL", "OPENAI_API_BASE"),
        model_env=("OPENAI_MODEL",),
    ),
    _p(
        "openai-completions",
        "Custom OpenAI-compatible endpoint",
        "custom_openai",
        sdk="openai",
        key_env=("OPENAI_API_KEY",),
        base_env=("OPENAI_BASE_URL",),
        model_env=("OPENAI_MODEL",),
        established=False,
    ),
    _p(
        "openai-responses",
        "Custom OpenAI Responses endpoint",
        "custom_responses",
        sdk="openai",
        key_env=("OPENAI_API_KEY",),
        base_env=("OPENAI_BASE_URL",),
        model_env=("OPENAI_MODEL",),
        established=False,
    ),
    _p(
        "anthropic",
        "Anthropic API",
        "anthropic",
        sdk="anthropic",
        key_env=("ANTHROPIC_API_KEY",),
        base_env=("ANTHROPIC_BASE_URL",),
        model_env=("ANTHROPIC_MODEL",),
    ),
    _p(
        "anthropic-custom",
        "Custom Anthropic-compatible endpoint",
        "custom_anthropic",
        sdk="anthropic",
        key_env=("ANTHROPIC_API_KEY",),
        base_env=("ANTHROPIC_BASE_URL",),
        model_env=("ANTHROPIC_MODEL",),
        established=False,
    ),
    _p(
        "omp",
        "Oh My Pi gateway",
        "openai_compatible",
        sdk="openai",
        key_env=("OMP_API_KEY", "OMP_GATEWAY_API_KEY"),
        base_env=("OMP_BASE_URL", "OMP_GATEWAY_BASE_URL"),
        model_env=("OMP_MODEL", "OMP_GATEWAY_MODEL"),
        requires_api_key=False,
    ),
    _p(
        "cliproxyapi",
        "CLIProxyAPI",
        "cliproxyapi",
        sdk="openai",
        key_env=("CLIPROXYAPI_API_KEY", "CLIPROXY_API_KEY"),
        base_env=("CLIPROXYAPI_BASE_URL", "CLIPROXY_BASE_URL"),
        model_env=("CLIPROXYAPI_MODEL", "CLIPROXY_MODEL"),
        established=False,
    ),
)

_BUILTIN_BY_ID: dict[str, ProviderDef] = {p.id: p for p in _BUILTINS}
_ALIAS: dict[str, str] = {}
for _provider in _BUILTINS:
    _ALIAS[_provider.id.lower()] = _provider.id
    for _alias in _provider.aliases:
        _ALIAS[_alias.lower()] = _provider.id


def _modelsdev_provider_supported(provider: modelsdotdev.Provider) -> bool:
    return provider.id in _PROVIDER_REGISTRY or provider.npm in _PROVIDER_REGISTRY


def _modelsdev_transport(provider: modelsdotdev.Provider) -> Transport:
    return _MODELSDEV_TRANSPORTS.get(provider.npm, "modelsdev")


def _modelsdev_provider_def(provider: modelsdotdev.Provider) -> ProviderDef:
    return _p(
        provider.id,
        provider.name,
        _modelsdev_transport(provider),
        sdk=provider.id,
        base=provider.api,
        key_env=tuple(str(name) for name in provider.env),
        requires_api_key=bool(provider.env),
    )


def _dynamic_login_providers() -> tuple[ProviderDef, ...]:
    from . import model_metadata

    model_metadata.ensure_fresh_catalog()
    rows: list[ProviderDef] = []
    for provider in modelsdotdev.iter_providers():
        if provider.id in _BUILTIN_BY_ID:
            continue
        if not _modelsdev_provider_supported(provider):
            continue
        rows.append(_modelsdev_provider_def(provider))
    return tuple(rows)


def all_providers() -> tuple[ProviderDef, ...]:
    return _BUILTINS


def login_providers() -> tuple[ProviderDef, ...]:
    return _BUILTINS + _dynamic_login_providers()


def normalize_provider_id(provider_id: str | None) -> str | None:
    if provider_id is None:
        return None
    key = provider_id.strip()
    if not key:
        return None
    lowered = key.lower()
    if lowered in _ALIAS:
        return _ALIAS[lowered]
    for provider in _dynamic_login_providers():
        if lowered == provider.id.lower():
            return provider.id
    return key


def get_provider(provider_id: str | None) -> ProviderDef | None:
    normalized = normalize_provider_id(provider_id)
    if normalized is None:
        return None
    provider = _BUILTIN_BY_ID.get(normalized)
    if provider is not None:
        return provider
    for candidate in _dynamic_login_providers():
        if candidate.id == normalized:
            return candidate
    return None


def known_provider_ids() -> set[str]:
    ids = set(_ALIAS)
    ids.update(provider.id for provider in login_providers())
    return ids


def parse_model_prefix(model: str | None) -> tuple[str | None, str | None]:
    """Return (provider_id, model_id) when model starts with a known provider id.

    Only the first slash segment is considered. Unknown slashy model ids are left
    untouched because OpenRouter, Hugging Face, and Ollama model ids commonly
    contain slashes that are part of the real model name.
    """

    if not model:
        return None, model
    if "/" not in model:
        return None, model
    first, rest = model.split("/", 1)
    provider_id = normalize_provider_id(first)
    if provider_id and get_provider(provider_id) is not None and rest:
        return provider_id, rest
    return None, model


def first_env(names: tuple[str, ...], env: Mapping[str, str] | None = None) -> str | None:
    source = os.environ if env is None else env
    for name in names:
        value = source.get(name)
        if value is not None and value != "":
            return value
    return None


def provider_base_url(provider: ProviderDef, explicit: str | None, env: Mapping[str, str] | None = None) -> str | None:
    if explicit:
        return explicit
    return first_env(provider.base_url_env, env) or provider.default_base_url


def provider_api_key(provider: ProviderDef, explicit: str | None, env: Mapping[str, str] | None = None) -> str | None:
    if explicit:
        return explicit
    return first_env(provider.api_key_env, env) or provider.default_api_key


def provider_model(provider: ProviderDef, explicit: str | None, env: Mapping[str, str] | None = None) -> str | None:
    if explicit:
        return explicit
    return first_env(provider.model_env, env) or provider.default_model


def assert_endpoint_configured(provider: ProviderDef, base_url: str | None) -> None:
    """Refuse to route a borrowed-SDK provider with no base URL.

    A provider whose ``effective_sdk_provider_id`` differs from its own ``id``
    (e.g. ``omp``/``cliproxyapi`` riding the OpenAI SDK, or the custom-endpoint
    providers) is only a thin shape over another vendor's SDK. With ``base_url``
    unset that SDK silently falls back to its *own* default endpoint and the
    matching environment credentials (``OPENAI_API_KEY`` against
    ``api.openai.com``, ``ANTHROPIC_API_KEY`` against ``api.anthropic.com``),
    sending the prompt — and that key — to the wrong service. Fail loudly so the
    misconfiguration is visible instead of leaking traffic.
    """
    if base_url:
        return
    sdk = provider.effective_sdk_provider_id
    if not sdk or sdk == provider.id:
        return
    hint = (
        f"set {provider.base_url_env[0]}"
        if provider.base_url_env
        else "configure its base URL"
    )
    raise ValueError(
        f"provider {provider.id!r} has no base URL configured; refusing to fall "
        f"back to the {sdk!r} SDK default endpoint and credentials. Run "
        f"`js --login {provider.id}` or {hint}."
    )


def discover_env_provider(env: Mapping[str, str] | None = None) -> ProviderDef | None:
    for provider in login_providers():
        if first_env(provider.api_key_env + provider.base_url_env + provider.model_env, env):
            return provider
    return None


def login_ids() -> list[str]:
    return [p.id for p in login_providers()]


def provider_for_login(provider_id: str) -> ProviderDef:
    normalized = normalize_provider_id(provider_id) or provider_id
    provider = get_provider(normalized)
    if provider is None:
        return _p(normalized, normalized, "custom_openai", sdk="openai", established=False)
    return provider


