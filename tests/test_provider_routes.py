"""What each provider route resolves to, at every place js asks.

Four answers say which wire a route speaks: ``wire_name``, the `-u` wire name
the CLI and the trace banner show (`routing.resolve_wire(...).name`),
`model_client.resolve_model` (the SDK provider and protocol a request is sent
with), ``route``, what `build_request_params` and `_stream_with_model` gate
on, and `history_record.history_to_ai_messages` (whether a replayed reply
keeps its reasoning). `fixtures/provider_routes.json` pins their answers for every
registered provider and for the routes no provider registers.
UPDATE_SNAPSHOTS=1 writes it from the current code.

Each derivation's answer is read as one wire kind: anthropic, responses,
chat (OpenAI chat completions) or the SDK id it names. `KNOWN_DISAGREEMENTS`
lists the routes where two derivations name different kinds.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import ai.types.messages
import pytest

from js import history_record, logins, model_client, providers, routing

SNAPSHOT = Path(__file__).parent / "fixtures" / "provider_routes.json"
BASE = "https://endpoint.example/v1"


def _builtin_rows() -> list[tuple[str, str | None, str | None, str]]:
    rows = []
    for provider in providers.all_providers():
        borrowed = provider.effective_sdk_provider_id != provider.id
        base = provider.default_base_url or (BASE if borrowed else None)
        rows.append((provider.id, provider.id, base, "test-model"))
    return rows


# (row name, provider id, base URL, model id)
ROWS: list[tuple[str, str | None, str | None, str]] = [
    *_builtin_rows(),
    ("openai, custom base URL", "openai", "http://127.0.0.1:9/v1", "test-model"),
    ("xai, custom base URL", "xai", "http://127.0.0.1:9/v1", "test-model"),
    ("openai-completions, hosted deepseek model", "openai-completions", BASE, "nvidia/deepseek-ai/deepseek-v4-pro"),
    ("no provider, base URL", None, BASE, "test-model"),
    ("no provider, base URL, deepseek model", None, BASE, "deepseek-v4-flash"),
    ("no provider, no base URL", None, None, "test-model"),
    ("unknown id", "nosuch", BASE, "test-model"),
    ("unknown id, mixed case", "NoSuch", BASE, "test-model"),
    ("unknown id, padded", " nosuch ", BASE, "test-model"),
    ("registered id, upper case", "OPENAI", None, "test-model"),
    ("registered id, title case", "Anthropic", None, "test-model"),
    ("registered id, mixed case", "DeepSeek", None, "test-model"),
    ("registered id, padded", " deepseek ", None, "test-model"),
    ("alias, mixed case", "Grok", "https://api.x.ai/v1", "test-model"),
    ("catalog, gateway transport", "vercel", None, "test-model"),
    ("catalog, openai transport", "meta", "https://api.meta.ai/v1", "test-model"),
    ("catalog, anthropic transport", "kimi-for-coding", "https://api.kimi.com/coding/v1", "test-model"),
    ("catalog, openai_compatible transport", "huggingface", "https://router.huggingface.co/v1", "test-model"),
    ("saved login, openai sdk", "mybox", BASE, "test-model"),
    ("saved login, anthropic sdk", "mybox-claude", BASE, "test-model"),
    ("saved login, openai-responses shape", "mybox-responses", BASE, "test-model"),
    ("saved login, cliproxyapi shape", "mybox-cpa", BASE, "test-model"),
    ("saved login, asked in another case", "MyBox", BASE, "test-model"),
    ("saved login, deepseek sdk with openai-responses shape", "mybox-mixed", BASE, "test-model"),
]

SAVED_LOGINS = [
    logins.Login(provider_id="mybox", sdk_provider_id="openai", provider_base_url=BASE),
    logins.Login(provider_id="mybox-claude", sdk_provider_id="anthropic", provider_base_url=BASE),
    logins.Login(provider_id="mybox-responses", sdk_provider_id="openai",
                 shape_provider_id="openai-responses", provider_base_url=BASE),
    logins.Login(provider_id="mybox-cpa", sdk_provider_id="openai",
                 shape_provider_id="cliproxyapi", provider_base_url=BASE),
    # The login flow pairs a shape with its own SDK; a hand-edited store need not.
    logins.Login(provider_id="mybox-mixed", sdk_provider_id="deepseek",
                 shape_provider_id="openai-responses", provider_base_url=BASE),
]


def _wire_name(provider_id: str | None) -> str:
    return routing.resolve_wire(provider_id).name


def _resolved(provider_id: str | None, base_url: str | None, model_id: str) -> dict | str:
    """The SDK provider id `resolve_model` asks the SDK for and the protocol
    it hands it, or the error it raises."""
    asked: list[str] = []
    get_provider = model_client.ai.get_provider

    def recording(sdk_id, *args, **kwargs):
        asked.append(sdk_id)
        return get_provider(sdk_id, *args, **kwargs)

    model_client.ai.get_provider = recording
    try:
        model = model_client.resolve_model(
            model_id, provider_id=provider_id, provider_base_url=base_url, provider_api_key="key")
    except Exception as exc:  # noqa: BLE001 - the error is the answer for this route
        return f"error: {type(exc).__name__}"
    finally:
        model_client.ai.get_provider = get_provider
    protocol = model.provider.protocol_override
    out: dict = {"sdk": asked[-1], "protocol": type(protocol).__name__ if protocol is not None else None}
    for flag in ("reasoning_blocks", "explicit_prompt_cache"):
        if hasattr(protocol, flag):
            out[flag] = getattr(protocol, flag)
    return out


def _route(provider_id: str | None, base_url: str | None, model_id: str) -> dict:
    """What a request's parameters and messages are built on."""
    wire = routing.resolve_wire(provider_id, base_url=base_url, model_id=model_id)
    return {"provider_name": wire.provider_name, "sdk_provider_name": wire.sdk_provider_name,
            "transport": wire.transport, "explicit_provider": wire.explicit_provider,
            "anthropic_wire": wire.anthropic, "responses_wire": wire.responses,
            "chat_completions_wire": wire.chat_completions,
            "deepseek_wire": wire.deepseek, "minimax": wire.minimax}


def _keeps_reasoning(provider_id: str | None, model_id: str) -> bool:
    record = {"role": "assistant", "content": "answer", "reasoning_content": "thought"}
    [message] = history_record.history_to_ai_messages("", [record], provider_id=provider_id, model_id=model_id)
    return any(isinstance(part, ai.types.messages.ReasoningPart) for part in message.parts)


def _observe(provider_id: str | None, base_url: str | None, model_id: str) -> dict:
    return {
        "wire_name": _wire_name(provider_id),
        "resolve_model": _resolved(provider_id, base_url, model_id),
        "route": _route(provider_id, base_url, model_id),
        "decoder_keeps_reasoning": _keeps_reasoning(provider_id, model_id),
    }


def _kinds(observed: dict) -> dict[str, str]:
    """Each derivation's answer as a wire kind. The decoder only tells chat
    completions from the rest; the send-time reasoning strip in
    `_stream_with_model` reads chat completions off the route."""
    wire = observed["wire_name"]
    kinds = {"wire_name": {"openai": "chat"}.get(wire, wire)}
    resolved = observed["resolve_model"]
    if isinstance(resolved, dict):
        kinds["resolve_model"] = {
            "_AnthropicThinkingProtocol": "anthropic",
            "_ResponsesProtocol": "responses",
            "_ReasoningContentProtocol": "chat",
            "OpenAIChatCompletionsProtocol": "chat",
        }.get(resolved["protocol"], resolved["sdk"])
    route = observed["route"]
    kinds["route"] = ("anthropic" if route["anthropic_wire"]
                      else "responses" if route["responses_wire"]
                      else "chat" if route["chat_completions_wire"]
                      else route["sdk_provider_name"])
    kinds["decoder"] = "chat" if observed["decoder_keeps_reasoning"] else "not chat"
    return kinds


def _disagreement(kinds: dict[str, str]) -> dict[str, str] | None:
    named = {site: kind for site, kind in kinds.items() if site != "decoder"}
    chat = {kind == "chat" for kind in named.values()}
    if len(set(named.values())) > 1 or chat != {kinds["decoder"] == "chat"}:
        return kinds
    return None


KNOWN_DISAGREEMENTS: dict[str, str] = {}


@pytest.fixture
def saved_logins():
    for login in SAVED_LOGINS:
        logins.save_login(login)


def test_every_route_resolves_as_the_snapshot_shows(saved_logins):
    observed = {name: _observe(provider_id, base_url, model_id) for name, provider_id, base_url, model_id in ROWS}
    if os.environ.get("UPDATE_SNAPSHOTS"):
        SNAPSHOT.write_text(json.dumps(observed, indent=1, sort_keys=True) + "\n")
    assert observed == json.loads(SNAPSHOT.read_text())


def test_the_derivations_disagree_only_where_known(saved_logins):
    found = {}
    for name, provider_id, base_url, model_id in ROWS:
        disagreement = _disagreement(_kinds(_observe(provider_id, base_url, model_id)))
        if disagreement is not None:
            found[name] = disagreement
    assert set(found) == set(KNOWN_DISAGREEMENTS), found
