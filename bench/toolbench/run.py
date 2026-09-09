"""Tool bench runner: race agents on bug-fix tasks mined from real repositories.

RepoRacer does the mining, worktrees, hidden tests, scoring, and HTML reports.
This runner clones the suite's repos, writes each one's `.reporacer/config.json`
from `suite.toml` (miner, agents, sandboxed install/test commands), runs
`reporacer run` per repo, then joins RepoRacer's per-task results with the
TOOLSTATS line each js agent prints, and writes a summary.

    python bench/toolbench/run.py                       # default agents, every repo
    python bench/toolbench/run.py --mine                # list tasks, no model
    python bench/toolbench/run.py --agents a,b --repos click --tasks 2
    python bench/toolbench/run.py --report bench/toolbench/results/<stamp>
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tomllib
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
SANDBOX = HERE / "sandbox.sh"


def load_suite(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def resolve_base_url(url: str) -> str:
    """Containers cannot resolve Tailscale or /etc/hosts names; ship the IP."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    try:
        socket.inet_aton(host)
        return url
    except OSError:
        pass
    # Only private/LAN names need the IP swap. A public https host must keep its
    # hostname or TLS fails on the bare IP (SNI/cert mismatch), which is how
    # https://api.deepseek.com became https://<cloudfront-ip> and every task died
    # with "connection failed".
    if parts.scheme == "https" and "." in host and not host.endswith(".local"):
        try:
            ip = socket.gethostbyname(host)
            if not ip.startswith(("10.", "192.168.", "127.", "100.")):
                return url
        except OSError:
            return url
    try:
        ip = socket.gethostbyname(host)
    except OSError as exc:
        raise SystemExit(f"toolbench: cannot resolve model host {host!r}: {exc}") from exc
    netloc = f"{ip}:{parts.port}" if parts.port else ip
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def model_env(suite: dict) -> dict[str, str]:
    model = suite.get("model", {})
    base_url = os.environ.get("TOOLBENCH_BASE_URL") or model.get("base_url", "")
    env = {
        "JS_PROVIDER": "openai",
        "JS_BASE_URL": resolve_base_url(base_url) if base_url else "",
        "JS_API_KEY": os.environ.get("TOOLBENCH_API_KEY") or model.get("api_key", "x"),
        "JS_MODEL": os.environ.get("TOOLBENCH_MODEL") or model.get("model", ""),
        # js reads JS_REASONING (settings.py model.reasoning_effort); the old
        # JS_MODEL_REASONING_EFFORT name was never read by anything.
        "JS_REASONING": os.environ.get("TOOLBENCH_REASONING_EFFORT") or model.get("reasoning_effort", ""),
    }
    return {k: v for k, v in env.items() if v}


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def sandboxed(command: str) -> str:
    return f"{SANDBOX} -- sh -lc {shell_quote(command)}"


def agent_entry(agent: dict, telemetry_dir: Path) -> dict:
    name = agent["name"]
    kind = agent.get("kind", "command")
    if kind == "builtin":
        return {"name": name, "enabled": True}
    if kind == "js":
        command = (
            f"{SANDBOX} --prompt {{{{promptFile}}}} --telemetry {telemetry_dir / name} -- "
            f"env JS_TOOL_DESCRIPTIONS={agent['descriptions']} js-bench-agent {agent['agent']} /prompt.md"
        )
        return {"name": name, "command": command, "enabled": True}
    if kind == "command":
        return {"name": name, "command": agent["command"], "enabled": True}
    raise SystemExit(f"toolbench: agent {name!r} has unknown kind {kind!r}")


def write_config(repo_dir: Path, suite: dict, repo: dict, agents: list[dict], tasks: int, telemetry_dir: Path) -> None:
    defaults = suite["defaults"]
    miner = suite["miner"]
    config_path = repo_dir / ".reporacer" / "config.json"
    if not config_path.exists():
        subprocess.run(["reporacer", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.update(
        {
            "testCommand": sandboxed(repo["test"]),
            "installCommand": sandboxed(repo["install"]),
            "maxTasks": tasks,
            "timeoutMinutesPerAgent": int(defaults["timeout_minutes"]),
            "parallelAgents": 1,
            "parallelTasks": 1,
            "baselineCheck": bool(defaults.get("baseline_check", True)),
            "evaluationMode": "hidden-target-tests",
            "keepWorktrees": False,
            "commitSelection": {
                "lookback": int(repo.get("lookback", miner["lookback"])),
                "minChangedFiles": int(miner["min_changed_files"]),
                "maxChangedFiles": int(miner["max_changed_files"]),
                "maxChangedLines": int(miner["max_changed_lines"]),
                "excludeMergeCommits": True,
                "excludePatterns": list(miner["exclude_patterns"]) + list(repo.get("exclude_patterns", [])),
                "preferMessages": list(miner["prefer_messages"]),
            },
            "hiddenTests": {"enabled": True, "includePatterns": list(miner["hidden_test_patterns"])},
            # the agent, install, and tests already run in our sandbox
            "sandbox": {
                "mode": "none",
                "dockerImage": defaults["image"],
                "network": "default",
                "cpus": defaults["cpus"],
                "memory": defaults["memory"],
            },
            "agents": [agent_entry(agent, telemetry_dir) for agent in agents],
        }
    )
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def ensure_repo(repo: dict, work_dir: Path, lookback: int) -> Path:
    repo_dir = work_dir / "repos" / repo["name"]
    if not (repo_dir / ".git").is_dir():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        print(f"toolbench: cloning {repo['url']} -> {repo_dir}", flush=True)
        subprocess.run(["git", "clone", "-q", "--depth", str(lookback + 50), repo["url"], str(repo_dir)], check=True)
        if repo.get("ref"):
            subprocess.run(["git", "checkout", "-q", repo["ref"]], cwd=repo_dir, check=True)
    return repo_dir


def selected(items: list[dict], names: str | None, default_only: bool) -> list[dict]:
    if names:
        wanted = [n.strip() for n in names.split(",") if n.strip()]
        by_name = {item["name"]: item for item in items}
        missing = [n for n in wanted if n not in by_name]
        if missing:
            raise SystemExit(f"toolbench: unknown name(s) {missing}; known: {sorted(by_name)}")
        return [by_name[n] for n in wanted]
    return [item for item in items if not default_only or item.get("default", True)]


def mine(repo_dir: Path, tasks: int) -> str:
    result = subprocess.run(["reporacer", "tasks", "-n", str(tasks)], cwd=repo_dir, capture_output=True, text=True)
    return result.stdout + result.stderr


def run_repo(repo_dir: Path, agents: list[dict], tasks: int, env: dict[str, str]) -> Path | None:
    names = ",".join(agent["name"] for agent in agents)
    cmd = ["reporacer", "run", "--agents", names, "--tasks", str(tasks), "--evaluation-mode", "hidden-target-tests", "--ci"]
    print(f"toolbench: {repo_dir.name}: {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, cwd=repo_dir, env=env, text=True)
    if proc.returncode not in (0, 1):
        print(f"toolbench: reporacer exited {proc.returncode} in {repo_dir}", file=sys.stderr)
    last_run = repo_dir / ".reporacer" / "last-run.txt"
    if not last_run.exists():
        return None
    run_id = last_run.read_text(encoding="utf-8").splitlines()[0].strip()
    return repo_dir / ".reporacer" / "runs" / run_id


def find_toolstats(value) -> dict | None:
    """The TOOLSTATS line sits in the agent's captured output somewhere inside
    RepoRacer's result record; walk every string in the record to find it."""
    if isinstance(value, str):
        for line in value.splitlines():
            line = line.strip()
            if line.startswith("TOOLSTATS "):
                try:
                    return json.loads(line[len("TOOLSTATS "):])
                except json.JSONDecodeError:
                    return None
        return None
    if isinstance(value, dict):
        for item in value.values():
            found = find_toolstats(item)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_toolstats(item)
            if found is not None:
                return found
    return None


def collect(run_dir: Path, repo_name: str, out_dir: Path) -> list[dict]:
    dest = out_dir / "reporacer" / repo_name
    dest.mkdir(parents=True, exist_ok=True)
    if run_dir != dest:
        for name in ("results.jsonl", "tasks.jsonl", "summary.json", "run-config.snapshot.json"):
            src = run_dir / name
            if src.exists():
                shutil.copy2(src, dest / name)
        for html in run_dir.glob("*.html"):
            shutil.copy2(html, dest / html.name)
    rows = []
    results = dest / "results.jsonl"
    if not results.exists():
        return rows
    for line in results.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        scores = record.get("scores") or {}
        tests = record.get("tests") or {}
        hidden = record.get("hiddenTests") or {}
        rows.append(
            {
                "repo": repo_name,
                "task": record.get("taskId"),
                "agent": record.get("agentName"),
                "status": record.get("status"),
                "solved": bool(scores.get("solved")),
                "score": scores.get("final"),
                "tests_passed": tests.get("passed"),
                "hidden_passed": None if hidden.get("skipped") else hidden.get("passed"),
                "duration_s": round((record.get("durationMs") or 0) / 1000, 1),
                "toolstats": find_toolstats(record),
            }
        )
    return rows


def _fmt(value, digits: int = 1) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def summarize(rows: list[dict]) -> str:
    by_agent: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_agent[row["agent"]].append(row)

    lines = ["# Tool bench summary", ""]
    lines.append(
        "| agent | tasks | solved | hidden tests pass | avg s | tool calls/task | errors/task "
        "| read via shell | search via shell | edit via shell | cd | result KB/task |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for agent, agent_rows in sorted(by_agent.items()):
        n = len(agent_rows)
        solved = sum(1 for r in agent_rows if r["solved"])
        hidden = sum(1 for r in agent_rows if r["hidden_passed"])
        avg_s = sum(r["duration_s"] for r in agent_rows) / n if n else 0.0
        stats = [r["toolstats"] for r in agent_rows if r["toolstats"]]
        habits: Counter = Counter()
        for s in stats:
            habits.update(s.get("shell_habits") or {})
        m = len(stats) or 1
        calls = sum(s.get("tool_calls", 0) for s in stats) / m if stats else None
        errors = sum(s.get("tool_errors", 0) for s in stats) / m if stats else None
        result_kb = sum(s.get("result_bytes", 0) for s in stats) / m / 1024 if stats else None
        habit = (lambda key: str(habits[key])) if stats else (lambda key: "-")
        lines.append(
            f"| {agent} | {n} | {solved} | {hidden} | {_fmt(avg_s, 0)} | {_fmt(calls)} | {_fmt(errors)} | "
            f"{habit('read_via_shell')} | {habit('search_via_shell')} | {habit('edit_via_shell')} | {habit('cd')} | {_fmt(result_kb)} |"
        )
    lines += [
        "",
        "## Per task",
        "",
        "| repo | task | agent | status | solved | hidden | s | calls | calls by tool |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in sorted(rows, key=lambda r: (r["repo"], r["task"] or "", r["agent"] or "")):
        stats = row["toolstats"] or {}
        by_tool = ", ".join(f"{k} {v}" for k, v in (stats.get("calls_by_tool") or {}).items())
        hidden = "-" if row["hidden_passed"] is None else ("pass" if row["hidden_passed"] else "fail")
        lines.append(
            f"| {row['repo']} | {row['task']} | {row['agent']} | {row['status']} | {'yes' if row['solved'] else 'no'} | "
            f"{hidden} | {_fmt(row['duration_s'], 0)} | {stats.get('tool_calls', '-')} | {by_tool} |"
        )
    return "\n".join(lines) + "\n"


def write_summary(rows: list[dict], out_dir: Path) -> None:
    (out_dir / "summary.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    text = summarize(rows)
    (out_dir / "summary.md").write_text(text, encoding="utf-8")
    print(text)


def report(out_dir: Path) -> int:
    rows: list[dict] = []
    for results in sorted((out_dir / "reporacer").glob("*/results.jsonl")):
        rows.extend(collect(results.parent, results.parent.name, out_dir))
    if not rows:
        print(f"toolbench: no results under {out_dir}", file=sys.stderr)
        return 1
    write_summary(rows, out_dir)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="toolbench", description=__doc__.split("\n\n")[0])
    parser.add_argument("--suite", type=Path, default=HERE / "suite.toml")
    parser.add_argument("--repos", help="comma-separated suite repo names (default: all)")
    parser.add_argument("--agents", help="comma-separated agent names (default: agents marked default)")
    parser.add_argument("--tasks", type=int, help="tasks per repo (default from suite)")
    parser.add_argument("--work", type=Path, help="clone/worktree dir (default from suite)")
    parser.add_argument("--mine", action="store_true", help="only mine and print tasks")
    parser.add_argument("--report", type=Path, help="rebuild the summary of a finished results dir")
    parser.add_argument("--no-hide-files", action="store_true", help="leave RepoRacer's touched-file list in the prompt")
    args = parser.parse_args(argv)

    if args.report:
        return report(args.report.resolve())

    suite = load_suite(args.suite)
    defaults = suite["defaults"]
    work_dir = (args.work or Path(defaults["work_dir"])).expanduser()
    tasks = args.tasks or int(defaults["tasks"])
    repos = selected(suite["repos"], args.repos, default_only=False)
    agents = selected(suite["agents"], args.agents, default_only=True)
    if not agents:
        raise SystemExit("toolbench: no agents selected")
    if shutil.which("reporacer") is None:
        raise SystemExit("toolbench: reporacer not on PATH (npm install -g reporacer)")

    if args.mine:
        for repo in repos:
            repo_dir = ensure_repo(repo, work_dir, int(repo.get("lookback", suite["miner"]["lookback"])))
            write_config(repo_dir, suite, repo, agents, tasks, work_dir / "telemetry")
            print(f"\n==== {repo['name']}  ({repo['category']})")
            print(mine(repo_dir, tasks), end="")
        return 0

    if any(agent.get("kind") == "js" for agent in agents):
        if subprocess.run(["docker", "image", "inspect", defaults["image"]], capture_output=True).returncode != 0:
            raise SystemExit(f"toolbench: image {defaults['image']} not built; run `just toolbench-image`")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = (REPO_ROOT / defaults["results_dir"] / stamp).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    telemetry_dir = out_dir / "telemetry"

    env = dict(os.environ)
    env.update(model_env(suite))
    env.update(
        {
            "TOOLBENCH_IMAGE": defaults["image"],
            "TOOLBENCH_CPUS": str(defaults["cpus"]),
            "TOOLBENCH_MEMORY": str(defaults["memory"]),
            "TOOLBENCH_HIDE_FILES": "0" if args.no_hide_files or not defaults.get("hide_files", True) else "1",
        }
    )
    (out_dir / "run.json").write_text(
        json.dumps(
            {
                "stamp": stamp,
                "agents": [a["name"] for a in agents],
                "repos": [r["name"] for r in repos],
                "tasks_per_repo": tasks,
                "model": env.get("JS_MODEL"),
                "base_url": env.get("JS_BASE_URL"),
                "image": defaults["image"],
                "hide_files": env["TOOLBENCH_HIDE_FILES"] == "1",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"toolbench: results -> {out_dir}", flush=True)

    rows: list[dict] = []
    for repo in repos:
        repo_dir = ensure_repo(repo, work_dir, int(repo.get("lookback", suite["miner"]["lookback"])))
        write_config(repo_dir, suite, repo, agents, tasks, telemetry_dir / repo["name"])
        run_dir = run_repo(repo_dir, agents, tasks, env)
        if run_dir is None:
            print(f"toolbench: {repo['name']}: no run recorded", file=sys.stderr)
            continue
        rows.extend(collect(run_dir, repo["name"], out_dir))
        write_summary(rows, out_dir)
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
