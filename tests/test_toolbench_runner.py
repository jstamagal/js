from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

RUNNER = Path(__file__).resolve().parents[1] / "bench" / "toolbench" / "run.py"
spec = importlib.util.spec_from_file_location("toolbench_run", RUNNER)
run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run)


def test_suite_file_parses_and_references_available_agent_assets():
    suite = run.load_suite(RUNNER.with_name("suite.toml"))
    assert suite["agents"]
    assert suite["repos"]
    for agent in suite["agents"]:
        if agent["kind"] == "js":
            assert (RUNNER.parent / "agents" / agent["agent"] / "00-tools.yaml").exists()
            assert agent["descriptions"] in ("slim", "stock")


def test_selection_respects_defaults_and_explicit_names():
    items = [
        {"name": "first", "default": True},
        {"name": "optional", "default": False},
        {"name": "implicit"},
    ]
    assert run.selected(items, None, default_only=True) == [items[0], items[2]]
    assert run.selected(items, None, default_only=False) == items
    assert run.selected(items, " implicit, optional ", default_only=True) == [items[2], items[1]]
    with pytest.raises(SystemExit):
        run.selected(items, "missing", default_only=False)


def test_js_agent_command_runs_in_the_sandbox_with_its_variant(tmp_path):
    entry = run.agent_entry({"name": "js-full-slim", "kind": "js", "agent": "bench-full", "descriptions": "slim"}, tmp_path)
    command = entry["command"]
    assert command.startswith(str(run.SANDBOX))
    assert "--prompt {{promptFile}}" in command
    assert f"--telemetry {tmp_path / 'js-full-slim'}" in command
    assert command.endswith("env JS_TOOL_DESCRIPTIONS=slim js-bench-agent bench-full /prompt.md")
    assert run.agent_entry({"name": "fake-noop", "kind": "builtin"}, tmp_path) == {"name": "fake-noop", "enabled": True}


def test_install_and_test_commands_are_wrapped_for_the_sandbox():
    wrapped = run.sandboxed("uv pip install -e . 'pytest<9'")
    assert wrapped.startswith(f"{run.SANDBOX} -- sh -lc '")
    assert "'\\''pytest<9'\\''" in wrapped  # inner single quotes survive the outer quoting


def test_base_url_hostnames_become_ips_for_the_container():
    assert run.resolve_base_url("http://10.1.2.3:8080/v1") == "http://10.1.2.3:8080/v1"
    assert run.resolve_base_url("http://localhost:42069/v1") == "http://127.0.0.1:42069/v1"


def test_toolstats_line_is_found_inside_a_result_record():
    stats = {"tool_calls": 3, "calls_by_tool": {"shell": 3}}
    record = {"agentName": "js-full-slim", "agent": {"output": "warning: bat not found\nTOOLSTATS " + json.dumps(stats) + "\n"}}
    assert run.find_toolstats(record) == stats
    assert run.find_toolstats({"output": "no stats here"}) is None


def test_summary_table_aggregates_per_agent():
    rows = [
        {"repo": "click", "task": "task-001", "agent": "js-full-slim", "status": "completed", "solved": True, "score": 90,
         "tests_passed": True, "hidden_passed": True, "duration_s": 60.0,
         "toolstats": {"tool_calls": 10, "tool_errors": 1, "result_bytes": 2048, "shell_habits": {"cd": 2, "read_via_shell": 1}, "calls_by_tool": {"shell": 6, "read": 4}}},
        {"repo": "click", "task": "task-001", "agent": "js-full-stock", "status": "completed", "solved": False, "score": 20,
         "tests_passed": False, "hidden_passed": False, "duration_s": 120.0, "toolstats": None},
    ]
    text = run.summarize(rows)
    assert "| js-full-slim | 1 | 1 | 1 | 60 | 10.0 | 1.0 | 1 | 0 | 0 | 2 | 2.0 |" in text
    assert "| js-full-stock | 1 | 0 | 0 | 120 | - | - | - | - | - | - | - |" in text
    assert "| click | task-001 | js-full-slim | completed | yes | pass | 60 | 10 | shell 6, read 4 |" in text
