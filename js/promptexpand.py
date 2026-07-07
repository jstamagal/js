r"""Inline directive expansion for js system prompts.

Three forms are resolved before the assembled system prompt reaches the model:

  {{NAME}}            -> value of environment variable NAME (unset -> "")
  !{subsystem args}   -> inline: run a subsystem, inject its output
  ```!subsystem       -> block: the fenced body is fed to the subsystem and the
  <body>                 whole fence is replaced by the subsystem's output
  ```

The token right after ``!`` names the SUBSYSTEM, so it is always unambiguous
what an inline is activating. Subsystems are a registry:

  env, file   -- always on; they only READ (env var value / file contents).
  sh, bash,   -- they EXECUTE arbitrary code embedded in the prompt file. They
  python, py,    run when ``allow_code`` is true, which is the DEFAULT
  c, node, js    (``runtime.allow_inline_code``). Opt out with ``--im-a-pussy``
                 (or ``set runtime.allow_inline_code off`` / ``JS_ALLOW_INLINE_CODE=0``);
                 a code directive then stays literal. Body is passed raw.

Expansion is a SINGLE pass: a directive's output is never re-scanned, so a value
(or a command's stdout) that happens to contain another ``!{...}`` / ``{{...}}``
cannot trigger further expansion. That is the injection guard. A directive that
fails to resolve is, by default, left literal (with a one-line stderr warning)
rather than aborting the load — call with ``on_error="raise"`` for the strict
behavior.

A prompt that documents the directive syntax to itself must be able to show a
directive without the loader running it. Two ways:

  * a backslash immediately before ANY form -- ``\!{sh ...}``, ``\{{VAR}}``,
    ``\``` ``!sub`` -- emits the directive verbatim, minus the one escape
    backslash. This is the universal escape and the only one for fenced blocks.
  * an inline ``!{...}`` / ``{{...}}`` wrapped in a markdown backtick code span --
    e.g. ```` `!{sh ...}` ```` -- is left literal, backticks and all. An
    ergonomic shortcut for prose; only a FULLY wrapped span is protected (a lone
    leading backtick still expands).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import settings
from .capped_process import CappedProcessResult, _run_capped, truncation_marker

__all__ = ["expand_prompt", "PromptExpansionError"]


class PromptExpansionError(ValueError):
    """A directive could not be resolved (unknown subsystem, gated/failed code,
    unreadable file, malformed token)."""


_DEFAULT_TIMEOUT_S = 300
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# One combined scanner: fenced block | inline | env shorthand. Matched in this
# order so a ```!fence wins over the inline form. re.sub replaces each match
# independently and never re-scans the replacement (single pass / injection-safe).
#
# A leading ``bs`` backslash escapes ANY of the three forms: when present the
# whole match is emitted verbatim minus that backslash (see _resolve) -- the only
# escape that reaches the fenced block. The inline / env forms additionally carry
# an optional leading backtick (itick / etick); the trailing ``(?(name)`)``
# matches a closing backtick ONLY when the leading one was captured, so a directive
# fully wrapped in a backtick code span is matched as a unit and emitted literally,
# while a directive with no backticks -- or only a stray leading one -- backtracks
# to the unwrapped form and expands.
_DIRECTIVE = re.compile(
    r"(?P<bs>\\)?"
    r"(?:"
    r"```!(?P<fsub>[A-Za-z0-9_+-]+)[^\n]*\n(?P<fbody>.*?)\n```"
    r"|(?P<itick>`)?!\{(?P<isub>[A-Za-z0-9_+-]+)(?:[ \t]+(?P<iargs>[^}]*))?\}(?(itick)`)"
    r"|(?P<etick>`)?\{\{(?P<env>[^{}]*?)\}\}(?(etick)`)"
    r")",
    re.DOTALL,
)


def expand_prompt(
    text: str,
    *,
    allow_code: bool = False,
    env: dict | None = None,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
    max_output_bytes: int = settings.DEFAULT_MAX_BASH_OUTPUT_BYTES,
    on_error: str = "warn",
) -> str:
    """Return ``text`` with ``{{VAR}}`` / ``!{sub ...}`` / ```` ```!sub ```` directives expanded.

    ``on_error`` governs what happens when a single directive cannot be resolved
    (unknown subsystem, a code subsystem while ``allow_code`` is false, an
    execution/read failure, a malformed token):

      ``"warn"`` (default) -- leave that directive LITERAL in the output, print
      one line to stderr, and keep going. A broken directive never aborts the
      load: js starts, that spot just stays unexpanded.
      ``"raise"`` -- raise :class:`PromptExpansionError` at the first failure
      (the strict behavior; used by tests).

    Either way, one bad directive never affects the others — every match is
    resolved independently in a single, non-re-scanned pass (the injection guard).

    ``env`` substitutes only for the read-only lookups ({{VAR}} and !{env});
    code subsystems (sh/bash/python/node/c) always execute against the real
    process environment.
    """
    if "{{" not in text and "!{" not in text and "```!" not in text:
        return text

    environ = os.environ if env is None else env

    def _resolve(m: re.Match) -> str:
        # \-escaped directive: emit it verbatim, minus the one escape backslash.
        if m.group("bs") is not None:
            return m.group(0)[1:]
        try:
            if m.group("fsub") is not None:
                return _run_subsystem(
                    m.group("fsub"),
                    m.group("fbody"),
                    allow_code,
                    environ,
                    timeout_s,
                    max_output_bytes,
                )
            # A directive wrapped in a backtick code span is documentation, not a
            # request to run -- hand it back verbatim (backticks included).
            if m.group("itick") is not None or m.group("etick") is not None:
                return m.group(0)
            if m.group("isub") is not None:
                return _run_subsystem(
                    m.group("isub"),
                    (m.group("iargs") or "").strip(),
                    allow_code,
                    environ,
                    timeout_s,
                    max_output_bytes,
                )
            # {{...}} env shorthand
            name = m.group("env").strip()
            if not _NAME.fullmatch(name):
                return m.group(0)  # not a real placeholder -> leave the literal alone
            return environ.get(name, "")
        except PromptExpansionError as exc:
            if on_error == "raise":
                raise
            # Degrade: keep the directive literal, warn once, don't brick startup.
            print(f"js: prompt directive left literal: {exc}", file=sys.stderr)
            return m.group(0)

    return _DIRECTIVE.sub(_resolve, text)


# --------------------------------------------------------------------------- #
# Subsystem registry
# --------------------------------------------------------------------------- #

def _run_subsystem(
    name: str,
    body: str,
    allow_code: bool,
    environ: dict,
    timeout_s: int,
    max_output_bytes: int,
) -> str:
    key = name.lower()
    spec = _SUBSYSTEMS.get(key)
    if spec is None:
        raise PromptExpansionError(
            f"unknown inline subsystem '{name}'. known: {', '.join(sorted(_SUBSYSTEMS))}"
        )
    is_code, runner = spec
    if is_code and not allow_code:
        raise PromptExpansionError(
            f"inline '{name}' executes code, but inline-code execution is off "
            f"(--im-a-pussy / set runtime.allow_inline_code off / JS_ALLOW_INLINE_CODE=0)"
        )
    if is_code:
        # Code subsystems run against the real process environment, always.
        return runner(body, timeout_s=timeout_s, max_output_bytes=max_output_bytes)
    return runner(body, environ=environ, timeout_s=timeout_s)


# ---- read-only subsystems (always on) ----

def _sub_env(body: str, *, environ: dict, timeout_s: int) -> str:
    name = body.strip()
    if not _NAME.fullmatch(name):
        raise PromptExpansionError(f"!{{env ...}} needs a variable name, got '{body}'")
    return environ.get(name, "")


def _sub_file(body: str, *, environ: dict, timeout_s: int) -> str:
    path = Path(os.path.expanduser(body.strip()))
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        # A missing/unreadable file embeds nothing rather than aborting the
        # whole prompt, so optional box-info embeds never brick startup.
        return ""


# ---- code subsystems (gated) ----

def _decode_capped(data: bytes, *, truncated: bool, max_output_bytes: int) -> str:
    text = data.decode("utf-8", errors="replace")
    if truncated:
        text = text.rstrip("\n") + f"\n{truncation_marker(max_output_bytes)}\n"
    return text


def _run_capture(argv, *, cwd=None, timeout_s: int, label: str, max_output_bytes: int) -> str:
    try:
        proc = _run_capped(
            argv,
            cwd=cwd,
            env=None,
            timeout=timeout_s,
            cap=max_output_bytes,
        )
    except FileNotFoundError:
        raise PromptExpansionError(f"{label}: '{argv[0]}' not found on PATH") from None
    except subprocess.TimeoutExpired:
        raise PromptExpansionError(f"{label}: timed out after {timeout_s}s") from None
    if not isinstance(proc, CappedProcessResult):
        proc = CappedProcessResult(proc[0], proc[1], proc[2])
    stdout = _decode_capped(
        proc.stdout,
        truncated=proc.stdout_truncated,
        max_output_bytes=max_output_bytes,
    )
    stderr = _decode_capped(
        proc.stderr,
        truncated=proc.stderr_truncated,
        max_output_bytes=max_output_bytes,
    )
    if proc.returncode != 0:
        err = stderr.strip()
        raise PromptExpansionError(f"{label}: exited {proc.returncode}: {err or '(no stderr)'}")
    return stdout.rstrip("\n")


def _sub_sh(body: str, *, timeout_s: int, max_output_bytes: int) -> str:
    return _run_capture(
        ["sh", "-c", body],
        timeout_s=timeout_s,
        label="!{sh}",
        max_output_bytes=max_output_bytes,
    )


def _sub_bash(body: str, *, timeout_s: int, max_output_bytes: int) -> str:
    return _run_capture(
        ["bash", "-c", body],
        timeout_s=timeout_s,
        label="!{bash}",
        max_output_bytes=max_output_bytes,
    )


def _interpreted(label: str, interp_argv, ext: str):
    """Build a runner: write body to a temp <ext> file, run `interp file`.

    The snippet lives in a temp dir (so it never litters the project), but it
    RUNS in the invocation cwd (``cwd=None`` inherits it) — a directive that
    probes the environment must see the directory js was launched from, not the
    throwaway compile dir. This matches the bare ``!{sh}``/``!{bash}`` runners.
    """
    def runner(body: str, *, timeout_s: int, max_output_bytes: int) -> str:
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / f"snippet{ext}"
            src.write_text(body, encoding="utf-8")
            return _run_capture(
                [*interp_argv, str(src)],
                timeout_s=timeout_s,
                label=label,
                max_output_bytes=max_output_bytes,
            )
    return runner


def _compiled(label: str, compiler: str, ext: str):
    """Build a runner: write body to temp <ext>, `compiler src -o exe`, run exe.

    Compilation stays in the temp dir; the built exe RUNS in the invocation cwd
    (``cwd=None``) so it observes the real working directory, like ``_interpreted``.
    """
    def runner(body: str, *, timeout_s: int, max_output_bytes: int) -> str:
        cc = shutil.which(compiler) or compiler
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / f"snippet{ext}"
            exe = Path(d) / "snippet.out"
            src.write_text(body, encoding="utf-8")
            _run_capture(
                [cc, str(src), "-o", str(exe)],
                cwd=d,
                timeout_s=timeout_s,
                label=f"{label} (compile)",
                max_output_bytes=max_output_bytes,
            )
            return _run_capture(
                [str(exe)],
                timeout_s=timeout_s,
                label=label,
                max_output_bytes=max_output_bytes,
            )
    return runner


# (is_code, runner). Add a language by adding one line here.
_SUBSYSTEMS: dict = {
    "env": (False, _sub_env),
    "file": (False, _sub_file),
    "sh": (True, _sub_sh),
    "bash": (True, _sub_bash),
    "python": (True, _interpreted("!{python}", ["python3"], ".py")),
    "py": (True, _interpreted("!{py}", ["python3"], ".py")),
    "node": (True, _interpreted("!{node}", ["node"], ".js")),
    "js": (True, _interpreted("!{js}", ["node"], ".js")),
    "c": (True, _compiled("!{c}", "cc", ".c")),
}
