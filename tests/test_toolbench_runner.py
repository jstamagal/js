from __future__ import annotations

import importlib.util
import json
import shutil
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


def test_js_agent_command_runs_in_the_sandbox(tmp_path):
    entry = run.agent_entry({"name": "js-full", "kind": "js", "agent": "bench-full"}, tmp_path)
    command = entry["command"]
    assert command.startswith(str(run.SANDBOX))
    assert "--prompt {{promptFile}}" in command
    assert f"--telemetry {tmp_path / 'js-full'}" in command
    assert command.endswith("-- js-bench-agent bench-full /prompt.md")
    assert run.agent_entry({"name": "fake-noop", "kind": "builtin"}, tmp_path) == {"name": "fake-noop", "enabled": True}


def test_install_and_test_commands_are_wrapped_for_the_sandbox():
    wrapped = run.sandboxed("uv pip install -e . 'pytest<9'")
    assert wrapped.startswith(f"{run.SANDBOX} -- sh -lc '")
    assert "'\\''pytest<9'\\''" in wrapped  # inner single quotes survive the outer quoting


def test_a_second_run_cannot_rewrite_the_shared_reporacer_config(tmp_path):
    work = tmp_path / "work"
    suite = run.load_suite(RUNNER.with_name("suite.toml"))
    repo = suite["repos"][0]
    repo_dir = work / "repos" / repo["name"]
    (repo_dir / ".reporacer").mkdir(parents=True)
    config = repo_dir / ".reporacer" / "config.json"
    config.write_text("{}", encoding="utf-8")
    first_arm, second_arm = suite["agents"][:2]

    with run.workspace_lock(work):
        run.write_config(repo_dir, suite, repo, [first_arm], 1, work / "telemetry-first")
        selected = json.loads(config.read_text(encoding="utf-8"))["agents"]

        with pytest.raises(SystemExit):
            with run.workspace_lock(work):
                run.write_config(repo_dir, suite, repo, [second_arm], 1, work / "telemetry-second")

        assert json.loads(config.read_text(encoding="utf-8"))["agents"] == selected


def test_distinct_work_directories_do_not_contend(tmp_path):
    with run.workspace_lock(tmp_path / "first"):
        with run.workspace_lock(tmp_path / "second"):
            pass


def test_base_url_hostnames_become_ips_for_the_container():
    assert run.resolve_base_url("http://10.1.2.3:8080/v1") == "http://10.1.2.3:8080/v1"
    assert run.resolve_base_url("http://localhost:42069/v1") == "http://127.0.0.1:42069/v1"


def test_toolstats_line_is_found_inside_a_result_record():
    stats = {"tool_calls": 3, "calls_by_tool": {"shell": 3}}
    record = {"agentName": "js-full", "agent": {"output": "warning: bat not found\nTOOLSTATS " + json.dumps(stats) + "\n"}}
    assert run.find_toolstats(record) == stats
    assert run.find_toolstats({"output": "no stats here"}) is None


def test_collect_joins_toolstats_from_the_reporacer_log_and_keeps_the_log(tmp_path):
    source = tmp_path / "run"
    saved = tmp_path / "saved"
    (source / "logs").mkdir(parents=True)
    stats = {"tool_calls": 7, "tool_errors": 1, "calls_by_tool": {"read": 7}}
    log = source / "logs" / "task-001-agent.log"
    log.write_text("TOOLSTATS " + json.dumps(stats) + "\n", encoding="utf-8")
    record = {
        "taskId": "task-001",
        "agentName": "agent",
        "status": "completed",
        "scores": {"solved": True, "final": 90},
        "logPath": str(log),
    }
    (source / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    rows = run.collect(source, "example", saved)

    assert rows[0]["toolstats"]["tool_calls"] == 7
    assert (saved / "reporacer" / "example" / "logs" / log.name).is_file()
    assert "| task-001 | agent | completed | yes | - | 0 | 7 | read 7 |" in run.summarize(rows)


def test_report_regeneration_reads_toolstats_after_the_workspace_is_removed(tmp_path):
    source = tmp_path / "run"
    saved = tmp_path / "saved"
    (source / "logs").mkdir(parents=True)
    stats = {"tool_calls": 7, "calls_by_tool": {"read": 7}}
    log = source / "logs" / "task-001-agent.log"
    log.write_text("TOOLSTATS " + json.dumps(stats) + "\n", encoding="utf-8")
    record = {"taskId": "task-001", "agentName": "agent", "status": "completed", "logPath": str(log)}
    (source / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    run.collect(source, "example", saved)
    shutil.rmtree(source)

    rows = run.collect(saved / "reporacer" / "example", "example", saved)

    assert rows[0]["toolstats"]["tool_calls"] == 7


def test_collect_keeps_inline_toolstats_when_the_record_carries_them(tmp_path):
    source = tmp_path / "run"
    source.mkdir()
    stats = {"tool_calls": 3}
    record = {"taskId": "task-001", "agentName": "agent", "output": "TOOLSTATS " + json.dumps(stats)}
    (source / "results.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")

    rows = run.collect(source, "example", tmp_path / "saved")

    assert rows[0]["toolstats"] == stats


def test_summary_table_aggregates_per_agent():
    rows = [
        {"repo": "click", "task": "task-001", "agent": "js-full", "status": "completed", "solved": True, "score": 90,
         "tests_passed": True, "hidden_passed": True, "duration_s": 60.0,
         "toolstats": {"tool_calls": 10, "tool_errors": 1, "result_bytes": 2048, "shell_habits": {"cd": 2, "read_via_shell": 1}, "calls_by_tool": {"shell": 6, "read": 4}}},
        {"repo": "click", "task": "task-001", "agent": "js-shell", "status": "completed", "solved": False, "score": 20,
         "tests_passed": False, "hidden_passed": False, "duration_s": 120.0, "toolstats": None},
    ]
    text = run.summarize(rows)
    assert "| js-full | 1 | 1 | 1 | 60 | 10.0 | 1.0 | 1 | 0 | 0 | 2 | 2.0 |" in text
    assert "| js-shell | 1 | 0 | 0 | 120 | - | - | - | - | - | - | - |" in text
    assert "| click | task-001 | js-full | completed | yes | pass | 60 | 10 | shell 6, read 4 |" in text
