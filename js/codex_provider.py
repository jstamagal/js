"""OpenAI Codex Responses provider for ai-python 0.2.0.

ai-python does not currently ship a native Codex/ChatGPT-OAuth provider, so js
carries the small provider boundary it needs: OAuth bearer + account headers,
Codex model listing, Responses-style streaming, and function tool calls.
"""

from __future__ import annotations

import base64
import json
import platform
import time
from collections.abc import AsyncGenerator, Mapping, Sequence
from typing import TYPE_CHECKING, Any

import ai
import httpx
import pydantic
from ai.models.core import params as ai_params

from . import codex_auth

if TYPE_CHECKING:
    from .logins import Login

_MODEL_PATHS = ("/codex/models", "/models")
_CLIENT_VERSION = "0.99.0"
_ORIGINATOR = "pi"
_USER_AGENT = f"js/0.1.0 ({platform.system().lower()} {platform.release()}; {platform.machine()})"
_PROVIDER = codex_auth.CODEX_PROVIDER_ID
_INTERRUPTED_TOOL_OUTPUT = "[No tool output recorded: the tool call was interrupted before it produced a result.]"


def _trim_base_url(base_url: str | None) -> str:
    raw = (base_url or codex_auth.DEFAULT_CODEX_BASE_URL).strip() or codex_auth.DEFAULT_CODEX_BASE_URL
    return raw.rstrip("/")


def responses_url(base_url: str | None) -> str:
    base = _trim_base_url(base_url)
    if base.endswith("/codex/responses"):
        return base
    if base.endswith("/codex"):
        return f"{base}/responses"
    return f"{base}/codex/responses"


def _account_id_from_token(access_token: str, account_id: str | None = None) -> str:
    if account_id:
        return account_id
    decoded_account_id, _email = codex_auth.token_profile(access_token)
    if not decoded_account_id:
        raise ai.ConfigurationError("OpenAI Codex access token is missing chatgpt_account_id")
    return decoded_account_id


def _codex_headers(access_token: str, account_id: str, *, stream: bool) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": _ORIGINATOR,
        "User-Agent": _USER_AGENT,
    }
    if stream:
        headers["accept"] = "text/event-stream"
        headers["content-type"] = "application/json"
    else:
        headers["accept"] = "application/json"
    return headers


def _response_body(response: httpx.Response) -> object | None:
    try:
        return response.json()
    except Exception:  # noqa: BLE001
        try:
            return response.text
        except Exception:  # noqa: BLE001
            return None


def _error_message(body: object | None, fallback: str) -> tuple[str, str | None, str | None]:
    code: str | None = None
    error_type: str | None = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            raw_message = error.get("message") or error.get("error_description") or error.get("code")
            raw_code = error.get("code")
            raw_type = error.get("type")
            if isinstance(raw_code, str):
                code = raw_code
            if isinstance(raw_type, str):
                error_type = raw_type
            if isinstance(raw_message, str) and raw_message:
                return raw_message, code, error_type
        elif isinstance(error, str):
            code = error
            desc = body.get("error_description")
            if isinstance(desc, str) and desc:
                return f"{error}: {desc}", code, error_type
            return error, code, error_type
        raw_message = body.get("message") or body.get("detail")
        if isinstance(raw_message, str) and raw_message:
            return raw_message, code, error_type
    return fallback, code, error_type


def _raise_status(response: httpx.Response, *, model_id: str | None = None) -> None:
    if response.status_code < 400:
        return
    body = _response_body(response)
    target = f" for model {model_id!r}" if model_id else ""
    fallback = f"OpenAI Codex request failed with HTTP {response.status_code}{target}"
    message, code, error_type = _error_message(body, fallback)
    if model_id and model_id not in message:
        message = f"{message}{target}"
    cls = ai.errors.http_status_to_provider_status_error_class(response.status_code)
    context = ai.errors.HTTPErrorContext(
        status_code=response.status_code,
        request=response.request,
        response=response,
    )
    kwargs = {
        "provider": _PROVIDER,
        "http_context": context,
        "body": body,
        "code": code,
        "error_type": error_type,
    }
    if cls is ai.ProviderNotFoundError and model_id:
        raise ai.ProviderModelNotFoundError(message, model_id=model_id, **kwargs)
    raise cls(message, **kwargs)


async def _aiter_sse_json(response: httpx.Response) -> AsyncGenerator[dict[str, Any]]:
    event_name = "message"
    data_lines: list[str] = []

    async def emit() -> dict[str, Any] | None:
        nonlocal event_name, data_lines
        if not data_lines:
            event_name = "message"
            return None
        raw_data = "\n".join(data_lines)
        event = event_name
        event_name = "message"
        data_lines = []
        if raw_data.strip() == "[DONE]":
            return {"type": "response.done"}
        try:
            payload = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            raise ai.ProviderResponseError(
                f"OpenAI Codex returned malformed SSE JSON: {exc}",
                provider=_PROVIDER,
            ) from exc
        if isinstance(payload, dict):
            payload.setdefault("type", event)
            return payload
        raise ai.ProviderResponseError("OpenAI Codex returned non-object SSE payload", provider=_PROVIDER)

    async for line in response.aiter_lines():
        if line == "":
            item = await emit()
            if item is not None:
                yield item
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip() or "message"
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            continue
    item = await emit()
    if item is not None:
        yield item


def _usage_from_response(response: Mapping[str, Any] | None) -> ai.types.usage.Usage | None:
    usage = response.get("usage") if isinstance(response, Mapping) else None
    if not isinstance(usage, Mapping):
        return None
    input_details = usage.get("input_tokens_details")
    output_details = usage.get("output_tokens_details")
    cached = input_details.get("cached_tokens") if isinstance(input_details, Mapping) else None
    reasoning = output_details.get("reasoning_tokens") if isinstance(output_details, Mapping) else None
    return ai.types.usage.Usage(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        reasoning_tokens=int(reasoning) if isinstance(reasoning, int) else None,
        cache_read_tokens=int(cached) if isinstance(cached, int) else None,
        raw=dict(usage),
    )


def _text_from_parts(parts: list[Any]) -> str:
    return "".join(p.text for p in parts if isinstance(p, ai.types.messages.TextPart))


async def _file_part_to_input(part: ai.types.messages.FilePart) -> dict[str, Any] | None:
    media_type = part.media_type
    data = part.data
    if media_type.startswith("image/"):
        return {
            "type": "input_image",
            "image_url": ai.types.media.data_to_data_url(data, media_type),
            "detail": "auto",
        }
    if media_type.startswith("text/"):
        if isinstance(data, bytes):
            text = data.decode("utf-8", errors="replace")
        elif ai.types.media.is_url(data):
            text = data
        else:
            try:
                text = base64.b64decode(data).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                text = str(data)
        return {"type": "input_text", "text": text}
    return {"type": "input_text", "text": f"[Unsupported file for Codex: {media_type}]"}


async def _user_content(parts: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for part in parts:
        if isinstance(part, ai.types.messages.TextPart):
            if part.text.strip():
                out.append({"type": "input_text", "text": part.text})
        elif isinstance(part, ai.types.messages.FilePart):
            item = await _file_part_to_input(part)
            if item is not None:
                out.append(item)
    return out


def _normalize_tool_call_id(tool_call_id: str) -> str:
    if "|" not in tool_call_id:
        return tool_call_id
    call_id, item_id = tool_call_id.split("|", 1)
    return call_id or item_id or tool_call_id


def _repair_tool_pairs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    calls = {i.get("call_id") for i in items if i.get("type") in {"function_call", "custom_tool_call"}}
    outputs = {i.get("call_id") for i in items if i.get("type") in {"function_call_output", "custom_tool_call_output"}}
    repaired: list[dict[str, Any]] = []
    for item in items:
        kind = item.get("type")
        call_id = item.get("call_id")
        if kind in {"function_call_output", "custom_tool_call_output"} and call_id not in calls:
            output = item.get("output", "")
            repaired.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": f"[Previous tool result; call_id={call_id}]: {output}"}],
                }
            )
            continue
        repaired.append(item)
        if kind in {"function_call", "custom_tool_call"} and call_id not in outputs:
            repaired.append(
                {
                    "type": "custom_tool_call_output" if kind == "custom_tool_call" else "function_call_output",
                    "call_id": call_id,
                    "output": _INTERRUPTED_TOOL_OUTPUT,
                }
            )
    return repaired


async def _messages_to_codex(messages: list[ai.messages.Message]) -> tuple[str | None, list[dict[str, Any]]]:
    instructions: str | None = None
    items: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "system":
            text = _text_from_parts(msg.parts)
            if not text.strip():
                continue
            if instructions is None:
                instructions = text
            else:
                items.append({"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]})
            continue
        if msg.role == "user":
            content = await _user_content(msg.parts)
            if content:
                items.append({"type": "message", "role": "user", "content": content})
            continue
        if msg.role == "assistant":
            text = _text_from_parts(msg.parts)
            if text.strip():
                items.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
            for part in msg.parts:
                if isinstance(part, ai.types.messages.ToolCallPart):
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": _normalize_tool_call_id(part.tool_call_id),
                            "name": part.tool_name,
                            "arguments": part.tool_args or "{}",
                        }
                    )
            continue
        if msg.role == "tool":
            for part in msg.parts:
                if isinstance(part, ai.types.messages.ToolResultPart):
                    model_input = part.get_model_input()
                    output = "" if model_input is None else str(model_input)
                    items.append(
                        {
                            "type": "function_call_output",
                            "call_id": _normalize_tool_call_id(part.tool_call_id),
                            "output": output,
                        }
                    )
            continue
        if msg.role == "internal":
            continue
        raise ValueError(f"unsupported Codex message role: {msg.role!r}")
    return instructions, _repair_tool_pairs(items)


def _tools_to_codex(tools: Sequence[ai.types.tools.Tool] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    out: list[dict[str, Any]] = []
    for tool in tools:
        if tool.kind == "provider":
            continue
        spec = tool.spec
        if not isinstance(spec, ai.types.tools.ToolSpec):
            raise TypeError(f"function tool {tool.name!r} has invalid spec")
        out.append(
            {
                "type": "function",
                "name": tool.name,
                "description": spec.description or "",
                "parameters": spec.params or {"type": "object"},
            }
        )
    return out or None



def _effort_from_params(params: ai_params.InferenceRequestParams | None) -> str | None:
    if params is None:
        return None
    reasoning = params.reasoning
    if not isinstance(reasoning, ai_params.ReasoningParams):
        return None
    effort = reasoning.effort
    if effort is None or isinstance(effort, ai_params.ModelProviderDefault):
        return None
    return effort


def _sampling_from_params(
    params: ai_params.InferenceRequestParams | None,
) -> tuple[float | None, float | None]:
    if params is None:
        return None, None
    sampling = params.sampling
    if isinstance(sampling, ai_params.ModelProviderDefault):
        return None, None
    temp = sampling.get(ai_params.TemperatureSamplerParams)
    top_p = sampling.get(ai_params.TopPSamplerParams)
    return (temp.temperature if temp is not None else None), (top_p.top_p if top_p is not None else None)


async def _build_body_async(
    model: ai.Model,
    messages: list[ai.messages.Message],
    tools: Sequence[ai.types.tools.Tool] | None,
    params: ai_params.InferenceRequestParams | None,
) -> dict[str, Any]:
    instructions, input_items = await _messages_to_codex(messages)
    body: dict[str, Any] = {
        "model": model.id,
        "input": input_items,
        "stream": True,
        "store": False,
        "text": {"verbosity": "low"},
        "include": ["reasoning.encrypted_content"],
    }
    if instructions:
        body["instructions"] = instructions
    codex_tools = _tools_to_codex(tools)
    if codex_tools:
        body["tools"] = codex_tools
    effort = _effort_from_params(params)
    if effort is not None:
        body["reasoning"] = {"effort": effort, "summary": "detailed"}
    # Codex rejects caller-supplied output caps; forward only the sampling knobs
    # the codex transport admitted into params.sampling.
    temperature, top_p = _sampling_from_params(params)
    if temperature is not None:
        body["temperature"] = temperature
    if top_p is not None:
        body["top_p"] = top_p
    return body


class OpenAICodexProvider(ai.providers.Provider[httpx.AsyncClient]):
    """Provider backed by ChatGPT/Codex OAuth and the Codex Responses endpoint.

    ai>=0.2.1 made ``Provider`` a frozen pydantic model: identity and static
    config are fields, the upstream client is a private attr installed via
    ``_set_client``, and ``base_url``/``api_key`` are read-only properties.
    Codex's mutable OAuth state — the rotating access token, refresh token, and
    account id — lives in private attrs so a refresh can update it in place.
    """

    provider_class_id: str = "openai-codex"
    name: str = _PROVIDER
    default_base_url: str = codex_auth.DEFAULT_CODEX_BASE_URL

    _access_token: str = pydantic.PrivateAttr(default="")
    _refresh_token: str | None = pydantic.PrivateAttr(default=None)
    _expires_at: float | None = pydantic.PrivateAttr(default=None)
    _account_id: str = pydantic.PrivateAttr(default="")
    _login: Login | None = pydantic.PrivateAttr(default=None)
    _owns_client: bool = pydantic.PrivateAttr(default=True)

    def __init__(
        self,
        *,
        access_token: str,
        refresh_token: str | None = None,
        expires_at: float | None = None,
        account_id: str | None = None,
        base_url: str | None = None,
        login: Login | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not access_token:
            raise ai.ConfigurationError("OpenAI Codex requires an OAuth access token; run js --login openai-codex")
        resolved_account_id = _account_id_from_token(access_token, account_id)
        super().__init__(
            name=_PROVIDER,
            default_base_url=_trim_base_url(base_url),
            api_key_value=access_token,
        )
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._expires_at = expires_at
        self._account_id = resolved_account_id
        self._login = login
        self._owns_client = client is None
        self._set_client(client or httpx.AsyncClient(timeout=None))

    @classmethod
    def from_login(cls, login: Login) -> OpenAICodexProvider:
        return cls(
            access_token=login.provider_api_key or "",
            refresh_token=login.codex_refresh_token,
            expires_at=login.codex_token_expires,
            account_id=login.codex_account_id,
            base_url=login.provider_base_url,
            login=login,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _ensure_access(self) -> tuple[str, str]:
        if self._refresh_token and (self._expires_at is None or time.time() >= float(self._expires_at) - 60.0):
            previous = codex_auth.CodexToken(
                access=self._access_token,
                refresh=self._refresh_token,
                expires_at=float(self._expires_at) if self._expires_at is not None else 0.0,
                account_id=self._account_id,
                email=self._login.codex_email if self._login is not None else None,
            )
            token = await codex_auth.refresh_token_async(self._refresh_token, client=self.client, previous=previous)
            self._access_token = token.access
            self._refresh_token = token.refresh
            self._expires_at = token.expires_at
            self._account_id = token.account_id or _account_id_from_token(token.access)
            if self._login is not None:
                # apply_refreshed_token() rotates only the token-derived fields
                # onto the EXISTING login — replace()ing login_from_token(token)
                # here used to rebuild a bare Login and silently reset
                # provider_headers (and any other field) to empty on every
                # ~hourly refresh.
                refreshed = codex_auth.apply_refreshed_token(self._login, token)
                codex_auth.save_refreshed_login(refreshed)
                self._login = refreshed
        return self._access_token, self._account_id

    async def list_models(self) -> list[str]:
        access, account_id = await self._ensure_access()
        base = _trim_base_url(self.base_url)
        headers = _codex_headers(access, account_id, stream=False)
        errors: list[str] = []
        for path in _MODEL_PATHS:
            response = await self.client.get(
                f"{base}{path}",
                params={"client_version": _CLIENT_VERSION},
                headers=headers,
            )
            if response.status_code >= 400:
                summary, _code, _type = _error_message(_response_body(response), "")
                errors.append(f"{path}: HTTP {response.status_code}{(': ' + summary[:200]) if summary else ''}")
                continue
            payload = response.json()
            if not isinstance(payload, dict):
                errors.append(f"{path}: response was not a JSON object")
                continue
            entries = payload.get("models") or payload.get("data") or []
            if not isinstance(entries, list):
                errors.append(f"{path}: model entries were not a list")
                continue
            models: list[str] = []
            for item in entries:
                if not isinstance(item, dict):
                    continue
                if item.get("supported_in_api") is False:
                    continue
                mid = item.get("slug") or item.get("id")
                if isinstance(mid, str) and mid:
                    models.append(mid)
            if models:
                models.append(codex_auth.CODEX_PHANTOM_MODEL_ID)
                return sorted(set(models))
            errors.append(f"{path}: no usable model ids in response")
        detail = f" ({'; '.join(errors)})" if errors else ""
        raise ai.ProviderResponseError(
            f"OpenAI Codex did not return a usable model list{detail}",
            provider=_PROVIDER,
            body=errors or None,
        )

    async def probe(self, model: ai.Model) -> None:
        models = await self.list_models()
        if model.id not in models:
            raise ai.ProviderModelNotFoundError(
                f"model {model.id!r} was not found for OpenAI Codex",
                model_id=model.id,
                provider=_PROVIDER,
            )

    def stream(
        self,
        model: ai.Model,
        messages: list[ai.messages.Message],
        *,
        tools: Sequence[ai.types.tools.Tool] | None = None,
        output_type: type[Any] | None = None,
        params: Any = None,
    ) -> AsyncGenerator[ai.events.Event]:
        if output_type is not None:
            raise NotImplementedError("OpenAI Codex provider does not support structured output yet")
        return self._stream(model, messages, tools=tools, params=params)

    async def _stream(
        self,
        model: ai.Model,
        messages: list[ai.messages.Message],
        *,
        tools: Sequence[ai.types.tools.Tool] | None,
        params: Any,
    ) -> AsyncGenerator[ai.events.Event]:
        access, account_id = await self._ensure_access()
        body = await _build_body_async(model, messages, tools, params)
        headers = _codex_headers(access, account_id, stream=True)
        url = responses_url(self.base_url)
        text_open = False
        reasoning_open = False
        active_tool_id: str | None = None
        active_tool_name = ""
        active_tool_chunks: list[str] = []
        emitted_tool_ids: set[str] = set()
        terminal_usage: ai.types.usage.Usage | None = None

        def start_tool(tool_id: str, name: str) -> ai.events.ToolStart | None:
            nonlocal active_tool_id, active_tool_name, active_tool_chunks
            if not tool_id:
                return None
            active_tool_id = tool_id
            active_tool_name = name
            active_tool_chunks = []
            return ai.events.ToolStart(tool_call_id=tool_id, tool_name=name)

        def close_tool(tool_id: str | None = None, name: str | None = None, args: str | None = None) -> list[ai.events.Event]:
            nonlocal active_tool_id, active_tool_name, active_tool_chunks
            tid = tool_id or active_tool_id
            if not tid or tid in emitted_tool_ids:
                return []
            out: list[ai.events.Event] = []
            if active_tool_id != tid:
                out.append(ai.events.ToolStart(tool_call_id=tid, tool_name=name or active_tool_name))
            final_args = args if args is not None else "".join(active_tool_chunks)
            if final_args and not active_tool_chunks:
                out.append(ai.events.ToolDelta(tool_call_id=tid, chunk=final_args))
            out.append(ai.events.ToolEnd(tool_call_id=tid, tool_call=ai.types.messages.DUMMY_TOOL_CALL))
            emitted_tool_ids.add(tid)
            if active_tool_id == tid:
                active_tool_id = None
                active_tool_name = ""
                active_tool_chunks = []
            return out

        try:
            async with self.client.stream("POST", url, headers=headers, json=body) as response:
                if response.status_code >= 400:
                    await response.aread()
                _raise_status(response, model_id=model.id)
                yield ai.events.StreamStart()
                async for raw in _aiter_sse_json(response):
                    event_type = raw.get("type")
                    if event_type == "response.output_item.added":
                        item = raw.get("item")
                        if isinstance(item, dict) and item.get("type") in {"function_call", "custom_tool_call"}:
                            call_id = str(item.get("call_id") or item.get("id") or "")
                            name = str(item.get("name") or "")
                            ev = start_tool(call_id, name)
                            if ev is not None:
                                yield ev
                        continue
                    if event_type == "response.reasoning_summary_text.delta":
                        delta = raw.get("delta")
                        if isinstance(delta, str) and delta:
                            if not reasoning_open:
                                reasoning_open = True
                                yield ai.events.ReasoningStart(block_id="reasoning")
                            yield ai.events.ReasoningDelta(block_id="reasoning", chunk=delta)
                        continue
                    if event_type in {"response.reasoning_summary_part.done", "response.output_item.done"}:
                        item = raw.get("item")
                        if event_type == "response.output_item.done" and isinstance(item, dict) and item.get("type") in {"function_call", "custom_tool_call"}:
                            call_id = str(item.get("call_id") or item.get("id") or active_tool_id or "")
                            name = str(item.get("name") or active_tool_name or "")
                            args = item.get("arguments") if item.get("type") == "function_call" else item.get("input")
                            for ev in close_tool(call_id, name, args if isinstance(args, str) else None):
                                yield ev
                            continue
                        if reasoning_open and isinstance(item, dict) and item.get("type") == "reasoning":
                            reasoning_open = False
                            yield ai.events.ReasoningEnd(block_id="reasoning")
                        continue
                    if event_type in {"response.output_text.delta", "response.refusal.delta"}:
                        delta = raw.get("delta")
                        if isinstance(delta, str) and delta:
                            if reasoning_open:
                                reasoning_open = False
                                yield ai.events.ReasoningEnd(block_id="reasoning")
                            if not text_open:
                                text_open = True
                                yield ai.events.TextStart(block_id="text")
                            yield ai.events.TextDelta(block_id="text", chunk=delta)
                        continue
                    if event_type in {"response.function_call_arguments.delta", "response.custom_tool_call_input.delta"}:
                        delta = raw.get("delta")
                        if isinstance(delta, str) and active_tool_id:
                            active_tool_chunks.append(delta)
                            yield ai.events.ToolDelta(tool_call_id=active_tool_id, chunk=delta)
                        continue
                    if event_type in {"response.function_call_arguments.done", "response.custom_tool_call_input.done"}:
                        args = raw.get("arguments") if event_type == "response.function_call_arguments.done" else raw.get("input")
                        if isinstance(args, str):
                            active_tool_chunks = [args]
                        continue
                    if event_type in {"response.completed", "response.done", "response.incomplete"}:
                        response_obj = raw.get("response")
                        response_map = response_obj if isinstance(response_obj, Mapping) else None
                        terminal_usage = _usage_from_response(response_map)
                        provider_metadata: dict[str, Any] | None = None
                        if event_type == "response.incomplete":
                            # A cut-short turn must not look like a normal stop: carry
                            # the reason onto the assistant message (ai's Stream copies
                            # StreamEnd.provider_metadata onto stream.message) instead of
                            # dropping incomplete_details on the floor.
                            details = response_map.get("incomplete_details") if response_map else None
                            reason = details.get("reason") if isinstance(details, Mapping) else None
                            provider_metadata = {"incomplete": True, "incomplete_reason": reason}
                        if reasoning_open:
                            reasoning_open = False
                            yield ai.events.ReasoningEnd(block_id="reasoning")
                        if text_open:
                            text_open = False
                            yield ai.events.TextEnd(block_id="text")
                        for ev in close_tool():
                            yield ev
                        yield ai.events.StreamEnd(usage=terminal_usage, provider_metadata=provider_metadata)
                        return
                    if event_type in {"response.failed", "error"}:
                        message, code, error_type = _error_message(raw, "OpenAI Codex stream failed")
                        raise ai.ProviderAPIError(
                            message,
                            provider=_PROVIDER,
                            body=raw,
                            code=code,
                            error_type=error_type,
                            is_retryable=code in {"model_error", "server_error", "internal_error"},
                        )
                if reasoning_open:
                    yield ai.events.ReasoningEnd(block_id="reasoning")
                if text_open:
                    yield ai.events.TextEnd(block_id="text")
                for ev in close_tool():
                    yield ev
                yield ai.events.StreamEnd(usage=terminal_usage)
        except httpx.TimeoutException as exc:
            raise ai.ProviderTimeoutError("OpenAI Codex request timed out", provider=_PROVIDER, is_retryable=True) from exc
        except httpx.TransportError as exc:
            raise ai.ProviderConnectionError(f"OpenAI Codex connection failed: {exc}", provider=_PROVIDER, is_retryable=True) from exc


async def fetch_models_for_login(login: Login) -> list[str]:
    provider = OpenAICodexProvider.from_login(await codex_auth.ensure_fresh_login_async(login))
    try:
        return await provider.list_models()
    finally:
        await provider.aclose()


def provider_from_login_or_token(
    *,
    provider_base_url: str | None,
    provider_api_key: str | None,
) -> OpenAICodexProvider:
    from . import logins

    login = logins.load_logins().get(codex_auth.CODEX_PROVIDER_ID)
    if login is not None and login.provider_api_key:
        return OpenAICodexProvider.from_login(login)
    return OpenAICodexProvider(
        access_token=provider_api_key or "",
        account_id=codex_auth.token_profile(provider_api_key or "")[0] if provider_api_key else None,
        base_url=provider_base_url,
    )
