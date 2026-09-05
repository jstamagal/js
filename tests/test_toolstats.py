from __future__ import annotations

import json

from js import toolstats


def test_shell_habits_name_the_plain_unix_tool_reached_for():
    c = toolstats.classify_shell_command
    assert c("cat src/app.py") == {"read_via_shell": 1}
    assert c("head -50 a.py | tail -20") == {"read_via_shell": 2}
    assert c("grep -rn foo . && rg bar src") == {"search_via_shell": 2}
    assert c("find . -name '*.py' | xargs wc -l") == {"find_via_shell": 1}
    assert c("sed -i 's/a/b/' x.py") == {"edit_via_shell": 1}
    assert c("cd /tmp && ls") == {"cd": 1}
    assert c("rm -rf build") == {"rm": 1}
    assert c("printf 'x' > out.txt") == {"write_via_shell": 1}
    assert c("cat > new.py <<'EOF'\nprint(1)\nEOF") == {"read_via_shell": 1, "write_via_shell": 1}
    assert c("echo done") == {"echo_only": 1}


def test_stderr_and_devnull_redirects_are_not_writes():
    c = toolstats.classify_shell_command
    assert c("pytest -q 2>&1") == {}
    assert c("make >/dev/null 2>&1") == {}
    assert c("FOO=1 env bar --flag") == {}


def test_summarize_counts_calls_errors_turns_and_bytes(tmp_path):
    session = tmp_path / "s.jsonl"

    def message(ts, **msg):
        return json.dumps({"kind": "message", "ts": ts, "version": 1, "message": msg})

    lines = [
        json.dumps({"kind": "session_metadata", "ts": 100.0, "agent": "full", "model": "m"}),
        message(101.0, role="user", content="fix it"),
        message(102.0, role="assistant", content="", tool_calls=[
            {"id": "1", "type": "function", "function": {"name": "shell", "arguments": json.dumps({"command": "cat a.py && cd src"})}},
            {"id": "2", "type": "function", "function": {"name": "read", "arguments": json.dumps({"file_path": "a.py"})}},
        ]),
        message(103.0, role="tool", tool_call_id="1", name="shell", content="ok"),
        message(103.5, role="tool", tool_call_id="2", name="read", content="ERROR: no such file"),
        message(110.0, role="assistant", content="Done."),
    ]
    session.write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = toolstats.summarize(session)
    assert summary["agent"] == "full"
    assert summary["turns"] == 2
    assert summary["tool_calls"] == 2
    assert summary["calls_by_tool"] == {"read": 1, "shell": 1}
    assert summary["tool_errors"] == 1
    assert summary["errors_by_tool"] == {"read": 1}
    assert summary["shell_habits"] == {"cd": 1, "read_via_shell": 1}
    assert summary["result_bytes"] == len("ok") + len("ERROR: no such file")
    assert summary["duration_s"] == 10.0
    assert summary["final_text_chars"] == len("Done.")


def test_cli_prints_one_tagged_line_with_extras(tmp_path, capsys):
    data = tmp_path / "js" / "sessions" / "full"
    data.mkdir(parents=True)
    (data / "a.jsonl").write_text(json.dumps({"kind": "session_metadata", "ts": 1.0, "agent": "full", "model": "m"}) + "\n")
    assert toolstats.main(["--latest", "--data-dir", str(tmp_path / "js"), "--agent", "full", "--tag", "TOOLSTATS", "--extra", "exit=0"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("TOOLSTATS {")
    assert json.loads(line[len("TOOLSTATS "):])["exit"] == "0"
