from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def test_run_preserves_prompt_argument(tmp_path):
    just = shutil.which("just")
    if just is None:
        pytest.skip("just is not installed")
    uv = tmp_path / "uv"
    uv.write_text("#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    uv.chmod(0o755)
    result = subprocess.run(
        [just, "run", "-p", "two words; literal $HOME"],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
        capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout)[-2:] == ["-p", "two words; literal $HOME"]


@pytest.mark.parametrize("recipe", [
    "migrate-agents", "migrate-home", "tool-bytes", "toolbench-mine",
    "toolbench-smoke", "toolbench", "test-file", "test-mark", "toolbench-report",
])
def test_recipes_preserve_argument_boundaries(tmp_path, recipe):
    just = shutil.which("just")
    if just is None:
        pytest.skip("just is not installed")
    uv = tmp_path / "uv"
    uv.write_text("#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    uv.chmod(0o755)
    argument = "two words $HOME"
    result = subprocess.run(
        [just, recipe, argument], cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"]},
        capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout)[-1] == argument


@pytest.mark.parametrize("kind", ["file", "other-link", "managed-link"])
def test_uninstall_removes_only_its_own_wiki_link(tmp_path, kind):
    just = shutil.which("just")
    if just is None:
        pytest.skip("just is not installed")
    root = Path(__file__).resolve().parents[1]
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    wiki = bin_dir / "wiki"
    if kind == "file":
        wiki.write_text("my wiki\n")
    else:
        target = root / "tools" / "wiki" if kind == "managed-link" else tmp_path / "other-wiki"
        wiki.symlink_to(target)
    uv = bin_dir / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n")
    uv.chmod(0o755)

    subprocess.run(
        [just, "uninstall"], cwd=root,
        env={**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"]},
        capture_output=True, text=True, check=True,
    )

    assert os.path.lexists(wiki) is (kind != "managed-link")


@pytest.mark.parametrize("kind", ["file", "other-link"])
def test_install_refuses_to_replace_an_unrelated_wiki(tmp_path, kind):
    just = shutil.which("just")
    if just is None:
        pytest.skip("just is not installed")
    root = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    shutil.copyfile(root / "justfile", checkout / "justfile")
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    venv_bin = checkout / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    for executable in (venv_bin / "js", venv_bin / "python", fake_bin / "uv"):
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    wiki = bin_dir / "wiki"
    other = tmp_path / "other-wiki"
    if kind == "file":
        wiki.write_text("my wiki\n")
    else:
        wiki.symlink_to(other)

    result = subprocess.run(
        [just, "install"], cwd=checkout,
        env={**os.environ, "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"]},
        capture_output=True, text=True,
    )

    if kind == "file":
        assert not wiki.is_symlink()
        assert wiki.read_text() == "my wiki\n"
    else:
        assert wiki.readlink() == other
    assert result.returncode != 0
