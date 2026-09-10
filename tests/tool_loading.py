"""Script model-side tool loading without bypassing runtime discovery or policy."""
from __future__ import annotations

import json

import ai
from ai.providers import history_utils

from js.model_client import ModelStreamResult, ModelToolCall


def after_loading(respond, *native_names, aliases=None, expected_native=None):
    """Run a scripted response only after its tools appear in the actual schema.

    Loading is a separate real model response. The runtime validates and dispatches
    it normally; subsequent calls retain the real discovery exchange in history.
    """
    aliases = aliases or {}

    def stream(**kwargs):
        history_utils.validate(kwargs["messages"])
        published = {tool.name for tool in kwargs.get("tools") or ()}
        if expected_native is not None:
            catalog = next((part.result for message in kwargs["messages"] for part in message.parts
                            if part.kind == "tool_result" and part.tool_call_id == "catalog_native"), None)
            if catalog is None:
                return _tool_response([ModelToolCall(
                    id="catalog_native", name="tool_discovery", arguments='{"kind":"native"}',
                )])
            entries = json.loads(catalog)
            assert not entries["truncated"]
            assert {item["name"] for item in entries["results"]} == set(expected_native)
        missing = [name for name in native_names if aliases.get(name, name) not in published]
        if not missing:
            return respond(**kwargs)
        assert "tool_discovery" in published, f"No discovery path for {missing}"
        prior_results = {part.tool_call_id: part.result for message in kwargs["messages"]
                         for part in message.parts if part.kind == "tool_result"}
        failed = {name: prior_results[f"load_{name}"] for name in missing
                  if f"load_{name}" in prior_results}
        assert not failed, f"Tools still unpublished after loading: {failed}"
        calls = [ModelToolCall(
            id=f"load_{name}", name="tool_discovery",
            arguments=json.dumps({"load": f"native:{name}"}),
        ) for name in missing]
        return _tool_response(calls)

    return stream


def _tool_response(calls):
    return ModelStreamResult(
        text="", reasoning="", usage=None, finish_reason="tool_calls", tool_calls=calls,
        assistant_message=ai.messages.Message(role="assistant", parts=[
            ai.types.messages.ToolCallPart(tool_call_id=call.id, tool_name=call.name,
                                       tool_args=call.arguments) for call in calls
        ]),
    )
