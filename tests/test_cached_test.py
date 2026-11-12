from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.fixture
def cached_test_repo(tmp_path):
    just = shutil.which("just")
    if just is None:
        pytest.skip("just is not installed")
    root = Path(__file__).resolve().parents[1]
    shutil.copy(root / "justfile", tmp_path / "justfile")
    (tmp_path / "scripts").mkdir()
    shutil.copy(root / "scripts/cached-test.sh", tmp_path / "scripts/cached-test.sh")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-c", "user.name=John Stamagal", "-c", "user.email=jstamagal@gmail.com",
         "commit", "--allow-empty", "-qm", "Initialize test fixture"],
        cwd=tmp_path, check=True,
    )
    runtime = tmp_path / ".git" / "runtime"
    runtime.mkdir(mode=0o700)
    uv = tmp_path / ".git" / "uv"
    uv.write_text(
        "#!/usr/bin/env python3\nimport json, pathlib, sys\n"
        "with pathlib.Path('.git/calls').open('a') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    uv.chmod(0o755)

    def run(*args, exit_code=0):
        result = subprocess.run(
            [just, "test", *args], cwd=tmp_path,
            env={**os.environ, "PATH": str(uv.parent) + os.pathsep + os.environ["PATH"],
                 "XDG_RUNTIME_DIR": str(runtime)},
            capture_output=True, text=True,
        )
        assert result.returncode == exit_code, result.stdout + result.stderr
        return [json.loads(line) for line in (tmp_path / ".git/calls").read_text().splitlines()]

    return run


@pytest.mark.parametrize("args", [
    ("tests/test_picker.py",),
    ("-k", "foo or bar"),
    ("-x",),
    ("tests/a file.py", "-k", "literal $HOME; value"),
])
def test_test_recipe_forwards_pytest_arguments(cached_test_repo, args):
    calls = cached_test_repo(*args)
    assert len(calls) == 1
    assert calls[0][:4] == ["run", "--extra", "test", "pytest"]
    assert calls[0][-len(args):] == list(args)


def test_test_recipe_cache_and_force(cached_test_repo):
    assert len(cached_test_repo("-k", "first")) == 1
    assert len(cached_test_repo("-k", "first")) == 1
    assert len(cached_test_repo("-k", "second")) == 2
    assert len(cached_test_repo("--force", "-k", "first")) == 3
    calls = cached_test_repo("-k", "first", "--force")
    assert len(calls) == 4
    assert all("--force" not in call for call in calls)
    assert len(cached_test_repo("-k", "first")) == 4
