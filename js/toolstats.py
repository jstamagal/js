"""Summarize one js session's tool traffic as a single JSON object.

    uv run python -m js.toolstats PATH.jsonl
    uv run python -m js.toolstats --latest [--data-dir DIR] [--agent NAME]

Counts what the model did with its tools: calls per tool, results that came
back as errors, assistant turns, bytes of arguments sent and results received,
and shell commands that reached for a plain Unix tool where a dedicated tool
may exist (cat/head/tail, grep/rg, find/fd, sed -i, cd, rm, redirects). Those
shell habits are reported raw: whether they were redundant depends on which
tools were on the surface, which the caller knows and this file does not.

The bench sandbox runs this after every agent run with ``--tag TOOLSTATS`` so
the one-line result lands in the agent's captured stdout, where the bench
runner joins it onto RepoRacer's pass/fail record.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

from . import paths as _paths

_SEGMENT_SPLIT = re.compile(r"\s*(?:\|\||&&|\||;)\s*")
_READ_CMDS = {"cat", "head", "tail", "less", "more", "bat"}
_SEARCH_CMDS = {"grep", "rg", "ag", "egrep", "fgrep"}
_FIND_CMDS = {"find", "fd", "fdfind", "locate"}
_REMOVE_CMDS = {"rm", "rmdir", "unlink"}
_REDIRECT = re.compile(r"(?<![<>&\d])>{1,2}\s*(?!&|/dev/)\S")  # a file redirect; not 2>&1, not >/dev/null
_HEREDOC = re.compile(r"<<-?\s*['\"]?\w")
_SED_INPLACE = re.compile(r"\bsed\b[^|;&]*\s-[a-zA-Z]*i")
_PERL_INPLACE = re.compile(r"\bperl\b[^|;&]*\s-[a-zA-Z]*i")


def classify_shell_command(command: str) -> Counter:
    """Which plain-Unix habits one shell command shows. Keys are stable names
    the bench report aggregates on."""
    habits: Counter = Counter()
    text = command.strip()
    if not text:
        return habits
    if re.search(r"(^|[;&|]\s*)cd\s", text):
        habits["cd"] += 1
    if _SED_INPLACE.search(text) or _PERL_INPLACE.search(text):
        habits["edit_via_shell"] += 1
    if _HEREDOC.search(text) or _REDIRECT.search(text):
        habits["write_via_shell"] += 1
    segments = [seg for seg in _SEGMENT_SPLIT.split(text) if seg]
    firsts = []
    for segment in segments:
        words = segment.split()
        # skip leading env assignments and sudo/env wrappers
        while words and (re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=\S*", words[0]) or words[0] in {"env", "sudo", "nohup", "time"}):
            words = words[1:]
        if words:
            firsts.append(Path(words[0]).name)
    for first in firsts:
        if first in _READ_CMDS:
            habits["read_via_shell"] += 1
        elif first in _SEARCH_CMDS:
            habits["search_via_shell"] += 1
        elif first in _FIND_CMDS:
            habits["find_via_shell"] += 1
        elif first in _REMOVE_CMDS:
            habits["rm"] += 1
    # echo/printf as the whole command and nothing written: talking through the shell
    if firsts and "write_via_shell" not in habits and all(first in {"echo", "printf"} for first in firsts):
        habits["echo_only"] += 1
    return habits


def summarize(path: Path) -> dict:
    calls: Counter = Counter()
    errors: Counter = Counter()
    habits: Counter = Counter()
    turns = 0
    args_bytes = 0
    result_bytes = 0
    first_ts = last_ts = None
    agent = model = None
    final_text = ""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = record.get("ts")
        if isinstance(ts, (int, float)):
            first_ts = ts if first_ts is None else min(first_ts, ts)
            last_ts = ts if last_ts is None else max(last_ts, ts)
        if record.get("kind") == "session_metadata":
            agent = record.get("agent", agent)
            model = record.get("model", model)
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "assistant":
            turns += 1
            if isinstance(message.get("content"), str) and message["content"].strip():
                final_text = message["content"]
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                name = str(function.get("name") or "?")
                arguments = function.get("arguments") or ""
                calls[name] += 1
                args_bytes += len(str(arguments).encode("utf-8"))
                if name == "shell":
                    try:
                        parsed = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        parsed = {}
                    habits.update(classify_shell_command(str(parsed.get("command") or "")))
        elif role == "tool":
            content = message.get("content")
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            result_bytes += len(text.encode("utf-8"))
            if text.lstrip().startswith("ERROR"):
                errors[str(message.get("name") or "?")] += 1
    return {
        "session": str(path),
        "agent": agent,
        "model": model,
        "turns": turns,
        "tool_calls": sum(calls.values()),
        "calls_by_tool": dict(sorted(calls.items())),
        "tool_errors": sum(errors.values()),
        "errors_by_tool": dict(sorted(errors.items())),
        "shell_habits": dict(sorted(habits.items())),
        "args_bytes": args_bytes,
        "result_bytes": result_bytes,
        "final_text_chars": len(final_text),
        "duration_s": round(last_ts - first_ts, 1) if first_ts is not None and last_ts is not None else None,
    }


def latest_session(data_dir: Path, agent: str | None) -> Path | None:
    root = data_dir / "sessions"
    if agent:
        root = root / agent
    candidates = list(root.rglob("*.jsonl")) if root.is_dir() else []
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="js.toolstats", description=__doc__.split("\n\n")[1])
    parser.add_argument("path", nargs="?", help="session JSONL to summarize")
    parser.add_argument("--latest", action="store_true", help="summarize the newest session instead of a path")
    parser.add_argument("--data-dir", type=Path, default=None, help="js data dir holding sessions/ (default: the platform data dir)")
    parser.add_argument("--agent", default=None, help="restrict --latest to this agent's sessions")
    parser.add_argument("--tag", default="", help="prefix the JSON line with this word, e.g. TOOLSTATS")
    parser.add_argument("--extra", action="append", default=[], metavar="K=V", help="extra key=value to include (repeatable)")
    args = parser.parse_args(argv)

    if args.latest:
        path = latest_session(args.data_dir or _paths.data_dir(), args.agent)
        if path is None:
            print("js: toolstats: no session found", file=sys.stderr)
            return 1
    elif args.path:
        path = Path(args.path)
    else:
        parser.error("give a session path or --latest")
    summary = summarize(path)
    for item in args.extra:
        key, _, value = item.partition("=")
        summary[key] = value
    line = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
    print(f"{args.tag} {line}" if args.tag else line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
