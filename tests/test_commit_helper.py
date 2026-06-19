"""Tests for js.commit_helper — survey + deterministic partial staging."""

from __future__ import annotations

import subprocess

import pytest

from js import commit_helper


def _git(repo, *args, stdin=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, input=stdin, check=True,
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A git repo with a committed baseline; cwd is set to it (helper uses cwd)."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "f.txt").write_text("\n".join(f"line{i}" for i in range(1, 31)) + "\n")
    _git(tmp_path, "add", "f.txt")
    _git(tmp_path, "commit", "-qm", "baseline")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _staged(repo):
    return _git(repo, "diff", "--cached").stdout


def test_survey_reports_status_hunks_and_log(repo, capsys):
    (repo / "f.txt").write_text(
        "\n".join((f"line{i}_X" if i in (2, 28) else f"line{i}") for i in range(1, 31)) + "\n"
    )
    (repo / "new.txt").write_text("brand new\n")
    assert commit_helper.main(["survey"]) == 0
    out = capsys.readouterr().out
    assert "branch: main" in out
    assert "### f.txt  (2 hunks)" in out          # two separated edits -> two hunks
    assert "?? new.txt" in out                    # untracked flagged
    assert "baseline" in out                       # recent log present


def test_stage_subset_of_hunks(repo):
    # three well-separated edits -> three hunks; stage only 1 and 3
    (repo / "f.txt").write_text(
        "\n".join((f"line{i}_X" if i in (2, 15, 28) else f"line{i}") for i in range(1, 31)) + "\n"
    )
    assert commit_helper.main(["stage", "f.txt", "1,3"]) == 0
    staged = _staged(repo)
    assert "line2_X" in staged and "line28_X" in staged
    assert "line15_X" not in staged               # middle hunk left unstaged
    assert "line15_X" in _git(repo, "diff").stdout


def test_stage_all_whole_file(repo):
    (repo / "f.txt").write_text(
        "\n".join((f"line{i}_X" if i in (2, 15, 28) else f"line{i}") for i in range(1, 31)) + "\n"
    )
    assert commit_helper.main(["stage", "f.txt", "all"]) == 0
    staged = _staged(repo)
    assert all(m in staged for m in ("line2_X", "line15_X", "line28_X"))


def test_stage_untracked_adds_whole(repo):
    (repo / "new.txt").write_text("hello\n")
    assert commit_helper.main(["stage", "new.txt", "all"]) == 0
    assert "new.txt" in _git(repo, "diff", "--cached", "--name-only").stdout


def test_stage_out_of_range_hunk_errors(repo, capsys):
    (repo / "f.txt").write_text("line1_X\n" + "\n".join(f"line{i}" for i in range(2, 31)) + "\n")
    assert commit_helper.main(["stage", "f.txt", "5"]) == 2   # only 1 hunk exists
    assert "out of range" in capsys.readouterr().err
    assert _staged(repo) == ""                                 # nothing staged


def test_stage_unknown_path_errors(repo, capsys):
    assert commit_helper.main(["stage", "nope.txt", "1"]) == 2
    assert "no pending changes" in capsys.readouterr().err
