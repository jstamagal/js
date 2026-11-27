"""The search tool: fs_search over ripgrep, the child processes it runs,
and stopping them when a turn is cancelled."""

from __future__ import annotations

import os
import re
import signal
import stat
import subprocess
import threading
from functools import lru_cache
from pathlib import Path

from .. import jail
from ..tool_binaries import resolve_binary
from .core import ToolContext
from .path_hints import missing_path_hint
from .sanitize import int_or_default


def _glob_literal(text: str) -> str:
    """``text`` as a ripgrep glob that matches only itself."""
    return re.sub(r"([\\\[\]*?{}])", r"\\\1", text)


_RG_MISSING = 'ERROR: rg (ripgrep) not found in tools/bin or PATH; run `just install` to provision it.'
_RG_TIMEOUT_S = 120
# ripgrep's line for a path its walk could not open or read, e.g.
# "rg: /a/b: IO error for operation on /a/b: Permission denied (os error 13)".
# rg exits 2 after one of these but still prints every match it reached.
_RG_WALK_ERROR = re.compile(r"^rg: (?P<path>.+?): (?:.*: )?(?P<reason>[^:]+?) \(os error \d+\)$")
_RG_WALK_ERRORS_SHOWN = 5


def _rg_binary() -> str | None:
    return resolve_binary("rg")


def _rg_env() -> dict[str, str]:
    # Inherit the real environment (PATH, locale) but drop the box-local ripgrep
    # config so the documented contract holds everywhere: .gitignore honoured
    # inside a git tree, .ignore/.rgignore anywhere, hidden + binary + non-regular
    # files skipped. A stray RIPGREP_CONFIG_PATH must not silently change results.
    env = dict(os.environ)
    env.pop("RIPGREP_CONFIG_PATH", None)
    return env


@lru_cache(maxsize=1)
def _rg_types(rg: str) -> frozenset[str]:
    """Names rg accepts for `--type` (`rust`, `py`, `js`, ...), from `rg --type-list`."""
    try:
        proc = subprocess.run(
            [rg, "--type-list"], capture_output=True, text=True, timeout=10, env=_rg_env()
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    return frozenset(
        line.split(":", 1)[0].strip() for line in proc.stdout.splitlines() if ":" in line
    )


_SEARCH_PROCESSES: dict[int, set[subprocess.Popen]] = {}
_SEARCH_PROCESSES_LOCK = threading.Lock()


def _kill_search_process(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def interrupt_inflight(context: ToolContext) -> int:
    """Stop searches owned by a cancelled turn; their workers reap the children."""
    with _SEARCH_PROCESSES_LOCK:
        processes = tuple(_SEARCH_PROCESSES.get(id(context), ()))
        for proc in processes:
            _kill_search_process(proc)
    return len(processes)


def _rg_stream(
    argv: list[str],
    want: int,
    timeout: int,
    input_text: str | None = None,
    cwd: str | None = None,
    context: ToolContext | None = None,
) -> tuple[list[str], int | None, str, bool]:
    """Run rg and collect at most *want* output lines, then stop it.

    Returns (lines, returncode, stderr, timed_out). returncode is None when rg
    was stopped early because enough lines were already gathered — the caller
    treats that as a successful match. Streaming with an early stop keeps memory
    bounded (a pattern matching millions of lines never buffers them all).

    The deadline is enforced by a watchdog that kills rg, NOT by a check inside
    the read loop: `for line in proc.stdout` blocks until rg emits something, so a
    loop-side check can only fire while output is already flowing — exactly the
    case that does not need a timeout. A long scan that matches nothing emits no
    lines at all, and used to run unbounded.

    The child gets its own session so the watchdog can signal the whole group.
    Killing only the direct child leaves any grandchild holding the write end of
    the pipe, and the read loop then blocks past the deadline anyway — the exact
    hang the watchdog exists to prevent."""
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=_rg_env(),
            start_new_session=True,
            cwd=cwd,
        )
    except OSError as exc:
        return [], 2, str(exc), False
    with _SEARCH_PROCESSES_LOCK:
        _SEARCH_PROCESSES.setdefault(id(context), set()).add(proc)
    lines: list[str] = []
    stopped_early = False
    expired = threading.Event()

    def _kill_group() -> None:
        _kill_search_process(proc)

    def _expire() -> None:
        expired.set()
        _kill_group()

    watchdog = threading.Timer(timeout, _expire)
    watchdog.daemon = True
    watchdog.start()
    stderr_chunks: list[bytes] = []
    assert proc.stderr is not None

    def read_stderr() -> None:
        stderr_chunks.append(proc.stderr.read())

    reader = threading.Thread(target=read_stderr, daemon=True, name="search-stderr")
    reader.start()
    writer: threading.Thread | None = None
    if input_text is not None:
        assert proc.stdin is not None

        def write_stdin() -> None:
            try:
                proc.stdin.write(input_text.encode("utf-8"))
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        writer = threading.Thread(target=write_stdin, daemon=True, name="search-stdin")
        writer.start()
    assert proc.stdout is not None
    try:
        # Split the raw bytes on b"\n" only. Text-mode universal newlines would
        # turn a bare CR inside a matched line (a file with CR line endings) into
        # a break, splitting one match into several lines; decoding with
        # backslashreplace renders bytes that are not valid UTF-8 as \xNN escapes
        # instead of dropping them to U+FFFD.
        for raw in proc.stdout:
            lines.append(raw.rstrip(b"\n").decode("utf-8", errors="backslashreplace"))
            if len(lines) >= want:
                stopped_early = True
                break
        if stopped_early:
            _kill_group()
        proc.wait()
        reader.join()
    finally:
        _kill_group()
        proc.wait()
        reader.join()
        if writer is not None:
            writer.join()
        for stream in (proc.stdout, proc.stderr, proc.stdin):
            if stream is not None:
                stream.close()
        watchdog.cancel()
        with _SEARCH_PROCESSES_LOCK:
            processes = _SEARCH_PROCESSES.get(id(context), set())
            processes.discard(proc)
            if not processes:
                _SEARCH_PROCESSES.pop(id(context), None)
    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    timed_out = expired.is_set()
    rc = None if stopped_early else proc.returncode
    return lines, rc, stderr, timed_out


def _rg_walk_errors(stderr: str) -> list[str] | None:
    """The paths rg's walk could not read, each as "path: reason", or None
    when stderr is empty or says anything else (a bad regex or glob)."""
    lines = [line for line in stderr.splitlines() if line.strip()]
    found = [_RG_WALK_ERROR.match(line) for line in lines]
    if not found or not all(found):
        return None
    return [f"{match['path']}: {match['reason']}" for match in found]


# Excludes every dot-prefixed basename, and prunes dot-prefixed directories with
# it, restoring ripgrep's default hidden behaviour after a whitelist glob has
# overridden it.
_HIDDEN_GUARD_GLOB = "!.*"


def _glob_asks_for_hidden(pattern: str | None) -> bool:
    """True when a caller's glob names a dot-prefixed path component itself.

    `.env`, `**/.github/*` and `.git/**` are explicit requests for hidden paths.
    `.` and `..` are path syntax, not hidden names, so they do not count.
    A negated glob (``!.env``) filters results; it never broadens the search to
    hidden paths, so it is not an explicit request.
    """
    if not pattern:
        return False
    text = str(pattern).strip()
    if text.startswith("!"):
        return False
    return any(
        part.startswith(".") and part not in (".", "..")
        for part in text.split("/")
        if part
    )


def _stat_stamp(path: Path) -> tuple[int, int, int] | None:
    """Identity of the file a search described, so the dedup cache stops
    describing a tree that moved underneath it: any edit, external write, or
    undo restore changes at least mtime or size."""
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size, info.st_ino)


def fs_search(
    pattern: str,
    path: str | None = None,
    glob: str | None = None,
    output_mode: str | None = None,
    before_context: int | None = None,
    after_context: int | None = None,
    context_lines: int | None = None,
    show_line_numbers: bool | None = True,
    case_insensitive: bool | None = False,
    file_type: str | None = None,
    head_limit: int | None = None,
    offset: int | None = None,
    multiline: bool | None = False,
    context: ToolContext | None = None,
    **rg_flags,
) -> str:
    assert context is not None
    before_context = rg_flags.get("-B", before_context)
    after_context = rg_flags.get("-A", after_context)
    context_lines = rg_flags.get("-C", context_lines)
    show_line_numbers = rg_flags.get("-n", show_line_numbers)
    case_insensitive = rg_flags.get("-i", case_insensitive)
    file_type = rg_flags.get("type", file_type)
    root = context.resolve_path(path or ".")
    if not root.exists():
        return f"ERROR: Path does not exist: {root}{missing_path_hint(context, root)}"
    try:
        root_stat = root.stat()
    except OSError as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"
    if not stat.S_ISDIR(root_stat.st_mode) and not stat.S_ISREG(root_stat.st_mode):
        return f"ERROR: not a regular file or directory: {root}"
    mode = output_mode or "files_with_matches"
    if file_type and (
        (glob and not str(glob).strip().startswith("!"))
        or (mode == "files" and pattern and not str(pattern).strip().startswith("!"))
    ):
        # ripgrep resolves a positive --glob ahead of --type, so a whitelist glob
        # makes file_type a no-op. The two cannot be intersected; name the
        # conflict instead of silently applying one.
        return (
            "ERROR: file_type cannot be combined with a positive glob; ripgrep "
            "would apply the glob and drop file_type. Pass one filter."
        )
    # A directory's own stat does not move when a nested file changes, so the
    # stamp cannot vouch for a directory-root result: skip the memo for
    # directory roots. A regular-file root's stamp does establish what the hit
    # described (st_ino catches a rename-over that preserves size and mtime).
    memoized = context.search_deduplicate and not stat.S_ISDIR(root_stat.st_mode)
    stamp = _stat_stamp(root) if memoized else None
    cache_key = repr((pattern, str(root), glob, mode, before_context, after_context, context_lines, show_line_numbers, case_insensitive, file_type, head_limit, offset, multiline, stamp))
    cached = context.search_cache.get(cache_key) if memoized else None
    if cached is not None:
        return cached + "\n[deduplicated repeated search]"

    rg = _rg_binary()
    if rg is None:
        return _RG_MISSING

    if head_limit is not None and not isinstance(head_limit, bool):
        try:
            if int(head_limit) < 1:
                return "ERROR: head_limit must be at least 1"
        except (TypeError, ValueError):
            pass
    skip = int_or_default(offset, 0, minimum=0)
    limit = int_or_default(head_limit, 10_000, minimum=1)
    before = int_or_default(context_lines if context_lines is not None else before_context, 0, minimum=0)
    after = int_or_default(context_lines if context_lines is not None else after_context, 0, minimum=0)

    # `--sort path` makes ripgrep walk and print in a stable single-threaded
    # order. Offset paging slices the captured stream, and ripgrep's parallel
    # output order is not stable, so without this consecutive pages overlap and
    # some entries are never returned.
    # `--one-file-system` keeps the walk on the filesystem the root is on: a
    # mount point under the root (an NFS share, an automount) is not entered.
    argv = [rg, "--color=never", "--sort", "path"]
    if context.search_one_file_system:
        argv.append("--one-file-system")
    if mode == "files":
        argv.append("--files")
    elif mode == "files_with_matches":
        argv.append("--files-with-matches")
    elif mode == "count":
        # --with-filename so a single explicit file still prints path:count;
        # ripgrep omits the path for a lone file otherwise.
        argv += ["--count", "--with-filename"]
    elif mode == "content":
        argv += ["--no-heading", "--with-filename"]
        argv.append("--line-number" if show_line_numbers else "--no-line-number")
        if before:
            argv += ["--before-context", str(before)]
        if after:
            argv += ["--after-context", str(after)]
    else:
        return "ERROR: output_mode must be one of files, files_with_matches, content, count"
    if case_insensitive and mode != "files":
        argv.append("--ignore-case")
    if multiline:
        argv += ["--multiline", "--multiline-dotall"]
    glob_flag = "--iglob" if case_insensitive and mode == "files" else "--glob"
    if glob:
        argv += [glob_flag, str(glob)]
    if file_type:
        # Prefer rg's own type table so `type=rust` matches *.rs (and `py` also
        # matches .pyi/.pyw), as ripgrep's `--type` does. A bare extension
        # rg has no type for ("gdshader") still works via an extension glob —
        # previously EVERY value became `*.<value>`, so `type=rust` silently matched
        # nothing at all.
        name = str(file_type).strip().lstrip(".")
        if name in _rg_types(rg):
            argv += ["--type", name]
        else:
            argv += ["--glob", f"*.{name}"]
    if mode == "files":
        argv += [glob_flag, pattern]
    else:
        argv += ["--regexp", pattern]
    # A whitelist --glob outranks ripgrep's hidden-file filter, so every mode that
    # passes one (files mode always does: `pattern` IS the glob) was silently
    # behaving like `rg --files --hidden` and dumping .git internals. The guard
    # goes LAST because overrides are last-match-wins. Skipped when the caller
    # names a dot-prefixed component themselves — that is an explicit ask, and it
    # needs --hidden too or the hidden directory is pruned before the glob is
    # consulted. An explicit `path` to a hidden file still works either way:
    # ripgrep never applies the hidden filter to a root it was handed.
    if _glob_asks_for_hidden(glob) or (mode == "files" and _glob_asks_for_hidden(pattern)):
        argv.append("--hidden")
    else:
        argv += ["--glob", _HIDDEN_GUARD_GLOB]
    # JAIL_HIDE paths inside the root stay out whatever the globs ask for.
    for glob in _hidden_globs(root if root.is_dir() else root.parent, context):
        argv += ["--glob", glob]
    argv += ["--", str(root)]

    # Run rg from the search root: a glob containing a slash is matched against
    # the path relative to rg's working directory, so `src/*.py` can never match
    # under an absolute root unless rg's cwd is that root. Absolute roots still
    # print absolute paths, so the output shape is unchanged.
    rg_cwd = str(root) if root.is_dir() else str(root.parent)
    # Ask for one past the page so an exact-fit result is not reported as
    # truncated; only a genuine extra line proves more matches exist.
    lines, rc, stderr, timed_out = _rg_stream(
        argv, skip + limit + 1, _RG_TIMEOUT_S, cwd=rg_cwd, context=context
    )
    if timed_out:
        return f"ERROR: search timed out after {_RG_TIMEOUT_S}s"
    # rc None = rg stopped early with a full page of matches; 0 = matches; 1 = no
    # matches (clean empty result); 2 with only walk errors on stderr = the
    # matches rg could reach; anything else = real rg error (bad regex/glob).
    unread = _rg_walk_errors(stderr or "")
    if rc is not None and rc not in (0, 1) and unread is None:
        detail = (stderr or "").strip()
        if len(detail) > 2000:
            detail = detail[:2000] + " …[diagnostic truncated]"
        return f"ERROR: {detail or f'rg exit {rc}'}"

    truncated = len(lines) > skip + limit
    sliced = lines[skip:skip + limit]
    out = "\n".join(sliced) if sliced else "(no matches)"
    if truncated:
        out += f"\n[more matches than head_limit={limit}; continue with offset={skip + limit}]"
    if unread:
        more = len(unread) - _RG_WALK_ERRORS_SHOWN
        named = "; ".join(unread[:_RG_WALK_ERRORS_SHOWN]) + (f"; and {more} more" if more > 0 else "")
        if rc is None:
            # The walk stopped with the page full; paths past that point were
            # never reached, so the count is a floor.
            out += (
                f"\n[not searched, rg could not read at least {len(unread)} path(s) "
                f"before the walk stopped at head_limit: {named}]"
            )
        else:
            out += f"\n[not searched, rg could not read {len(unread)} path(s): {named}]"
    if memoized:
        context.search_cache[cache_key] = out
    return out


def _hidden_globs(base: Path, context: ToolContext) -> tuple[str, ...]:
    """ripgrep globs, relative to working directory ``base``, that
    leave out every `JAIL_HIDE` path inside it."""
    out: list[str] = []
    for hidden in jail.hidden_under(base, context):
        rel = _glob_literal(hidden.relative_to(jail.real_path(base)).as_posix())
        out += [f"!/{rel}", f"!/{rel}/**"]
    return tuple(out)
