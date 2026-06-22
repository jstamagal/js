from __future__ import annotations

import subprocess

from pathlib import Path

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
            },
        }
    ]


def test_run_commit_auto_inits_non_repo_and_injects_survey(tmp_path, monkeypatch):
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


def test_commit_prompt_references_stage_helper_not_interactive_patch():
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "commit" / "01-prompt.md"
    prompt = prompt_path.read_text(encoding="utf-8")

    assert "js.commit_helper" in prompt
    assert "stage <file> <hunks|all>" in prompt
    assert "git add -p" not in prompt
