"""Repeat complete CLI invocations as fresh, isolated processes."""
from __future__ import annotations

import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time


_DURATION = re.compile(r"([1-9][0-9]*)([mMhH])\Z")
_DEFAULT_PROMPT = "Carry out your assigned task for this fresh session."


def parse_limit(value: str) -> tuple[int | None, float | None]:
    if value == "":
        return None, None
    if value.isdecimal() and int(value) > 0:
        return int(value), None
    match = _DURATION.fullmatch(value)
    if match:
        return None, int(match[1]) * (60 if match[2].lower() == "m" else 3600)
    raise ValueError("--loop expects a positive turn count, minutes (M), or hours (H)")


def child_argv(argv: list[str], limit: str | None = None) -> list[str]:
    """Remove the parsed loop option, preserving prompt text and literal args."""
    end = argv.index("--") if "--" in argv else len(argv)
    candidates = []
    for index, arg in enumerate(argv[:end]):
        prompt_value = bool(index and argv[index - 1] in {"-p", "--prompt"})
        if arg == "--loop":
            value = (argv[index + 1] if index + 1 < end and
                     (argv[index + 1].isdecimal() or _DURATION.fullmatch(argv[index + 1])) else "")
            if limit is None or value == limit:
                candidates.append((index, bool(value), prompt_value))
        elif arg.startswith("--loop=") and (limit is None or arg[7:] == limit):
            candidates.append((index, False, prompt_value))
    if not candidates:
        return list(argv)
    index, has_value, _ = next((item for item in reversed(candidates) if not item[2]), candidates[-1])
    return argv[:index] + argv[index + 1 + has_value:]


def _detached_descendant_groups(root_pid: int) -> set[int]:
    """Process groups currently below our child, including new sessions."""
    children: dict[int, list[tuple[int, int]]] = {}
    try:
        entries = os.scandir("/proc")
    except OSError:
        return set()
    with entries:
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            try:
                stat = Path(entry.path, "stat").read_text()
                fields = stat[stat.rfind(")") + 2:].split()
                parent, group = int(fields[1]), int(fields[2])
            except (OSError, ValueError, IndexError):
                continue
            children.setdefault(parent, []).append((int(entry.name), group))
    groups: set[int] = set()
    pending = [root_pid]
    while pending:
        for pid, group in children.get(pending.pop(), ()):
            pending.append(pid)
            if group > 0 and group not in {root_pid, os.getpgrp()}:
                groups.add(group)
    return groups


def _stop_child(child: subprocess.Popen) -> None:
    group = child.pid
    detached = _detached_descendant_groups(group)
    for sig, grace in ((signal.SIGINT, 3), (signal.SIGTERM, 2)):
        detached.update(_detached_descendant_groups(group))
        for owned_group in (group, *detached):
            try:
                os.killpg(owned_group, sig)
            except ProcessLookupError:
                pass
        try:
            child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        else:
            if sig == signal.SIGTERM:
                time.sleep(0.2)
    detached.update(_detached_descendant_groups(group))
    for owned_group in (group, *detached):
        try:
            os.killpg(owned_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    child.wait()


def run(argv: list[str], limit: str, *, bench: str | None, prompt: str | None,
        child_command: list[str] | None = None) -> int:
    count, seconds = parse_limit(limit)
    command = list(child_command or [sys.executable, "-m", "js"])
    args = child_argv(argv, limit)
    stdin_data = sys.stdin.buffer.read() if not sys.stdin.isatty() else None
    if (prompt is None and bench is None and not stdin_data
            and not any(a in args for a in ("--commit", "--printonly"))):
        args += ["-p", _DEFAULT_PROMPT]
    deadline = time.monotonic() + seconds if seconds is not None else None
    completed = failures = attempts = 0
    max_attempts = count * 3 if count is not None else None
    try:
        while (count is None or completed < count) and (deadline is None or time.monotonic() < deadline):
            if max_attempts is not None and attempts >= max_attempts:
                print(f"STOP: {completed} completed turns after {attempts} attempts.", file=sys.stderr)
                return 1
            attempts += 1
            print(f"\n=== js loop attempt {attempts}, completed {completed}/{count or 'unlimited'} ===", flush=True)
            child = subprocess.Popen(command + args, start_new_session=True,
                                     stdin=subprocess.PIPE if stdin_data is not None else None)
            try:
                child.communicate(input=stdin_data)
                code = child.returncode
            except BaseException:
                _stop_child(child)
                raise
            if code == 0:
                completed += 1
                failures = 0
                continue
            if code == 130:
                return 130
            failures += 1
            if count is not None or failures >= 5:
                print(f"STOP: child exited {code}; {completed} turns completed.", file=sys.stderr)
                return code or 1
            delay = min(2 ** (failures - 1), 8)
            print(f"Child exited {code}; retrying in {delay}s ({failures}/5 consecutive failures).", file=sys.stderr)
            if deadline is not None and time.monotonic() + delay >= deadline:
                return code or 1
            time.sleep(delay)
    except KeyboardInterrupt:
        print("Stopped by operator; owned child has exited.", file=sys.stderr)
        return 130
    return 0
