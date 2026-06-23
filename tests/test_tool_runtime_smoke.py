from __future__ import annotations

import base64
import re
import shutil
import sys
import types

import pytest

from js import model_client, runtime, setcmd, settings, tools as runtime_tools
from js.model_client import ModelStreamResult, ModelToolCall
from js.toolkit import Tool, ToolContext, ToolRegistry, build_default_registry
from js.toolkit import fs, process_net
import ai


ANCHOR_RE = re.compile(r"^1[a-f0-9]{2}\|alpha$", re.MULTILINE)


def test_anchored_read_patch_and_undo_are_grounded_in_temp_cwd(tmp_path):
    target = tmp_path / "sample.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    unread_patch = fs.patch(path="sample.txt", old_string="alpha", new_string="ALPHA", context=context)
    assert unread_patch == "ERROR: You must read the file with the read tool before attempting to edit it."

    read_result = fs.read("sample.txt", context=context)
    assert ANCHOR_RE.search(read_result)
    assert "2" in read_result and "|beta" in read_result

    patch_result = fs.patch(path="sample.txt", old_string="alpha", new_string="ALPHA", context=context)
    assert patch_result.startswith(f"patched {target}")
    assert target.read_text(encoding="utf-8") == "ALPHA\nbeta\n"

    undo_result = fs.undo("sample.txt", context=context)
    assert undo_result.startswith(f"restored {target}")
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\n"


def test_read_boolean_line_range_values_are_ignored(tmp_path):
    target = tmp_path / "lines.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    actual = fs.read("lines.txt", start_line=True, end_line=True, context=context)

    assert "alpha" in actual
    assert "beta" in actual
    assert "invalid line range" not in actual


def test_read_start_past_eof_returns_non_error_note(tmp_path):
    target = tmp_path / "lines.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    actual = fs.read("lines.txt", start_line=50, end_line=80, context=context)

    assert not actual.startswith("ERROR")
    assert "2 total lines" in actual
    assert "start_line=50" in actual


def test_write_overwrite_guard_requires_explicit_overwrite_and_prior_read(tmp_path):
    target = tmp_path / "guarded.txt"
    target.write_text("old\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    assert fs.write("guarded.txt", "new\n", context=context) == "ERROR: Cannot overwrite existing file: overwrite flag not set."
    assert fs.write("guarded.txt", "new\n", overwrite=True, context=context) == (
        "ERROR: You must read the file with the read tool before attempting to overwrite it."
    )
    fs.read("guarded.txt", context=context)

    write_result = fs.write("guarded.txt", "new\n", overwrite=True, context=context)
    assert write_result.startswith(f"wrote 4 bytes to {target}")
    assert target.read_text(encoding="utf-8") == "new\n"


def test_undo_restores_created_file_to_nonexistent_state(tmp_path):
    context = ToolContext(cwd=tmp_path)
    target = tmp_path / "created.txt"

    write_result = fs.write("created.txt", "new file\n", context=context)
    undo_result = fs.undo("created.txt", context=context)

    assert write_result.startswith(f"wrote 9 bytes to {target}")
    assert undo_result.startswith(f"restored deletion state for {target}")
    assert not target.exists()


def test_remove_directory_snapshot_can_be_restored_by_undo(tmp_path):
    context = ToolContext(cwd=tmp_path)
    root = tmp_path / "tree"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "top.txt").write_text("top\n", encoding="utf-8")
    (nested / "child.txt").write_text("child\n", encoding="utf-8")

    remove_result = fs.remove("tree", permanent=True, context=context)
    undo_result = fs.undo("tree", context=context)

    assert remove_result == f"removed {root}"
    assert undo_result == f"restored directory {root}"
    assert (root / "top.txt").read_text(encoding="utf-8") == "top\n"
    assert (nested / "child.txt").read_text(encoding="utf-8") == "child\n"


def test_remove_small_file_uses_trash_by_default(tmp_path, monkeypatch):
    context = ToolContext(cwd=tmp_path)
    target = tmp_path / "small.txt"
    target.write_text("small\n", encoding="utf-8")
    calls = []

    def fake_run(command, context, timeout):
        calls.append(command)
        target.unlink()
        return 0, "", ""

    monkeypatch.setattr(fs, "_trash_command", lambda: "/usr/bin/trash")
    monkeypatch.setattr(fs, "run", fake_run)

    result = fs.remove("small.txt", context=context)

    assert result == f"trashed {target}"
    assert calls == [["/usr/bin/trash", str(target)]]
    assert not target.exists()


def test_remove_chonker_requires_confirmed_permanent_delete(tmp_path, monkeypatch):
    context = ToolContext(cwd=tmp_path)
    target = tmp_path / "big.log"
    target.write_text("not actually big\n", encoding="utf-8")

    monkeypatch.setattr(fs, "_path_size_no_follow", lambda path: fs._TRASH_MAX_BYTES + 1)

    result = fs.remove("big.log", context=context)

    assert result == (
        f"ERROR: target is over the 512 MiB trash limit ({fs._TRASH_MAX_BYTES + 1} bytes); "
        "confirm with KING and pass permanent=true to delete directly."
    )
    assert target.exists()
    assert not context.snapshots.get(target)


def test_remove_symlink_does_not_follow_target_and_undo_restores_link(tmp_path):
    context = ToolContext(cwd=tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    kept = outside / "keep.txt"
    kept.write_text("keep\n", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)

    remove_result = fs.remove("link", permanent=True, context=context)
    undo_result = fs.undo("link", context=context)

    assert remove_result == f"removed {link}"
    assert kept.read_text(encoding="utf-8") == "keep\n"
    assert undo_result == f"restored symlink {link}"
    assert link.is_symlink()
    assert link.readlink() == outside

def test_fs_search_negative_bounds_are_ignored(tmp_path):
    (tmp_path / "one.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("needle\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    actual = fs.fs_search("needle", path=".", head_limit=-1, offset=-1, context_lines=-1, context=context)

    assert str(tmp_path / "one.txt") in actual
    assert str(tmp_path / "two.txt") in actual


def test_semantic_search_negative_limit_is_ignored(tmp_path):
    (tmp_path / "one.txt").write_text("needle alpha\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("needle beta\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    actual = fs.sem_search([{"query": "needle", "limit": -1}], context=context)

    assert "one.txt" in actual
    assert "two.txt" in actual

def test_fs_search_boolean_head_limit_is_ignored(tmp_path):
    (tmp_path / "one.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("needle\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    actual = fs.fs_search("needle", path=".", head_limit=True, context=context)

    assert str(tmp_path / "one.txt") in actual
    assert str(tmp_path / "two.txt") in actual

def test_fs_search_repeated_query_is_deduplicated(tmp_path):
    (tmp_path / "one.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("hay\nneedle\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    first = fs.fs_search("needle", path=".", output_mode="files_with_matches", context=context)
    second = fs.fs_search("needle", path=".", output_mode="files_with_matches", context=context)

    assert str(tmp_path / "one.txt") in first
    assert str(tmp_path / "two.txt") in first
    assert second == first + "\n[deduplicated repeated search]"


def test_semantic_search_boolean_limit_is_ignored(tmp_path):
    (tmp_path / "one.txt").write_text("needle alpha\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("needle beta\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    actual = fs.sem_search([{"query": "needle", "limit": True}], context=context)

    assert "one.txt" in actual
    assert "two.txt" in actual


def test_semantic_search_boolean_scope_values_are_ignored(tmp_path):
    (tmp_path / "one.txt").write_text("needle alpha\n", encoding="utf-8")
    context = ToolContext(cwd=tmp_path)

    actual = fs.sem_search([{"query": "needle", "path": True, "glob": True}], context=context)

    assert "path does not exist" not in actual
    assert "one.txt" in actual

def test_tool_resolution_is_canonical_case_insensitive_and_not_compat_alias_based():
    registry = build_default_registry()

    assert registry.resolve("read").name == "read"
    assert registry.resolve("Read").name == "read"
    assert registry.resolve("write").name == "write"
    assert registry.resolve("Write").name == "write"
    assert registry.resolve("task").name == "task"
    assert registry.resolve("Task").name == "task"
    assert registry.resolve("grep") is None
    assert registry.resolve("rg") is None
    assert registry.resolve("cat") is None
    assert registry.resolve("forge__read_file") is None
    assert registry.resolve("delete") is None


def test_runtime_repairs_common_jsonish_argument_shapes():
    assert runtime._repair_jsonish('{"path":"a",}') == {"path": "a"}
    assert runtime._repair_jsonish('"{\\"path\\":\\"a\\"}"') == {"path": "a"}
    assert runtime._repair_jsonish('{"path":"a"') == {"path": "a"}
    assert runtime._repair_jsonish("") == {}

    with pytest.raises(ValueError, match="tool args must be an object"):
        runtime._repair_jsonish('["not", "an", "object"]')


def test_canonical_tool_args_preserves_valid_and_repairs_blankable():
    # Valid JSON is returned byte-for-byte (model's exact bytes preserved).
    valid = '{"file_path": "/tmp/out.md", "content": "hi"}'
    assert runtime._canonical_tool_args(valid) == valid

    # Trailing comma / unclosed brace: js repairs for execution, but the raw
    # string would be blanked to "{}" by the SDK's integrity pass on resend.
    # _canonical_tool_args must substitute the repaired, json-decodable form.
    for bad in ('{"file_path": "/tmp/out.md", "content": "hi",}',
                '{"file_path": "/tmp/out.md", "content": "hi"'):
        fixed = runtime._canonical_tool_args(bad)
        import json as _json
        assert _json.loads(fixed) == {"file_path": "/tmp/out.md", "content": "hi"}


def test_canonical_tool_args_unwraps_double_encoded_object():
    # O2: a double-encoded payload — valid JSON, but a STRING wrapping the real
    # object — is what local OpenAI-shaped servers (vLLM/llama.cpp/qwen) tend to
    # emit. Execution unwraps it via _repair_jsonish; history must match, or the
    # model is shown its own prior call as a quoted blob and imitates it. The bug
    # was returning the raw string untouched because it happens to be valid JSON.
    import json as _json
    double = '"{\\"file_path\\": \\"/tmp/out.md\\", \\"content\\": \\"hi\\"}"'
    fixed = runtime._canonical_tool_args(double)
    assert _json.loads(fixed) == {"file_path": "/tmp/out.md", "content": "hi"}
    assert isinstance(_json.loads(fixed), dict)


def test_canonical_tool_args_survives_sdk_integrity_pass():
    """The end-to-end guarantee: a malformed-but-repairable tool call, once
    canonicalized and round-tripped through the SDK's integrity pass, retains
    non-empty json-decodable args instead of being blanked to "{}"."""
    from ai.types import integrity

    bad = '{"file_path": "/tmp/out.md", "content": "hi",}'  # trailing comma
    fixed = runtime._canonical_tool_args(bad)

    asst = ai.assistant_message(
        ai.types.messages.ToolCallPart(
            tool_call_id="c1", tool_name="write", tool_args=fixed
        )
    )
    tool_res = ai.tool_message(
        ai.types.messages.ToolResultPart(
            tool_call_id="c1", tool_name="write", result="ok"
        )
    )
    prepared = integrity.prepare_messages([ai.user_message("hi"), asst, tool_res])

    seen = [
        part.tool_args
        for msg in prepared
        for part in msg.parts
        if isinstance(part, ai.types.messages.ToolCallPart)
    ]
    assert seen, "expected a tool call in the prepared history"
    import json as _json
    for args in seen:
        assert args != "{}", "args were blanked by the integrity pass"
        assert _json.loads(args) == {"file_path": "/tmp/out.md", "content": "hi"}


def test_sanitize_assistant_message_repairs_sdk_tool_call_parts():
    bad = '{"file_path": "/tmp/out.md", "content": "hi"'  # unclosed brace
    msg = ai.assistant_message(
        ai.types.messages.ToolCallPart(
            tool_call_id="c1", tool_name="write", tool_args=bad
        )
    )
    fixed_msg = runtime._sanitize_assistant_message(msg)
    import json as _json
    parts = [
        p for p in fixed_msg.parts
        if isinstance(p, ai.types.messages.ToolCallPart)
    ]
    assert parts and _json.loads(parts[0].tool_args) == {
        "file_path": "/tmp/out.md",
        "content": "hi",
    }
    # Untouched when already valid: same object back.
    good = ai.assistant_message(
        ai.types.messages.ToolCallPart(
            tool_call_id="c2", tool_name="write",
            tool_args='{"file_path": "/tmp/out.md", "content": "hi"}',
        )
    )
    assert runtime._sanitize_assistant_message(good) is good


def test_retry_metadata_is_appended_and_resets_after_success():
    tracker = runtime.ToolErrorTracker(limit=2)

    assert tracker.record("read", "ERROR: bad path") == (
        "ERROR: bad path\n<retry>attempts_left=1, allowed_max_attempts=2</retry>"
    )
    assert tracker.record("read", "ERROR: still bad") == (
        "ERROR: still bad\n<retry>attempts_left=0, allowed_max_attempts=2</retry>"
    )
    assert tracker.limit_reached()

    assert tracker.record("read", "ok") == "ok"
    assert not tracker.limit_reached()


def test_dispatch_uses_canonical_name_repairs_args_and_adds_retry_metadata(tmp_path, monkeypatch):
    context = ToolContext(cwd=tmp_path)
    monkeypatch.setattr(runtime_tools, "DEFAULT_CONTEXT", context)
    telemetry_events: list[tuple[str, dict]] = []

    class TelemetryStub:
        def event(self, kind: str, **payload):
            telemetry_events.append((kind, payload))

    args, result = runtime._dispatch(
        "write",
        '{"file_path":"created.txt","content":"ok",}',
        TelemetryStub(),
        cap_bytes=4096,
        error_tracker=runtime.ToolErrorTracker(limit=2),
    )

    assert args == {"file_path": "created.txt", "content": "ok"}
    assert result.startswith(f"wrote 2 bytes to {tmp_path / 'created.txt'}")
    assert (tmp_path / "created.txt").read_text(encoding="utf-8") == "ok"
    assert telemetry_events == [("tool_ok", {"tool": "write", "latency_ms": 0})] or telemetry_events[0][0] == "tool_ok"

    _, error = runtime._dispatch(
        "missing_tool",
        "{}",
        TelemetryStub(),
        cap_bytes=4096,
        error_tracker=runtime.ToolErrorTracker(limit=2),
    )
    assert error.endswith("<retry>attempts_left=1, allowed_max_attempts=2</retry>")


_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
)


def _simple_pdf_bytes(text: str) -> bytes:
    stream = f"BT /F1 24 Tf 72 72 Td ({text}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for idx, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out.extend(f"{idx} 0 obj\n".encode("ascii"))
        out.extend(obj)
        out.extend(b"\nendobj\n")
    xref = len(out)
    out.extend(f"xref\n0 {len(objects)+1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    out.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    return bytes(out)


def test_fs_read_image_returns_marker_without_inline_base64(tmp_path):
    image = tmp_path / "pixel.png"
    image.write_bytes(_TINY_PNG)
    context = ToolContext(cwd=tmp_path, vision_enabled=True)

    result = fs.fs_read("pixel.png", context=context)

    assert result.startswith("IMAGE_RESULT\t")
    _, path, mime, stub = result.split("\t", 3)
    assert path == str(image)
    assert mime == "image/png"
    assert "VISUAL_FILE" in stub
    assert base64.b64encode(_TINY_PNG).decode("ascii") not in result


def test_fs_read_image_oversize_and_vision_off_fallback(tmp_path):
    image = tmp_path / "pixel.png"
    image.write_bytes(_TINY_PNG)

    # Oversize is rejected before any vision handling, regardless of vision state.
    too_small = ToolContext(cwd=tmp_path, max_file_bytes=4, vision_enabled=True)
    assert fs.fs_read("pixel.png", context=too_small).startswith("ERROR: image size")

    # Vision off (the default): a plain text descriptor, never an image marker.
    vision_off = ToolContext(cwd=tmp_path)
    result = fs.fs_read("pixel.png", context=vision_off)
    assert result.startswith(f"VISUAL_FILE {image} mime=image/png")
    assert not result.startswith("IMAGE_RESULT")


def test_build_tool_result_messages_expands_image_marker_with_user_file_part(tmp_path):
    image = tmp_path / "pixel.png"
    image.write_bytes(_TINY_PNG)
    marker = f"IMAGE_RESULT\t{image}\timage/png\tvisual stub"
    messages = model_client.build_tool_result_messages("call_1", "read", marker)

    assert [msg.role for msg in messages] == ["tool", "user"]
    trp = messages[0].parts[0]
    assert isinstance(trp, ai.types.messages.ToolResultPart)
    assert trp.tool_call_id == "call_1"
    assert trp.tool_name == "read"
    assert not trp.is_error
    assert trp.result == "visual stub"
    assert isinstance(messages[1].parts[0], ai.types.messages.TextPart)
    assert messages[1].parts[0].text == "visual stub"
    assert isinstance(messages[1].parts[1], ai.types.messages.FilePart)
    assert messages[1].parts[1].data == _TINY_PNG
    assert messages[1].parts[1].media_type == "image/png"


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="pdftotext is not installed")
def test_fs_read_pdf_uses_pdftotext(tmp_path):
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(_simple_pdf_bytes("Hello PDF Text"))
    context = ToolContext(cwd=tmp_path)

    result = fs.fs_read("sample.pdf", context=context)

    assert "Hello PDF Text" in result
    assert not result.startswith("IMAGE_RESULT")


def test_shell_sanitizes_bool_command_and_invalid_timeouts(tmp_path, monkeypatch):
    calls: list[dict] = []

    def run_stub(cmd, **kwargs):
        calls.append({"cmd": cmd, "timeout": kwargs["timeout"]})
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(process_net.subprocess, "run", run_stub)
    monkeypatch.setattr(process_net, "_default_shell", lambda: "/bin/sh")
    context = ToolContext(cwd=tmp_path)

    process_net.shell(True, timeout=True, context=context)
    process_net.shell("echo ok", timeout=-1, context=context)
    process_net.shell("echo ok", timeout="bad", context=context)

    assert calls == [
        {"cmd": ["/bin/sh", "-c", ""], "timeout": 300},
        {"cmd": ["/bin/sh", "-c", "echo ok"], "timeout": 300},
        {"cmd": ["/bin/sh", "-c", "echo ok"], "timeout": 300},
    ]

@pytest.mark.skipif(sys.platform == "win32", reason="Unix shell behavior")
def test_shell_uses_env_shell_with_dash_c_and_reports_shell(tmp_path, monkeypatch):
    fake_shell = tmp_path / "fake-shell"
    fake_shell.write_text(
        "#!/bin/sh\n"
        "printf 'argv0=%s\\n' \"$0\"\n"
        "printf 'arg1=%s\\n' \"$1\"\n"
        "printf 'arg2=%s\\n' \"$2\"\n",
        encoding="utf-8",
    )
    fake_shell.chmod(0o700)
    monkeypatch.setenv("SHELL", str(fake_shell))
    context = ToolContext(cwd=tmp_path)

    actual = process_net.shell("printf should-not-need-real-shell", context=context)

    expected = (
        f"shell={fake_shell}\n"
        "exit=0\n"
        "--- stdout ---\n"
        f"argv0={fake_shell}\n"
        "arg1=-c\n"
        "arg2=printf should-not-need-real-shell\n"
    )
    assert actual == expected


@pytest.mark.skipif(sys.platform == "win32", reason="Unix shell behavior")
def test_shell_falls_back_to_bin_sh_when_shell_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("SHELL", raising=False)
    context = ToolContext(cwd=tmp_path)

    actual = process_net.shell("printf fallback-ok", context=context)

    expected = "shell=/bin/sh\nexit=0\n--- stdout ---\nfallback-ok"
    assert actual == expected




def test_history_tool_result_message_dehydrates_image(tmp_path):
    image = tmp_path / "pixel.png"
    image.write_bytes(_TINY_PNG)
    marker = f"IMAGE_RESULT\t{image}\timage/png\tVISUAL_FILE stub"
    pc = runtime._PendingToolCall(id="c1", name="read")
    b64 = base64.b64encode(_TINY_PNG).decode("ascii")

    msgs = runtime._history_tool_result_message(pc, marker)

    assert msgs == [{"role": "tool", "tool_call_id": "c1", "name": "read", "content": "VISUAL_FILE stub"}]
    assert b64 not in str(msgs)
    assert runtime._history_tool_result_message(pc, "plain done")[0]["content"] == "plain done"


def test_tool_display_keys_are_canonical_tool_names():
    canonical = {t.name for t in build_default_registry().tools}
    extra = set(runtime._TOOL_DISPLAY) - canonical
    assert not extra, f"_TOOL_DISPLAY has non-canonical keys: {extra}"


def test_alias_profile_rewrites_outgoing_spec_names_and_descriptions():
    registry = build_default_registry().select(["read", "write", "task", "shell", "fs_search"])
    specs = registry.openai_specs()
    alias_map = {"read": "Read", "write": "Write", "task": "Task"}

    aliased = runtime._aliased_tool_specs(specs, alias_map)
    actual_names = [spec["function"]["name"] for spec in aliased]

    assert actual_names == ["Read", "Write", "fs_search", "shell", "Task"]
    # Originals are untouched (deep-copied).
    assert [spec["function"]["name"] for spec in specs] == ["read", "write", "fs_search", "shell", "task"]
    # Backtick-wrapped cross-references inside descriptions follow the rename:
    # write.md references `read`, so the outgoing write spec must say `Read`.
    write_desc = next(s["function"]["description"] for s in aliased if s["function"]["name"] == "Write")
    assert "`Read`" in write_desc and "`read`" not in write_desc
    # Empty alias map is a no-op returning the same object.
    assert runtime._aliased_tool_specs(specs, {}) is specs


def test_alias_profile_skips_existing_tool_name_collisions_from_config():
    live_settings = settings.seed_defaults()
    result = setcmd.run_repl_command(
        live_settings,
        '/set tools.alias_profiles [{"match":["openai"],"aliases":{"read":"Write"}}]',
    )
    registry = build_default_registry().select(["read", "write"])

    assert result.error is None
    alias_map = runtime._resolve_alias_profile(live_settings, "openai-test", None)
    specs = runtime._aliased_tool_specs(registry.openai_specs(), alias_map)
    aliased_registry = registry.aliased(alias_map)

    assert [spec["function"]["name"] for spec in specs] == ["read", "write"]
    assert aliased_registry.resolve("WRITE").name == "write"


def test_alias_profile_resolution_skips_unusable_matching_profiles_from_config():
    live_settings = settings.seed_defaults()
    result = setcmd.run_repl_command(
        live_settings,
        '/set tools.alias_profiles ['
        '{"match":["openai"],"aliases":{"missing_tool":"MissingTool"}},'
        '{"match":["openai"],"aliases":{"read":"Read"}}'
        ']',
    )
    registry = build_default_registry().select(["read"])

    assert result.error is None
    assert runtime._resolve_alias_profile(live_settings, "openai-test", None, registry) == {
        "read": "Read",
    }


def test_alias_profile_match_values_ignore_surrounding_whitespace_from_config():
    live_settings = settings.seed_defaults()
    result = setcmd.run_repl_command(
        live_settings,
        '/set tools.alias_profiles [{"match":[" openai "],"aliases":{"read":"Read"}}]',
    )

    assert result.error is None
    assert runtime._resolve_alias_profile(live_settings, "openai-test", None) == {
        "read": "Read",
    }


def test_resolve_alias_profile_matches_model_or_provider_substring():
    settings = {"tools": {"alias_profiles": [
        {"match": ["claude"], "aliases": {"read": "Read", "write": "Write", "task": "Task"}},
    ]}}
    assert runtime._resolve_alias_profile(settings, "openai/proxy-claude-sonnet-4", None) == {
        "read": "Read", "write": "Write", "task": "Task"}
    # Non-matching model id falls through to the default (empty) map.
    assert runtime._resolve_alias_profile(settings, "openai/proxy-gpt-5", None) == {}
    # The provider id is also matched against the substrings.
    prov = {"tools": {"alias_profiles": [{"match": ["anthropic"], "aliases": {"read": "Read"}}]}}
    assert runtime._resolve_alias_profile(prov, "some-model", "anthropic") == {"read": "Read"}
    # No profiles configured → empty map → default tool names.
    assert runtime._resolve_alias_profile({}, "openai/proxy-claude-sonnet-4", None) == {}


def test_registry_aliased_resolves_noncase_variant_alias_to_canonical():
    base = build_default_registry().select(["read", "write"])
    aliased = base.aliased({"read": "view_file"})
    # A non-case-variant alias dispatches back to the canonical handler.
    assert aliased.resolve("view_file").name == "read"
    # Case-insensitive resolution still works.
    assert aliased.resolve("Read").name == "read"
    # Empty/None profile returns the same registry object.
    assert base.aliased({}) is base
    assert base.aliased(None) is base


def test_jsonl_long_lines_use_dedicated_cap_while_other_suffixes_truncate(tmp_path):
    # One ~64K-char line: under the .jsonl cap (65536) but far over the normal
    # per-line cap (2000). The .jsonl read must keep it whole; the .py read must truncate.
    long_value = "x" * 64_000
    record = f'{{"k":"{long_value}"}}'
    context = ToolContext(cwd=tmp_path, max_line_chars=2_000, jsonl_max_line_chars=65_536)

    (tmp_path / "data.jsonl").write_text(record + "\n", encoding="utf-8")
    (tmp_path / "data.py").write_text(record + "\n", encoding="utf-8")

    jsonl_out = fs.read("data.jsonl", context=context)
    py_out = fs.read("data.py", context=context)

    assert "[truncated, line exceeds" not in jsonl_out
    assert long_value in jsonl_out
    assert "[truncated, line exceeds 2000 chars]" in py_out
    assert long_value not in py_out


def test_task_tool_calls_from_one_assistant_batch_run_in_parallel_and_restore_order():
    events: list[str] = []

    def sleeper(label: str, context: ToolContext | None = None) -> str:
        import time

        time.sleep(0.15 if label == "slow" else 0.01)
        events.append(label)
        return label.upper()

    registry = ToolRegistry(
        tools=(Tool("task", "test task", sleeper, {"label": {"type": "string"}}, required=("label",)),),
        aliases={"task": "task"},
    )
    calls = [
        runtime._PendingToolCall(id="slow", name="task", arg_chunks=['{"label":"slow"}']),
        runtime._PendingToolCall(id="fast", name="Task", arg_chunks=['{"label":"fast"}']),
    ]

    started = __import__("time").perf_counter()
    actual = runtime._dispatch_tool_calls(
        calls,
        runtime.Telemetry(None),
        cap_bytes=4096,
        trace=False,
        error_tracker=runtime.ToolErrorTracker(limit=2),
        registry=registry,
        tool_context=ToolContext(),
    )
    elapsed = __import__("time").perf_counter() - started

    assert [(pc.id, result) for pc, _, result in actual] == [("slow", "SLOW"), ("fast", "FAST")]
    assert events == ["fast", "slow"]
    assert elapsed < 0.25


def test_vision_enabled_for_model_detects_families(monkeypatch):
    from js import config

    monkeypatch.delenv("JS_VISION", raising=False)
    assert config.vision_enabled_for_model("ollama_chat/gemma4:e4b") is True
    assert config.vision_enabled_for_model("openai/qwen3-vl:235b-instruct") is True
    assert config.vision_enabled_for_model("openai/claude-opus-4-8") is True
    assert config.vision_enabled_for_model("anthropic/claude-sonnet-4") is True
    assert config.vision_enabled_for_model("openai/auto/glm-5.1") is False
    assert config.vision_enabled_for_model("gemma4-coder-q8-256k:latest") is False
    monkeypatch.setenv("JS_VISION", "1")
    assert config.vision_enabled_for_model("openai/auto/glm-5.1") is True
    monkeypatch.setenv("JS_VISION", "0")
    assert config.vision_enabled_for_model("ollama_chat/gemma4:e4b") is False


def test_run_turn_sends_image_once_and_persists_dehydrated_stub(tmp_path, monkeypatch):
    import json
    from js import config as js_config

    monkeypatch.delenv("JS_VISION", raising=False)
    monkeypatch.delenv("JS_IMAGE_RESULT_SHAPE", raising=False)
    image = tmp_path / "pixel.png"
    image.write_bytes(_TINY_PNG)
    b64 = base64.b64encode(_TINY_PNG).decode("ascii")

    context = ToolContext(cwd=tmp_path)
    monkeypatch.setattr(runtime_tools, "DEFAULT_CONTEXT", context)

    call_messages: list[list[ai.messages.Message]] = []

    def stream_model_stub(**kw):
        msgs = kw["messages"]
        call_messages.append(list(msgs))
        if len(call_messages) == 1:
            return ModelStreamResult(
                text="",
                tool_calls=[ModelToolCall(id="c1", name="read", arguments='{"file_path":"pixel.png"}')],
                reasoning="",
                usage=None,
                finish_reason="tool_calls",
                assistant_message=ai.messages.Message(
                    role="assistant",
                    parts=[ai.types.messages.ToolCallPart(
                        tool_call_id="c1", tool_name="read", tool_args='{"file_path":"pixel.png"}'
                    )],
                ),
            )
        return ModelStreamResult(
            text="I can see the image.",
            tool_calls=[],
            reasoning="",
            usage=None,
            finish_reason="stop",
            assistant_message=ai.messages.Message(
                role="assistant",
                parts=[ai.types.messages.TextPart(text="I can see the image.")],
            ),
        )

    monkeypatch.setattr(model_client, "stream_model", stream_model_stub)

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    agent_dir = tmp_path / ".js" / "sessions" / "test"
    cfg = js_config.Config(
        agent_id="test",
        agent_dir=agent_dir,
        model="vision-test-gemma4",
        provider_id=None,
        provider_base_url=None,
        provider_api_key=None,
        reasoning_effort=None,
        max_output_tokens=64,
        max_tool_iterations=5,
        max_bash_output_bytes=65536,
        max_tool_result_bytes=256 * 1024,
        fetch_timeout_s=5,
        debug_log=None,
        trace=False,
        history_file=tmp_path / ".history",
        sessions_dir=agent_dir,
        session_file=agent_dir / "session.jsonl",
        prompts_dir=prompts_dir,
    )

    class _Tel:
        def event(self, *args, **kwargs):
            pass

    messages: list[dict] = [{"role": "user", "content": "what's in pixel.png?"}]
    runtime.run_turn(cfg, "system", messages, _Tel())

    # Image bytes were sent exactly the turn it was read (2nd call), never the 1st.
    assert len(call_messages) == 2
    # First call: no files (only the user message, no tool result yet)
    assert not any(msg.files for msg in call_messages[0])
    # Second call: has a direct user FilePart after the dehydrated tool-result stub.
    assert any(
        msg.role == "user" and msg.files
        for msg in call_messages[1]
    )
    # Persisted history carries the dehydrated stub, never the base64 — no re-bill on replay.
    persisted = json.dumps(messages, default=str)
    assert b64 not in persisted
    assert any(m.get("role") == "tool" and "VISUAL_FILE" in str(m.get("content", "")) for m in messages)
    # Vision auto-enabled for the gemma model via the name heuristic.
    assert context.vision_enabled is True
