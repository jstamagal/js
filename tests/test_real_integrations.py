from __future__ import annotations

import contextlib
import io
import os
import re
from pathlib import Path

import pytest

from js import tools as runtime_tools
from js.config import from_env
from js.runtime import Telemetry, run_turn
from js.toolkit import ToolContext
from js.toolkit.fs import fs_search, patch, read, sem_search, write
from js.toolkit.meta import task


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _require_provider() -> None:
    """Skip the test unless a real provider is reachable.

    A real-model turn is available when one of these is true:
    - ``cfg.provider_id`` is set
    - ``AI_GATEWAY_API_KEY`` is set (gateway-routed models)
    - the model id has a ``provider:`` prefix (e.g. ``openai:gpt-4o``)
    """
    cfg = from_env(save_session=False)
    has_explicit_provider = cfg.provider_id is not None
    has_gateway_key = "AI_GATEWAY_API_KEY" in os.environ
    has_direct_prefix = ":" in (cfg.model or "")
    if not (has_explicit_provider or has_gateway_key or has_direct_prefix):
        pytest.skip("no provider configured — set JS_PROVIDER, AI_GATEWAY_API_KEY, or use "
                     "provider:model syntax")


def test_toolkit_exercises_grounded_file_lifecycle_and_search(tmp_path):
    context = ToolContext(cwd=tmp_path)
    target = tmp_path / "code.py"
    target.write_text("class TaskRunner:\n    def run_task(self):\n        return 'old'\n", encoding="utf-8")

    actual_unread = patch("code.py", old_string="old", new_string="new", context=context)
    assert actual_unread == "ERROR: You must read the file with the read tool before attempting to edit it."

    actual_read = read("code.py", context=context)
    assert re.search(r"^1[a-f0-9]{2}\|class TaskRunner:", actual_read, re.MULTILINE)

    actual_patch = patch("code.py", old_string="return 'old'", new_string="return 'new'", context=context)
    assert actual_patch.startswith(f"patched {target}")
    assert target.read_text(encoding="utf-8") == "class TaskRunner:\n    def run_task(self):\n        return 'new'\n"

    actual_search = fs_search("TaskRunner", path=".", output_mode="content", context=context)
    assert f"{target}:1:class TaskRunner:" in actual_search
    actual_search_again = fs_search("TaskRunner", path=".", output_mode="content", context=context)
    assert actual_search_again == actual_search + "\n[deduplicated repeated search]"

    created = write("created.txt", "created\n", context=context)
    assert created.startswith(f"wrote 8 bytes to {tmp_path / 'created.txt'}")


def test_sem_search_finds_ranked_local_code_without_external_index():
    context = ToolContext(cwd=PROJECT_ROOT)

    actual = sem_search(
        [
            {
                "query": "task delegation backend",
                "use_case": "find where delegated worker tasks are run",
                "path": "js",
                "glob": "*.py",
                "limit": 6,
            }
        ],
        context=context,
    )

    assert actual.startswith("Local semantic-ish search")
    assert "js/toolkit/meta.py" in actual
    assert "def task(" in actual or "js/toolkit/meta.py" in actual
    assert "ERROR: sem_search requires a code index backend" not in actual


@pytest.mark.ai_provider
def test_task_backend_runs_real_child_turn_through_local_proxy():
    _require_provider()
    context = ToolContext(cwd=PROJECT_ROOT)

    actual = task(
        ["Reply with exactly TASK_CHILD_OK"],
        agent_id="pytest-smoke",
        session_id="task-backend",
        context=context,
    )

    assert actual.startswith("TASK_RESULTS agent=pytest-smoke session_id=task-backend")
    assert "TASK_CHILD_OK" in actual
    assert "backend is not wired" not in actual


@pytest.mark.ai_provider
@pytest.mark.e2e
def test_real_model_turn_uses_sem_search_and_task_end_to_end(monkeypatch):
    _require_provider()
    monkeypatch.chdir(PROJECT_ROOT)
    runtime_tools.DEFAULT_CONTEXT = ToolContext(cwd=PROJECT_ROOT)
    cfg = from_env()
    messages = [
        {
            "role": "user",
            "content": (
                "Use sem_search to find where task delegation is implemented in js, "
                "then use the task tool to delegate this exact task: Reply with exactly TASK_CHILD_OK. "
                "Finally answer with exactly PARENT_OK if the delegated task succeeded."
            ),
        }
    ]

    capture = io.StringIO()
    with contextlib.redirect_stdout(capture):
        run_turn(
            cfg,
            "You are a precise tool-using test agent. Use requested tools. Keep final answer exact.",
            messages,
            Telemetry(None),
            trace_override=False,
        )

    tool_names = [message.get("name") for message in messages if message.get("role") == "tool"]
    final = next(
        message.get("content", "")
        for message in reversed(messages)
        if message.get("role") == "assistant" and message.get("content")
    )

    assert "sem_search" in tool_names
    assert "task" in tool_names
    assert any("TASK_CHILD_OK" in message.get("content", "") for message in messages if message.get("role") == "tool")
    assert "PARENT_OK" in final


@pytest.mark.ai_provider
def test_real_model_turn_is_available_only_when_local_proxy_is_configured():
    _require_provider()
    cfg = from_env()
    assert cfg.model

@pytest.mark.vision
@pytest.mark.e2e
def test_live_vision_model_reads_image_through_real_message_path(tmp_path):
    """End-to-end proof that image bytes reach a model that actually reads them.

    Real fs_read -> real model_client.build_tool_result_message -> a LIVE
    vision model via model_client.stream_model. Skips unless Pillow and a
    reachable local vision model are present, so it never fails spuriously
    where the local model is absent."""
    import urllib.request

    from js import model_client, runtime
    from js.config import vision_enabled_for_model
    from js.toolkit import fs

    pytest.importorskip("PIL")
    from PIL import Image

    model = os.environ.get("JS_VISION_TEST_MODEL", "gemma4:e4b")
    base_url = os.environ.get("JS_BASE_URL", "http://localhost:11434/v1")
    api_key = os.environ.get("JS_API_KEY", "ollama")
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=5) as resp:
            if "gemma4:e4b" not in resp.read().decode("utf-8", "replace"):
                pytest.skip("ollama gemma4:e4b model not pulled")
    except Exception:
        pytest.skip("ollama not reachable on localhost:11434")

    image = tmp_path / "swatch.png"
    Image.new("RGB", (256, 256), (20, 40, 200)).save(image)

    # Real detection (no env override).
    enabled = vision_enabled_for_model(model)
    assert enabled is True
    context = ToolContext(cwd=tmp_path, vision_enabled=enabled)

    marker = fs.fs_read("swatch.png", context=context)
    assert marker.startswith("IMAGE_RESULT\t")

    convo = model_client.history_to_ai_messages("You can see images.", [
        {"role": "user", "content": "Read swatch.png and name its color in one word."},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "read", "arguments": '{"file_path":"swatch.png"}'}}
        ]},
    ])
    convo.extend(model_client.build_tool_result_messages("c1", "read", marker))

    reply = ""
    for _ in range(4):
        try:
            result = model_client.stream_model(
                model_id=model,
                provider_id="openai",
                provider_base_url=base_url,
                provider_api_key=api_key,
                messages=convo,
                tools=None,
                max_output_tokens=12,
                reasoning_effort=None,
                on_text=lambda _t: None,
            )
        except Exception as exc:
            pytest.skip(f"vision model call failed: {type(exc).__name__}: {exc}")
        reply = result.text.strip()
        if "blue" in reply.lower():
            break
    assert "blue" in reply.lower(), f"live vision model did not read the blue image; last reply={reply!r}"
