"""Stats output failures remain CLI errors without hiding model results."""

import ai
import pytest

from js import cli, messages, runtime, stats
from js.model_client import ModelStreamResult


def result(text):
    return ModelStreamResult(text=text, tool_calls=[], reasoning="", usage=ai.types.usage.Usage(),
                             finish_reason="stop", assistant_message=ai.assistant_message(text))


@pytest.mark.parametrize("kind", ["json", "csv"])
@pytest.mark.parametrize("answer", ["answer", ""])
def test_prompt_stats_directory_reports_error_and_preserves_turn_result(tmp_path, monkeypatch, capsys, kind, answer):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runtime.model_client, "stream_model_async", lambda **kwargs: result(answer))
    target = tmp_path / "output"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_text("keep")

    assert cli._run_prompt("question", save=False, **{f"stats_{kind}": str(target)}) == 1

    captured = capsys.readouterr()
    assert str(target) in captured.err
    assert "Traceback" not in captured.err
    if answer:
        assert answer in captured.out
    else:
        assert messages.NO_ANSWER.words() in captured.err
    assert sentinel.read_text() == "keep"
    assert not list(tmp_path.glob(".stats-*.tmp"))


def test_prompt_stats_write_failure_does_not_replace_existing_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runtime.model_client, "stream_model_async", lambda **kwargs: result("answer"))
    target = tmp_path / "stats.json"
    target.write_text("original")

    def fail(*args):
        raise PermissionError("injected output failure")

    monkeypatch.setattr(stats.os, "replace", fail)
    assert cli._run_prompt("question", save=False, stats_json=str(target)) == 1
    captured = capsys.readouterr()
    assert "injected output failure" in captured.err
    assert "answer" in captured.out
    assert target.read_text() == "original"
    assert not list(tmp_path.glob(".stats-*.tmp"))


def test_original_turn_failure_is_not_hidden_by_requested_stats(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    def fail(**kwargs):
        raise RuntimeError("original turn failure")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", fail)
    assert cli._run_prompt("question", save=False, stats_json=str(tmp_path)) == 1
    assert "original turn failure" in capsys.readouterr().err


@pytest.mark.parametrize("interrupt, expected", [(False, 1), (True, 130)])
def test_benchmark_stats_failure_preserves_interrupted_status(tmp_path, monkeypatch, capsys, interrupt, expected):
    monkeypatch.chdir(tmp_path)
    agent = tmp_path / ".js" / "agents" / "stats-check"
    agent.mkdir(parents=True)
    (agent / "01-prompt.md").write_text("Answer tersely.")
    (agent / "01-benchmark.md").write_text("question")

    def stream(**kwargs):
        if interrupt:
            raise KeyboardInterrupt
        return result("answer")

    monkeypatch.setattr(runtime.model_client, "stream_model_async", stream)
    assert cli._run_bench(
        "stats-check", model=None, reasoning=None, maxout=None, quiet=True, extras=None,
        ignore_local_config=False, ignore_global_config=False, presets=None,
        stats_json=str(tmp_path), stats_csv=None,
    ) == expected
    captured = capsys.readouterr()
    assert str(tmp_path) in captured.err
    assert "Traceback" not in captured.err


def test_stats_helper_attempts_remaining_output_and_does_not_swallow_programming_errors(tmp_path, monkeypatch, capsys):
    csv_path = tmp_path / "stats.csv"
    assert not cli._write_requested_stats(str(tmp_path), str(csv_path), {}, [])
    assert csv_path.is_file()
    assert capsys.readouterr().err

    def broken(*args):
        raise TypeError("programming error")

    monkeypatch.setattr(stats, "write_json", broken)
    with pytest.raises(TypeError, match="programming error"):
        cli._write_requested_stats(str(tmp_path / "stats.json"), None, {}, [])
