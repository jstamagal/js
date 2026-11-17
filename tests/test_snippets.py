"""Instruction resolution and substitution are runtime behavior."""
from pathlib import Path

import pytest

from js import paths, snippets
from js.toolkit import descriptions


def test_snippets_preserve_whitespace_and_do_not_rescan_values(monkeypatch, tmp_path):
    path = tmp_path / "snippets" / "fixture.md"
    path.parent.mkdir()
    path.write_text("\n  {{BODY}} / {{BODY}} / {{OTHER}}\n")
    monkeypatch.setattr(paths, "find", lambda relative: tmp_path / relative, raising=False)
    monkeypatch.setattr(paths, "layers", lambda: (tmp_path,), raising=False)

    assert snippets.render("fixture", BODY="{{OTHER}} !{sh false}", OTHER="value") == (
        "\n  {{OTHER}} !{sh false} / {{OTHER}} !{sh false} / value\n"
    )
    path.write_text("edited {{BODY}}")
    assert snippets.render("fixture", BODY="now") == "edited now"


def test_snippets_resolve_each_call_and_report_all_locations(monkeypatch, tmp_path):
    stock, home = tmp_path / "stock", tmp_path / "home"
    for root, text in [(stock, "stock"), (home, "home")]:
        (root / "snippets").mkdir(parents=True)
        (root / "snippets/fixture.md").write_text(text)
    selected = [stock]
    monkeypatch.setattr(paths, "find", lambda relative: selected[0] / relative, raising=False)
    monkeypatch.setattr(paths, "layers", lambda: (stock, home), raising=False)
    assert snippets.render("fixture") == "stock"
    selected[0] = home
    assert snippets.render("fixture") == "home"
    with pytest.raises(snippets.SnippetError) as exc:
        snippets.render("absent")
    assert str(stock / "snippets/absent.md") in str(exc.value)
    assert str(home / "snippets/absent.md") in str(exc.value)


def test_snippets_missing_value_is_a_hard_error(monkeypatch, tmp_path):
    path = tmp_path / "fixture.md"
    path.write_text("{{REQUIRED}}")
    monkeypatch.setattr(paths, "find", lambda relative: path, raising=False)
    with pytest.raises(snippets.SnippetError, match="REQUIRED"):
        snippets.render("fixture")


def test_snippets_unreadable_file_is_a_hard_error(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "find", lambda relative: tmp_path, raising=False)
    monkeypatch.setattr(paths, "layers", lambda: (tmp_path,), raising=False)
    with pytest.raises(snippets.SnippetError, match="searched"):
        snippets.render("fixture")


def test_parameter_prose_is_loaded_separately_from_tool_contract(monkeypatch, tmp_path):
    path = tmp_path / "fixture.md"
    path.write_text("Tool contract.\n\n<!--parameters-->\n\n### path\n\nPath {{LABEL}}.\n")
    monkeypatch.setattr(paths, "find", lambda relative: tmp_path / Path(relative).name)
    descriptions._load_description.cache_clear()
    try:
        assert descriptions.load_description("fixture") == "Tool contract."
        assert descriptions.load_parameter_description("fixture", "path", LABEL="{{LITERAL}}") == "Path {{LITERAL}}."
        with pytest.raises(ValueError, match="absent"):
            descriptions.load_parameter_description("fixture", "absent")
    finally:
        descriptions._load_description.cache_clear()


def test_output_limit_recovery_reads_the_current_snippet(monkeypatch, tmp_path):
    from js import runtime
    from js.turn_budget import TurnConvo

    path = tmp_path / "resume.md"
    monkeypatch.setattr(paths, "find", lambda relative: path, raising=False)
    messages = []
    convo = TurnConvo("", messages, provider_id=None, model="fixture")
    for text in ["continue A", "continue B"]:
        path.write_text(text)
        runtime._send_resume_nudge(
            convo, n=1, limit=2, incomplete_reason="max_output_tokens",
            model="fixture", telemetry=runtime.Telemetry(None), suppress_output=True,
        )
    assert [message["content"] for message in messages] == ["continue A", "continue B"]
    assert [message.text for message in convo.ai] == ["continue A", "continue B"]


@pytest.mark.parametrize("text", ["", "\n  "])
def test_empty_tool_file_suppresses_parameter_prose(monkeypatch, tmp_path, text):
    (tmp_path / "fixture.md").write_text(text)
    monkeypatch.setattr(paths, "find", lambda relative: tmp_path / Path(relative).name)
    assert descriptions.load_parameter_description("fixture", "path") == ""


def test_snippets_report_invalid_utf8_as_unreadable(monkeypatch, tmp_path):
    path = tmp_path / "fixture.md"
    path.write_bytes(b"\xff")
    monkeypatch.setattr(paths, "find", lambda relative: path, raising=False)
    monkeypatch.setattr(paths, "layers", lambda: (tmp_path,), raising=False)
    with pytest.raises(snippets.SnippetError, match="searched"):
        snippets.render("fixture")


def test_missing_commit_snippet_exits_two_before_model_request(monkeypatch, tmp_path, capsys):
    from types import SimpleNamespace

    from js import cli

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    real_find = paths.find
    monkeypatch.setattr(paths, "find", lambda relative: (
        tmp_path / "missing" / relative if str(relative).startswith("snippets/") else real_find(relative)
    ))
    monkeypatch.setattr(paths, "layers", lambda: (tmp_path / "stock", tmp_path / "home"), raising=False)
    monkeypatch.setattr(cli, "_run_prompt", lambda *a, **k: pytest.fail("model request started"))
    target = tmp_path / "repo"
    target.mkdir()

    assert cli.main(["--commit", str(target), "--no-save"]) == 2
    error = capsys.readouterr().err
    assert "commit-task" in error
    assert str(tmp_path / "stock/snippets/commit-task.md") in error
    assert str(tmp_path / "home/snippets/commit-task.md") in error


@pytest.mark.parametrize("mode", ["prompt", "repl", "json"])
def test_missing_runtime_snippet_exits_two(monkeypatch, tmp_path, capsys, mode):
    from dataclasses import replace

    from test_cli_prompt_mode import _fake_stream_result, _prompt_cfg
    from turn_driver import js

    from js import cli, runtime
    from js.toolkit import ToolContext

    monkeypatch.setenv("HOME", str(tmp_path))
    prompt_dir = tmp_path / "prompts"
    prompt_dir.mkdir()
    (prompt_dir / "01-prompt.md").write_text("SYSTEM\n")
    cfg = replace(_prompt_cfg(tmp_path, prompt_dir, "runtime"), max_output_tokens=4096)
    monkeypatch.setattr(cli, "_from_env", lambda *a, **k: cfg)
    monkeypatch.setattr(runtime.T, "STOCK_CONTEXT", ToolContext(cwd=tmp_path))
    monkeypatch.setattr(runtime, "_resolve_context_window", lambda *a, **k: 1_000_000)
    monkeypatch.setattr(runtime.model_metadata, "resolve_max_output", lambda *a, **k: 4096)
    real_find = paths.find
    monkeypatch.setattr(paths, "find", lambda relative: (
        tmp_path / "absent" / relative if str(relative).startswith("snippets/") else real_find(relative)
    ))
    calls = []

    def model(**kwargs):
        calls.append(kwargs)
        return replace(_fake_stream_result("partial"), finish_reason="incomplete:max_output_tokens",
                       incomplete_reason="max_output_tokens")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", model)
    monkeypatch.chdir(tmp_path)
    if mode == "prompt":
        assert js(monkeypatch, "-p", "go") == 2
    elif mode == "json":
        assert js(monkeypatch, "--json", "-p", "go") == 2
    else:
        from repl_driver import stub_screen

        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        stub_screen(monkeypatch, ["go"])
        assert cli.main([]) == 2
    assert len(calls) == 1
    assert "output-limit-resume" in capsys.readouterr().err


def test_missing_rehydration_snippet_is_not_a_file_read_failure(monkeypatch, tmp_path):
    from js import compaction
    from js.toolkit import ToolContext

    path = tmp_path / "binary.dat"
    path.write_bytes(b"\x00data")
    context = ToolContext()
    context.reads.remember(path, "read")
    real_find = paths.find
    monkeypatch.setattr(paths, "find", lambda relative: (
        tmp_path / "missing" / relative if str(relative).startswith("snippets/") else real_find(relative)
    ))
    with pytest.raises(snippets.SnippetError, match="compaction-file-nontext"):
        compaction._post_compact_rehydration(context)


def test_tool_dispatch_propagates_snippet_configuration_failure(monkeypatch, tmp_path):
    from test_runtime_offline_integration import model_tool_call_result, offline_config

    from js import runtime
    from js.toolkit import Tool, ToolContext, ToolRegistry

    real_find = paths.find
    monkeypatch.setattr(paths, "find", lambda relative: (
        tmp_path / "missing" / relative if str(relative).startswith("snippets/") else real_find(relative)
    ))
    tool = Tool("fixture", "fixture", lambda: snippets.render("absent"), {})
    registry = ToolRegistry((tool,), {"fixture": "fixture"})
    monkeypatch.setattr(runtime.model_client, "stream_model_async", lambda **kwargs: (
        model_tool_call_result("fixture", ["{}"])))
    monkeypatch.setattr(runtime, "_resolve_context_window", lambda *a, **k: 1_000_000)
    monkeypatch.setattr(runtime.model_metadata, "resolve_max_output", lambda *a, **k: 4096)
    with pytest.raises(snippets.SnippetError, match="absent"):
        runtime.run_turn(
            offline_config(tmp_path), "SYSTEM", [{"role": "user", "content": "go"}],
            runtime.Telemetry(None), tool_registry=registry,
            tool_context=ToolContext(cwd=tmp_path), suppress_output=True,
        )


@pytest.mark.parametrize("mode", ["sync", "async", "supervised"])
def test_worker_snippet_failures_are_not_tool_results(mode):
    import asyncio

    from js import supervisor
    from js.toolkit import subagents

    error = snippets.SnippetError("missing worker instruction")

    async def worker(index, item):
        raise error

    async def drive():
        previous = supervisor.get_current()
        if mode == "supervised":
            supervisor.set_current(supervisor.Supervisor(asyncio.get_running_loop()))
        try:
            await subagents._fan_out_async([(1, "work")], worker)
        finally:
            supervisor.set_current(previous)

    with pytest.raises(snippets.SnippetError, match="worker instruction"):
        if mode == "sync":
            subagents._fan_out([(1, "work")], worker)
        else:
            asyncio.run(drive())


def test_json_stream_preserves_snippet_exit_status(capsys):
    import json

    from js import cli

    def run():
        raise snippets.SnippetError("missing instruction; searched: fixture")

    assert cli._json_run(run) == 2
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["type"] == "end"
    assert events[-1]["exit_code"] == 2


def test_background_worker_preserves_snippet_error(monkeypatch, tmp_path):
    from concurrent.futures import Future

    from js.toolkit import task_jobs

    error = snippets.SnippetError("missing background instruction")
    outcome = Future()
    outcome.set_exception(error)
    job = task_jobs.TaskJob("fixture", "worker", 1, 0, tmp_path / "result", "owner")
    settled = []
    task_jobs._settle(job, outcome, lambda: settled.append(True))
    assert settled == [True]
    with pytest.raises(snippets.SnippetError, match="background instruction"):
        job.result()
    monkeypatch.setattr(task_jobs, "_JOBS", {job.id: job})
    with pytest.raises(snippets.SnippetError, match="background instruction"):
        task_jobs.completion_notes("owner")
