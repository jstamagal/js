"""Harness bench: what each thing js does regularly costs, with no model in it.

    uv run python bench/harness/run.py                       # every scenario
    uv run python bench/harness/run.py tools turn            # some of them
    uv run python bench/harness/run.py --js-root ../js-main  # another checkout's js
    uv run python bench/harness/run.py --compare ../js-main  # that checkout beside this one
    uv run python bench/harness/run.py turn --profile turn.txt   # py-spy stacks of one scenario

Scenarios (``scenarios.py`` has the in-process ones):

- startup: a cold process to done, for `import js.cli`, `js --list-agents`
  and a `js -p` that takes one short reply; wall and CPU, the median of 5.
- config: reading the layered settings, and settling a run.
- tools: each local tool, the handler alone and through the turn's dispatcher.
- session: saving 300 tool rounds, replaying the file, listing sessions.
- budget: what a model call computes before sending, on a 600-message history.
- turn: a 40-round turn with a 2000-token reply against the fake model.
- idle: CPU a second of a REPL sitting at its prompt.
- repl: the same 40-round turn typed into a REPL on a pty, drawn on the screen.

The fake model (``bench/swarm/fake_llm.py``) runs in its own process, flat
out, so the time measured is js's. JS_HOME is a directory of the run's own.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
IN_PROCESS = ("config", "tools", "session", "budget", "turn")
ALL = ("startup", *IN_PROCESS, "idle", "repl")


def _env(work: Path, port: int, js_root: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("JS_")}
    home = work / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "jsrc").write_text("set MODEL_CATALOG_AUTO_REFRESH off\n", encoding="utf-8")
    env.update({"JS_HOME": str(home), "JS_BASE_URL": f"http://127.0.0.1:{port}/v1", "JS_API_KEY": "bench",
                "JS_MODEL": "fake", "NO_COLOR": "1", "PYTHONPATH": str(js_root),
                "JS_BENCH_WORK": str(work / "work")})
    return env


def _model(work: Path, steps: int, reply: int) -> tuple[subprocess.Popen, int]:
    proc = subprocess.Popen(
        [sys.executable, str(REPO / "bench" / "swarm" / "fake_llm.py"), "--port", "0", "--tokens-per-second", "0",
         "--reply-tokens", str(reply), "--steps", str(steps), "--read-path", str(REPO / "js" / "swarm.py")],
        stdout=subprocess.PIPE, text=True)
    return proc, int(proc.stdout.readline())


def _cold(argv: list[str], env: dict[str, str], cwd: Path, runs: int = 5) -> tuple[float, float]:
    """Median wall and CPU milliseconds of ``argv`` run to its end."""
    walls, cpus = [], []
    for _ in range(runs):
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        start = time.perf_counter()
        subprocess.run(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=False)
        walls.append(time.perf_counter() - start)
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpus.append(after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime)
    return statistics.median(walls) * 1000, statistics.median(cpus) * 1000


def startup(work: Path, js_root: Path) -> dict:
    model, port = _model(work, steps=0, reply=5)
    try:
        env = _env(work, port, js_root)
        cwd = work / "work"
        cwd.mkdir(parents=True, exist_ok=True)
        py = sys.executable
        out = {}
        for label, argv in {
            "python_alone": [py, "-c", "pass"],
            "import_js_cli": [py, "-c", "import js.cli"],
            "list_agents": [py, "-m", "js", "--list-agents"],
            "prompt_one_reply": [py, "-m", "js", "-p", "hi", "--model", "fake", "-n"],
        }.items():
            wall, cpu = _cold(argv, env, cwd)
            out[f"{label}_wall"], out[f"{label}_cpu"] = round(wall, 1), round(cpu, 1)
        return out
    finally:
        model.terminate()
        model.wait()


def _repl(work: Path, js_root: Path, port: int):
    """An interactive js on a pty: the process, a drain of what it draws, and
    its CPU seconds so far."""
    import pty

    env = {**_env(work, port, js_root), "TERM": "xterm-256color"}
    env.pop("NO_COLOR", None)
    cwd = work / "work"
    cwd.mkdir(parents=True, exist_ok=True)
    master, slave = pty.openpty()
    proc = subprocess.Popen([sys.executable, "-m", "js", "--model", "fake", "-n"], env=env, cwd=cwd,
                            stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    os.close(slave)
    os.set_blocking(master, False)

    def drain() -> int:
        drawn = 0
        try:
            while chunk := os.read(master, 65536):
                drawn += len(chunk)
        except OSError:
            pass
        return drawn

    def cpu() -> float:
        fields = Path(f"/proc/{proc.pid}/stat").read_text().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")

    deadline = time.time() + 6
    while time.time() < deadline:
        drain()
        time.sleep(0.2)
    return proc, master, drain, cpu


def idle(work: Path, js_root: Path, seconds: float = 8.0) -> dict:
    """CPU a second of an interactive js at its prompt, on a pty."""
    model, port = _model(work, steps=0, reply=5)
    try:
        proc, master, drain, cpu = _repl(work, js_root, port)
        if proc.poll() is not None:
            return {"idle_error": f"js exited {proc.returncode} before it idled"}
        before = cpu()
        end = time.time() + seconds
        while time.time() < end:
            drain()
            time.sleep(0.2)
        used = cpu() - before
        proc.kill()
        proc.wait()
        os.close(master)
        return {"repl_idle_cpu_ms_per_s": round(used / seconds * 1000, 2)}
    finally:
        model.terminate()
        model.wait()


def repl(work: Path, js_root: Path) -> dict:
    """One turn typed into an interactive js on a pty: 40 tool rounds and a
    2000-token reply, drawn on the screen. CPU and wall from the Enter to the
    process falling idle again."""
    model, port = _model(work, steps=40, reply=2000)
    try:
        proc, master, drain, cpu = _repl(work, js_root, port)
        if proc.poll() is not None:
            return {"repl_error": f"js exited {proc.returncode} before its prompt"}
        before, start = cpu(), time.time()
        os.write(master, b"bench turn go\r")
        drawn, last, quiet_since, busy_until = 0, before, time.time(), start
        while time.time() - start < 120 and time.time() - quiet_since < 2.0:
            time.sleep(0.1)
            drawn += drain()
            now = cpu()
            if now - last > 0.02:
                quiet_since = busy_until = time.time()
            last = now
        used = cpu() - before
        proc.kill()
        proc.wait()
        os.close(master)
        return {"repl_turn_cpu_s": round(used, 3), "repl_turn_wall_s": round(busy_until - start, 2),
                "repl_turn_drawn_kb": round(drawn / 1024, 1)}
    finally:
        model.terminate()
        model.wait()


def in_process(name: str, work: Path, js_root: Path, profile: str = "") -> dict:
    model, port = _model(work, steps=40, reply=2000)
    try:
        argv = [sys.executable, str(HERE / "scenarios.py"), name]
        if profile:
            argv = ["py-spy", "record", "--format", "raw", "--rate", "500", "-o", profile, "--", *argv]
        done = subprocess.run(argv, env=_env(work, port, js_root), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              check=False)
        lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
        if not lines:
            return {f"{name}_error": (done.stderr or done.stdout).strip().splitlines()[-1][:200] if (
                done.stderr or done.stdout).strip() else f"exit {done.returncode}"}
        return json.loads(lines[-1])
    finally:
        model.terminate()
        model.wait()


def run(names: list[str], js_root: Path, profile: str = "") -> dict[str, dict]:
    results = {}
    for name in names:
        work = Path(tempfile.mkdtemp(prefix=f"js-harnessbench-{name}-"))
        try:
            if name == "startup":
                results[name] = startup(work, js_root)
            elif name == "idle":
                results[name] = idle(work, js_root)
            elif name == "repl":
                results[name] = repl(work, js_root)
            else:
                results[name] = in_process(name, work, js_root, profile)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return results


def show(results: dict[str, dict], other: dict[str, dict] | None = None) -> None:
    for name, metrics in results.items():
        print(f"\n{name}")
        width = max((len(k) for k in metrics), default=0)
        for key, value in metrics.items():
            line = f"  {key:<{width}}  {value!s:>12}"
            before = (other or {}).get(name, {}).get(key)
            if isinstance(before, (int, float)) and isinstance(value, (int, float)) and before:
                line = f"  {key:<{width}}  {before!s:>12}  ->  {value!s:>12}  {value / before:5.2f}x"
            print(line)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("scenarios", nargs="*", help=f"any of: {' '.join(ALL)} (default: all)")
    ap.add_argument("--js-root", default="", help="run the js in this checkout")
    ap.add_argument("--compare", default="", help="also run this checkout's js, and print it beside this one's")
    ap.add_argument("--profile", default="", help="record one in-process scenario with py-spy to this file")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    names = args.scenarios or list(ALL)
    unknown = [n for n in names if n not in ALL]
    if unknown:
        ap.error(f"no such scenario: {' '.join(unknown)}")
    root = Path(args.js_root).resolve() if args.js_root else REPO
    results = run(names, root, str(Path(args.profile).resolve()) if args.profile else "")
    other = run(names, Path(args.compare).resolve()) if args.compare else None
    if args.json:
        print(json.dumps({"this": results, **({"compare": other} if other is not None else {})}))
    else:
        show(results, other)
    return 0


if __name__ == "__main__":
    sys.exit(main())
