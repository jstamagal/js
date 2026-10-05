r"""Inline directive expansion for js system prompts.

Three forms are resolved before the assembled system prompt reaches the model:

  {{NAME}}            -> value of environment variable NAME (unset -> "")
  %%NAME%%            -> value of a js built-in variable
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
import tempfile
from pathlib import Path
from collections.abc import Mapping

from . import paths, settings
from . import messages as msgs
from .capped_process import CappedProcessResult, _run_capped, truncation_marker

__all__ = ["expand_prompt", "session_variables", "PromptExpansionError"]


class PromptExpansionError(ValueError):
    """A directive could not be resolved (unknown subsystem, gated/failed code,
    unreadable file, malformed token)."""


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_VARIABLE = re.compile(r"(?P<vbs>\\)?(?P<vtick>`)?%%(?P<variable>[A-Za-z_][A-Za-z0-9_]*)%%(?(vtick)`)")


def session_variables(cfg) -> dict[str, str]:
    """Built-in values for the session and effective model in ``cfg``."""
    path = Path(getattr(cfg, "session_file", os.devnull)).expanduser().absolute()
    return {
        "CURRENT_SESSION": path.stem if path != Path(os.devnull) else "",
        "CURRENT_SESSION_FULLPATH": str(path),
        "CURRENT_SESSION_AGENT": getattr(cfg, "agent_id", ""),
        "CURRENT_SESSION_MODEL": getattr(cfg, "model", ""),
    }

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
#
# The fenced form additionally must OPEN A LINE, like a real markdown fence: the
# lookbehind admits only the start of the text or a position straight after a newline
# (an optional escape backslash may sit between). Without it, prose that merely
# mentions the syntax -- AGENTS.md documents `` ```!sub `` mid-sentence -- matched, and
# since the body is .*? under DOTALL it swallowed the following paragraph as a script
# and reported `unknown inline subsystem 'sub'` on every single run.
_DIRECTIVE = re.compile(
    r"(?:"
    r"(?<![^\n])(?P<find>[ ]{0,3})(?P<bs>\\)?```!(?P<fsub>[A-Za-z0-9_+-]+)[^\n]*\n(?P<fbody>.*?)\n```"
    r"|(?P<ibs>\\)?(?P<itick>`)?!\{(?P<isub>[A-Za-z0-9_+-]+)(?:[ \t]+(?P<iargs>[^}]*))?\}(?(itick)`)"
    r"|(?P<ebs>\\)?(?P<etick>`)?\{\{(?P<env>[^{}]*?)\}\}(?(etick)`)"
    r"|" + _VARIABLE.pattern +
    r")",
    re.DOTALL,
)


def expand_prompt(
    text: str,
    *,
    allow_code: bool = False,
    env: dict | None = None,
    variables: Mapping[str, str] | None = None,
    timeout_s: int | None = None,
    max_output_bytes: int | None = None,
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

    ``timeout_s`` and ``max_output_bytes`` left None take the js/jsrc values of
    limits.inline_code_timeout_s and limits.max_bash_output_bytes.
    """
    if "{{" not in text and "!{" not in text and "```!" not in text and "%%" not in text:
        return text
    if timeout_s is None:
        timeout_s = settings.default_value("limits.inline_code_timeout_s")
    if max_output_bytes is None:
        max_output_bytes = settings.default_value("limits.max_bash_output_bytes")

    environ = os.environ if env is None else env
    builtins = variables or {}

    def _variable(m: re.Match) -> str:
        if m.group("vbs") is not None:
            return m.group(0)[1:]
        if m.group("vtick") is not None:
            return m.group(0)
        return builtins.get(m.group("variable"), m.group(0))

    def _resolve(m: re.Match) -> str:
        # \-escaped directive: emit it verbatim, minus the one escape backslash.
        # Each branch carries its own escape group because the fenced branch has to
        # sit behind a line-start lookbehind that a shared leading group would break.
        if (m.group("bs") or m.group("ibs") or m.group("ebs")) is not None:
            # Drop the escape backslash only. A fenced match may carry up to three
            # spaces of markdown indent ahead of it, so slicing [1:] would eat a space.
            return m.group(0).replace("\\", "", 1)
        try:
            if m.group("fsub") is not None:
                # Put the markdown indent back. The fence may sit up to three
                # spaces in because it is nested in a list; dropping the indent
                # pulls the expansion out of that list and changes the structure
                # of the prompt the model reads.
                return m.group("find") + _run_subsystem(
                    m.group("fsub"),
                    _VARIABLE.sub(_variable, m.group("fbody")),
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
                    _VARIABLE.sub(_variable, (m.group("iargs") or "").strip()),
                    allow_code,
                    environ,
                    timeout_s,
                    max_output_bytes,
                )
            if m.group("variable") is not None:
                return _variable(m)
            # {{...}} env shorthand
            name = m.group("env").strip()
            if not _NAME.fullmatch(name):
                return m.group(0)  # not a real placeholder -> leave the literal alone
            return environ.get(name, "")
        except PromptExpansionError as exc:
            if on_error == "raise":
                raise
            # Keep the directive as written, warn, and let startup go on.
            msgs.warn(msgs.DIRECTIVE_NOT_EXPANDED, error=exc)
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
            msgs.DIRECTIVE_UNKNOWN_SUBSYSTEM.text(name=name, known=", ".join(sorted(_SUBSYSTEMS)))
        )
    is_code, runner = spec
    if is_code and not allow_code:
        raise PromptExpansionError(msgs.DIRECTIVE_CODE_OFF.text(name=name))
    if is_code:
        # Code subsystems run against the real process environment, always.
        return runner(body, timeout_s=timeout_s, max_output_bytes=max_output_bytes)
    return runner(body, environ=environ, timeout_s=timeout_s)


# ---- read-only subsystems (always on) ----

def _sub_env(body: str, *, environ: dict, timeout_s: int) -> str:
    name = body.strip()
    if not _NAME.fullmatch(name):
        raise PromptExpansionError(msgs.DIRECTIVE_ENV_NEEDS_NAME.text(body=body))
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
        raise PromptExpansionError(msgs.DIRECTIVE_NOT_ON_PATH.text(label=label, program=argv[0])) from None
    except subprocess.TimeoutExpired:
        raise PromptExpansionError(msgs.DIRECTIVE_TIMED_OUT.text(label=label, seconds=timeout_s)) from None
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
        if err:
            raise PromptExpansionError(msgs.DIRECTIVE_EXITED.text(label=label, code=proc.returncode, stderr=err))
        raise PromptExpansionError(msgs.DIRECTIVE_EXITED_SILENT.text(label=label, code=proc.returncode))
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
        with tempfile.TemporaryDirectory(dir=paths.tmp_dir()) as d:
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
        with tempfile.TemporaryDirectory(dir=paths.tmp_dir()) as d:
            src = Path(d) / f"snippet{ext}"
            exe = Path(d) / "snippet.out"
            src.write_text(body, encoding="utf-8")
            _run_capture(
                [cc, str(src), "-o", str(exe)],
                cwd=d,
                timeout_s=timeout_s,
                label=f"{label} compile",
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
