from __future__ import annotations

import subprocess
from pathlib import Path

from js.toolkit import ToolContext
from js.toolkit.wiki.helpers import today
from js.toolkit.wiki.ops import wiki_commit, wiki_log


def _ctx(tmp_path: Path, max_bytes: int = 4096) -> ToolContext:
    return ToolContext(cwd=tmp_path, max_tool_result_bytes=max_bytes)


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "wiki-test"
    vault.mkdir()
    return vault


def _git(vault: Path, *args: str) -> None:
    # Hermetic repo identity so the commit path does not depend on global config.
    subprocess.run(["git", "-C", str(vault), *args], check=True, capture_output=True)


def test_wiki_log_appends_dated_entry_with_note(tmp_path):
    # ops.py:122 — entry = "\n## [{today()}] {op} | {title}\n{note}\n", appended.
    vault = _vault(tmp_path)

    result = wiki_log(str(vault), "synth", "First Title", "a note", context=_ctx(tmp_path))

    log_text = (vault / "log.md").read_text(encoding="utf-8")
    assert result == f"logged: [{today()}] synth | First Title"
    assert log_text == f"\n## [{today()}] synth | First Title\na note\n"


def test_wiki_log_defaults_empty_note_and_appends_each_call(tmp_path):
    # note defaults to "" (ops.py:122); each call opens log.md in append mode.
    vault = _vault(tmp_path)
    context = _ctx(tmp_path)

    first = wiki_log(str(vault), "ingest", "Entry One", context=context)
    second = wiki_log(str(vault), "lint", "Entry Two", "second note", context=context)

    log_text = (vault / "log.md").read_text(encoding="utf-8")
    assert first == f"logged: [{today()}] ingest | Entry One"
    assert second == f"logged: [{today()}] lint | Entry Two"
    # Empty-note entry still writes a blank body line, then the second entry follows.
    assert log_text == (
        f"\n## [{today()}] ingest | Entry One\n\n"
        f"\n## [{today()}] lint | Entry Two\nsecond note\n"
    )
    # Both headings present -> append, not overwrite.
    assert log_text.count("## [") == 2


def test_wiki_commit_no_op_on_non_git_vault(tmp_path):
    # _maybe_git_commit (ops.py:140-141): no .git -> silent skip, no repo created.
    vault = _vault(tmp_path)
    (vault / "page.md").write_text("body\n", encoding="utf-8")

    result = wiki_commit(str(vault), "snapshot", context=_ctx(tmp_path))

    assert result == "git: (no repo — skipping auto-commit)"
    assert not (vault / ".git").exists()


def test_wiki_commit_commits_when_vault_is_git_repo(tmp_path):
    # ops.py:142-158 — stage -A, commit, report short sha.
    vault = _vault(tmp_path)
    _git(vault, "init")
    _git(vault, "config", "user.email", "ape@example.com")
    _git(vault, "config", "user.name", "Ape Test")
    (vault / "page.md").write_text("body\n", encoding="utf-8")

    result = wiki_commit(str(vault), "ingest: a thing", context=_ctx(tmp_path))

    assert result.startswith("git: committed ")
    sha = result.split("git: committed ", 1)[1].strip()
    assert sha and sha != "?"
    # The commit actually exists with the given message and includes the file.
    log = subprocess.run(
        ["git", "-C", str(vault), "log", "-1", "--pretty=%s"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert log == "ingest: a thing"
    tracked = subprocess.run(
        ["git", "-C", str(vault), "ls-files"],
        check=True, capture_output=True, text=True,
    ).stdout.split()
    assert "page.md" in tracked


def test_wiki_commit_skips_when_nothing_staged(tmp_path):
    # ops.py:148-150 — clean repo: git diff --cached --quiet rc==0 -> "nothing to commit".
    # NOTE: vault_lock (helpers.py:39-40) creates a .wiki.lock file at the vault
    # root BEFORE _maybe_git_commit runs `git add -A`. So on a real repo there is
    # always something to stage unless the lock is ignored — gitignore it here to
    # reach the genuine "nothing to commit" branch.
    vault = _vault(tmp_path)
    _git(vault, "init")
    _git(vault, "config", "user.email", "ape@example.com")
    _git(vault, "config", "user.name", "Ape Test")
    (vault / ".gitignore").write_text(".wiki.lock\n", encoding="utf-8")
    (vault / "page.md").write_text("body\n", encoding="utf-8")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-m", "initial")

    result = wiki_commit(str(vault), "no changes here", context=_ctx(tmp_path))

    assert result == "git: nothing to commit"
    count = subprocess.run(
        ["git", "-C", str(vault), "rev-list", "--count", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert count == "1"
