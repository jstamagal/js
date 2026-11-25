"""The bus is importable and usable with only the standard library.

A program outside js (a swarm runner's front end) imports `js.swarm` from the
checkout into its own interpreter, which has none of js's third-party
packages, to send messages and read the room, the claims and the work board."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SCRIPT = r"""
import sys
sys.path.insert(0, sys.argv[1])
from js import swarm

room = swarm.Room(sys.argv[2])
room.join("kivu")
seq = room.send("steer", "kivu", "hello", "say").seq
room.send("steer", "kivu", "", swarm.STOP)
cell = room.post_cell("steer", "a title", "a body")
yours, holder, _ = room.take_cell(cell.id, "kivu")
done = room.finish_cell(cell.id, "kivu", "a result")
stamp = room.ping()
print(seq, room.members(), room.quiet(), room.asleep("kivu"), sorted(room.claims()),
      [c.as_dict()["status"] for c in room.cells()], room.cell(cell.id).title, yours, done.status,
      room.answered("kivu", stamp), swarm.ROLLCALL_WAIT_S > 0)
"""


def test_the_bus_runs_on_the_standard_library_alone(tmp_path):
    # -S leaves site-packages off the path: only the standard library and the checkout are importable.
    result = subprocess.run([sys.executable, "-S", "-c", SCRIPT, str(REPO), str(tmp_path / "bus")],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split()[0] == "1"
    assert "'kivu'" in result.stdout and "done" in result.stdout
