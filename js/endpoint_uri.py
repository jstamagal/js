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

import re
from dataclasses import dataclass

# An endpoint reached this way is usually a local box that ignores the key, but
# most OpenAI-shaped servers still require the header to exist.
DUMMY_KEY = "sk-dummy-key"

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
            f"model.id={self.model}",
            f"provider.id={self.provider_id}",
            f"provider.base_url={self.base_url}",
            f"provider.api_key={self.api_key}",
        ]
        if self.reasoning_effort is not None:
            pairs.append(f"model.reasoning_effort={self.reasoning_effort}")
        return pairs


def parse(spec: str) -> EndpointSpec:
    text = (spec or "").strip()
    if not text:
        raise EndpointSpecError("empty endpoint spec; expected model[:key][[shape]][[effort]]@url")

    # Split at the @ that introduces the URL. Prefer one followed by a scheme so
    # a key containing @ does not steal the split; fall back to the last @.
    match = re.search(r"@(?=[a-zA-Z][a-zA-Z0-9+.-]*://)", text)
    at = match.start() if match else text.rfind("@")
    if at < 0:
        raise EndpointSpecError(
            f"no @endpoint in {spec!r}; expected something like "
            "qwen27b@http://host:8000/v1"
        )
    left, base_url = text[:at], text[at + 1:].strip()
    if not base_url:
        raise EndpointSpecError(f"no endpoint after @ in {spec!r}")
    if "://" not in base_url:
        raise EndpointSpecError(
            f"endpoint {base_url!r} has no scheme; write http:// or https://"
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
            raise EndpointSpecError(f"empty [] in {spec!r}")
        if tag in _SHAPES:
            if shape is not None:
                raise EndpointSpecError(f"two wire shapes in {spec!r}: [{tag}] and one more")
            shape = tag
        elif tag in _EFFORTS:
            if effort is not None:
                raise EndpointSpecError(f"two reasoning efforts in {spec!r}: [{tag}] and one more")
            effort = tag
        else:
            raise EndpointSpecError(
                f"unknown tag [{tag}] in {spec!r}; wire shapes are "
                f"{', '.join(sorted(set(_SHAPES)))}; efforts are {', '.join(_EFFORTS)}"
            )

    if not left:
        raise EndpointSpecError(f"no model id in {spec!r}")

    # Last colon splits the key off. Last, not first, so an ollama-style tagged
    # id keeps its tag: `qwen3:27b:sk-foo` is model `qwen3:27b`, key `sk-foo`.
    # A tagged id with no key is genuinely ambiguous — pass a key explicitly.
    if ":" in left:
        model, _, api_key = left.rpartition(":")
        model, api_key = model.strip(), api_key.strip()
        if not api_key:
            raise EndpointSpecError(f"empty api key after ':' in {spec!r}")
        if not model:
            raise EndpointSpecError(f"no model id before ':' in {spec!r}")
    else:
        model, api_key = left, DUMMY_KEY

    return EndpointSpec(
        model=model,
        api_key=api_key,
        base_url=base_url,
        provider_id=_SHAPES[shape or "openai"],
        reasoning_effort=effort,
    )
