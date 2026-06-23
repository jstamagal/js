from __future__ import annotations

import pytest

from js import drain


def _isolate(monkeypatch, tmp_path):
    """Quiet, network-free env: clean HOME/cwd so no real jsrc/config leaks in,
    JS_MODEL pinned so cfg.model is deterministic, auto_budget stubbed so no
    models.dev lookup happens. Mirrors the dry-run fixtures in
    tests/test_drain_harness.py (drain.py:401, drain.py:411)."""
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("JS_MODEL", "offline-model")
    monkeypatch.chdir(work)
    monkeypatch.setattr(drain, "auto_budget_tokens", lambda model, provider_id=None: 1000)


def _deterministic_simulate(monkeypatch):
    """Replace the randomized _simulate (drain.py:263, hash%6) with a quiet,
    deterministic stub that always succeeds and records the modes string it was
    handed. Lets us assert plumbing without flaky pass/fail counts or TUI noise."""
    seen = {"modes": [], "jobs": []}

    def stub(job, screen, inbox, vault_name, modes, jobs):
        seen["modes"].append(modes)
        seen["jobs"].append(job)
        job.rc = 0
        job.status = "done"

    monkeypatch.setattr(drain, "_simulate", stub)
    return seen


# --------------------------------------------------------------------------
# -f / --from : drain an arbitrary folder instead of <vault>/inbox
# --------------------------------------------------------------------------
def test_from_flag_drains_named_folder_not_the_vault_inbox(tmp_path, monkeypatch, capsys):
    _isolate(monkeypatch, tmp_path)
    seen = _deterministic_simulate(monkeypatch)
    vault = tmp_path / "vault"
    (vault / "inbox").mkdir(parents=True)
    (vault / "inbox" / "ignore-me.txt").write_text("vault inbox file\n", encoding="utf-8")
    folder = tmp_path / "dump"
    folder.mkdir()
    (folder / "a.txt").write_text("alpha\n", encoding="utf-8")
    (folder / "b.txt").write_text("bravo\n", encoding="utf-8")

    rc = drain.main([str(vault), "--from", str(folder), "--dry-run"])

    assert rc == 0
    # Both --from files were planned; the vault inbox file was NOT touched.
    srcs = {s.name for job in seen["jobs"] for s in job.srcs}
    assert srcs == {"a.txt", "b.txt"}
    assert "ignore-me.txt" not in srcs


def test_short_f_flag_is_equivalent_to_long_from(tmp_path, monkeypatch, capsys):
    _isolate(monkeypatch, tmp_path)
    seen = _deterministic_simulate(monkeypatch)
    vault = tmp_path / "vault"
    (vault / "inbox").mkdir(parents=True)
    folder = tmp_path / "dump"
    folder.mkdir()
    (folder / "only.txt").write_text("just one\n", encoding="utf-8")

    rc = drain.main([str(vault), "-f", str(folder), "-n"])

    assert rc == 0
    assert {s.name for job in seen["jobs"] for s in job.srcs} == {"only.txt"}


# --------------------------------------------------------------------------
# -a / --archive : header switches to archive mode; leave-in-place by default
# --------------------------------------------------------------------------
def test_archive_flag_shows_archive_header_and_clears_no_archive_env(tmp_path, monkeypatch, capsys):
    _isolate(monkeypatch, tmp_path)
    _deterministic_simulate(monkeypatch)
    monkeypatch.delenv("JS_WIKI_NO_ARCHIVE", raising=False)
    vault = tmp_path / "vault"
    inbox = vault / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "a.txt").write_text("alpha\n", encoding="utf-8")

    rc = drain.main([str(vault), "--archive", "--dry-run"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "archive→Clippings" in out
    assert "leave-in-place" not in out


def test_default_is_leave_in_place(tmp_path, monkeypatch, capsys):
    _isolate(monkeypatch, tmp_path)
    _deterministic_simulate(monkeypatch)
    vault = tmp_path / "vault"
    inbox = vault / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "a.txt").write_text("alpha\n", encoding="utf-8")

    rc = drain.main([str(vault), "--dry-run"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "leave-in-place" in out
    assert "archive→Clippings" not in out


def test_archive_with_from_forces_drainer_owned_archiving(tmp_path, monkeypatch, capsys):
    """--archive + --source means mark_drainer_archives runs with force=True
    (drain.py:417), so every job from the arbitrary folder is drainer-owned."""
    _isolate(monkeypatch, tmp_path)
    seen = _deterministic_simulate(monkeypatch)
    vault = tmp_path / "vault"
    (vault / "inbox").mkdir(parents=True)
    folder = tmp_path / "dump"
    folder.mkdir()
    (folder / "x.txt").write_text("x\n", encoding="utf-8")
    (folder / "y.txt").write_text("y\n", encoding="utf-8")

    rc = drain.main([str(vault), "-f", str(folder), "-a", "-n"])

    assert rc == 0
    assert seen["jobs"], "expected at least one job"
    assert all(job.drainer_archives for job in seen["jobs"])


# --------------------------------------------------------------------------
# -M / --modes : the comma list handed to per-job execution / --wiki
# --------------------------------------------------------------------------
def test_modes_flag_flows_to_per_job_execution(tmp_path, monkeypatch, capsys):
    _isolate(monkeypatch, tmp_path)
    seen = _deterministic_simulate(monkeypatch)
    vault = tmp_path / "vault"
    inbox = vault / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "a.txt").write_text("alpha\n", encoding="utf-8")

    rc = drain.main([str(vault), "--modes", "ingest,synthesize", "--dry-run"])

    assert rc == 0
    assert seen["modes"] == ["ingest,synthesize"]


def test_modes_defaults_to_ingest(tmp_path, monkeypatch, capsys):
    _isolate(monkeypatch, tmp_path)
    seen = _deterministic_simulate(monkeypatch)
    vault = tmp_path / "vault"
    inbox = vault / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "a.txt").write_text("alpha\n", encoding="utf-8")

    rc = drain.main([str(vault), "--dry-run"])

    assert rc == 0
    assert seen["modes"] == ["ingest"]


def test_short_M_flag_sets_modes(tmp_path, monkeypatch, capsys):
    _isolate(monkeypatch, tmp_path)
    seen = _deterministic_simulate(monkeypatch)
    vault = tmp_path / "vault"
    inbox = vault / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "a.txt").write_text("alpha\n", encoding="utf-8")

    rc = drain.main([str(vault), "-M", "synthesize", "-n"])

    assert rc == 0
    assert seen["modes"] == ["synthesize"]


# --------------------------------------------------------------------------
# -R / --ralph-wiggum : dumb mode, one whole file per job, no staging
# --------------------------------------------------------------------------
def test_ralph_wiggum_shows_ralph_header_and_feeds_whole_files(tmp_path, monkeypatch, capsys):
    _isolate(monkeypatch, tmp_path)
    seen = _deterministic_simulate(monkeypatch)
    vault = tmp_path / "vault"
    inbox = vault / "inbox"
    inbox.mkdir(parents=True)
    big = inbox / "big.txt"
    # In ralph mode this stays ONE job even though it would otherwise split.
    big.write_text("line\n" * 5000, encoding="utf-8")

    rc = drain.main([str(vault), "--ralph-wiggum", "--dry-run"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "ralph-wiggum" in out
    # one whole file -> one job, fed as the file itself, js owns archiving
    assert len(seen["jobs"]) == 1
    job = seen["jobs"][0]
    assert job.srcs == [big]
    assert job.feed == big
    assert job.drainer_archives is False


def test_short_R_flag_enables_ralph_mode(tmp_path, monkeypatch, capsys):
    _isolate(monkeypatch, tmp_path)
    _deterministic_simulate(monkeypatch)
    vault = tmp_path / "vault"
    inbox = vault / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "a.txt").write_text("alpha\n", encoding="utf-8")

    rc = drain.main([str(vault), "-R", "-n"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "ralph-wiggum" in out


# --------------------------------------------------------------------------
# -b / --budget : force a per-job token budget; header reports (set)
# --------------------------------------------------------------------------
def test_budget_flag_overrides_auto_and_marks_header_set(tmp_path, monkeypatch, capsys):
    _isolate(monkeypatch, tmp_path)
    # auto_budget would raise if it ran (we want to prove --budget bypasses it)
    monkeypatch.setattr(
        drain, "auto_budget_tokens",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("auto_budget called despite --budget")),
    )
    _deterministic_simulate(monkeypatch)
    vault = tmp_path / "vault"
    inbox = vault / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "a.txt").write_text("alpha\n", encoding="utf-8")

    rc = drain.main([str(vault), "--budget", "6000", "--dry-run"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "~6000 tok/job (set)" in out


def test_auto_budget_used_when_no_budget_flag_marks_header_auto(tmp_path, monkeypatch, capsys):
    _isolate(monkeypatch, tmp_path)  # stub returns 1000
    _deterministic_simulate(monkeypatch)
    vault = tmp_path / "vault"
    inbox = vault / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "a.txt").write_text("alpha\n", encoding="utf-8")

    rc = drain.main([str(vault), "--dry-run"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "~1000 tok/job (auto)" in out


def test_short_b_flag_sets_budget(tmp_path, monkeypatch, capsys):
    _isolate(monkeypatch, tmp_path)
    _deterministic_simulate(monkeypatch)
    vault = tmp_path / "vault"
    inbox = vault / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "a.txt").write_text("alpha\n", encoding="utf-8")

    rc = drain.main([str(vault), "-b", "4242", "-n"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "~4242 tok/job (set)" in out


def test_budget_drives_packing_so_small_files_split_across_jobs(tmp_path, monkeypatch, capsys):
    """A tiny --budget forces files apart instead of bundling them, proving the
    flag reaches plan_jobs' budget_chars (drain.py:412)."""
    _isolate(monkeypatch, tmp_path)
    seen = _deterministic_simulate(monkeypatch)
    vault = tmp_path / "vault"
    inbox = vault / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "a.txt").write_text("aaaa\n", encoding="utf-8")
    (inbox / "b.txt").write_text("bbbb\n", encoding="utf-8")

    # budget 1 token == 4 chars; each ~5-byte file overflows a shared bundle,
    # so they land as two separate jobs rather than one bundle.
    rc = drain.main([str(vault), "-b", "1", "-n"])

    assert rc == 0
    assert len(seen["jobs"]) == 2


# --------------------------------------------------------------------------
# -t / --timeout : parsed as an int; not exercised under --dry-run (no Popen)
# --------------------------------------------------------------------------
def test_timeout_flag_parses_and_runs_clean_under_dry_run(tmp_path, monkeypatch, capsys):
    """--dry-run never reaches _run_one, so timeout cannot fire here; we assert
    it is accepted/parsed and the run completes. The kill path lives in
    _run_one (drain.py:252) and needs a live subprocess, so it is out of scope
    for an offline test."""
    _isolate(monkeypatch, tmp_path)
    seen = _deterministic_simulate(monkeypatch)
    vault = tmp_path / "vault"
    inbox = vault / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "a.txt").write_text("alpha\n", encoding="utf-8")

    rc = drain.main([str(vault), "--timeout", "600", "--dry-run"])

    assert rc == 0
    assert len(seen["jobs"]) == 1


def test_timeout_rejects_non_integer(tmp_path, monkeypatch, capsys):
    _isolate(monkeypatch, tmp_path)
    vault = tmp_path / "vault"
    (vault / "inbox").mkdir(parents=True)

    with pytest.raises(SystemExit) as exc:
        drain.main([str(vault), "--timeout", "nope", "--dry-run"])

    assert exc.value.code != 0


# --------------------------------------------------------------------------
# -l / --limit : run only the first N jobs, leave the rest behind
# --------------------------------------------------------------------------
def test_limit_flag_runs_only_first_n_jobs(tmp_path, monkeypatch, capsys):
    _isolate(monkeypatch, tmp_path)
    seen = _deterministic_simulate(monkeypatch)
    vault = tmp_path / "vault"
    inbox = vault / "inbox"
    inbox.mkdir(parents=True)
    # ralph mode == one job per file, so 5 files == 5 candidate jobs.
    for i in range(5):
        (inbox / f"f{i}.txt").write_text(f"file {i}\n", encoding="utf-8")

    rc = drain.main([str(vault), "--ralph-wiggum", "--limit", "2", "--dry-run"])

    out = capsys.readouterr().out
    assert rc == 0
    assert len(seen["jobs"]) == 2
    assert "2 jobs" in out


def test_short_l_flag_limits_jobs(tmp_path, monkeypatch, capsys):
    _isolate(monkeypatch, tmp_path)
    seen = _deterministic_simulate(monkeypatch)
    vault = tmp_path / "vault"
    inbox = vault / "inbox"
    inbox.mkdir(parents=True)
    for i in range(4):
        (inbox / f"f{i}.txt").write_text(f"file {i}\n", encoding="utf-8")

    rc = drain.main([str(vault), "-R", "-l", "1", "-n"])

    assert rc == 0
    assert len(seen["jobs"]) == 1


def test_no_limit_runs_every_job(tmp_path, monkeypatch, capsys):
    _isolate(monkeypatch, tmp_path)
    seen = _deterministic_simulate(monkeypatch)
    vault = tmp_path / "vault"
    inbox = vault / "inbox"
    inbox.mkdir(parents=True)
    for i in range(3):
        (inbox / f"f{i}.txt").write_text(f"file {i}\n", encoding="utf-8")

    rc = drain.main([str(vault), "--ralph-wiggum", "--dry-run"])

    assert rc == 0
    assert len(seen["jobs"]) == 3
