"""`needs_bwrap`: skip a test when bubblewrap cannot start a jail here, as
inside a js -C jail, where a nested bwrap cannot set up its uid map."""

import functools
import shutil
import subprocess

import pytest


@functools.cache
def bwrap_starts() -> bool:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return False
    try:
        probe = subprocess.run([bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--unshare-all", "true"],
                               capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


needs_bwrap = pytest.mark.skipif(not bwrap_starts(), reason="bubblewrap cannot start a jail here")
