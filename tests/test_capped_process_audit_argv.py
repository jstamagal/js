import ast
import shlex
from types import SimpleNamespace

import pytest

from js import capped_process, jail, settings


_WRAPPER = ["/usr/bin/bwrap", "--die-with-parent", "--unshare-ipc", "--unshare-uts",
            "--unshare-cgroup-try", "--ro-bind", "/", "/", "--"]


@pytest.mark.parametrize(("args", "expected"), [
    (["git", "diff", "--", "f"], ["git", "diff", "--", "f"]),
    (["rg", "--", "pat"], ["rg", "--", "pat"]),
    (["echo", "ordinary"], ["echo", "ordinary"]),
    (_WRAPPER + ["git", "diff", "--", "a file"], ["git", "diff", "--", "a file"]),
    (jail._IGNORE_SIGINT + _WRAPPER + ["rg", "--", "pat"], ["rg", "--", "pat"]),
    (["bwrap", "--not-our-wrapper", "--", "value"], ["bwrap", "--not-our-wrapper", "--", "value"]),
    (["echo", "bwrap", "--", "value"], ["echo", "bwrap", "--", "value"]),
    ("echo -- plain", ["echo -- plain"]),
])
def test_kill_audit_records_command_not_ordinary_separator(tmp_path, monkeypatch, args, expected):
    logfile = tmp_path / "kill.log"
    monkeypatch.setattr(settings, "live_value", lambda key: str(logfile))
    process = SimpleNamespace(args=args, pid=123)
    capped_process._log_kill(process, [123, 124])
    line = logfile.read_text()
    recorded = ast.literal_eval(line.split(" cmd=", 1)[1].split(" via ", 1)[0])
    assert recorded == shlex.join(expected)
