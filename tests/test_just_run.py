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
