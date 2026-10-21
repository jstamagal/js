"""HYMEM: what is sent to the memory server, and which repository it is filed under."""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

from js import hymem
from js import memory as M
from js.toolkit import jev


def _turns(n):
    out = []
    for i in range(n):
        out += [{"role": "user", "content": f"u{i}"}, {"role": "assistant", "content": f"a{i}"}]
    return out


def _filing(monkeypatch, tmp_path):
    """A memory server that records each add's message contents; returns
    (cfg for ``session.jsonl`` in ``tmp_path``, the adds)."""
    adds = []
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    monkeypatch.setattr(hymem._paths, "state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(hymem, "_post", lambda cfg, path, body, timeout: adds.append(
        [m["content"] for m in body["messages"]]) or {"queued": "x"})
    cfg = SimpleNamespace(hymem_url="http://m", hymem_user="u", session_file=str(tmp_path / "session.jsonl"))
    return cfg, adds


def test_a_rolled_back_answer_is_never_filed(monkeypatch, tmp_path):
    cfg, adds = _filing(monkeypatch, tmp_path)
    session = tmp_path / "session.jsonl"
    M.persist_messages(session, [{"role": "user", "content": "u0"}, {"role": "assistant", "content": "wrong"}])
    M.persist_messages(session, [{"role": "user", "content": "u0"}, {"role": "assistant", "content": "right"}])
    hymem.file(cfg, tmp_path, final=True)
    assert adds == [["u0", "right"]]


def test_a_rollback_into_what_was_sent_files_that_turn_again(monkeypatch, tmp_path):
    cfg, adds = _filing(monkeypatch, tmp_path)
    session = tmp_path / "session.jsonl"
    M.persist_messages(session, [{"role": "user", "content": "u0"}, {"role": "assistant", "content": "wrong"}])
    hymem.file(cfg, tmp_path, final=True)
    M.persist_messages(session, [{"role": "user", "content": "u0"}, {"role": "assistant", "content": "right"}])
    hymem.file(cfg, tmp_path, final=True)
    assert adds == [["u0", "wrong"], ["u0", "right"]]


def test_a_damaged_sent_mark_does_not_stop_the_run(monkeypatch, tmp_path):
    cfg, adds = _filing(monkeypatch, tmp_path)
    M.persist_messages(tmp_path / "session.jsonl", _turns(1))
    (tmp_path / "state" / "hymem").mkdir(parents=True)
    (tmp_path / "state" / "hymem" / "session.sent").write_text("")
    hymem.file(cfg, tmp_path, final=True)
    assert adds == [["u0", "a0"]]


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
                          hymem_relevance_min=0.5)
    assert hymem.search(cfg, "fix the build", tmp_path) == ["- build fix: pass -j1", "- the build needs -j1"]


def test_jev_down_means_no_memory_not_unjudged_memory(monkeypatch, tmp_path):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    monkeypatch.setattr(hymem, "_post", lambda *a, **k: {"facts": [{"statement": "the build needs -j1"}]})
    monkeypatch.setattr(jev, "_client", lambda model: (_ for _ in ()).throw(RuntimeError("no key")))
    cfg = SimpleNamespace(hymem_url="http://m", hymem_user="u", hymem_recall_max=8, hymem_relevance_min=0.5)
    assert hymem.recall_block(cfg, "fix the build", tmp_path) == ""


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


def test_memory_is_off_without_the_jev_key(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert not hymem.enabled(SimpleNamespace(hymem_url="http://m"))
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    assert hymem.enabled(SimpleNamespace(hymem_url="http://m"))


def test_the_server_token_is_sent_when_set(monkeypatch):
    seen = []

    class _Reply:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"{}"

    monkeypatch.setenv("HYMEM_TOKEN", "sekrit")
    monkeypatch.setattr(hymem.urllib.request, "urlopen", lambda req, timeout: seen.append(req) or _Reply())
    hymem._post(SimpleNamespace(hymem_url="http://m"), "/search", {}, 1)
    assert seen[0].get_header("Authorization") == "Bearer sekrit"


def test_repl_first_turn_carries_recalled_memory(monkeypatch):
    from js import attach, cli
    monkeypatch.setattr(cli.hymem, "recall_block", lambda cfg, query, cwd: "<workspace_memory>m</workspace_memory>")
    bundle = attach.build_user_message("join #test", [], SimpleNamespace())
    first = cli._with_first_turn_memory(SimpleNamespace(), [], bundle, "join #test")
    later = cli._with_first_turn_memory(SimpleNamespace(), [{"role": "user", "content": "yo"}], bundle, "join #test")
    assert "<workspace_memory>" in json.dumps(first.runtime_message)
    assert later is bundle
