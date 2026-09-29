"""~/.js is the one home: the old locations move in once, safely.

Every test runs against the tmp HOME the conftest fixture installs.
"""

from __future__ import annotations

import errno
import io
import json
import os
import re
import string
import time
from pathlib import Path

from js import cli, config, home, paths, session_store
from js import messages as msgs
from js.memory import append_message, load_replay_messages


def _is(entry: msgs.Message, line: str) -> bool:
    """Whether `line` is `entry` as the screen shows it, whatever its holes hold."""
    pattern = "".join(re.escape(literal) + (".+" if name is not None else "")
                      for literal, name, _spec, _conv in string.Formatter().parse(entry.template))
    return re.fullmatch(re.escape(msgs.banner("")) + pattern, line) is not None


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
    # A session with no recorded start directory is filed under ~'s folder.
    assert (session_store.folder_for(paths.user_home()) / "old.jsonl").is_file()
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


def _unused_entries() -> list[Path]:
    legacy = _legacy()
    return [
        _write(legacy["config"] / "jsrc.bak", "set model.id older\n"),
        _write(legacy["config"] / "workspace" / "scratch.md", "w\n"),
        _write(legacy["data"] / "sessions2" / "defaultagent" / "s.jsonl", '{"role":"user","content":"x"}\n'),
        _write(legacy["data"] / "logs2" / "debug.log", "l\n"),
        _write(legacy["data"] / "state.bak", "b\n"),
    ]


def test_entries_js_does_not_use_are_left_in_place_and_named(tmp_path):
    _old_layout(tmp_path)
    unused = _unused_entries()
    config, data = _legacy()["config"], _legacy()["data"]
    left = sorted([config / "jsrc.bak", config / "workspace", data / "sessions2", data / "logs2", data / "state.bak"])
    out = io.StringIO()

    steps = home.migrate_once(out)

    assert sorted(step.source for step in steps if step.kind == "unused") == left
    for path in unused:
        assert path.is_file()
    lines = out.getvalue().splitlines()
    for path in left:
        assert len([line for line in lines if home._short(path) in line]) == 1
    assert config.is_dir() and data.is_dir()
    # What js reads still moved, and the inbox became ~/.js/work.
    assert paths.global_config_file().read_text(encoding="utf-8") == "set model.id old-model\n"
    assert (paths.work_dir() / "design.md").is_file()
    assert not os.path.lexists(_legacy()["inbox"])


def test_home_holds_only_layout_entries_after_migration(tmp_path):
    legacy = _legacy()
    _old_layout(tmp_path)
    _unused_entries()
    for name in ("JS.md", "JS.local.md", "tools.yaml", ".env", "config.toml", "models-cache.json"):
        _write(legacy["config"] / name, "x\n")
    _write(legacy["config"] / "toolbox" / "t" / "tool.md", "t\n")
    for name in ("state", "logs", "commit-backups"):
        _write(legacy["data"] / name / "f", "f\n")

    home.migrate_once(io.StringIO())

    layout = {path.name for path in paths.layout_dirs()}
    files = {"jsrc", "config.toml", "JS.md", "JS.local.md", "tools.yaml", ".env"}
    assert {entry.name for entry in paths.home().iterdir()} <= layout | files
    assert sorted(entry.name for entry in legacy["config"].iterdir()) == ["jsrc.bak", "workspace"]
    assert sorted(entry.name for entry in legacy["data"].iterdir()) == ["logs2", "sessions2", "state.bak"]


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
    moved = [line for line in applied if _is(msgs.HOME_MOVED, line)]
    assert moved and len(moved) == len([line for line in preview if _is(msgs.HOME_WOULD_MOVE, line)])
    for old in _legacy().values():
        assert not os.path.lexists(old)


def test_dry_run_exits_nonzero_when_a_move_would_be_refused(tmp_path, capsys):
    _write(_legacy()["config"] / "jsrc", "old\n")
    _write(paths.global_config_file(), "new\n")

    assert home.main([]) == 1
    assert paths.global_config_file().read_text(encoding="utf-8") == "new\n"


def test_dry_run_reports_what_apply_does_when_old_entries_share_a_target(tmp_path, capsys):
    legacy = _legacy()
    _write(legacy["config"] / "state" / "x", "one\n")
    _write(legacy["data"] / "state" / "x", "two\n")
    _write(legacy["data"] / "state" / "y", "y\n")
    _write(legacy["data"] / "commit-backups" / "b.patch", "p\n")
    _write(legacy["inbox"] / "notes" / "n.md", "inbox\n")
    _write(legacy["inbox"] / "notes" / "only-inbox.md", "i\n")
    _write(legacy["data"] / "notes" / "n.md", "data\n")
    _write(legacy["data"] / "notes" / "only-data.md", "d\n")

    def outcome(steps):
        return [(step.kind, step.source, step.target) for step in steps if step.kind != "rmdir"]

    planned = outcome(home.steps(apply=False))
    assert home.main([]) == 1
    capsys.readouterr()

    assert planned == outcome(home.steps(apply=True))


def test_an_entry_that_cannot_be_compared_is_refused_and_the_rest_still_moves(tmp_path, monkeypatch):
    config = _legacy()["config"]
    _write(config / "JS.md", "context\n")
    _write(config / "jsrc", "set model.id old\n")
    clash = _write(config / "agents" / "a" / "01-prompt.md", "old\n")
    _write(paths.global_agents_dir() / "a" / "01-prompt.md", "new\n")

    def unreadable(*args, **kwargs):
        raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(home.filecmp, "cmp", unreadable)
    assert home.main([]) == 1

    out = io.StringIO()
    steps = home.migrate_once(out)

    assert [step.source for step in steps if step.kind == "refuse"] == [clash]
    assert clash.is_file()
    assert paths.global_config_file().read_text(encoding="utf-8") == "set model.id old\n"
    assert (paths.home() / "JS.md").is_file()
    assert len(out.getvalue().splitlines()) == len(steps)
    assert paths.home_migration_marker().is_file()


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
    # The session is then filed by start directory; its record keeps what it had.
    filed = session_store.folder_for(paths.user_home()) / "one.jsonl"
    assert filed.read_text(encoding="utf-8").startswith("1\n")
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
    filed = session_store.folder_for(paths.user_home()) / "one.jsonl"
    assert filed.read_text(encoding="utf-8").startswith("1\n")
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

    assert cli.main(["--list", "--json"]) == 0

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(item["agent"], item["name"]) for item in records] == [("defaultagent", "old")]


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


def test_one_start_converts_moved_agents_and_prints_what_it_dropped(tmp_path, monkeypatch, capsys):
    from js import persona

    monkeypatch.chdir(tmp_path)
    agents = _legacy()["config"] / "agents"
    _write(agents / "builder" / "00-tools.yaml",
           "tools:\n  - read\n  - multi_patch\n  - helper\n  - wiki_*\n  - artifact_*\n")
    _write(agents / "builder" / "01-prompt.md", "BUILD\n")
    _write(agents / "helper" / "01-prompt.md", "HELP\n")

    assert cli.main(["--list"]) == 0

    builder = paths.global_agents_dir() / "builder"
    assert not (builder / "00-tools.yaml").exists()
    assert persona.load_prompt_spec(builder).tool_selectors == ("read:eager", "helper:eager", "wiki_*:eager")
    printed = [line for line in capsys.readouterr().err.splitlines() if _is(msgs.HOME_DROPPED, line)]
    assert len(printed) == 1
    assert "multi_patch" in printed[0] and "artifact_*" in printed[0] and "builder" in printed[0]


def test_the_dry_run_names_the_conversion_and_changes_nothing(tmp_path):
    agents = _legacy()["config"] / "agents"
    manifest = _write(agents / "builder" / "00-tools.yaml", "tools: [read, multi_patch]\n")
    _write(agents / "builder" / "01-prompt.md", "BUILD\n")

    planned = list(home.steps(apply=False))

    assert [(step.kind, step.reason) for step in planned if step.kind == "drop"] == [("drop", "multi_patch")]
    assert any(step.kind == "convert" for step in planned)
    assert manifest.read_text(encoding="utf-8") == "tools: [read, multi_patch]\n"


def test_a_moved_relative_symlink_still_reaches_what_it_reached(tmp_path):
    shared = tmp_path / "srv" / "research"
    _write(shared / "01-prompt.md", "remote\n")
    agents = _legacy()["config"] / "agents"
    _write(agents / "mine" / "01-prompt.md", "mine\n")
    os.symlink(os.path.relpath(shared, agents), agents / "research")
    os.symlink("mine", agents / "alias")  # a sibling that moves along with it
    top = _legacy()["config"] / "JS.md"
    _write(tmp_path / "notes" / "context.md", "context\n")
    os.symlink(os.path.relpath(tmp_path / "notes" / "context.md", top.parent), top)

    steps = home.migrate_once(io.StringIO())

    moved = paths.global_agents_dir() / "research"
    assert moved.resolve() == shared.resolve()
    assert (moved / "01-prompt.md").read_text(encoding="utf-8") == "remote\n"
    assert (paths.home() / "JS.md").read_text(encoding="utf-8") == "context\n"
    assert os.readlink(paths.global_agents_dir() / "alias") == "mine"
    assert sorted(step.source.name for step in steps if step.kind == "relink") == ["JS.md", "research"]


def test_every_start_lays_out_the_whole_home(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert cli.main(["--list"]) == 0

    names = {entry.name for entry in paths.home().iterdir() if entry.is_dir()}
    assert names >= {"agents", "skills", "toolbox", "logins", "sessions", "state", "logs",
                     "cache", "work", "tmp", "plans", "probes"}
    assert {path.name for path in paths.layout_dirs()} <= names


def _record(path: Path, *records: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")
    return path


def _meta(cwd: Path, ts: float = 100.0) -> dict:
    return {"kind": "session_metadata", "version": 2, "ts": ts, "cwd": str(cwd), "agent": "defaultagent",
            "model": "m", "caller_key": None, "job_id": None}


def _msg(role: str, content: str, ts: float = 101.0, **extra) -> dict:
    return {"kind": "message", "version": 1, "ts": ts, "message": {"role": role, "content": content, **extra}}


def test_old_sessions_are_filed_by_start_directory_and_keep_their_names(tmp_path, monkeypatch, capsys):
    project = tmp_path / "work" / "my_repo"
    project.mkdir(parents=True)
    old = _legacy()["data"] / "sessions" / "defaultagent"
    generated = _record(old / "20260929T101208214271Z-acc53bcfc3ea7213.jsonl",
                        _meta(project), _msg("user", "fix it"),
                        _msg("assistant", "", 102.0, tool_calls=[{
                            "id": "t1", "type": "function",
                            "function": {"name": "task", "arguments": json.dumps(
                                {"agent_id": "reviewer", "tasks": ["review the diff"]})}}]),
                        _msg("tool", "looks fine", 110.0, tool_call_id="t1"))
    _record(old / "merrygoround.jsonl", _meta(project), _msg("user", "named"))
    _record(old / "nocwd.jsonl", _msg("user", "where was I"))
    _record(old / "derived" / "abc123.jsonl", _meta(project), _msg("user", "keyed"))
    _write(old / ".history", "+hello\n")
    _write(old / "latest.json", json.dumps({"session_file": str(generated), "session_name": generated.name}))
    child = _record(_legacy()["data"] / "sessions" / "reviewer" / "task-1789016792000-0123456789abcdef.jsonl",
                    _msg("user", "review the diff", 103.0), _msg("assistant", "lgtm", 104.0))

    home.migrate_once(io.StringIO())

    folder = session_store.folder_for(project)
    assert folder.name == "-" + str(project).strip("/").replace("/", "-").replace("_", "-")
    filed = folder / generated.name
    assert filed.is_file() and (folder / "merrygoround.jsonl").is_file()
    assert (folder / "derived" / "abc123.jsonl").is_file()
    assert (session_store.folder_for(paths.user_home()) / "nocwd.jsonl").is_file()
    moved_child = session_store.subagent_folder(filed) / child.name
    assert moved_child.is_file()
    for path in (filed, folder / "merrygoround.jsonl", moved_child):
        assert path.with_suffix(".txt").is_file()
    assert not (paths.sessions_root() / "defaultagent").exists()
    assert not (paths.sessions_root() / "reviewer").exists()
    assert (paths.state_root() / "defaultagent" / "history").read_text(encoding="utf-8") == "+hello\n"
    from js.session_catalog import first_metadata
    assert first_metadata(moved_child)["agent"] == "reviewer"
    assert first_metadata(moved_child)["parent_session"] == str(filed)

    # The old name and a hash tail still resolve, from anywhere.
    monkeypatch.chdir(tmp_path)
    assert config.resolve_session_file(session_store.folder_for(tmp_path), generated.stem) == filed
    assert config.resolve_session_file(session_store.folder_for(tmp_path), "3ea7213") == filed
    assert config.resolve_session_file(session_store.folder_for(tmp_path), "merrygoround") == folder / "merrygoround.jsonl"
    # --last finds the session latest.json named.
    assert cli._latest_session_name("defaultagent") == str(filed)


def test_a_migrated_session_has_an_id_and_a_parent_on_every_record(tmp_path):
    project = tmp_path / "proj"
    old = _record(_legacy()["data"] / "sessions" / "defaultagent" / "s.jsonl",
                  {"role": "user", "content": "from before the envelope"},
                  _meta(project), _msg("user", "one"), _msg("assistant", "two", 102.0),
                  {"kind": "mark", "version": 1, "ts": 103.0, "marker": "rollback_to:1"},
                  _msg("assistant", "three", 104.0))
    with old.open("a", encoding="utf-8") as stream:
        stream.write("not json\n")
    os.utime(old, (1_000_000, 1_000_000))
    replay = load_replay_messages(old)

    home.migrate_once(io.StringIO())

    filed = session_store.folder_for(project) / "s.jsonl"
    lines = filed.read_text(encoding="utf-8").splitlines()
    assert lines[-1] == "not json"
    records = [json.loads(line) for line in lines[:-1]]
    ids = [record["id"] for record in records]
    assert all(re.fullmatch(r"[0-9a-f]{8}", record_id) for record_id in ids) and len(set(ids)) == len(ids)
    path = [record for record in records if session_store.on_path(record)]
    assert len(path) == 5 and path[0]["parent"] is None
    assert [record["parent"] for record in path[1:]] == [record["id"] for record in path[:-1]]
    assert load_replay_messages(filed) == replay
    assert filed.stat().st_mtime == 1_000_000
    # New records chain on after the migrated ones.
    append_message(filed, {"role": "user", "content": "four"})
    assert json.loads(filed.read_text(encoding="utf-8").splitlines()[-1])["parent"] == path[-1]["id"]
    assert session_store.link_file(filed) == 0


def test_the_dry_run_names_the_filing_and_moves_no_session(tmp_path, capsys):
    project = tmp_path / "proj"
    old = _record(_legacy()["data"] / "sessions" / "defaultagent" / "s.jsonl", _meta(project), _msg("user", "hi"))
    before = old.read_bytes()

    assert home.main([]) == 0

    out = capsys.readouterr().out.splitlines()
    assert any(_is(msgs.HOME_WOULD_REFILE, line) for line in out)
    assert old.read_bytes() == before
    assert not paths.home().exists()
