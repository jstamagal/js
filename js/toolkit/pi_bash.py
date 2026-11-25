"""Pi's synchronous bash tool, using js's confined command runner."""

from __future__ import annotations

import math
import os
import secrets
import shutil
from pathlib import Path

from .. import jail, paths
from ..capped_process import start_capped
from .core import Tool, ToolContext, ToolResult, view
from .descriptions import load_description, load_parameter_description
from .presentation import Presentation
from .shell import blocking_command, shell_environment

_MAX_BYTES = 50 * 1024
_MAX_LINES = 2000
_MAX_SECONDS = 2_147_483_647 / 1000


def _output(stream) -> str:
    # Retain the cut character and final newline when locating whole lines.
    tail = stream.read(max(0, stream.total - _MAX_BYTES - 5), stream.total)
    total_lines = stream.newlines + int(bool(tail) and not tail.endswith(b"\n"))
    decoded = tail.decode("utf-8", "replace")
    if stream.total <= _MAX_BYTES and len(decoded.encode()) <= _MAX_BYTES and total_lines <= _MAX_LINES:
        return decoded
    path = stream.ensure_spilled()
    lines = decoded.removesuffix("\n").split("\n")
    kept = []
    room = _MAX_BYTES
    partial = False
    for line in reversed(lines[-_MAX_LINES:]):
        data = line.encode()
        if len(data) > room:
            if room > 0 and (not kept or (line.strip() and not any(part.strip() for part in kept))):
                kept.append(data[-room:].decode("utf-8", "ignore"))
                partial = True
            break
        kept.append(line)
        room -= len(data) + 1
    start = total_lines - len(kept) + 1
    content = "\n".join(reversed(kept))
    full = f"Full output: {path}" if path else "Could not save the full output"
    if partial:
        info = f"Showing last {len(content.encode()) / 1024:.1f}KB of line {start}"
    else:
        limit = "" if len(kept) == _MAX_LINES else " (50.0KB limit)"
        info = f"Showing lines {start}-{total_lines} of {total_lines}{limit}"
    return f"{content}\n\n[{info}. {full}]"


def bash(command: str, timeout: float | None = None, *, context: ToolContext) -> ToolResult:
    if timeout is not None:
        if not math.isfinite(timeout) or timeout <= 0:
            return ToolResult.text("Invalid timeout: must be a finite number of seconds", is_error=True)
        if timeout > _MAX_SECONDS:
            return ToolResult.text(f"Invalid timeout: maximum is {_MAX_SECONDS} seconds", is_error=True)
    executable = shutil.which("bash") or shutil.which("sh")
    if executable is None:
        return ToolResult.text("Cannot find bash or sh on PATH", is_error=True)
    _, env = shell_environment(context)
    cfg = context.config
    if cfg is not None:
        if cfg.session_file and cfg.session_file != Path(os.devnull):
            env["PI_SESSION_FILE"] = str(cfg.session_file)
            env["PI_SESSION_ID"] = cfg.session_file.stem
        for key, value in (("PI_MODEL", cfg.model), ("PI_PROVIDER", cfg.provider_id),
                           ("PI_REASONING_LEVEL", cfg.reasoning_effort)):
            if value:
                env[key] = value
    context.invalidate_search_cache()
    argv = jail.wrap([executable, "-c", command], context, cwd=context.cwd, env=env,
                     extra_ro=(Path(executable),))
    try:
        process = start_capped(
            argv, cwd=str(context.cwd), env=env, merge_stderr=True, cap=2 * (_MAX_BYTES + 5),
            spill_prefix=paths.tool_results_dir() / f"pi-bash-{secrets.token_hex(8)}",
        )
    except OSError as exc:
        return ToolResult.text(str(exc), is_error=True)
    with blocking_command(context, command, process):
        result = process.wait(timeout, pipe_idle=0.1)
        timed_out = result is None
        if timed_out:
            result = process.kill()
    text = _output(process.stream("stdout"))
    exit_code = result.returncode if result.returncode >= 0 else 128 - result.returncode
    view(command=command, running=False, exit_code=exit_code, stdout=text, stderr="")
    status = ""
    if timed_out:
        status = f"Command timed out after {timeout:g} seconds"
    else:
        text = text or "(no output)"
        if exit_code:
            status = f"Command exited with code {exit_code}"
    if status:
        text = f"{text}\n\n{status}" if text else status
    return ToolResult.text(jail.shown(text), is_error=bool(status))


def tools() -> tuple[Tool, ...]:
    params = {
        name: {"type": kind, "description": load_parameter_description("pi_bash", name)}
        for name, kind in (("command", "string"), ("timeout", "number"))
    }
    return (Tool("pi_bash", load_description("pi_bash"), bash, params, required=("command",),
                 presentation=Presentation(key=("command",), body="command", lexer="bash",
                                           kind="command", result="command", live=True)),)
