"""Shell and network tools."""

from __future__ import annotations

import html
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from .core import Tool, ToolContext
from .sanitize import int_or_default, text_or_default
from .descriptions import load_description


_ENV_ALLOW = {"PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM", "PWD", "SHELL"}
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_TAG_RE = re.compile(r"<[^>]+>")



def _default_shell() -> str:
    if sys.platform == "win32":
        return os.environ.get("COMSPEC", "cmd.exe")
    return os.environ.get("SHELL", "/bin/sh")


def shell(
    command: str,
    cwd: str | None = None,
    timeout: int = 300,
    keep_ansi: bool = False,
    env: list[str] | None = None,
    description: str | None = None,
    context: ToolContext | None = None,
) -> str:
    assert context is not None
    command = text_or_default(command)
    timeout = int_or_default(timeout, 300, minimum=1)
    workdir = context.resolve_path(cwd) if cwd else context.cwd
    allowed = set(env or []) | _ENV_ALLOW
    safe_env = {key: os.environ[key] for key in allowed if key in os.environ}
    shell_path = _default_shell()
    shell_arg = "/C" if sys.platform == "win32" else "-c"
    try:
        proc = subprocess.run(
            [shell_path, shell_arg, command],
            capture_output=True,
            timeout=timeout,
            cwd=str(workdir),
            env=safe_env,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"
    except OSError as exc:
        return f"ERROR: {exc}"

    stdout = proc.stdout[: context.max_bash_output_bytes].decode("utf-8", errors="replace")
    stderr = proc.stderr[: context.max_bash_output_bytes].decode("utf-8", errors="replace")
    if not keep_ansi:
        stdout = _ANSI_RE.sub("", stdout)
        stderr = _ANSI_RE.sub("", stderr)
    parts = [f"shell={shell_path}", f"exit={proc.returncode}"]
    if description:
        parts.append(f"description={description}")
    if stdout:
        parts.append(f"--- stdout ---\n{stdout}")
    if stderr:
        parts.append(f"--- stderr ---\n{stderr}")
    if not stdout and not stderr:
        parts.append("(no output)")
    return "\n".join(parts)


def _html_to_text(raw: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", raw)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    lines = [line.strip() for line in text.splitlines()]
    compact: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if not blank:
                compact.append("")
            blank = True
        else:
            compact.append(line)
            blank = False
    return "\n".join(compact).strip()


def fetch(url: str, raw: bool | None = False, context: ToolContext | None = None) -> str:
    assert context is not None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "js-agent/0.1"})
        with urllib.request.urlopen(req, timeout=context.fetch_timeout_s) as resp:
            content_type = resp.headers.get("content-type", "")
            data = resp.read(context.max_tool_result_bytes + 1)
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: {type(exc).__name__}: {exc}"
    text = data.decode("utf-8", errors="replace")
    truncated = len(data) > context.max_tool_result_bytes
    if not raw and "html" in content_type.lower():
        text = _html_to_text(text)
    if len(text) > context.max_tool_result_bytes:
        text = text[: context.max_tool_result_bytes]
        truncated = True
    if truncated:
        text += "\n[truncated]"
    return text


def tools() -> tuple[Tool, ...]:
    return (
        Tool(
            "shell",
            load_description("shell"),
            shell,
            {
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "keep_ansi": {"type": "boolean", "default": False},
                "env": {"type": "array", "items": {"type": "string"}},
                "description": {"type": "string"},
            },
            required=("command",),
        ),
        Tool(
            "fetch",
            load_description("fetch"),
            fetch,
            {"url": {"type": "string"}, "raw": {"type": "boolean", "default": False}},
            required=("url",),
        ),
    )
