"""The prompt derivation consumes the relocated stock agent and selected add-ins."""
import os
from pathlib import Path
import subprocess
import sys


def test_derive_forge_uses_stock_prompt_and_selected_profile(tmp_path):
    profile = tmp_path / "profile"
    custom = profile / "custom/voice.md"
    custom.parent.mkdir(parents=True)
    custom.write_text("selected profile voice\n")
    ship, personal = tmp_path / "ship.md", tmp_path / "personal.md"
    ship.write_text("---\nname: forge\n---\nold\n")
    script = Path(__file__).resolve().parents[1] / "scripts/derive-forge.py"
    result = subprocess.run([sys.executable, str(script), "--ship", str(ship),
                             "--personal", str(personal)],
                            env={**os.environ, "JS_HOME": str(profile)},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "selected profile voice" in personal.read_text()
    assert "selected profile voice" not in ship.read_text()
