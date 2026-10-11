"""Deterministic CLI repetition checks with real harmless child processes."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

from js.loop_controller import _stop_child, child_argv, parse_limit


def test_limit_and_argv_contract():
    assert parse_limit("10") == (10, None)
    assert parse_limit("45M") == (None, 2700)
    assert parse_limit("3h") == (None, 10800)
    assert parse_limit("") == (None, None)
    assert child_argv(["-C", "/tmp", "--loop", "10", "-m", "sol", "--extra", "x=y"]) == [
        "-C", "/tmp", "-m", "sol", "--extra", "x=y",
    ]
    assert child_argv(["--loop=45M", "--bench", "any-agent"]) == ["--bench", "any-agent"]
    assert child_argv(["--loop", "10", "-p", "--loop"], "10") == ["-p", "--loop"]
    assert child_argv(["-p", "--loop", "--loop", "10"], "10") == ["-p", "--loop"]
    assert child_argv(["--loop", "10", "--", "--loop", "45M"], "10") == ["--", "--loop", "45M"]
    assert child_argv(["-p", "--loop"], "") == ["-p"]


def test_count_runs_real_fresh_children(tmp_path: Path):
    output = tmp_path / "turns"
    child = ("import os,sys;"
             "f=open(sys.argv[1],'a');f.write(str(os.getpid())+' '+os.getcwd()+'\\n');f.close()")
    controller = ("import sys;from js.loop_controller import run;"
                  "sys.exit(run(['--loop','3','--extra','x=y'],'3',bench=None,prompt='task',"
                  "child_command=[sys.executable,'-c',sys.argv[1],sys.argv[2]]))")
    result = subprocess.run([sys.executable, "-c", controller, child, str(output)],
                            cwd=tmp_path, input=b"", capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    rows = output.read_text().splitlines()
    assert len(rows) == 3
    assert len({row.split()[0] for row in rows}) == 3
    assert all(row.split(" ", 1)[1] == str(tmp_path) for row in rows)

    pipe_output = tmp_path / "pipe"
    pipe_child = ("import pathlib,sys;"
                  "pathlib.Path(sys.argv[1]).write_text(sys.stdin.read()+'\\n'+str(sys.argv[2:]))")
    pipe_controller = ("import sys;from js.loop_controller import run;"
                       "sys.exit(run(['--loop','1'],'1',bench=None,prompt=None,"
                       "child_command=[sys.executable,'-c',sys.argv[1],sys.argv[2]]))")
    result = subprocess.run([sys.executable, "-c", pipe_controller, pipe_child, str(pipe_output)],
                            cwd=tmp_path, input=b"meaningful pipe", capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    assert pipe_output.read_text() == "meaningful pipe\n[]"


def test_interrupt_reaps_owned_sleep_child(tmp_path: Path):
    pid_path = tmp_path / "pid"
    child = ("import os,sys,time;"
             "open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(30)")
    controller = ("import sys;from js.loop_controller import run;"
                  "sys.exit(run(['--loop','10'],'10',bench=None,prompt='task',"
                  "child_command=[sys.executable,'-c',sys.argv[1],sys.argv[2]]))")
    parent = subprocess.Popen([sys.executable, "-c", controller, child, str(pid_path)],
                              cwd=tmp_path, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_path.exists()
        child_pid = int(pid_path.read_text())
        os.kill(parent.pid, signal.SIGINT)
        assert parent.wait(timeout=10) == 130
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError("owned child survived Ctrl-C")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()


def test_interrupt_reaps_detached_capped_child(tmp_path: Path):
    pid_path = tmp_path / "detached.pid"
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text("import os,sys,time\nopen(sys.argv[1],'w').write(str(os.getpid()))\ntime.sleep(30)\n")
    shell_body = " ".join(shlex.quote(part) for part in (sys.executable, str(sleeper), str(pid_path)))
    js_child = ("import sys;from js.promptexpand import _sub_sh;"
                "_sub_sh(sys.argv[1],timeout_s=30,max_output_bytes=1024)")
    controller = ("import sys;from js.loop_controller import run;"
                  "sys.exit(run(['--loop','10'],'10',bench=None,prompt='task',"
                  "child_command=[sys.executable,'-c',sys.argv[1],sys.argv[2]]))")
    parent = subprocess.Popen([sys.executable, "-c", controller, js_child, shell_body],
                              cwd=tmp_path, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_path.exists(), parent.stderr.read().decode() if parent.poll() is not None else ""
        detached_pid = int(pid_path.read_text())
        os.kill(parent.pid, signal.SIGINT)
        assert parent.wait(timeout=10) == 130
        try:
            os.kill(detached_pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError("detached capped child survived Ctrl-C")
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()


def test_stop_child_reaps_residual_process_group_member(tmp_path: Path):
    pid_path = tmp_path / "residual.pid"
    residual = ("import os,signal,sys,time;"
                "signal.signal(signal.SIGINT,signal.SIG_IGN);"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(30)")
    leader = ("import subprocess,sys,time;"
              "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]);"
              "time.sleep(30)")
    child = subprocess.Popen([sys.executable, "-c", leader, residual, str(pid_path)],
                             cwd=tmp_path, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_path.exists()
        residual_pid = int(pid_path.read_text())
        _stop_child(child)
        proc_state = Path(f"/proc/{residual_pid}/stat")
        deadline = time.monotonic() + 2
        while proc_state.exists() and proc_state.read_text().split(") ", 1)[1][0] != "Z" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not proc_state.exists() or proc_state.read_text().split(") ", 1)[1][0] == "Z"
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def test_interrupt_kills_detached_group_when_js_ignores_signals(tmp_path: Path):
    pid_path = tmp_path / "detached-resistant.pid"
    detached = ("import os,signal,sys,time;"
                "signal.signal(signal.SIGINT,signal.SIG_IGN);"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(30)")
    js_child = ("import signal,sys,time;from js.capped_process import start_capped;"
                "signal.signal(signal.SIGINT,signal.SIG_IGN);"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "start_capped([sys.executable,'-c',sys.argv[1],sys.argv[2]],cwd=None,cap=1024);"
                "time.sleep(30)")
    controller = ("import sys;from js.loop_controller import run;"
                  "sys.exit(run(['--loop','10'],'10',bench=None,prompt='task',"
                  "child_command=[sys.executable,'-c',sys.argv[1],sys.argv[2],sys.argv[3]]))")
    parent = subprocess.Popen([sys.executable, "-c", controller, js_child, detached, str(pid_path)],
                              cwd=tmp_path, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_path.exists(), parent.stderr.read().decode() if parent.poll() is not None else ""
        detached_pid = int(pid_path.read_text())
        os.kill(parent.pid, signal.SIGINT)
        assert parent.wait(timeout=12) == 130
        proc_state = Path(f"/proc/{detached_pid}/stat")
        deadline = time.monotonic() + 2
        while proc_state.exists() and proc_state.read_text().split(") ", 1)[1][0] != "Z" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not proc_state.exists() or proc_state.read_text().split(") ", 1)[1][0] == "Z"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()
