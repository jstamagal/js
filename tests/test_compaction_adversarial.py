"""Fresh-process adversarial probes independent of the existing test fixtures."""

from pathlib import Path
import json
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "script,result_file", [("harness.py", "results.json"), ("wire.py", "wire-results.json")]
)
def test_adversarial_compaction(tmp_path, script, result_file):
    root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [
            sys.executable,
            str(root / "tests" / "compaction_harness" / script),
            str(root),
            str(tmp_path),
        ],
        cwd=tmp_path,
        env={**os.environ, "HOME": str(tmp_path), "JS_VISION": "false"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    results = json.loads((tmp_path / result_file).read_text())
    assert results and all(result["pass"] for result in results)
