"""Pi bash behavior through the harness tool contract."""

from pathlib import Path

from js.toolkit import pi_bash
from js.toolkit.core import ToolContext, call_tool


def run(cwd, command, **kwargs):
    return call_tool(pi_bash.tools()[0], {"command": command, **kwargs}, ToolContext(cwd=cwd))


def test_command_schema_and_combined_output(tmp_path):
    schema = pi_bash.tools()[0].openai_spec()["function"]["parameters"]
    assert schema["required"] == ["command"]
    assert set(schema["properties"]) == {"command", "timeout"}
    out = run(tmp_path, "printf first; printf second >&2; printf third; false | true")
    assert not out.is_error
    assert out.dehydrated() == "firstsecondthird"
    failed = run(tmp_path, "exit 17")
    assert failed.is_error and "code 17" in failed.dehydrated()


def test_timeout_terminates_process_group(tmp_path):
    result = run(tmp_path, "printf started; sleep 5", timeout=0.1)
    assert result.is_error and "started" in result.dehydrated()
    assert "timed out after 0.1 seconds" in result.dehydrated()


def test_tail_and_full_output(tmp_path, monkeypatch):
    monkeypatch.setattr(pi_bash.paths, "tool_results_dir", lambda: tmp_path / "spills")
    result = run(tmp_path, "seq 2003")
    assert not result.is_error
    text = result.dehydrated()
    assert text.startswith("4\n5\n")
    assert "2003\n\n[Showing lines 4-2003" in text
    saved = Path(text.partition("Full output: ")[2].removesuffix("]"))
    assert saved.read_text().splitlines() == [str(i) for i in range(1, 2004)]


def test_long_line_keeps_the_end(tmp_path, monkeypatch):
    monkeypatch.setattr(pi_bash.paths, "tool_results_dir", lambda: tmp_path)
    result = run(tmp_path, "head -c 180000 /dev/zero | tr '\\0' x; printf last")
    text, _, note = result.dehydrated().partition("\n\n[")
    assert text == "x" * (50 * 1024 - 4) + "last"
    assert "Full output:" in note


def test_shell_environment_is_filtered(tmp_path, monkeypatch):
    monkeypatch.setenv("JS_PI_UNLISTED", "private value")
    assert run(tmp_path, 'printf "%s" "${JS_PI_UNLISTED-unset}"').dehydrated() == "unset"


def test_timeout_covers_background_writer_after_shell_exit(tmp_path):
    result = run(tmp_path, "(while true; do echo tick; sleep 0.02; done) &", timeout=0.3)
    assert result.is_error and "timed out" in result.dehydrated()
