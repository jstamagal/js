"""`js init` in a js home: `defaults/` whole laid into the home; `-f`
replaces, `-b` moves the home aside first, `-a` also turns the moved home
into one checked tarball."""

from __future__ import annotations

import datetime
import io
import os
import sys
import tarfile
from pathlib import Path

import pytest

from js import entry, init_home, paths


@pytest.fixture
def where(tmp_path, monkeypatch):
    """A working directory outside any repository, and a home path below
    its own parent that does not exist yet."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return work, tmp_path / "user" / "home-js"


_FAKE_B3SUM = """#!{python}
import hashlib, os, sys
with open(os.environ["FAKE_B3SUM_LOG"], "a") as log:
    log.write("run\\n")
digest = hashlib.blake2b(digest_size=32)
for chunk in iter(lambda: sys.stdin.buffer.read(65536), b""):
    digest.update(chunk)
mode = os.environ.get("FAKE_B3SUM_MODE", "ok")
if mode == "fail":
    sys.exit(3)
print("not a digest" if mode == "garbage" else digest.hexdigest() + "  -")
"""


def _path_with(tmp_path: Path, monkeypatch, *, fake: bool) -> Path:
    """PATH holding only a fake `b3sum`, or nothing; the log of the fake's
    runs, one line each."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "b3sum.log"
    monkeypatch.setenv("FAKE_B3SUM_LOG", str(log))
    monkeypatch.setenv("PATH", str(bin_dir))
    if fake:
        program = bin_dir / "b3sum"
        program.write_text(_FAKE_B3SUM.format(python=sys.executable))
        program.chmod(0o755)
    return log


@pytest.fixture(params=["b3sum", "none"])
def hasher(request, tmp_path, monkeypatch):
    return request.param, _path_with(tmp_path, monkeypatch, fake=request.param == "b3sum")


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def _answer(monkeypatch, text: str | None) -> None:
    """A terminal on stdin that answers ``text`` (end of input when it is
    empty), or with None, Ctrl-C."""
    monkeypatch.setattr(sys, "stdin", _Terminal(text or ""))
    if text is None:
        def interrupted(prompt=""):
            sys.stdout.write(prompt)
            raise KeyboardInterrupt
        monkeypatch.setattr("builtins.input", interrupted)


def _moved(home: Path, suffix: str = "") -> Path:
    return home.with_name(f"{home.name}-{datetime.date.today().isoformat()}{suffix}")


def _init(home: Path, *flags: str) -> int:
    return entry.main(["--home", str(home), "init", *flags])


def _files(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*")
            if path.is_file() and not path.is_symlink()}


def _stock() -> dict[str, bytes]:
    return _files(paths.defaults_dir())


def test_a_missing_home_is_made_and_filled(where):
    _work, home = where
    assert _init(home) == 0
    got = _files(home)
    assert {rel: got.get(rel) for rel in _stock()} == _stock()
    for directory in paths.layout_dirs():
        assert directory.is_dir()
    assert not (home / ".git").exists() and not (home.parent / ".git").exists()


def test_an_existing_home_gains_only_what_is_missing(where, capsys):
    _work, home = where
    (home / "agents" / "slim").mkdir(parents=True)
    (home / "jsrc").write_text("set BANNER mine\n")
    (home / "agents" / "slim" / "01-prompt.md").write_text("my slim\n")
    (home / "agents" / "mine.md").write_text("mine\n")
    (home / "themes").mkdir()
    (home / "themes" / "paper").symlink_to(home / "nowhere")
    before = _files(home)

    assert _init(home) == 0

    after = _files(home)
    assert {rel: after[rel] for rel in before} == before
    assert (home / "themes" / "paper").is_symlink()
    missing = set(_stock()) - set(before) - {"themes/paper"}
    assert all(after[rel] == _stock()[rel] for rel in missing)
    out = capsys.readouterr().out
    assert str(home / "jsrc") in out and str(home / "agents/slim/01-prompt.md") in out
    assert str(home / "agents/mine.md") not in out


def test_force_replaces_a_changed_stock_file_and_leaves_the_users_own(where, capsys):
    _work, home = where
    assert _init(home) == 0
    (home / "jsrc").write_text("set BANNER mine\n")
    (home / "agents" / "mine.md").write_text("mine\n")
    (home / "themes" / "paper").unlink()
    (home / "themes" / "paper").symlink_to(home / "nowhere")
    capsys.readouterr()

    assert _init(home, "-f") == 0

    assert (home / "jsrc").read_bytes() == (paths.defaults_dir() / "jsrc").read_bytes()
    assert (home / "agents" / "mine.md").read_text() == "mine\n"
    assert (home / "themes" / "paper").is_symlink()
    assert str(home / "jsrc") in capsys.readouterr().out


def test_backup_moves_the_whole_home_aside_and_starts_over(where):
    _work, home = where
    assert _init(home) == 0
    (home / "jsrc").write_text("set BANNER mine\n")
    (home / "sessions" / "s.jsonl").write_text("{}\n")
    (home / "logins" / "token").write_text("secret\n")
    before = _files(home)

    assert _init(home, "--backup") == 0

    moved = home.with_name(f"{home.name}-{datetime.date.today().isoformat()}")
    assert _files(moved) == before
    assert _files(home) == _stock()


def test_two_backups_in_one_day_do_not_collide(where):
    _work, home = where
    assert _init(home) == 0
    assert _init(home, "-b") == 0
    (home / "jsrc").write_text("second\n")
    assert _init(home, "-b") == 0
    stamp = datetime.date.today().isoformat()
    first, second = home.with_name(f"{home.name}-{stamp}"), home.with_name(f"{home.name}-{stamp}-2")
    assert first.is_dir() and second.is_dir()
    assert (second / "jsrc").read_text() == "second\n"
    assert (first / "jsrc").read_bytes() == (paths.defaults_dir() / "jsrc").read_bytes()


def _members(tarball: Path) -> dict[str, bytes]:
    with tarfile.open(tarball, "r:gz") as tar:
        return {member.name: tar.extractfile(member).read() for member in tar
                if member.isfile() or member.islnk()}


def test_archive_leaves_only_a_tarball_holding_every_file(where, hasher, monkeypatch, capsys):
    _work, home = where
    kind, log = hasher
    assert _init(home) == 0
    (home / "sessions" / "s.jsonl").write_text("{}\n")
    (home / "agents" / "link").symlink_to("slim")
    os.link(home / "jsrc", home / "work" / "jsrc-again")
    before = _files(home)
    _answer(monkeypatch, "y\n")

    assert _init(home, "-a") == 0

    moved = home.with_name(f"{home.name}-{datetime.date.today().isoformat()}")
    tarball = init_home.tarball_name(moved, home.parent)
    assert not moved.exists()
    held = _members(tarball)
    assert {name.removeprefix(moved.name + "/"): body for name, body in held.items()} == before
    with tarfile.open(tarball, "r:gz") as tar:
        assert tar.getmember(f"{moved.name}/agents/link").linkname == "slim"
    assert _files(home) == _stock()
    assert [path.name for path in home.parent.iterdir() if path.name.startswith(".")] == []
    out = capsys.readouterr().out
    assert ("BLAKE3" if kind == "b3sum" else "sha256") in out
    # One process for the tarball's bytes, one for the moved home's.
    assert (log.read_text().count("run") if log.exists() else 0) == (2 if kind == "b3sum" else 0)


@pytest.mark.parametrize("mode", ["fail", "garbage"])
def test_a_failing_b3sum_falls_back_to_sha256_and_says_so(where, tmp_path, monkeypatch, capsys, mode):
    _path_with(tmp_path, monkeypatch, fake=True)
    _work, home = where
    assert _init(home) == 0
    monkeypatch.setenv("FAKE_B3SUM_MODE", mode)
    _answer(monkeypatch, "yes\n")
    capsys.readouterr()

    assert _init(home, "-a") == 0

    captured = capsys.readouterr()
    assert "b3sum" in captured.err
    assert "sha256" in captured.out
    moved = home.with_name(f"{home.name}-{datetime.date.today().isoformat()}")
    assert init_home.tarball_name(moved, home.parent).is_file() and not moved.exists()


@pytest.mark.parametrize("answer", ["\n", "n\n", "", None])
def test_anything_but_yes_keeps_the_moved_home(where, monkeypatch, capsys, answer):
    _work, home = where
    assert _init(home) == 0
    before = _files(home)
    _answer(monkeypatch, answer)

    assert _init(home, "-a") == 0

    moved = _moved(home)
    assert _files(moved) == before
    assert init_home.tarball_name(moved, home.parent).is_file()
    out = capsys.readouterr().out
    assert str(init_home.tarball_name(moved, home.parent)) in out and f"Delete {moved}?" in out


def test_without_a_terminal_nobody_is_asked_and_the_moved_home_stays(where, capsys):
    _work, home = where
    assert _init(home) == 0
    before = _files(home)

    assert _init(home, "-a") == 0

    moved = _moved(home)
    assert _files(moved) == before
    assert init_home.tarball_name(moved, home.parent).is_file()
    assert "Delete" not in capsys.readouterr().out


def test_archive_into_a_directory_puts_the_tarball_there_only(where, tmp_path, monkeypatch):
    _work, home = where
    drive = tmp_path / "drive"
    drive.mkdir()
    assert _init(home) == 0
    _answer(monkeypatch, "y\n")

    assert _init(home, "-a", str(drive)) == 0

    moved = _moved(home)
    assert init_home.tarball_name(moved, drive).is_file()
    assert sorted(path.name for path in home.parent.iterdir()) == [home.name]
    assert [path.name for path in drive.iterdir()] == [f"{moved.name}.tar.gz"]


@pytest.mark.parametrize("bad", ["missing", "file", "inside"])
def test_a_bad_archive_directory_refuses_before_moving_anything(where, tmp_path, bad):
    _work, home = where
    assert _init(home) == 0
    target = {"missing": tmp_path / "no-such-drive", "file": tmp_path / "a-file",
              "inside": home / "work"}[bad]
    if bad == "file":
        target.write_text("x")
    before = _files(home)

    assert _init(home, "-a", str(target)) == 2

    assert _files(home) == before
    assert sorted(path.name for path in home.parent.iterdir()) == [home.name]


def test_a_second_archive_in_one_day_takes_the_next_name(where, tmp_path, monkeypatch):
    _work, home = where
    drive = tmp_path / "drive"
    drive.mkdir()
    assert _init(home) == 0
    # A tarball of that day already in the drive, its directory long gone.
    (drive / f"{_moved(home).name}.tar.gz").write_bytes(b"older")
    _answer(monkeypatch, "y\n")

    assert _init(home, "-a", str(drive)) == 0

    assert (drive / f"{_moved(home).name}.tar.gz").read_bytes() == b"older"
    assert init_home.tarball_name(_moved(home, "-2"), drive).is_file()


@pytest.mark.parametrize("argv", [["-a", "--full"], ["-a", "-f"], ["-a", "-b"]])
def test_archive_followed_by_another_flag_is_that_flag(where, argv):
    _work, home = where
    with pytest.raises(SystemExit) as refused:
        _init(home, *argv)
    assert refused.value.code == 2
    assert sorted(path.name for path in home.parent.iterdir()) == [home.name]


def test_a_running_session_stops_a_backup(where, monkeypatch):
    from js import memory
    from js.session_catalog import acquire_session

    work, home = where
    assert _init(home) == 0
    session = paths.sessions_root() / "-proj" / "2026-11-27T1200-ab12.jsonl"
    session.parent.mkdir(parents=True)
    memory.append_message(session, {"role": "user", "content": "here"})
    with acquire_session(session):
        before = _files(home)
        assert _init(home, "-b") == 1
        assert _files(home) == before
    assert not _moved(home).exists()


def _swap_a_body(staged: Path) -> None:
    """Rewrite the tarball whole and valid, one file's bytes changed and
    its size kept: only the bytes tell it apart."""
    with tarfile.open(staged, "r:gz") as tar:
        members = [(member, tar.extractfile(member).read() if member.isfile() else None) for member in tar]
    with tarfile.open(staged, "w:gz") as tar:
        for member, body in members:
            if member.name.endswith("/jsrc"):
                body = bytes(reversed(body))
            tar.addfile(member, io.BytesIO(body) if body is not None else None)


def _flip(staged: Path) -> None:
    data = bytearray(staged.read_bytes())
    data[len(data) // 2] ^= 0xFF
    staged.write_bytes(bytes(data))


def _truncate(staged: Path) -> None:
    os.truncate(staged, staged.stat().st_size // 2)


def _fail_write(*_args, **_kwargs):
    raise OSError(28, "No space left on device")


@pytest.mark.parametrize("break_it", ["flip", "truncate", "swap", "write"])
def test_a_tarball_that_fails_leaves_the_moved_home_whole(where, hasher, monkeypatch, capsys, break_it):
    _work, home = where
    assert _init(home) == 0
    (home / "sessions" / "s.jsonl").write_text("{}\n")
    before = _files(home)
    if break_it == "write":
        monkeypatch.setattr(tarfile.TarFile, "add", _fail_write)
    else:
        monkeypatch.setattr(init_home, "_written", {"flip": _flip, "truncate": _truncate, "swap": _swap_a_body}[break_it])

    assert _init(home, "--archive") != 0

    moved = home.with_name(f"{home.name}-{datetime.date.today().isoformat()}")
    assert _files(moved) == before
    assert not init_home.tarball_name(moved, home.parent).exists()
    assert [path.name for path in home.parent.iterdir() if path.name.startswith(".")] == []
    assert _files(home) == _stock()
    if break_it == "swap":
        assert "jsrc" in capsys.readouterr().err


def test_a_file_changed_after_the_tarball_was_written_is_not_removed(where, monkeypatch):
    _work, home = where
    assert _init(home) == 0
    moved = home.with_name(f"{home.name}-{datetime.date.today().isoformat()}")

    def change(_staged):
        (moved / "jsrc").write_text("written after\n")

    monkeypatch.setattr(init_home, "_written", change)
    assert _init(home, "-a") != 0
    assert (moved / "jsrc").read_text() == "written after\n"
    assert not init_home.tarball_name(moved, home.parent).exists()


def test_the_home_holding_directory_and_the_home_itself_are_the_home_case(where, monkeypatch):
    _work, _home = where
    user = where[1].parent
    home = user / ".js"
    monkeypatch.setenv("JS_HOME", str(home))
    monkeypatch.chdir(user.parent)
    user.mkdir(exist_ok=True)
    monkeypatch.chdir(user)
    assert entry.main(["init"]) == 0
    assert (home / "jsrc").is_file() and not (user / ".git").exists()
    monkeypatch.chdir(home)
    assert entry.main(["init"]) == 0
    assert not (home / ".git").exists() and not (home / ".js").exists()


def test_a_project_init_never_runs_in_the_users_home_directory(where, monkeypatch):
    work, _home = where
    monkeypatch.setenv("HOME", str(work))
    monkeypatch.setenv("JS_HOME", str(work.parent / "elsewhere"))
    assert entry.main(["init"]) == 2
    assert list(work.iterdir()) == []


@pytest.mark.parametrize("flag", ["-f", "--force", "-b", "--backup", "-a", "--archive"])
def test_the_home_forms_are_refused_for_a_project(where, monkeypatch, flag):
    work, _home = where
    monkeypatch.setenv("JS_HOME", str(work.parent / "elsewhere"))
    with pytest.raises(SystemExit) as refused:
        entry.main(["init", flag])
    assert refused.value.code == 2
    assert list(work.iterdir()) == []


def test_full_is_refused_in_the_home(where):
    _work, home = where
    with pytest.raises(SystemExit) as refused:
        _init(home, "--full")
    assert refused.value.code == 2
    assert not (home / "jsrc").exists()
