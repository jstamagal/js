from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cached-test.sh"


def test_changed_command_does_not_replay_another_commands_success(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=t@t",
         "commit", "--allow-empty", "-qm", "baseline"], check=True,
    )

    def run(code):
        return subprocess.run(
            ["bash", str(SCRIPT), sys.executable, "-c", code], cwd=tmp_path,
            capture_output=True, text=True,
        )

    assert run("raise SystemExit(0)").returncode == 0
    assert run("raise SystemExit(7)").returncode == 7
