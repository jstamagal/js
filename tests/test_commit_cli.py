from __future__ import annotations

import subprocess


from js import cli


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)


class TtyStdin:
    def isatty(self):
        return True

    def read(self):
        raise AssertionError("stdin should not be read when isatty is true")


def test_cli_commit_arg_dispatches_target_and_operator_context(tmp_path, monkeypatch):
    calls: list[dict] = []

    def run_commit_stub(target, **kwargs):
        calls.append({"target": target, "kwargs": kwargs})
        return 0

    monkeypatch.setattr(cli, "_run_commit", run_commit_stub)
    monkeypatch.setattr(cli.sys, "stdin", TtyStdin())

    actual = cli.main(["--commit", str(tmp_path), "--no-save", "-p", "operator note"])

    assert actual == 0
    assert calls == [
        {
            "target": str(tmp_path),
            "kwargs": {
                "model": None,
                "debug": False,
                "debug_file": None,
                "session": None,
                "save": False,
                "reasoning": None,
                "maxout": None,
                "extra_context": "operator note",
                "extras": [],
                "ignore_local_config": False,
                "ignore_global_config": False,
                "presets": [],
            },
        }
    ]


def test_run_commit_auto_inits_non_repo_and_injects_survey(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    target = tmp_path / "work"
    target.mkdir()
    (target / "work.txt").write_text("hello\n")
    calls: list[dict] = []

    def run_prompt_stub(prompt, **kwargs):
        calls.append({"prompt": prompt, "kwargs": kwargs})
        return 0

    monkeypatch.setattr(cli, "_run_prompt", run_prompt_stub)

    actual = cli._run_commit(str(target), save=False)

    assert actual == 0
    assert (target / ".git").is_dir()
    assert len(calls) == 1
    prompt = calls[0]["prompt"]
    assert "Initial deterministic commit_helper survey" in prompt
    assert "?? work.txt" in prompt
    assert "python -m js.commit_helper -C" in prompt
    assert "stage <file> <hunks|all>" in prompt
    assert calls[0]["kwargs"]["agent"] == "commit"
    assert calls[0]["kwargs"]["tool_context"].cwd == target.resolve(strict=False)



def test_run_commit_injects_messy_repo_snapshot_without_crashing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    repo = tmp_path / "messy"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("\n".join(f"line{i}" for i in range(1, 31)) + "\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "baseline")
    (repo / "f.txt").write_text(
        "\n".join(("line2_STAGED" if i == 2 else f"line{i}") for i in range(1, 31)) + "\n"
    )
    _git(repo, "add", "f.txt")
    (repo / "f.txt").write_text(
        "\n".join(
            (
                "line2_STAGED" if i == 2
                else "line28_UNSTAGED" if i == 28
                else f"line{i}"
            )
            for i in range(1, 31)
        ) + "\n"
    )
    (repo / "new.txt").write_text("new\n")
    calls: list[dict] = []

    def run_prompt_stub(prompt, **kwargs):
        calls.append({"prompt": prompt, "kwargs": kwargs})
        return 0

    monkeypatch.setattr(cli, "_run_prompt", run_prompt_stub)

    actual = cli._run_commit(str(repo), save=False)

    assert actual == 0
    prompt = calls[0]["prompt"]
    assert "-- staged diff" in prompt
    assert "-- unstaged diff" in prompt
    assert "line2_STAGED" in prompt
    assert "line28_UNSTAGED" in prompt
    assert "?? new.txt" in prompt
    assert "(clean tree, nothing to commit)" not in prompt


def _init_repo(repo):
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("\n".join(f"line{i}" for i in range(1, 31)) + "\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "baseline")


def test_run_commit_snapshots_tracked_patch_and_untracked_file(tmp_path, monkeypatch):
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    repo = tmp_path / "messy"
    _init_repo(repo)
    (repo / "f.txt").write_text("changed-line1\n" + "\n".join(f"line{i}" for i in range(2, 31)) + "\n")
    (repo / "new.txt").write_text("brand new untracked\n")

    monkeypatch.setattr(cli, "_run_prompt", lambda prompt, **kwargs: 0)
    assert cli._run_commit(str(repo), save=False) == 0

    backups = data_home / "js" / "commit-backups"
    snaps = sorted(backups.iterdir())
    assert len(snaps) == 1
    snap = snaps[0]
    patch = (snap / "tracked.patch").read_text()
    assert "changed-line1" in patch
    assert (snap / "untracked" / "new.txt").read_text() == "brand new untracked\n"


def test_run_commit_makes_no_snapshot_on_clean_tree(tmp_path, monkeypatch):
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    repo = tmp_path / "clean"
    _init_repo(repo)

    monkeypatch.setattr(cli, "_run_prompt", lambda prompt, **kwargs: 0)
    assert cli._run_commit(str(repo), save=False) == 0

    backups = data_home / "js" / "commit-backups"
    assert not backups.exists() or not any(backups.iterdir())


def test_run_commit_snapshot_prune_keeps_ten(tmp_path, monkeypatch):
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    backups = data_home / "js" / "commit-backups"
    backups.mkdir(parents=True)
    # Twelve older snapshots already on disk (names sort before any current UTC stamp).
    for i in range(12):
        (backups / f"20200101T0000{i:02d}000000Z").mkdir()

    repo = tmp_path / "messy"
    _init_repo(repo)
    (repo / "new.txt").write_text("fresh\n")

    monkeypatch.setattr(cli, "_run_prompt", lambda prompt, **kwargs: 0)
    assert cli._run_commit(str(repo), save=False) == 0

    remaining = sorted(p.name for p in backups.iterdir())
    assert len(remaining) == 10
    # The three oldest were pruned; the just-written snapshot is the newest kept.
    assert "20200101T000000000000Z" not in remaining
    assert "20200101T000002000000Z" not in remaining
    assert remaining[-1] not in {f"20200101T0000{i:02d}000000Z" for i in range(12)}


