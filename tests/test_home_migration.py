"""~/.js is the one home: the old locations move in once, safely.

Every test runs against the tmp HOME the conftest fixture installs.
"""

from __future__ import annotations

import errno
import io
import json
import os
import time
from pathlib import Path

from js import cli, config, home, paths


def _legacy() -> dict[str, Path]:
    return paths.legacy_homes()


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _old_layout(tmp_path: Path) -> Path:
    """An old config/data/inbox tree. Returns the "NFS" directory a symlinked agent points at."""
    legacy = _legacy()
    nfs = tmp_path / "nfs" / "research"
    _write(nfs / "01-prompt.md", "remote agent\n")
    _write(legacy["config"] / "jsrc", "set model.id old-model\n")
    _write(legacy["config"] / "logins.toml", "[x]\n")
    _write(legacy["config"] / "agents" / "mine" / "01-prompt.md", "mine\n")
    os.symlink(nfs, legacy["config"] / "agents" / "research")
    _write(legacy["config"] / "skills" / "s" / "SKILL.md", "---\nname: s\n---\n")
    _write(legacy["data"] / "sessions" / "defaultagent" / "old.jsonl", '{"role":"user","content":"hi"}\n')
    _write(legacy["data"] / "transcript" / "defaultagent" / "t.log", "t\n")
    _write(legacy["data"] / "modelsdotdev" / "status.json", "{}\n")
    _write(legacy["data"] / "notes" / "notes.md", "a note\n")
    _write(legacy["inbox"] / "design.md", "design\n")
    return nfs


def test_first_run_moves_every_old_location_into_the_layout(tmp_path):
    nfs = _old_layout(tmp_path)
    out = io.StringIO()

    steps = home.migrate_once(out)

    js = paths.home()
    assert (js / "jsrc").read_text(encoding="utf-8") == "set model.id old-model\n"
    assert (paths.login_store_dir() / "logins.toml").is_file()
    assert (paths.global_agents_dir() / "mine" / "01-prompt.md").is_file()
    assert (paths.global_skills_dir() / "s" / "SKILL.md").is_file()
    assert (paths.sessions_root() / "defaultagent" / "old.jsonl").is_file()
    assert (paths.transcript_root() / "defaultagent" / "t.log").is_file()
    assert (paths.model_catalog_dir() / "status.json").is_file()
    assert (paths.notes_dir() / "notes.md").read_text(encoding="utf-8") == "a note\n"
    assert (paths.work_dir() / "design.md").is_file()
    for old in _legacy().values():
        assert not os.path.lexists(old)
    # The symlinked agent moved as a link; what it points at was not touched.
    moved_link = paths.global_agents_dir() / "research"
    assert moved_link.is_symlink()
    assert os.readlink(moved_link) == str(nfs)
    assert (nfs / "01-prompt.md").read_text(encoding="utf-8") == "remote agent\n"
    # One line per move, on the stream it was given.
    moves = [step for step in steps if step.kind == "move"]
    assert moves
    assert len(out.getvalue().splitlines()) == len(steps)


def test_a_directory_others_are_renamed_into_still_moves_whole(tmp_path):
    data = _legacy()["data"]
    _write(data / "state" / "defaultagent" / "debug.log", "d\n")
    _write(data / "commit-backups" / "b.patch", "p\n")

    steps = home.migrate_once(io.StringIO())

    assert home.Step("move", data / "state", paths.state_root()) in steps
    assert (paths.state_root() / "defaultagent" / "debug.log").is_file()
    assert (paths.commit_backups_dir() / "b.patch").is_file()


def test_it_never_moves_twice(tmp_path):
    _old_layout(tmp_path)
    home.migrate_once(io.StringIO())
    # An old location that reappears later is left alone by the startup migration.
    _write(_legacy()["config"] / "jsrc", "set model.id reappeared\n")
    out = io.StringIO()

    assert home.migrate_once(out) == []
    assert out.getvalue() == ""
    assert (_legacy()["config"] / "jsrc").is_file()
    assert (paths.global_config_file()).read_text(encoding="utf-8") == "set model.id old-model\n"


def test_nothing_to_move_touches_nothing(tmp_path):
    assert home.migrate_once(io.StringIO()) == []
    assert not paths.home().exists()


def test_a_target_with_different_content_is_refused_and_the_source_kept(tmp_path):
    source = _write(_legacy()["config"] / "jsrc", "set model.id old\n")
    _write(paths.global_config_file(), "set model.id new\n")
    out = io.StringIO()

    steps = home.migrate_once(out)

    assert [step.kind for step in steps] == ["refuse"]
    assert source.read_text(encoding="utf-8") == "set model.id old\n"
    assert paths.global_config_file().read_text(encoding="utf-8") == "set model.id new\n"
    assert str(paths.global_config_file().name) in out.getvalue()


def test_an_identical_target_drops_the_old_copy(tmp_path):
    _write(_legacy()["config"] / "jsrc", "same\n")
    _write(paths.global_config_file(), "same\n")

    steps = home.migrate_once(io.StringIO())

    assert [step.kind for step in steps] == ["duplicate", "rmdir"]
    assert not _legacy()["config"].exists()
    assert paths.global_config_file().read_text(encoding="utf-8") == "same\n"


def test_an_existing_directory_is_merged_and_a_symlink_is_never_descended(tmp_path):
    nfs = tmp_path / "nfs" / "research"
    _write(nfs / "01-prompt.md", "remote\n")
    agents = _legacy()["config"] / "agents"
    _write(agents / "old" / "01-prompt.md", "old\n")
    os.symlink(nfs, agents / "research")
    _write(paths.global_agents_dir() / "new" / "01-prompt.md", "new\n")
    # A target directory where the source is a link: refused, not merged through the link.
    _write(paths.global_agents_dir() / "clash" / "01-prompt.md", "clash\n")
    os.symlink(nfs, agents / "clash")

    steps = home.migrate_once(io.StringIO())

    assert (paths.global_agents_dir() / "old" / "01-prompt.md").is_file()
    assert (paths.global_agents_dir() / "new" / "01-prompt.md").is_file()
    assert os.readlink(paths.global_agents_dir() / "research") == str(nfs)
    refused = [step for step in steps if step.kind == "refuse"]
    assert [step.source for step in refused] == [agents / "clash"]
    assert (agents / "clash").is_symlink()
    assert sorted(p.name for p in nfs.iterdir()) == ["01-prompt.md"]


def test_dry_run_changes_nothing_and_apply_does_the_same_moves(tmp_path, capsys):
    _old_layout(tmp_path)
    before = sorted(str(p) for p in tmp_path.rglob("*"))

    assert home.main([]) == 0
    preview = capsys.readouterr().out.splitlines()
    assert sorted(str(p) for p in tmp_path.rglob("*")) == before
    assert not paths.home().exists()

    assert home.main(["--apply"]) == 0
    applied = capsys.readouterr().out.splitlines()
    moved = [line for line in applied if "->" in line]
    assert len(moved) == len([line for line in preview if "->" in line])
    for old in _legacy().values():
        assert not os.path.lexists(old)


def test_dry_run_exits_nonzero_when_a_move_would_be_refused(tmp_path, capsys):
    _write(_legacy()["config"] / "jsrc", "old\n")
    _write(paths.global_config_file(), "new\n")

    assert home.main([]) == 1
    assert paths.global_config_file().read_text(encoding="utf-8") == "new\n"


def test_across_filesystems_a_directory_lands_whole_then_leaves_the_source(tmp_path, monkeypatch):
    nfs = tmp_path / "nfs"
    nfs.mkdir()
    sessions = _legacy()["data"] / "sessions"
    _write(sessions / "a" / "one.jsonl", "1\n")
    os.symlink(nfs, sessions / "a" / "linked")
    real_rename = os.rename

    def cross_device(src, dst):
        if Path(src) == sessions:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(src, dst)

    monkeypatch.setattr(home.os, "rename", cross_device)

    home.migrate_once(io.StringIO())

    target = paths.sessions_root()
    assert (target / "a" / "one.jsonl").read_text(encoding="utf-8") == "1\n"
    assert os.readlink(target / "a" / "linked") == str(nfs)
    assert not sessions.exists()
    assert nfs.is_dir()
    assert [p.name for p in target.parent.iterdir() if "migrating" in p.name] == []


def test_an_old_copy_that_cannot_be_removed_is_reported_after_the_move(tmp_path, monkeypatch):
    sessions = _legacy()["data"] / "sessions"
    _write(sessions / "a" / "one.jsonl", "1\n")
    real_rename = os.rename

    def cross_device(src, dst):
        if Path(src) == sessions:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(src, dst)

    def stuck(path, *args, **kwargs):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(home.os, "rename", cross_device)
    monkeypatch.setattr(home.shutil, "rmtree", stuck)

    steps = home.migrate_once(io.StringIO())

    assert [step.kind for step in steps if step.source == sessions] == ["move", "refuse"]
    assert (paths.sessions_root() / "a" / "one.jsonl").read_text(encoding="utf-8") == "1\n"
    assert (sessions / "a" / "one.jsonl").is_file()


def test_a_failed_copy_across_filesystems_leaves_the_source_whole(tmp_path, monkeypatch):
    sessions = _legacy()["data"] / "sessions"
    _write(sessions / "a" / "one.jsonl", "1\n")
    real_rename = os.rename

    def cross_device(src, dst):
        if Path(src) == sessions:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(src, dst)

    def broken_copy(src, dst, symlinks=False):
        Path(dst).mkdir()
        (Path(dst) / "partial").write_text("x", encoding="utf-8")
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(home.os, "rename", cross_device)
    monkeypatch.setattr(home.shutil, "copytree", broken_copy)

    steps = home.migrate_once(io.StringIO())

    assert [step.kind for step in steps if step.source == sessions] == ["refuse"]
    assert (sessions / "a" / "one.jsonl").read_text(encoding="utf-8") == "1\n"
    assert not paths.sessions_root().exists()
    assert [p.name for p in paths.home().iterdir() if "migrating" in p.name] == []


def test_js_startup_migrates_and_then_lists_the_moved_session(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _write(_legacy()["data"] / "sessions" / "defaultagent" / "old.jsonl", '{"role":"user","content":"hi"}\n')

    assert cli.main(["--blocking", "--list", "--json"]) == 0

    captured = capsys.readouterr()
    assert len([line for line in captured.err.splitlines() if "->" in line]) == 1
    records = [json.loads(line) for line in captured.out.splitlines()]
    assert [(item["agent"], item["name"]) for item in records] == [("defaultagent", "old")]

    assert cli.main(["--blocking", "--list", "--json"]) == 0
    assert "->" not in capsys.readouterr().err


def test_home_as_the_working_directory_loads_the_global_jsrc_once():
    loaded = config.jsrc_paths(paths.user_home())
    assert loaded.count(paths.global_config_file()) == 1


def test_sweep_clears_stale_tmp_entries_and_keeps_fresh_ones(tmp_path):
    root = paths.tmp_dir()
    stale_dir = root / "old-probe"
    _write(stale_dir / "junk.txt", "x")
    fresh = _write(root / "fresh.txt", "y")
    outside = tmp_path / "outside"
    _write(outside / "keep.txt", "z")
    link = root / "link"
    os.symlink(outside, link)
    old = time.time() - home.TMP_MAX_AGE_SECONDS - 60
    os.utime(stale_dir, (old, old))
    os.utime(link, (old, old), follow_symlinks=False)

    removed = home.sweep_tmp()

    assert sorted(p.name for p in removed) == ["link", "old-probe"]
    assert fresh.is_file()
    assert (outside / "keep.txt").is_file()


def test_no_module_outside_paths_names_the_old_locations():
    root = Path(__file__).resolve().parent.parent / "js"
    offenders = []
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in (".py", ".md") or path.name == "paths.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if ".config/js" in text or ".local/share/js" in text or "inbox/agents/js" in text:
            offenders.append(str(path.relative_to(root)))
    assert offenders == []


def test_a_fresh_working_directory_is_left_empty_by_plans_and_snapshots(tmp_path, monkeypatch):
    from js.toolkit import ToolContext, meta

    work = tmp_path / "fresh"
    work.mkdir()
    meta.plan(plan_name="p", version="v1", content="x", context=ToolContext(cwd=work))

    assert list(work.iterdir()) == []
    assert (paths.plans_dir() / "p-v1.md").is_file()
