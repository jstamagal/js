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
  sh, bash,   -- they EXECUTE arbitrary code embedded in the prompt file, so
  python, py,    they run ONLY when allow_code is true (the CLI sets this from
  c, node, js    --dangerously-evaluate-inline-code). Body is passed raw.

Expansion is a SINGLE pass: a directive's output is never re-scanned, so a value
(or a command's stdout) that happens to contain another ``!{...}`` / ``{{...}}``
cannot trigger further expansion. That is the injection guard.

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
import tempfile
from pathlib import Path

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
) -> str:
    """Return ``text`` with ``{{VAR}}`` / ``!{sub ...}`` / ```` ```!sub ```` directives expanded.

    Raises :class:`PromptExpansionError` on an unknown subsystem, a code
    subsystem invoked while ``allow_code`` is false, or any execution/read failure.
    """
    if "{{" not in text and "!{" not in text and "```!" not in text:
        return text

    environ = os.environ if env is None else env

    def _resolve(m: re.Match) -> str:
        # \-escaped directive: emit it verbatim, minus the one escape backslash.
        if m.group("bs") is not None:
            return m.group(0)[1:]
        if m.group("fsub") is not None:
            return _run_subsystem(m.group("fsub"), m.group("fbody"), allow_code, environ, timeout_s)
        # A directive wrapped in a backtick code span is documentation, not a
        # request to run -- hand it back verbatim (backticks included).
        if m.group("itick") is not None or m.group("etick") is not None:
            return m.group(0)
        if m.group("isub") is not None:
            return _run_subsystem(m.group("isub"), (m.group("iargs") or "").strip(), allow_code, environ, timeout_s)
        # {{...}} env shorthand
        name = m.group("env").strip()
        if not _NAME.fullmatch(name):
            return m.group(0)  # not a real placeholder -> leave the literal alone
        return environ.get(name, "")

    return _DIRECTIVE.sub(_resolve, text)


# --------------------------------------------------------------------------- #
# Subsystem registry
# --------------------------------------------------------------------------- #

def _run_subsystem(name: str, body: str, allow_code: bool, environ: dict, timeout_s: int) -> str:
    key = name.lower()
    spec = _SUBSYSTEMS.get(key)
    if spec is None:
        raise PromptExpansionError(
            f"unknown inline subsystem '{name}'. known: {', '.join(sorted(_SUBSYSTEMS))}"
        )
    is_code, runner = spec
    if is_code and not allow_code:
        raise PromptExpansionError(
            f"inline '{name}' executes code, but inline-code execution is off; "
            f"pass --dangerously-evaluate-inline-code to enable"
        )
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

def _run_capture(argv, *, cwd=None, timeout_s: int, label: str) -> str:
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout_s)
    except FileNotFoundError:
        raise PromptExpansionError(f"{label}: '{argv[0]}' not found on PATH") from None
    except subprocess.TimeoutExpired:
        raise PromptExpansionError(f"{label}: timed out after {timeout_s}s") from None
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        raise PromptExpansionError(f"{label}: exited {proc.returncode}: {err or '(no stderr)'}")
    return proc.stdout.rstrip("\n")


def _sub_sh(body: str, *, environ: dict, timeout_s: int) -> str:
    return _run_capture(["sh", "-c", body], timeout_s=timeout_s, label="!{sh}")


def _sub_bash(body: str, *, environ: dict, timeout_s: int) -> str:
    return _run_capture(["bash", "-c", body], timeout_s=timeout_s, label="!{bash}")


def _interpreted(label: str, interp_argv, ext: str):
    """Build a runner: write body to a temp <ext> file, run `interp file`.

    The snippet lives in a temp dir (so it never litters the project), but it
    RUNS in the invocation cwd (``cwd=None`` inherits it) — a directive that
    probes the environment must see the directory js was launched from, not the
    throwaway compile dir. This matches the bare ``!{sh}``/``!{bash}`` runners.
    """
    def runner(body: str, *, environ: dict, timeout_s: int) -> str:
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / f"snippet{ext}"
            src.write_text(body, encoding="utf-8")
            return _run_capture([*interp_argv, str(src)], timeout_s=timeout_s, label=label)
    return runner


def _compiled(label: str, compiler: str, ext: str):
    """Build a runner: write body to temp <ext>, `compiler src -o exe`, run exe.

    Compilation stays in the temp dir; the built exe RUNS in the invocation cwd
    (``cwd=None``) so it observes the real working directory, like ``_interpreted``.
    """
    def runner(body: str, *, environ: dict, timeout_s: int) -> str:
        cc = shutil.which(compiler) or compiler
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / f"snippet{ext}"
            exe = Path(d) / "snippet.out"
            src.write_text(body, encoding="utf-8")
            _run_capture([cc, str(src), "-o", str(exe)], cwd=d, timeout_s=timeout_s, label=f"{label} (compile)")
            return _run_capture([str(exe)], timeout_s=timeout_s, label=label)
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
