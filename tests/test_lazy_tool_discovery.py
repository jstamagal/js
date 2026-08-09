from __future__ import annotations

import json
from pathlib import Path

import ai
import ai.types.messages
import ai.types.usage
import pytest

from js import context_budget, runtime
from js.config import Config
from js.model_client import ModelStreamResult, ModelToolCall
from js.toolkit import ToolContext
from js.toolkit.registry import build_default_registry


def _names(surface) -> list[str]:
    return [tool.name for tool in surface.tools]


def _spec_names(specs: list[dict]) -> list[str]:
    return [spec["function"]["name"] for spec in specs]


def _result(*calls: tuple[str, str, str], text: str = "") -> ModelStreamResult:
    tool_calls = [ModelToolCall(id=call_id, name=name, arguments=args) for call_id, name, args in calls]
    parts: list = [
        ai.types.messages.ToolCallPart(tool_call_id=call.id, tool_name=call.name, tool_args=call.arguments)
        for call in tool_calls
    ]
    if text or not parts:
        parts.append(ai.types.messages.TextPart(text=text))
    return ModelStreamResult(
        text=text,
        tool_calls=tool_calls,
        reasoning="",
        usage=ai.types.usage.Usage(input_tokens=100, output_tokens=1),
        finish_reason="tool_calls" if tool_calls else "stop",
        assistant_message=ai.messages.Message(role="assistant", parts=parts),
    )


def _cfg(tmp_path: Path, settings: dict | None = None) -> Config:
    return Config(
        agent_id="lazy-test",
        agent_dir=tmp_path / ".js" / "sessions" / "lazy-test",
        model="offline-test",
        provider_id=None,
        provider_base_url=None,
        provider_api_key=None,
        reasoning_effort=None,
        max_output_tokens=None,
        max_tool_iterations=5,
        max_bash_output_bytes=65536,
        max_tool_result_bytes=65536,
        fetch_timeout_s=5,
        debug_log=None,
        trace=False,
        history_file=tmp_path / ".history",
        sessions_dir=tmp_path / ".js" / "sessions" / "lazy-test",
        session_file=tmp_path / ".js" / "sessions" / "lazy-test" / "session.jsonl",
        prompts_dir=tmp_path / "prompts",
        settings=settings or {},
    )


def test_catalog_search_is_stable_and_respects_selected_policy(tmp_path):
    allowed = build_default_registry().select(["browser_probe", "terminal_session", "read"])
    surface = allowed.lazy_surface(tmp_path)

    assert _names(surface) == ["read", "tool_discovery"]
    results = json.loads(surface.discover(kind="native"))["results"]
    assert [item["id"] for item in results] == [
        "native:browser_probe",
        "native:terminal_session",
    ]
    assert json.loads(surface.discover(source="browser"))["results"][0]["name"] == "browser_probe"
    assert json.loads(surface.discover(query="terminal session"))["results"][0]["source"] == "terminal"
    assert surface.discover(load="native:wiki_search").startswith("ERROR: no allowed catalog entry")

    _, rejected = runtime._dispatch(
        "browser_probe",
        "{}",
        runtime.Telemetry(None),
        cap_bytes=4096,
        registry=surface,
        tool_context=ToolContext(cwd=tmp_path),
    )
    assert rejected.startswith("ERROR: no tool named browser_probe")


def test_discovery_name_is_reserved_from_generated_agents(tmp_path):
    blocked = tmp_path / "tool_discovery"
    blocked.mkdir()
    (blocked / "10-system.md").write_text("You must not shadow discovery.\n", encoding="utf-8")
    allowed = tmp_path / "helper_agent"
    allowed.mkdir()
    (allowed / "10-system.md").write_text("Help with the request.\n", encoding="utf-8")

    registry = build_default_registry(prompts_root=tmp_path)

    assert registry.resolve("tool_discovery") is None
    assert registry.resolve("helper_agent") is not None
    surface = registry.select(["helper_agent"]).lazy_surface(tmp_path)
    assert _spec_names(surface.openai_specs()).count("tool_discovery") == 1
    assert [item["id"] for item in json.loads(surface.discover(kind="native"))["results"]] == [
        "native:helper_agent"
    ]


def test_discovery_name_cannot_be_taken_by_an_alias(tmp_path):
    allowed = build_default_registry().select(["read", "browser_probe"])
    surface = allowed.aliased({"read": "tool_discovery"}).lazy_surface(tmp_path)

    assert _spec_names(surface.openai_specs()) == ["read", "tool_discovery"]
    assert surface.resolve("tool_discovery").name == "tool_discovery"
    assert surface.dispatch_registry().resolve("tool_discovery").name == "tool_discovery"


def test_loading_native_tool_changes_only_current_surface(tmp_path):
    allowed = build_default_registry().select(["browser_probe", "read"])
    first = allowed.lazy_surface(tmp_path)

    assert "browser_probe" not in _spec_names(first.openai_specs())
    assert json.loads(first.discover(load="native:browser_probe"))["loaded"] == ["browser_probe"]
    assert "browser_probe" in _spec_names(first.openai_specs())
    assert first.resolve("browser_probe") is not None

    later_turn = allowed.lazy_surface(tmp_path)
    assert "browser_probe" not in _spec_names(later_turn.openai_specs())
    assert later_turn.resolve("browser_probe") is None


def test_skill_load_returns_instructions_and_activates_allowed_requirements(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "inspect.md").write_text(
        "---\ndescription: Inspect a rendered page\ntools:\n  - browser_probe\n---\n"
        "Open the page and report visual defects.\n",
        encoding="utf-8",
    )
    allowed = build_default_registry().select(["skill", "browser_probe"])
    surface = allowed.lazy_surface(tmp_path)

    skills_found = json.loads(surface.discover(kind="skill"))["results"]
    assert [item["id"] for item in skills_found] == ["skill:inspect"]
    loaded = json.loads(surface.discover(load="skill:inspect"))
    assert loaded["instructions"] == "Open the page and report visual defects.\n"
    assert loaded["loaded"] == ["browser_probe"]
    assert surface.resolve("browser_probe") is not None

    forbidden = build_default_registry().select(["skill"]).lazy_surface(tmp_path)
    denied = json.loads(forbidden.discover(load="skill:inspect"))
    assert denied["instructions"] == "Open the page and report visual defects.\n"
    assert denied["loaded"] == []
    assert denied["denied_tools"] == ["browser_probe"]
    assert forbidden.resolve("browser_probe") is None


def test_skill_load_distinguishes_denied_and_missing_requirements(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "inspect.md").write_text(
        "---\ndescription: Inspect with unavailable tools\ntools:\n"
        "  - browser_probe\n  - tool_that_does_not_exist\n---\n"
        "Inspect the target.\n",
        encoding="utf-8",
    )
    surface = build_default_registry().select(["skill"]).lazy_surface(tmp_path)

    loaded = json.loads(surface.discover(load="skill:inspect"))

    assert loaded["loaded"] == []
    assert loaded["denied_tools"] == ["browser_probe"]
    assert loaded["missing_tools"] == ["tool_that_does_not_exist"]


def test_skill_catalog_is_metadata_only_and_instructions_load_from_disk(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    path = skills / "notes.md"
    path.write_text("---\ndescription: Take notes\n---\nOriginal body.\n", encoding="utf-8")
    surface = build_default_registry().select(["skill"]).lazy_surface(tmp_path)
    path.write_text("---\ndescription: Take notes\n---\nUpdated body.\n", encoding="utf-8")
    loaded = json.loads(surface.discover(load="skill:notes"))
    assert loaded["instructions"] == "Updated body.\n", "instructions must load from disk on demand"


def test_skill_catalog_rejects_repeated_requirements(tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "inspect.md").write_text(
        "---\ndescription: Inspect files and pages\ntools:\n  - read\n  - read\n  - browser_probe\n---\n"
        "Inspect the requested target.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="contains duplicate 'read'"):
        build_default_registry().select(["skill", "read", "browser_probe"]).lazy_surface(tmp_path)


def test_discovery_cannot_authorize_another_call_from_same_response(monkeypatch, tmp_path):
    calls: list[list[dict]] = []
    results = iter(
        [
            _result(
                ("discover", "tool_discovery", '{"load":"native:browser_probe"}'),
                ("probe", "browser_probe", "{}"),
            ),
            _result(text="done"),
        ]
    )

    def stream_stub(**kwargs):
        calls.append(kwargs["tools"] or [])
        return next(results)

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream_stub)
    messages = [{"role": "user", "content": "inspect"}]
    runtime.run_turn(
        _cfg(tmp_path),
        "system",
        messages,
        runtime.Telemetry(None),
        tool_registry=build_default_registry().select(["browser_probe", "read"]),
        tool_context=ToolContext(cwd=tmp_path),
    )

    tool_results = [message["content"] for message in messages if message.get("role") == "tool"]
    assert json.loads(tool_results[0])["loaded"] == ["browser_probe"]
    assert tool_results[1].startswith("ERROR: no tool named browser_probe")
    assert "browser_probe" not in [tool.name for tool in calls[0]]
    assert "browser_probe" in [tool.name for tool in calls[1]]


def test_runtime_ignores_configured_alias_that_uses_discovery_name(monkeypatch, tmp_path):
    calls: list[list[dict]] = []
    results = iter(
        [
            _result(("discover", "tool_discovery", '{"kind":"native"}')),
            _result(text="done"),
        ]
    )

    def stream_stub(**kwargs):
        calls.append(kwargs["tools"] or [])
        return next(results)

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream_stub)
    cfg = _cfg(
        tmp_path,
        {"tools": {"alias_profiles": [{"match": ["offline"], "aliases": {"read": "tool_discovery"}}]}},
    )
    messages = [{"role": "user", "content": "find tools"}]
    runtime.run_turn(
        cfg,
        "system",
        messages,
        runtime.Telemetry(None),
        tool_registry=build_default_registry().select(["read", "browser_probe"]),
        tool_context=ToolContext(cwd=tmp_path),
    )

    assert [tool.name for tool in calls[0]] == ["read", "tool_discovery"]
    result = next(message["content"] for message in messages if message.get("role") == "tool")
    assert json.loads(result)["results"][0]["id"] == "native:browser_probe"


def test_runtime_regenerates_schemas_preserves_alias_history_and_resets_next_turn(monkeypatch, tmp_path):
    calls: list[list[dict]] = []
    results = iter(
        [
            _result(("discover", "tool_discovery", '{"load":"native:browser_probe"}')),
            _result(("probe", "Probe", "{}")),
            _result(text="done"),
            _result(text="later"),
        ]
    )

    def stream_stub(**kwargs):
        calls.append(kwargs["tools"] or [])
        return next(results)

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream_stub)
    registry = build_default_registry().select(["browser_probe", "read"])
    cfg = _cfg(
        tmp_path,
        {"tools": {"alias_profiles": [{"match": ["offline"], "aliases": {"browser_probe": "Probe"}}]}},
    )
    messages = [{"role": "user", "content": "inspect"}]
    context = ToolContext(cwd=tmp_path)

    runtime.run_turn(cfg, "system", messages, runtime.Telemetry(None), tool_registry=registry, tool_context=context)

    assert "Probe" not in [tool.name for tool in calls[0]]
    assert "Probe" in [tool.name for tool in calls[1]]
    assert "Probe" in [tool.name for tool in calls[2]]
    persisted_names = [
        call["function"]["name"]
        for message in messages
        for call in message.get("tool_calls", [])
    ]
    assert persisted_names == ["tool_discovery", "browser_probe"]
    assert [message["name"] for message in messages if message.get("role") == "tool"] == [
        "tool_discovery",
        "browser_probe",
    ]

    later_messages = [{"role": "user", "content": "again"}]
    runtime.run_turn(cfg, "system", later_messages, runtime.Telemetry(None), tool_registry=registry, tool_context=context)
    assert "Probe" not in [tool.name for tool in calls[3]]


def test_context_budget_tool_tokens_track_each_emitted_schema_set(monkeypatch, tmp_path):
    emitted: list[list] = []
    estimates: list[int] = []
    results = iter(
        [
            _result(("discover", "tool_discovery", '{"load":"native:browser_probe"}')),
            _result(text="done"),
        ]
    )

    def stream_stub(**kwargs):
        tools = kwargs["tools"] or []
        emitted.append(tools)
        estimates.append(context_budget.estimate_tools_tokens(tools))
        return next(results)

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream_stub)
    context = ToolContext(cwd=tmp_path)
    runtime.run_turn(
        _cfg(tmp_path),
        "system",
        [{"role": "user", "content": "inspect"}],
        runtime.Telemetry(None),
        tool_registry=build_default_registry().select(["browser_probe", "read"]),
        tool_context=context,
    )

    assert estimates[1] > estimates[0]
    assert context.context_budget_state._anchor.tool_tokens == estimates[1]


def test_persisted_call_names_canonicalize_even_for_unloaded_lazy_tools(tmp_path):
    from js.runtime import _canonical_tool_call_name

    allowed = build_default_registry().select(["read", "browser_probe"])
    surface = allowed.aliased({"browser_probe": "Probe"}).lazy_surface(tmp_path)
    batch = surface.dispatch_registry()
    # browser_probe is lazy and unloaded: no Tool object in this batch,
    # but history must still record the canonical name for its alias.
    assert batch.resolve("Probe") is None
    assert _canonical_tool_call_name("Probe", batch) == "browser_probe"
    assert _canonical_tool_call_name("unknown_thing", batch) == "unknown_thing"
