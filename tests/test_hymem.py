"""HYMEM: what is sent to the memory server, and which repository it is filed under."""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

from js import hymem


def _turns(n):
    out = []
    for i in range(n):
        out += [{"role": "user", "content": f"u{i}"}, {"role": "assistant", "content": f"a{i}"}]
    return out


def test_a_running_session_sends_only_whole_chunks_and_the_rest_at_the_end():
    messages = _turns(23)
    running = list(hymem.chunks(messages, 0, final=False))
    assert [len([m for m in c if m["role"] == "user"]) for _, c in running] == [10, 10]
    sent = running[-1][0]
    assert list(hymem.chunks(messages, sent, final=False)) == []
    (end, rest), = hymem.chunks(messages, sent, final=True)
    assert end == len(messages) and [m["content"] for m in rest if m["role"] == "user"] == ["u20", "u21", "u22"]


def test_a_worktree_files_under_its_main_checkout(tmp_path):
    main = tmp_path / "repo"
    main.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], cwd=main, check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], cwd=main, check=True)
    subprocess.run([*git, "worktree", "add", "-q", str(tmp_path / "wt"), "-b", "wt"], cwd=main, check=True)
    assert hymem.workspace(tmp_path / "wt") == hymem.workspace(main) == str(main.resolve())
    assert hymem.workspace(tmp_path) == str(tmp_path.resolve())


def test_what_memory_put_in_front_of_the_model_is_not_sent_back():
    messages = hymem._unrecalled([
        {"role": "user", "content": "fix the build\n\n<workspace_memory repo=\"/r\">\n- old lesson\n</workspace_memory>"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "memory_search", "arguments": "{}"}},
            {"id": "c2", "function": {"name": "shell", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "- old lesson"},
        {"role": "tool", "tool_call_id": "c2", "content": "make: ok"},
    ])
    assert "old lesson" not in json.dumps(messages)
    assert messages[0]["content"] == "fix the build"
    assert [c["id"] for c in messages[1]["tool_calls"]] == ["c2"]
    assert [m["content"] for m in messages if m["role"] == "tool"] == ["make: ok"]


def test_only_memories_jev_judges_helpful_pass_most_helpful_first(monkeypatch, tmp_path):
    found = {"experiences": [{"task": "gorilla search", "solution": "chest beats", "score": 0.9},
                             {"task": "build fix", "solution": "pass -j1", "score": 0.4}],
             "facts": [{"statement": "the build needs -j1", "score": 0.5}]}
    monkeypatch.setattr(hymem, "_post", lambda *a, **k: found)
    monkeypatch.setattr(hymem, "_relevance", lambda cfg, q, memories: [0.05, 0.9, 0.7])
    cfg = SimpleNamespace(hymem_url="http://m", hymem_user="u", hymem_recall_max=8,
                          hymem_relevance_min=0.5, hymem_score_min=0.6)
    assert hymem.search(cfg, "fix the build", tmp_path) == ["- build fix: pass -j1", "- the build needs -j1"]
    monkeypatch.setattr(hymem, "_relevance", lambda cfg, q, memories: None)
    assert hymem.search(cfg, "fix the build", tmp_path) == ["- gorilla search: chest beats"]


def test_a_memory_whose_files_changed_after_it_was_written_says_so(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

    def commit(day):
        (repo / "fs.py").write_text(day)
        env = {"PATH": "/usr/bin:/bin", "GIT_COMMITTER_DATE": f"{day}T12:00:00Z", "GIT_AUTHOR_DATE": f"{day}T12:00:00Z"}
        subprocess.run(["git", "add", "fs.py"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", day],
                       cwd=repo, check=True, env=env)

    commit("2026-10-01")
    commit("2026-10-15")
    root = str(repo.resolve())
    assert hymem.changed_since(root, ["fs.py", str(repo), "~/nowhere"], "2026-10-10T00:00:00") == (1, ["fs.py"])
    assert hymem.changed_since(root, [str(repo / "fs.py")], "2026-10-16T00:00:00") == (0, [])
