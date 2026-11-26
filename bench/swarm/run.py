"""Swarm bench: what js costs the machine per agent, against a fake model.

    uv run python bench/swarm/run.py swarm 100          # 100 agents in one `js.swarm run`
    uv run python bench/swarm/run.py procs 50           # 50 concurrent `js -p` processes
    uv run python bench/swarm/run.py swarm 200 --tps 0  # the model answers as fast as it can

The model is ``fake_llm.py``, a stand-alone chat-completions server in its own
process, so nothing it does is charged to js. Each agent gets the same script:
``--steps`` tool rounds (read, fs_search, shell), then a reply. On the bus each
agent also broadcasts one ``send`` and answers the wake that broadcast causes in
every other agent, so the bus, the inboxes and the sleep loop are all in the
run. When every agent has gone back to sleep the bench sends ``stop`` to all.

The numbers, all from outside js:

- wall: start to the last process exiting.
- cpu: user+sys seconds of the js processes (the fake model is not counted).
- cpu/agent: cpu divided by agents.
- startup: start to each agent's first model request (median, max).
- step gap: from a model stream ending to that agent's next request, which is
  js handling the reply, running the tool and building the next request
  (median, p95, max). Tool time is in it; the tools are cheap on purpose.
- idle cpu: CPU the swarm burns per second while every agent sleeps.
- rss: peak resident memory of the largest js process.

A JS_HOME of its own under the work directory keeps the owner's setup out of it.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
# --js-root: the js checkout the agents run, ahead of the installed one.
JS_ROOT: Path | None = None


def _pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


def _start_model(work: Path, args: argparse.Namespace) -> tuple[subprocess.Popen, int, Path]:
    log = work / "requests.jsonl"
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "fake_llm.py"), "--port", "0", "--tokens-per-second", str(args.tps),
         "--tokens-per-chunk", str(args.chunk), "--reply-tokens", str(args.reply_tokens),
         "--steps", str(args.steps), "--read-path", str(REPO / "js" / "swarm.py"), "--log", str(log)],
        stdout=subprocess.PIPE, text=True)
    port = int(proc.stdout.readline())
    return proc, port, log


def _env(work: Path, port: int) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("JS_")}
    env.update({"JS_HOME": str(work / "home"), "JS_BASE_URL": f"http://127.0.0.1:{port}/v1",
                "JS_API_KEY": "bench", "JS_MODEL": "fake", "NO_COLOR": "1"})
    if JS_ROOT is not None:
        env["PYTHONPATH"] = str(JS_ROOT)
    (work / "home").mkdir(parents=True, exist_ok=True)
    # Quiet the startup checks that are not the agent's own work.
    (work / "home" / "jsrc").write_text("set MODEL_CATALOG_AUTO_REFRESH off\n", encoding="utf-8")
    return env


def _children_cpu() -> float:
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    return ru.ru_utime + ru.ru_stime


def _stat(pid: int) -> list[str] | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except OSError:
        return None


def _proc_cpu(pid: int) -> float:
    """User+sys seconds of ``pid`` and every live process under it."""
    parents: dict[int, int] = {}
    for entry in os.listdir("/proc"):
        if entry.isdigit() and (fields := _stat(int(entry))) is not None:
            parents[int(entry)] = int(fields[1])
    tree, grew = {pid}, True
    while grew:
        grew = False
        for child, parent in parents.items():
            if parent in tree and child not in tree:
                tree.add(child)
                grew = True
    ticks = os.sysconf("SC_CLK_TCK")
    total = 0.0
    for member in tree:
        if (fields := _stat(member)) is not None:
            total += (int(fields[11]) + int(fields[12])) / ticks
    return total


def run_procs(args: argparse.Namespace, work: Path, env: dict[str, str]) -> dict:
    js = [sys.executable, "-m", "js"]
    started = time.time()
    procs = []
    for i in range(args.agents):
        cwd = work / "cwd"
        cwd.mkdir(exist_ok=True)
        procs.append(subprocess.Popen([*js, "-p", f"bench agent {i:03d} go", "--model", "fake", "-q"],
                                      cwd=cwd, env=env, stdout=subprocess.DEVNULL,
                                      stderr=open(work / f"agent{i:03d}.err", "w")))  # noqa: SIM115
    codes = [p.wait() for p in procs]
    return {"started": started, "ended": time.time(), "failed": sum(1 for c in codes if c),
            "idle_cpu_per_s": None}


def run_swarm(args: argparse.Namespace, work: Path, env: dict[str, str]) -> dict:
    root = work / "bus"
    cwd = work / "cwd"
    cwd.mkdir(exist_ok=True)
    spec = {"root": str(root), "slots": 0, **({"processes": args.processes} if args.processes else {}), "agents": [
        {"name": f"a{i:03d}", "opener": f"bench agent {i:03d} go", "model": "fake", "cwd": str(cwd),
         "session": f"bench-{i:03d}"} for i in range(args.agents)]}
    spec_file = work / "spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    started = time.time()
    cmd = [sys.executable, "-m", "js.swarm", "run", str(spec_file)]
    if args.profile:
        cmd = ["py-spy", "record", "--format", "raw", "--rate", "200", "--nonblocking",
               "-o", str(Path(args.profile).resolve()), "--", *cmd]
    proc = subprocess.Popen(cmd, cwd=cwd, env=env,
                            stdout=open(work / "events.jsonl", "w"),  # noqa: SIM115
                            stderr=open(work / "swarm.err", "w"))  # noqa: SIM115
    sys.path.insert(0, str(JS_ROOT or REPO))
    from js import swarm

    room = swarm.Room(root)
    idle = None
    # Settled: every agent joined, all asleep, inboxes empty, for a moment.
    deadline = time.time() + args.timeout
    while time.time() < deadline and proc.poll() is None:
        if len(room.members()) == args.agents and room.quiet():
            time.sleep(1.0)
            if room.quiet():
                break
        time.sleep(0.2)
    settled = time.time()
    if args.idle > 0 and proc.poll() is None:
        before = _proc_cpu(proc.pid)
        time.sleep(args.idle)
        idle = (_proc_cpu(proc.pid) - before) / args.idle
    room.send("bench", swarm.BROADCAST, "stop", swarm.STOP)
    try:
        proc.wait(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        proc.send_signal(signal.SIGINT)
        proc.wait()
    return {"started": started, "ended": settled, "failed": int(proc.returncode not in (0, None)),
            "idle_cpu_per_s": idle, "exit_at": time.time()}


def summarize(mode: str, args: argparse.Namespace, log: Path, run: dict, cpu: float, rss_kb: int) -> dict:
    rows = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    first: dict[str, float] = {}
    gaps = []
    for r in rows:
        first.setdefault(r["agent"], r["arrived"])
        if r["previous_end"] is not None:
            gaps.append(r["arrived"] - r["previous_end"])
    startups = [t - run["started"] for t in first.values()]
    return {
        "mode": mode, "agents": args.agents, "tps": args.tps, "steps": args.steps,
        "requests": len(rows), "agents_seen": len(first), "failed": run["failed"],
        "wall_s": round(run["ended"] - run["started"], 2),
        "cpu_s": round(cpu, 2), "cpu_per_agent_s": round(cpu / max(1, args.agents), 3),
        "startup_median_s": round(statistics.median(startups), 2) if startups else None,
        "startup_max_s": round(max(startups), 2) if startups else None,
        "gap_median_ms": round(1000 * statistics.median(gaps), 1) if gaps else None,
        "gap_p95_ms": round(1000 * _pct(gaps, 0.95), 1) if gaps else None,
        "gap_max_ms": round(1000 * max(gaps), 1) if gaps else None,
        "idle_cpu_per_s": None if run["idle_cpu_per_s"] is None else round(run["idle_cpu_per_s"], 3),
        "rss_mb": round(rss_kb / 1024, 1),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("mode", choices=("swarm", "procs"))
    ap.add_argument("agents", type=int)
    ap.add_argument("--tps", type=float, default=80.0, help="fake model tokens per second per stream; 0 = flat out")
    ap.add_argument("--chunk", type=int, default=1, help="tokens per SSE chunk")
    ap.add_argument("--reply-tokens", type=int, default=200)
    ap.add_argument("--steps", type=int, default=3)
    ap.add_argument("--idle", type=float, default=5.0, help="seconds to measure the sleeping swarm's CPU")
    ap.add_argument("--timeout", type=float, default=600.0)
    ap.add_argument("--work", default="", help="keep the run's files here")
    ap.add_argument("--processes", type=int, default=0, help="swarm mode: the spec's processes; 0 leaves SWARM_PROCESSES to decide")
    ap.add_argument("--js-root", default="", help="run the js in this checkout (say, a worktree of main)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--profile", default="", help="swarm mode: record the run with py-spy to this file (raw stacks)")
    args = ap.parse_args(argv)
    global JS_ROOT
    JS_ROOT = Path(args.js_root).resolve() if args.js_root else None
    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="js-swarmbench-"))
    work.mkdir(parents=True, exist_ok=True)
    model, port, log = _start_model(work, args)
    try:
        env = _env(work, port)
        before = _children_cpu()
        run = (run_swarm if args.mode == "swarm" else run_procs)(args, work, env)
        cpu = _children_cpu() - before
        rss = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    finally:
        model.terminate()
        model.wait()
    result = summarize(args.mode, args, log, run, cpu, rss)
    if args.json:
        print(json.dumps(result))
    else:
        width = max(len(k) for k in result)
        for k, v in result.items():
            print(f"{k:<{width}}  {v}")
        print(f"{'work':<{width}}  {work}")
    if not args.work:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
