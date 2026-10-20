"""One-string endpoint specs for ``js -u``.

    model[:api-key][[shape]][[effort]]@endpoint

The point is to reach an endpoint that has no saved login without spelling out
four settings. These are the same:

    JS_PROVIDER=openai-completions JS_BASE_URL=http://foo/v1 \\
      JS_API_KEY=sk-no-key JS_MODEL=qwen27b js -p ...

    js -u qwen27b:sk-no-key@http://foo/v1 -p ...

Shape tags pick the wire, defaulting to OpenAI chat-completions:

    openai | completions -> openai-completions   (default)
    responses            -> openai-responses
    anthropic            -> anthropic-custom

Effort tags are the usual ladder: off minimal low medium high xhigh max.
Both tags are optional and order between them does not matter:

    qwen27b@http://foo/v1
    claude-sonnet-5:sk-foo-bar-baz[anthropic][max]@http://localhost:8317
    gpt-6-astra[responses]@https://gw.example/v1
"""

from __future__ import annotations

from . import messages as msgs

from . import settings as _settings

import re
from dataclasses import dataclass

# An endpoint reached this way is usually a local box that ignores the key, but
# most OpenAI-shaped servers still require the header to exist.
DUMMY_KEY = _settings.default_value("ENDPOINT_DEFAULT_KEY")

_SHAPES = {
    "openai": "openai-completions",
    "completions": "openai-completions",
    "responses": "openai-responses",
    "anthropic": "anthropic-custom",
}

_EFFORTS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")

_TRAILING_TAG = re.compile(r"\[([^\[\]]*)\]\s*$")


class EndpointSpecError(ValueError):
    """A -u spec that could not be parsed, phrased for a terminal."""


@dataclass(frozen=True)
class EndpointSpec:
    model: str
    api_key: str
    base_url: str
    provider_id: str
    reasoning_effort: str | None

    def as_settings(self) -> list[str]:
        """``key=value`` pairs in the form ``--extra`` already accepts."""
        pairs = [
            f"MODEL={self.model}",
            f"PROVIDER={self.provider_id}",
            f"BASE_URL={self.base_url}",
            f"API_KEY={self.api_key}",
        ]
        if self.reasoning_effort is not None:
            pairs.append(f"EFFORT={self.reasoning_effort}")
        return pairs


def parse(spec: str, *, settings: dict | None = None) -> EndpointSpec:
    text = (spec or "").strip()
    if not text:
        raise EndpointSpecError(msgs.ENDPOINT_SPEC_EMPTY_ENDPOINT_SPEC_EXPECTED_MODEL_KEY_SHAPE.format())

    # Split at the @ that introduces the URL. Prefer one followed by a scheme so
    # a key containing @ does not steal the split; fall back to the last @.
    match = re.search(r"@(?=[a-zA-Z][a-zA-Z0-9+.-]*://)", text)
    at = match.start() if match else text.rfind("@")
    if at < 0:
        raise EndpointSpecError(
            msgs.ENDPOINT_SPEC_NO_ENDPOINT_IN_EXPECTED_SOMETHING_LIKE_QWEN.format(spec=spec)
        )
    left, base_url = text[:at], text[at + 1:].strip()
    if not base_url:
        raise EndpointSpecError(msgs.ENDPOINT_SPEC_NO_ENDPOINT_AFTER_IN.format(spec=spec))
    if "://" not in base_url:
        raise EndpointSpecError(
            msgs.ENDPOINT_SPEC_ENDPOINT_HAS_NO_SCHEME_WRITE_HTTP_OR.format(base_url=base_url)
        )

    # Trailing [tag] groups, innermost last. Collected before the key split so a
    # key is never confused with a tag.
    shape: str | None = None
    effort: str | None = None
    while True:
        tag_match = _TRAILING_TAG.search(left)
        if tag_match is None:
            break
        tag = tag_match.group(1).strip().lower()
        left = left[: tag_match.start()].rstrip()
        if not tag:
            raise EndpointSpecError(msgs.ENDPOINT_SPEC_EMPTY_IN.format(spec=spec))
        if tag in _SHAPES:
            if shape is not None:
                raise EndpointSpecError(msgs.ENDPOINT_SPEC_TWO_WIRE_SHAPES_IN_AND_ONE_MORE.format(spec=spec, tag=tag))
            shape = tag
        elif tag in _EFFORTS:
            if effort is not None:
                raise EndpointSpecError(msgs.ENDPOINT_SPEC_TWO_REASONING_EFFORTS_IN_AND_ONE_MORE.format(spec=spec, tag=tag))
            effort = tag
        else:
            raise EndpointSpecError(
                msgs.ENDPOINT_SPEC_UNKNOWN_TAG_IN_WIRE_SHAPES_ARE_EFFORTS.format(tag=tag, spec=spec, value=', '.join(sorted(set(_SHAPES))), value2=', '.join(_EFFORTS))
            )

    if not left:
        raise EndpointSpecError(msgs.ENDPOINT_SPEC_NO_MODEL_ID_IN.format(spec=spec))

    # Last colon splits the key off. Last, not first, so an ollama-style tagged
    # id keeps its tag: `qwen3:27b:sk-foo` is model `qwen3:27b`, key `sk-foo`.
    # A tagged id with no key is genuinely ambiguous — pass a key explicitly.
    if ":" in left:
        model, _, api_key = left.rpartition(":")
        model, api_key = model.strip(), api_key.strip()
        if not api_key:
            raise EndpointSpecError(msgs.ENDPOINT_SPEC_EMPTY_API_KEY_AFTER_IN.format(spec=spec))
        if not model:
            raise EndpointSpecError(msgs.ENDPOINT_SPEC_NO_MODEL_ID_BEFORE_IN.format(spec=spec))
    else:
        model, api_key = left, _settings.knob(settings, "ENDPOINT_DEFAULT_KEY")

    return EndpointSpec(
        model=model,
        api_key=api_key,
        base_url=base_url,
        provider_id=_SHAPES[shape] if shape else _settings.knob(settings, "ENDPOINT_DEFAULT_SHAPE"),
        reasoning_effort=effort,
    )
