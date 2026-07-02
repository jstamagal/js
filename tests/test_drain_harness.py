from __future__ import annotations

import subprocess
import sys

import pytest

from js import drain


def test_plan_jobs_packs_small_files_splits_large_text_and_feeds_large_binary(tmp_path):
    inbox = tmp_path / "vault" / "inbox"
    staging = tmp_path / "staging"
    inbox.mkdir(parents=True)
    staging.mkdir()
    first = inbox / "a.txt"
    second = inbox / "b.txt"
    large_text = inbox / "big.txt"
    binary = inbox / "blob.bin"
    first.write_text("a" * 10, encoding="utf-8")
    second.write_text("b" * 10, encoding="utf-8")
    large_text.write_text("line1\nline2-long\nline3\n", encoding="utf-8")
    binary.write_bytes(b"\xff" * 50)

    actual = drain.plan_jobs(inbox, budget_chars=20, ralph=False, staging=staging)

    assert len(actual) == 4
    bundle, part_one, part_two, blob = actual
    assert bundle.srcs == [first, second]
    assert bundle.drainer_archives is True
    assert bundle.feed.parent == staging
    assert bundle.feed.read_text(encoding="utf-8") == (
        "<<<< SOURCE: a.txt >>>>\n"
        "aaaaaaaaaa\n\n"
        "<<<< SOURCE: b.txt >>>>\n"
        "bbbbbbbbbb"
    )
    assert part_one.srcs == [large_text]
    assert part_one.drainer_archives is True
    assert part_one.part == 1
    assert part_one.parts == 2
    assert part_one.feed.read_text(encoding="utf-8") == "line1\nline2-long\n"
    assert part_two.part == 2
    assert part_two.parts == 2
    assert part_two.feed.read_text(encoding="utf-8") == "line3\n"
    assert blob.srcs == [binary]
    assert blob.feed == binary
    assert blob.drainer_archives is False


def test_plan_jobs_shatter_pieces_unique_across_same_name_in_different_subdirs(tmp_path):
    """Two big same-named text files in different subdirs must not write over each
    other's staged pieces (keyed on stem alone would collide and lose one file)."""
    inbox = tmp_path / "vault" / "inbox"
    staging = tmp_path / "staging"
    (inbox / "A").mkdir(parents=True)
    (inbox / "B").mkdir(parents=True)
    staging.mkdir()
    (inbox / "A" / "notes.txt").write_text("A1\nA2-long\nA3\n", encoding="utf-8")
    (inbox / "B" / "notes.txt").write_text("B1\nB2-long\nB3\n", encoding="utf-8")

    jobs = drain.plan_jobs(inbox, budget_chars=6, ralph=False, staging=staging)

    feeds = [j.feed for j in jobs]
    assert len(feeds) == len(set(feeds))          # no staging path collides
    contents = "".join(f.read_text(encoding="utf-8") for f in feeds)
    assert "A1" in contents and "B1" in contents  # both originals survive to staging


def test_drain_help_describes_model_as_configured_env_override(capsys):
    with pytest.raises(SystemExit) as exc:
        drain.main(["--help"])

    captured = capsys.readouterr()
    assert exc.value.code == 0
    assert "override configured/env model for every job" in captured.out
    assert "override JS_MODEL" not in captured.out


def test_plan_jobs_ralph_mode_feeds_each_file_whole_and_lets_js_archive(tmp_path):
    inbox = tmp_path / "vault" / "inbox"
    inbox.mkdir(parents=True)
    first = inbox / "a.txt"
    nested = inbox / "project" / "b.txt"
    nested.parent.mkdir()
    first.write_text("a\n", encoding="utf-8")
    nested.write_text("b\n", encoding="utf-8")

    actual = drain.plan_jobs(inbox, budget_chars=1, ralph=True, staging=None)

    assert [(job.srcs, job.feed, job.drainer_archives) for job in actual] == [
        ([first], first, False),
        ([nested], nested, False),
    ]


def test_archive_done_moves_only_successful_drainer_owned_sources(tmp_path):
    vault = tmp_path / "vault"
    inbox = vault / "inbox"
    inbox.mkdir(parents=True)
    split = inbox / "project" / "split.txt"
    failed = inbox / "failed.txt"
    direct = inbox / "direct.txt"
    outside = tmp_path / "outside.txt"
    split.parent.mkdir()
    split.write_text("split\n", encoding="utf-8")
    failed.write_text("failed\n", encoding="utf-8")
    direct.write_text("direct\n", encoding="utf-8")
    outside.write_text("outside\n", encoding="utf-8")
    jobs = [
        drain.Job(srcs=[split], feed=tmp_path / "split.1", drainer_archives=True, status="done"),
        drain.Job(srcs=[split], feed=tmp_path / "split.2", drainer_archives=True, status="done"),
        drain.Job(srcs=[failed], feed=tmp_path / "failed.1", drainer_archives=True, status="done"),
        drain.Job(srcs=[failed], feed=tmp_path / "failed.2", drainer_archives=True, status="failed"),
        drain.Job(srcs=[direct], feed=direct, drainer_archives=False, status="done"),
        drain.Job(srcs=[outside], feed=outside, drainer_archives=True, status="done"),
    ]

    drain.archive_done(jobs, inbox, vault)

    assert not split.exists()
    assert (vault / "Clippings" / "project" / "split.txt").read_text(encoding="utf-8") == "split\n"
    assert failed.read_text(encoding="utf-8") == "failed\n"
    assert direct.read_text(encoding="utf-8") == "direct\n"
    assert outside.read_text(encoding="utf-8") == "outside\n"


def test_archive_done_uses_full_plan_so_limited_split_runs_do_not_move_original(tmp_path):
    vault = tmp_path / "vault"
    inbox = vault / "inbox"
    inbox.mkdir(parents=True)
    split = inbox / "split.txt"
    split.write_text("part1\npart2\n", encoding="utf-8")
    all_jobs = [
        drain.Job(srcs=[split], feed=tmp_path / "split.1", drainer_archives=True, status="done", part=1, parts=2),
        drain.Job(srcs=[split], feed=tmp_path / "split.2", drainer_archives=True, status="pending", part=2, parts=2),
    ]

    drain.archive_done(all_jobs, inbox, vault)

    assert split.read_text(encoding="utf-8") == "part1\npart2\n"
    assert not (vault / "Clippings" / "split.txt").exists()


def test_mark_drainer_archives_marks_nested_inbox_files_and_forced_source_dirs(tmp_path):
    inbox = tmp_path / "vault" / "inbox"
    top = inbox / "top.md"
    nested = inbox / "project" / "nested.md"
    external = tmp_path / "external" / "raw.md"
    nested.parent.mkdir(parents=True)
    external.parent.mkdir(parents=True)
    top.write_text("top\n", encoding="utf-8")
    nested.write_text("nested\n", encoding="utf-8")
    external.write_text("external\n", encoding="utf-8")
    top_job = drain.Job(srcs=[top], feed=top, drainer_archives=False)
    nested_job = drain.Job(srcs=[nested], feed=nested, drainer_archives=False)
    external_job = drain.Job(srcs=[external], feed=external, drainer_archives=False)

    drain.mark_drainer_archives([top_job, nested_job, external_job], inbox)

    assert top_job.drainer_archives is False
    assert nested_job.drainer_archives is True
    assert external_job.drainer_archives is False

    drain.mark_drainer_archives([external_job], inbox, force=True)

    assert external_job.drainer_archives is True


def test_summarize_reports_failures_retry_commands_and_interruption(capsys, tmp_path):
    vault = tmp_path / "vault"
    inbox = vault / "inbox"
    inbox.mkdir(parents=True)
    done_feed = inbox / "done.txt"
    failed_feed = inbox / "failed.txt"
    pending_feed = inbox / "pending.txt"
    jobs = [
        drain.Job(srcs=[done_feed], feed=done_feed, drainer_archives=False, status="done", rc=0),
        drain.Job(
            srcs=[failed_feed],
            feed=failed_feed,
            drainer_archives=False,
            status="failed",
            rc=2,
            err="model stopped",
        ),
        drain.Job(srcs=[pending_feed], feed=pending_feed, drainer_archives=False, status="pending"),
    ]

    actual = drain.summarize(jobs, inbox, vault, "ingest", interrupted=True)
    output = capsys.readouterr().out

    assert actual == 1
    assert "1 done" in output
    assert "1 failed" in output
    assert "1 not reached (interrupted)" in output
    assert "(rc=2)" in output
    assert "model stopped" in output
    assert f"retry: js --wiki=ingest --vault={vault} {failed_feed} -d" in output


def test_run_one_constructs_js_wiki_command_and_records_process_result(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    inbox = vault / "inbox"
    feed = inbox / "raw.txt"
    inbox.mkdir(parents=True)
    feed.write_text("raw\n", encoding="utf-8")
    job = drain.Job(srcs=[feed], feed=feed, drainer_archives=False)
    calls: list[dict] = []

    class PopenStub:
        def __init__(self, cmd, stdout, stderr, env):
            calls.append({"cmd": cmd, "stderr": stderr, "env": env})
            stdout.write(b"model complete\n")
            self.returncode = 0

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    class ScreenStub:
        def paint(self, text):
            pass

    env = {"JS_WIKI_NO_ARCHIVE": "1"}
    monkeypatch.setattr(drain.subprocess, "Popen", PopenStub)

    drain._run_one(
        job,
        vault,
        "ingest,synthesize",
        "offline-model",
        30,
        ScreenStub(),
        inbox,
        [job],
        env,
    )

    assert job.status == "done"
    assert job.rc == 0
    assert job.err == "model complete"
    assert calls == [
        {
            "cmd": [
                sys.executable,
                "-m",
                "js.cli",
                "--wiki",
                "ingest,synthesize",
                "--vault",
                str(vault),
                "-n",
                str(feed),
                "-m",
                "offline-model",
            ],
            "stderr": subprocess.STDOUT,
            "env": env,
        }
    ]


def test_main_prefers_js_model_over_me_model_for_auto_budget(tmp_path, monkeypatch, capsys):
    vault = tmp_path / "vault"
    (vault / "inbox").mkdir(parents=True)
    seen: list[str] = []

    def auto_budget_stub(model: str, provider_id: str | None = None) -> int:
        seen.append(model)
        return 123

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JS_MODEL", "js-model")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(drain, "auto_budget_tokens", auto_budget_stub)

    rc = drain.main([str(vault), "--dry-run"])

    assert rc == 0
    assert seen == ["js-model"]
    assert "empty" in capsys.readouterr().out


def test_main_uses_layered_config_model_for_auto_budget_when_no_env_or_flag(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    project = tmp_path / "project"
    vault = tmp_path / "vault"
    (vault / "inbox").mkdir(parents=True)
    (project / ".js").mkdir(parents=True)
    (project / ".js" / "jsrc").write_text("set model.id project-model\n", encoding="utf-8")
    seen: list[str] = []

    def auto_budget_stub(model: str, provider_id: str | None = None) -> int:
        seen.append(model)
        return 123

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("JS_MODEL", raising=False)
    monkeypatch.chdir(project)
    monkeypatch.setattr(drain, "auto_budget_tokens", auto_budget_stub)

    rc = drain.main([str(vault), "--dry-run"])

    assert rc == 0
    assert seen == ["project-model"]
    assert "empty" in capsys.readouterr().out


def test_final_commit_stages_and_commits_only_when_repo_has_changes(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / ".git").mkdir(parents=True)
    calls: list[list[str]] = []
    returncodes = [0, 1, 0]

    def run_stub(cmd, capture_output=False):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncodes.pop(0), stdout=b"", stderr=b"")

    monkeypatch.setattr(drain.subprocess, "run", run_stub)

    drain._final_commit(vault, "ingest")

    assert calls == [
        ["git", "-C", str(vault), "add", "-A"],
        ["git", "-C", str(vault), "diff", "--cached", "--quiet"],
        ["git", "-C", str(vault), "commit", "-m", "drain: ingest"],
    ]


def test_final_commit_skips_commit_when_nothing_is_staged(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / ".git").mkdir(parents=True)
    calls: list[list[str]] = []

    def run_stub(cmd, capture_output=False):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(drain.subprocess, "run", run_stub)

    drain._final_commit(vault, "ingest")

    assert calls == [
        ["git", "-C", str(vault), "add", "-A"],
        ["git", "-C", str(vault), "diff", "--cached", "--quiet"],
    ]
