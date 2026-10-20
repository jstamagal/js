"""HYMEM: what is sent to the memory server, and which repository it is filed under."""
from __future__ import annotations

import subprocess

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
