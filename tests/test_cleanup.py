"""The rules of `js cleanup` (`js.cleanup`): what each stage of the clock does
to a piece of work, that an interrupted stage change is finished or undone,
that a run with nothing due changes nothing, and that nothing outside the
workspace and its archive area is touched."""

from __future__ import annotations

import gzip
import os
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

from js import cleanup

DAY = 86400
AGES = cleanup.Ages(archive=30 * DAY, gzip=60 * DAY, tar=90 * DAY)
NOW = time.time()


def elf(e_type: int, machine: int = 62) -> bytes:
    """A little-endian ELF header of the given type and machine (x86-64 by
    default), padded with NULs."""
    return (b"\x7fELF\x02\x01\x01" + b"\0" * 9 + e_type.to_bytes(2, "little")
            + machine.to_bytes(2, "little") + b"\0" * 44)


def age(path: Path, days: float) -> None:
    """Set every file and directory under ``path`` to ``days`` old by their
    modification times. Their change times stay now, so the live stage, which
    counts those too, is reached by a run ``days`` later (`run`)."""
    stamp = NOW - days * DAY
    for root, dirs, files in os.walk(path):
        for name in [*dirs, *files]:
            os.utime(Path(root) / name, (stamp, stamp), follow_symlinks=False)
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def snapshot(*roots: Path) -> dict:
    out = {}
    for root in roots:
        for current, dirs, files in os.walk(root):
            for name in [*dirs, *files]:
                path = Path(current) / name
                stat = path.lstat()
                out[str(path)] = (stat.st_mtime, path.read_bytes() if path.is_file() and not path.is_symlink() else None)
    return out


@pytest.fixture
def spaces(tmp_path):
    workspace, area = tmp_path / "work", tmp_path / "archive" / "work"
    workspace.mkdir()
    return workspace, area


def run(workspace: Path, area: Path, days: float = 0, **kwargs) -> cleanup.Report:
    """A run ``days`` from now."""
    return cleanup.Cleanup(workspace, AGES, archive=area, log=area.parent / "cleanup.log",
                           now=NOW + days * DAY, **kwargs).run()


def build_piece(workspace: Path) -> Path:
    piece = workspace / "sdcpp-bench"
    build = piece / "sd" / "build"
    (build / "CMakeFiles" / "3.31.0").mkdir(parents=True)
    (build / "CMakeFiles" / "ggml.dir").mkdir()
    (build / "bin").mkdir()
    (build / "Testing" / "Temporary").mkdir(parents=True)
    (piece / "sd" / "CMakeLists.txt").write_text("project(sd)\n")
    (build / "CMakeCache.txt").write_text("CMAKE_BUILD_TYPE:STRING=Release\n")
    (build / "CMakeFiles" / "3.31.0" / "CMakeCXXCompiler.cmake").write_text('set(CMAKE_CXX_COMPILER_VERSION "19.1")\n')
    (build / "CMakeFiles" / "ggml.dir" / "ggml.c.o").write_bytes(elf(1))
    (build / "bin" / "sd").write_bytes(elf(2))
    (build / "libggml.so.1").write_bytes(elf(3))
    (build / "ggml.hip-amdgcn-amd-amdhsa--gfx1030").write_bytes(elf(3, machine=224))
    (build / "libggml.so").symlink_to("libggml.so.1")
    (build / "libggml.a").write_bytes(b"!<arch>\n" + b"\0" * 8)
    (build / "build.ninja").write_text("rule cc\n")
    (build / "compile_commands.json").write_text("[]\n")
    (build / "Testing" / "Temporary" / "LastTest.log").write_text("100% tests passed\n")
    (build / "bench-tp16.csv").write_text("threads,it/s\n16,3.2\n")
    (piece / "NOTES.md").write_text("tensor parallel sd.cpp, it/s per thread count\n")
    (piece / "render.mp4").write_bytes(b"\0\0\0\x18ftypisom" + b"\0" * 32)
    (piece / "node_modules" / "left-pad").mkdir(parents=True)
    (piece / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 1\n")
    return piece


def test_a_piece_past_thirty_days_is_archived_with_its_junk_gone(spaces):
    workspace, area = spaces
    build_piece(workspace)

    report = run(workspace, area, days=40)

    archived = area / "sdcpp-bench"
    assert not (workspace / "sdcpp-bench").exists()
    assert (archived / "NOTES.md").is_file() and (archived / "sd" / "CMakeLists.txt").is_file()
    kept = archived / cleanup.BUILD_KEPT / "sd" / "build"
    for name in ("CMakeCache.txt", "CMakeFiles/3.31.0/CMakeCXXCompiler.cmake", "bin/sd", "libggml.so.1",
                 "compile_commands.json", "Testing/Temporary/LastTest.log", "bench-tp16.csv"):
        assert (kept / name).is_file(), name
    assert (kept / "libggml.so").is_symlink()
    assert not (kept / "libggml.a").exists() and not (kept / "build.ninja").exists()
    assert not (kept / "ggml.hip-amdgcn-amd-amdhsa--gfx1030").exists()
    assert not (kept / "CMakeFiles" / "ggml.dir").exists()
    assert not (archived / "sd" / "build").exists()
    assert not (archived / "render.mp4").exists() and not (archived / "node_modules").exists()
    assert report.archived == ["sdcpp-bench"] and report.freed > 0
    log = (area.parent / "cleanup.log").read_text().splitlines()
    assert any("  deleted  " in line and line.split("  ")[2].endswith("render.mp4") for line in log)
    assert any("  moved  " in line and f"->  {area / 'sdcpp-bench'}" in line for line in log)
    index = (area / cleanup.INDEX).read_text()
    assert "## sdcpp-bench" in index and "render.mp4" in index and "bench-tp16.csv" in index
    assert str(area / cleanup.INDEX) in (workspace / cleanup.INDEX).read_text()


def test_a_young_piece_is_left_untouched(spaces):
    workspace, area = spaces
    piece = build_piece(workspace)
    before = snapshot(piece)

    report = run(workspace, area, days=10)

    assert snapshot(piece) == before
    assert report.removed == report.moved == 0


def test_a_second_run_with_nothing_due_changes_nothing(spaces):
    workspace, area = spaces
    build_piece(workspace)
    run(workspace, area, days=40)
    before = snapshot(workspace, area)

    report = run(workspace, area, days=40)

    assert snapshot(workspace, area) == before
    assert report.removed == report.moved == 0 and not report.lines


def test_a_dry_run_changes_nothing_and_says_what_it_would_do(spaces):
    workspace, area = spaces
    build_piece(workspace)
    before = snapshot(workspace)

    report = run(workspace, area, days=40, dry=True)

    assert snapshot(workspace) == before and not area.exists()
    assert report.archived == ["sdcpp-bench"] and report.removed


def test_loose_files_go_by_day_text_kept_binaries_removed(spaces):
    workspace, area = spaces
    (workspace / "argsort-gpu1.log").write_text("1.9 ms\n")
    (workspace / "ik-argsort.o").write_bytes(elf(1))

    assert run(workspace, area, days=10).moved == 0
    run(workspace, area, days=45)

    day = cleanup._date(NOW)
    assert (area / f"loose-{day}" / "argsort-gpu1.log").read_text() == "1.9 ms\n"
    assert not (workspace / "ik-argsort.o").exists() and not (area / f"loose-{day}" / "ik-argsort.o").exists()


def test_a_tree_copied_in_with_old_times_is_as_new_as_the_copy(spaces):
    workspace, area = spaces
    piece = workspace / "backup-copy"
    piece.mkdir()
    (piece / "notes.md").write_text("copied\n")
    age(piece, 400)

    report = run(workspace, area)

    assert (piece / "notes.md").is_file() and report.moved == 0


def test_files_the_workspace_repository_tracks_stay(spaces):
    workspace, area = spaces
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "run.sh").write_text("echo\n")
    subprocess.run(["git", "-C", str(workspace), "add", "run.sh"], check=True)

    run(workspace, area, days=45)

    assert (workspace / "run.sh").is_file()


def test_gzip_at_sixty_days_then_tar_at_ninety(spaces):
    workspace, area = spaces
    build_piece(workspace)
    run(workspace, area, days=40)
    piece = area / "sdcpp-bench"
    original = (piece / "NOTES.md").read_bytes()
    stamp = (piece / "NOTES.md").stat().st_mtime

    report = run(workspace, area, days=70)

    assert report.gzipped == ["sdcpp-bench"]
    assert not (piece / "NOTES.md").exists()
    assert gzip.decompress((piece / "NOTES.md.gz").read_bytes()) == original
    assert (piece / "NOTES.md.gz").stat().st_mtime == stamp
    assert "gzipped" in (area / cleanup.INDEX).read_text()

    report = run(workspace, area, days=100)

    tar = area / "sdcpp-bench.tar"
    assert report.tarred == ["sdcpp-bench"] and tar.is_file() and not piece.exists()
    with tarfile.open(tar) as archive:
        assert "sdcpp-bench/NOTES.md.gz" in archive.getnames()
    index = (area / cleanup.INDEX).read_text()
    assert "tarred" in index and "sdcpp-bench/NOTES.md.gz" in index


def test_a_piece_past_ninety_days_reaches_the_last_stage_in_one_run(spaces):
    workspace, area = spaces
    build_piece(workspace)

    run(workspace, area, days=120)

    assert (area / "sdcpp-bench.tar").is_file() and not (area / "sdcpp-bench").exists()


def test_an_interrupted_gzip_swap_is_finished(spaces):
    workspace, area = spaces
    old = area / ".piece.cleanup-old"
    new = area / ".piece.cleanup-new"
    old.mkdir(parents=True)
    (old / "a.txt").write_text("a\n")
    new.mkdir()
    (new / "a.txt.gz").write_bytes(gzip.compress(b"a\n"))

    run(workspace, area)

    assert (area / "piece" / "a.txt.gz").is_file()
    assert not old.exists() and not new.exists()


def test_an_interrupted_gzip_build_is_undone(spaces):
    workspace, area = spaces
    (area / "piece").mkdir(parents=True)
    (area / "piece" / "a.txt").write_text("a\n")
    (area / ".piece.cleanup-new").mkdir()

    run(workspace, area)

    assert (area / "piece" / "a.txt").read_text() == "a\n"
    assert not (area / ".piece.cleanup-new").exists()


def test_an_interrupted_tar_is_finished_or_undone(spaces):
    workspace, area = spaces
    (area / "done").mkdir(parents=True)
    (area / "done" / "a.txt.gz").write_bytes(b"x")
    with tarfile.open(area / "done.tar", "w") as archive:
        archive.add(area / "done", arcname="done")
    (area / "half").mkdir()
    (area / ".half.tar.tmp").write_bytes(b"partial")

    run(workspace, area)

    assert (area / "done.tar").is_file() and not (area / "done").exists()
    assert (area / "half").is_dir() and not (area / ".half.tar.tmp").exists()


def test_nothing_outside_the_workspace_and_archive_is_touched(spaces, tmp_path):
    workspace, area = spaces
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "weights.bin").write_bytes(b"\0" * 64)
    piece = workspace / "linked"
    piece.mkdir()
    (piece / "weights.bin").symlink_to(outside / "weights.bin")
    (piece / "notes.md").write_text("n\n")

    run(workspace, area, days=40)

    assert (outside / "weights.bin").read_bytes() == b"\0" * 64
    with pytest.raises(PermissionError):
        cleanup.Cleanup(workspace, AGES, archive=area)._inside(outside / "weights.bin")


def _git(*args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=a", "-c", "user.email=a@x", *args],
                          check=True, capture_output=True, text=True).stdout


def _worktree_piece(tmp_path: Path, workspace: Path, *, work: bool) -> tuple[Path, Path]:
    owner = tmp_path / "owner"
    _git("init", "-q", str(owner))
    (owner / "main.c").write_text("int main(void) { return 0; }\n")
    _git("-C", str(owner), "add", "main.c")
    _git("-C", str(owner), "commit", "-q", "-m", "base")
    piece = workspace / "sd-bench"
    piece.mkdir()
    checkout = piece / "sd"
    _git("-C", str(owner), "worktree", "add", "-q", str(checkout), "-b", "agent")
    (checkout / "build").mkdir()
    (checkout / "build" / "CMakeCache.txt").write_text("CMAKE_BUILD_TYPE:STRING=Release\n")
    (checkout / "build" / "main.o").write_bytes(elf(1))
    if work:
        (checkout / "main.c").write_text("int main(void) { return 1; }\n")
        _git("-C", str(checkout), "commit", "-q", "-am", "agent change")
        (checkout / "tune.h").write_text("#define TILE 64\n")
    (piece / "results.csv").write_text("it/s\n3.2\n")
    return owner, checkout


def test_a_worktree_with_work_its_repository_lacks_is_patched_then_retired(spaces, tmp_path):
    workspace, area = spaces
    owner, checkout = _worktree_piece(tmp_path, workspace, work=True)

    report = run(workspace, area, days=40)

    patch = area / "sd-bench" / "sd-worktree.patch"
    text = patch.read_text()
    assert "agent change" in text and "return 1" in text and "TILE 64" in text
    assert (area / "sd-bench" / cleanup.BUILD_KEPT / "sd" / "build" / "CMakeCache.txt").is_file()
    assert (area / "sd-bench" / "results.csv").is_file()
    assert str(checkout) not in _git("-C", str(owner), "worktree", "list")
    assert not (owner / ".git" / "worktrees" / "sd").exists()
    assert "agent" in _git("-C", str(owner), "branch", "--list", "agent")
    assert report.retired == [str(checkout)]


def test_a_worktree_its_repository_already_has_is_retired_without_a_patch(spaces, tmp_path):
    workspace, area = spaces
    owner, checkout = _worktree_piece(tmp_path, workspace, work=False)

    report = run(workspace, area, days=40)

    assert not report.patches and not (area / "sd-bench" / "sd-worktree.patch").exists()
    assert str(checkout) not in _git("-C", str(owner), "worktree", "list")


def test_a_clone_the_rules_cannot_compare_keeps_its_piece_live(spaces, tmp_path):
    workspace, area = spaces
    piece = workspace / "research"
    _git("init", "-q", str(piece / "llama.cpp"))
    (piece / "findings.md").write_text("q8 wins\n")

    report = run(workspace, area, days=40)

    assert (piece / "findings.md").is_file() and (piece / "llama.cpp" / ".git").is_dir()
    assert report.checkouts == [piece]


def test_text_or_binary_is_decided_by_content(tmp_path):
    text, wide, binary = tmp_path / "results", tmp_path / "bench.txt", tmp_path / "data.log"
    text.write_text("tok/s 41.2\n")
    wide.write_text("tok/s 41.2\n", encoding="utf-16")
    binary.write_bytes(b"abc\0def")

    assert cleanup.is_text(text) and cleanup.is_text(wide) and not cleanup.is_text(binary)


def test_an_in_tree_build_keeps_its_source(spaces):
    workspace, area = spaces
    tree = workspace / "proj" / "argsort"
    (tree / "CMakeFiles").mkdir(parents=True)
    (tree / "CMakeLists.txt").write_text("project(argsort)\n")
    (tree / "CMakeCache.txt").write_text("X:STRING=1\n")
    (tree / "argsort.cpp").write_text("int main() {}\n")
    (tree / "argsort.o").write_bytes(elf(1))

    run(workspace, area, days=40)

    archived = area / "proj" / "argsort"
    assert (archived / "argsort.cpp").is_file() and (archived / "CMakeLists.txt").is_file()
    assert not (archived / "argsort.o").exists()


def test_workspaces_expand_a_directory_of_harnesses(tmp_path):
    (tmp_path / "agents" / "claude").mkdir(parents=True)
    (tmp_path / "agents" / "codex").mkdir()

    found = cleanup.workspaces([str(tmp_path / "agents" / "*"), str(tmp_path / "work")])

    assert found == [tmp_path / "agents" / "claude", tmp_path / "agents" / "codex", tmp_path / "work"]


def test_the_work_directory_archives_beside_the_session_archive():
    from js import paths

    assert cleanup.archive_area(paths.work_dir()) == paths.archive_root() / "work"
    assert cleanup.archive_area(Path("/srv/agents")).parent == paths.archive_root()
