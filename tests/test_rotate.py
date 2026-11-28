"""`js --rotate` and `--unrotate`: old sessions and logs move to the archive
tree, compressed, and stay listed, searchable and resumable."""

from __future__ import annotations

import fcntl
import hashlib
import os
import time
from pathlib import Path

import pytest
from repl_driver import stub_screen, turn_stub

from js import (
    archive,
    cli,
    memory,
    paths,
    rotate,
    runtime,
    session_index,
    session_store,
    session_text,
)
from js.config import resolve_session_file
from js.session_catalog import acquire_session, catalog_sessions

DAY = 86400
OLD = time.time() - 60 * DAY


@pytest.fixture(autouse=True)
def fresh_tool_context(monkeypatch, tmp_path):
    from js.toolkit.core import ToolContext

    monkeypatch.setattr(runtime.T, "STOCK_CONTEXT", ToolContext(cwd=tmp_path))


@pytest.fixture
def project(monkeypatch, tmp_path):
    directory = tmp_path / "proj"
    directory.mkdir()
    monkeypatch.chdir(directory)
    return directory


def _session(project: Path, name: str, words: str, *, mtime: float | None = OLD) -> Path:
    path = session_store.project_folder(project) / f"{name}{session_store.SUFFIX}"
    memory.append_message(path, {"role": "user", "content": words})
    memory.append_message(path, {"role": "assistant", "content": "noted"})
    session_text.refresh(path)
    if mtime is not None:
        for each in (path, session_store.text_path(path)):
            if each.exists():
                os.utime(each, (mtime, mtime))
    return path


def _log(relative: str, *, mtime: float = OLD, size: int = 20000) -> Path:
    path = paths.logs_root() / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(b"line %d\n" % n for n in range(size)))
    os.utime(path, (mtime, mtime))
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree(root: Path) -> dict[str, tuple[int, int, str]]:
    return {str(path): (path.stat().st_size, path.stat().st_mtime_ns, _digest(path))
            for path in sorted(root.rglob("*")) if path.is_file()}


def _rotate(age: str = "30d") -> rotate.Tally:
    return rotate.rotate(rotate.parse_age(age), "xz")


# --- ages ---------------------------------------------------------------------


@pytest.mark.parametrize(("text", "idle_days", "selected"), [
    ("30d", 31, True), ("30d", 29, False), (">30d", 30, True), (">30d", 10, False),
    ("<7d", 3, True), ("<7d", 8, False), ("2w", 15, True), ("2w", 13, False),
    ("12h", 0.75, True), ("12h", 0.25, False),
])
def test_age_forms_select_by_modification_time(text, idle_days, selected):
    now = 1_000_000_000.0
    assert rotate.parse_age(text).selects(now - idle_days * DAY, now, 0) is selected


def test_the_floor_keeps_files_just_written_even_in_a_newer_than_window():
    now = 1_000_000_000.0
    assert rotate.parse_age("<7d").selects(now - 60, now, 3600) is False


@pytest.mark.parametrize("text", ["", "30", "d", "30x", ">=30d", "-3d"])
def test_ages_that_are_not_ages_are_refused(text):
    with pytest.raises(ValueError):
        rotate.parse_age(text)


# --- a rotated session --------------------------------------------------------


def test_a_rotated_session_keeps_its_bytes_and_its_place_in_the_mirror(project):
    session = _session(project, "2026-09-01T1200-ab12", "the zebra crossing")
    text = session_store.text_path(session)
    before = {session: (_digest(session), session.stat().st_mtime_ns),
              text: (_digest(text), text.stat().st_mtime_ns)}

    tally = _rotate()

    relative = session.relative_to(paths.home())
    archived = paths.archive_root() / relative.parent / (relative.name + ".xz")
    assert not session.exists() and not text.exists()
    assert archived.is_file()
    assert archived.stat().st_mtime_ns == before[session][1]
    assert tally.files == 2
    for live, (digest, mtime_ns) in before.items():
        assert archive.restore(live)
        assert _digest(live) == digest
        assert live.stat().st_mtime_ns == mtime_ns
    assert not archived.exists()


def test_a_rotated_session_is_listed_searched_and_read(project):
    session = _session(project, "2026-09-01T1200-ab12", "the zebra crossing")
    rows_before = [row.lines for row in session_text.rows(session)]

    _rotate()

    [listed] = [record for record in catalog_sessions(paths.sessions_root()) if record["path"] == str(session)]
    assert listed["user_turns"] == 1
    assert listed["archived"] is True
    assert str(session.resolve(strict=False)) in {entry["path"] for entry in session_index.catalog()}
    assert str(session.resolve(strict=False)) in session_index.search("zebra")
    assert [row.lines for row in session_text.rows(session)] == rows_before
    assert session_store.find(session.stem, Path(session.name), session.parent) == session
    assert not session.exists()


def test_resolving_a_rotated_session_restores_it_and_says_so(project, capsys):
    session = _session(project, "kept", "named and old")
    digest = _digest(session)
    _rotate()

    found = resolve_session_file(session_store.project_folder(project), "kept", create=True)

    assert found == session
    assert _digest(session) == digest
    assert len(memory.load_messages(session)) == 2
    assert "kept" in capsys.readouterr().err


def test_appending_to_a_rotated_session_continues_it(project):
    session = _session(project, "2026-09-01T1200-ab12", "first words")
    _rotate()

    memory.append_message(session, {"role": "user", "content": "more words"})

    assert [m["content"] for m in memory.load_messages(session)] == ["first words", "noted", "more words"]
    assert not archive.archives_of(session)


def test_last_resumes_a_rotated_session_with_its_history(project, monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.runtime, "run_turn_async", turn_stub(
        lambda cfg, system, messages, *a, **k: messages.append({"role": "assistant", "content": "ok"})))
    stub_screen(monkeypatch, ["first run"], wait=True)
    assert cli.main([]) == 0
    [session] = archive.files_under(paths.sessions_root(), "*.jsonl")
    for each in (session, session_store.text_path(session)):
        os.utime(each, (OLD, OLD))
    _rotate()
    assert not session.exists()
    seen: list = []
    stub_screen(monkeypatch, ["second run"], wait=True)
    monkeypatch.setattr(cli.runtime, "run_turn_async", turn_stub(
        lambda cfg, system, messages, *a, **k: seen.append([m["content"] for m in messages])))

    assert cli.main(["--last"]) == 0

    assert seen == [["first run", "ok", "second run"]]
    assert session.is_file()
    assert archive.files_under(paths.sessions_root(), "*.jsonl") == [session]


# --- what is left alone -------------------------------------------------------


def test_a_file_a_process_has_open_is_not_rotated(project):
    session = _session(project, "2026-09-01T1200-ab12", "open")
    log = _log("troop/run-1.log")
    with open(session, "rb"), open(log, "rb"):
        _rotate()
    assert session.is_file() and log.is_file()


def test_a_session_someone_is_in_is_not_rotated(project):
    session = _session(project, "2026-09-01T1200-ab12", "in use")
    with acquire_session(session):
        os.utime(session, (OLD, OLD))
        tally = _rotate()
    assert session.is_file()
    assert not archive.archives_of(session)
    assert tally.files == 0


def test_a_session_whose_write_lock_is_held_is_not_rotated(project, monkeypatch):
    session = _session(project, "2026-09-01T1200-ab12", "being written")
    # The lock alone, as a writer in another mount namespace would hold it.
    monkeypatch.setattr(rotate, "_open_files", set)
    with open(session, "rb") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        tally = _rotate()
    assert session.is_file()
    assert not archive.archives_of(session)
    assert [path for path, _ in tally.skipped] == [session]


def test_a_young_session_and_its_logs_stay_and_old_logs_go(project):
    young = _session(project, "2026-11-27T1200-cd34", "fresh", mtime=None)
    old = _session(project, "2026-09-01T1200-ab12", "stale")
    young_log = _log(f"defaultagent/{young.stem}.log")
    old_log = _log(f"defaultagent/{old.stem}.log")
    screen_log = _log(f"transcript/defaultagent/{old.stem}.log")
    orphan = _log("troop/run-1.log")
    recent_orphan = _log("troop/run-2.log", mtime=time.time())
    packed = _log("archive/earlier.tar.zst")

    _rotate()

    assert young.is_file() and young_log.is_file() and recent_orphan.is_file() and packed.is_file()
    for gone in (old, old_log, screen_log, orphan):
        assert not gone.exists()
        assert archive.is_archived(gone)


def test_a_dry_run_changes_nothing(project, capsys):
    _session(project, "2026-09-01T1200-ab12", "stale")
    _log("defaultagent/2026-09-01T1200-ab12.log")
    before = _tree(paths.home())

    assert cli.main(["--rotate", "--dry-run"]) == 0

    assert _tree(paths.home()) == before
    assert not paths.archive_root().exists()
    assert "2026-09-01T1200-ab12" in capsys.readouterr().out


def test_unrotate_puts_one_session_back_with_its_logs(project):
    session = _session(project, "2026-09-01T1200-ab12", "stale")
    other = _session(project, "2026-09-02T1200-ef56", "also stale")
    log = _log(f"defaultagent/{session.stem}.log")
    digest = _digest(log)
    _rotate()

    assert cli.main(["--unrotate", session.stem]) == 0

    assert session.is_file() and session_store.text_path(session).is_file()
    assert _digest(log) == digest
    assert archive.is_archived(other)


def test_unrotate_puts_everything_back_and_the_tree_matches(project):
    _session(project, "2026-09-01T1200-ab12", "stale")
    _log("defaultagent/2026-09-01T1200-ab12.log")
    _log("troop/run-1.log")

    def kept(tree):
        # The search index is derived, and the lock files nothing held are
        # removed with their session.
        return {path: entry for path, entry in tree.items()
                if "/cache/" not in path and not path.endswith(".lock")}

    before = kept(_tree(paths.home()))
    _rotate()

    assert cli.main(["--unrotate"]) == 0

    assert kept(_tree(paths.home())) == before


# --- stopping part way --------------------------------------------------------


def test_a_rotation_that_fails_before_the_rename_leaves_the_original(project, monkeypatch):
    session = _session(project, "2026-09-01T1200-ab12", "stale")
    digest = _digest(session)

    def broken(*_args):
        raise OSError("disk went away")

    monkeypatch.setattr(archive, "_digest_archive", broken)
    tally = _rotate()

    assert _digest(session) == digest
    assert not archive.archives_of(session)
    assert tally.files == 0
    assert not [path for path in paths.archive_root().rglob("*") if path.is_file()]


def test_a_rotation_stopped_before_the_original_was_removed_reads_and_rotates_again(project):
    session = _session(project, "2026-09-01T1200-ab12", "stale")
    digest = _digest(session)
    # The state a stop between the rename and the unlink leaves: both, identical.
    copy = archive.archive_path(session, "xz")
    original = session.read_bytes()
    archive.archive_file(session, "xz")
    session.write_bytes(original)
    os.utime(session, (OLD, OLD))
    assert copy.is_file() and session.is_file()

    assert memory.load_messages(session)[0]["content"] == "stale"
    _rotate()

    assert not session.exists()
    assert archive.restore(session)
    assert _digest(session) == digest


def test_a_restore_that_runs_out_of_space_keeps_the_archive(project, monkeypatch):
    session = _session(project, "2026-09-01T1200-ab12", "stale")
    _rotate()
    [archived] = archive.archives_of(session)
    real_link = os.link

    def full(*_args, **_kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "link", full)
    with pytest.raises(OSError):
        archive.restore(session)
    monkeypatch.setattr(os, "link", real_link)

    assert archived.is_file()
    assert not session.exists()
    assert not [path for path in session.parent.iterdir() if path.name.endswith(".tmp")]
    assert archive.restore(session)


def test_an_archive_that_is_not_a_prefix_of_the_live_file_is_left_alone(project):
    session = _session(project, "2026-09-01T1200-ab12", "stale")
    _rotate()
    [archived] = archive.archives_of(session)
    session.write_text("something else\n", encoding="utf-8")
    os.utime(session, (OLD, OLD))
    held = archived.read_bytes()

    tally = _rotate()

    assert archived.read_bytes() == held
    assert session.read_text(encoding="utf-8") == "something else\n"
    assert [path for path, _ in tally.skipped] == [session]
