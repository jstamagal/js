"""Endpoint URLs for ``js -u`` and ``JS_BASE_URL``.

    [wire+]scheme://model[:key]@host[:port]/path

    js -u http://qwen3:27b@box:11434/v1
    js -u http://qwen3.8-27b:sk-foo-bar@box/v1
    js -u https://nex-agi/nex-n2.5-pro:free:sk-or-xxxx@openrouter.ai/api/v1
    js -u anthropic+https://deepseek-v4-pro:sk-xxxx@api.deepseek.com/anthropic

Wire: an explicit prefix wins (``openai+`` chat-completions, ``responses+``,
``anthropic+``). With none, a path ending in ``/v1`` is OpenAI
chat-completions and anything else is Anthropic, the way each SDK defines its
base URL. The prefix is stripped; the rest, minus the model and key, is the
base URL.

Model and key: everything between ``://`` and the last ``@``. The text after
the last colon is the key when the shared secret detector
(``js/secret_patterns.py``) calls it a secret; otherwise it is part of the
model, so ``:free``, ``:27b`` and ``:Q4_K_M`` stay in the model id. With no
key, the key is ``JS_API_KEY`` when set, else ``ENDPOINT_DEFAULT_KEY``. With
no ``@``, the model is ``JS_MODEL``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import messages as msgs
from . import secret_patterns
from . import settings as _settings

# An endpoint reached this way is usually a local box that ignores the key, but
# most OpenAI-shaped servers still require the header to exist.
DUMMY_KEY = _settings.default_value("ENDPOINT_DEFAULT_KEY")

WIRES = {
    "openai": "openai-completions",
    "responses": "openai-responses",
    "anthropic": "anthropic-custom",
}

_SCHEMES = ("http", "https")


class EndpointSpecError(ValueError):
    """An endpoint URL js cannot read one exact way, phrased for a terminal."""


@dataclass(frozen=True)
class EndpointURL:
    """What the URL itself says: each field None when the URL leaves it out."""

    wire: str | None
    model: str | None
    api_key: str | None
    base_url: str

    @property
    def provider_id(self) -> str:
        if self.wire is not None:
            return WIRES[self.wire]
        path = urlsplit(self.base_url).path.rstrip("/")
        return WIRES["openai"] if path.endswith("/v1") else WIRES["anthropic"]


@dataclass(frozen=True)
class EndpointSpec:
    model: str
    api_key: str
    base_url: str
    provider_id: str

    def as_settings(self) -> list[str]:
        """``key=value`` pairs in the form ``--extra`` already accepts."""
        return [
            f"MODEL={self.model}",
            f"PROVIDER={self.provider_id}",
            f"BASE_URL={self.base_url}",
            f"API_KEY={self.api_key}",
        ]


def shown(text: str) -> str:
    """``text`` for an error message: the part after the last colon before the
    last ``@`` reads ``***``, so a key never reaches the terminal."""
    head, sep, rest = text.partition("://")
    at = rest.rfind("@")
    if not sep or at < 0 or ":" not in rest[:at]:
        return text
    model, _, _key = rest[:at].rpartition(":")
    return f"{head}://{model}:***{rest[at:]}"


def split(text: str) -> EndpointURL:
    """Read ``[wire+]scheme://model[:key]@host...`` into its parts."""
    text = (text or "").strip()
    spec = shown(text)
    if not text:
        raise EndpointSpecError(msgs.ENDPOINT_EMPTY.format())
    scheme, sep, rest = text.partition("://")
    userinfo = rest.rsplit("@", 1)[0] if "@" in rest else ""
    if "[" in scheme or "[" in userinfo:
        raise EndpointSpecError(msgs.ENDPOINT_BRACKET.format(spec=spec))
    if not sep:
        raise EndpointSpecError(msgs.ENDPOINT_NO_SCHEME.format(spec=spec))
    wire = None
    if "+" in scheme:
        wire, _, scheme = scheme.partition("+")
        if wire not in WIRES:
            raise EndpointSpecError(
                msgs.ENDPOINT_UNKNOWN_WIRE.format(wire=wire, wires=", ".join(f"{w}+" for w in WIRES)))
    scheme = scheme.lower()
    if scheme not in _SCHEMES:
        raise EndpointSpecError(msgs.ENDPOINT_UNKNOWN_SCHEME.format(scheme=scheme, spec=spec))

    model = api_key = None
    at = rest.rfind("@")
    if at >= 0:
        userinfo, rest = rest[:at], rest[at + 1:]
        model = userinfo
        head, colon, tail = userinfo.rpartition(":")
        if colon and not tail:
            raise EndpointSpecError(msgs.ENDPOINT_EMPTY_AFTER_COLON.format(spec=spec))
        if colon and secret_patterns.is_secret(tail):
            model, api_key = head, tail
        if not model:
            raise EndpointSpecError(msgs.ENDPOINT_NO_MODEL_BEFORE_AT.format(spec=spec))
    if not rest or rest.startswith("/"):
        raise EndpointSpecError(msgs.ENDPOINT_NO_HOST.format(spec=spec))
    return EndpointURL(wire=wire, model=model, api_key=api_key, base_url=f"{scheme}://{rest}")


def is_endpoint_form(text: str) -> bool:
    """Whether a ``JS_BASE_URL`` value carries a wire prefix or a model."""
    scheme, sep, rest = (text or "").strip().partition("://")
    return bool(sep) and ("+" in scheme or "@" in rest)


def _env(env: Mapping[str, str], key: str) -> str | None:
    for name in _settings.env_names_for(_settings.SPEC_BY_KEY[key]):
        if env.get(name):
            return env[name]
    return None


def parse(spec: str, *, settings: dict | None = None, env: Mapping[str, str] | None = None) -> EndpointSpec:
    """The ``-u`` spec as the four settings it sets. The flag beats
    ``JS_MODEL`` and ``JS_API_KEY``; each fills only what the URL leaves out."""
    env = {} if env is None else env
    url = split(spec)
    model = url.model or _env(env, "MODEL")
    if not model:
        raise EndpointSpecError(msgs.ENDPOINT_NO_MODEL.format(spec=shown(spec.strip())))
    api_key = url.api_key or _env(env, "API_KEY") or _settings.knob(settings, "ENDPOINT_DEFAULT_KEY")
    return EndpointSpec(model=model, api_key=api_key, base_url=url.base_url, provider_id=url.provider_id)


def from_env(env: Mapping[str, str], *, settings: dict | None = None) -> EndpointSpec | None:
    """``JS_BASE_URL`` in the ``-u`` form as the four settings it sets; None
    for a plain base URL. A model, key or provider in another JS_ variable
    that contradicts the URL is an error naming both."""
    raw = env.get("JS_BASE_URL")
    if not raw or not is_endpoint_form(raw):
        return None
    url = split(raw)
    env_model, env_key = _env(env, "MODEL"), _env(env, "API_KEY")
    env_provider = _env(env, "PROVIDER")
    if url.model and env_model and url.model != env_model:
        raise EndpointSpecError(msgs.ENDPOINT_ENV_MODEL_DIFFERS.format(url=url.model, env=env_model))
    if url.api_key and env_key and url.api_key != env_key:
        raise EndpointSpecError(msgs.ENDPOINT_ENV_KEY_DIFFERS.format())
    if url.wire and env_provider and env_provider != url.provider_id:
        raise EndpointSpecError(
            msgs.ENDPOINT_ENV_PROVIDER_DIFFERS.format(wire=url.wire, provider=url.provider_id, env=env_provider))
    model = url.model or env_model
    if not model:
        raise EndpointSpecError(msgs.ENDPOINT_ENV_NO_MODEL.format())
    provider = url.provider_id if url.wire or not env_provider else env_provider
    api_key = url.api_key or env_key or _settings.knob(settings, "ENDPOINT_DEFAULT_KEY")
    return EndpointSpec(model=model, api_key=api_key, base_url=url.base_url, provider_id=provider)
